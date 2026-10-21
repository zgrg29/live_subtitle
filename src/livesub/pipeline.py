"""主流水线：音频帧 → VAD → 切句 → 识别 → 翻译 → 广播/落盘。

线程模型（刻意保持简单）：
    [采集回调线程] --queue--> [pipeline 线程: VAD/切句/ASR/MT] --call_soon_threadsafe--> [uvicorn 事件循环] --> WebSocket

ASR/MT 都是同步阻塞调用，所以整条推理放在一个独立线程里，
既不阻塞 WebSocket 推送，也不会让事件循环卡住。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .audio.capture import AudioSource
from .audio.vad import EnergyVAD, Segmenter, Utterance
from .composer import ComposedLine, SubtitleComposer
from .config import Config, data_dir
from .metrics import Metrics, UtteranceTrace


@dataclass
class PipelineStatus:
    running: bool = False
    source: str = ""
    asr: str = ""
    mt: str = ""
    demo: bool = False
    started_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        uptime = round(time.perf_counter() - self.started_at, 1) if self.started_at else 0.0
        return {
            "running": self.running,
            "source": self.source,
            "asr": self.asr,
            "mt": self.mt,
            "demo": self.demo,
            "uptime_s": uptime,
            "last_error": self.last_error,
        }


class Publisher:
    """把 pipeline 线程产生的事件安全地推给所有 WebSocket 客户端。"""

    def __init__(self, history: int = 30) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=history)
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_client(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        with self._lock:
            self._clients.add(queue)
        return queue

    def remove_client(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._clients.discard(queue)

    def history(self) -> list[dict]:
        # 与 publish() 共用锁：否则 WS 握手时可能撞上 append，
        # 抛出 "deque mutated during iteration" 并被上层静默吞掉。
        with self._lock:
            return list(self._history)

    def publish(self, message: dict) -> None:
        if message.get("type") == "line":
            with self._lock:
                self._history.append(message)
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, message)
        except RuntimeError:
            pass

    def _fanout(self, message: dict) -> None:
        with self._lock:
            clients = list(self._clients)
        for queue in clients:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # 客户端卡住时丢消息，绝不反压推理线程
                pass


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        source: AudioSource,
        asr: Any,
        mt: Any,
        publisher: Publisher,
        metrics: Metrics,
        status: PipelineStatus,
        log_dir: str | Path = "logs",
    ) -> None:
        self._cfg = cfg
        self._source = source
        self._asr = asr
        self._mt = mt
        self._pub = publisher
        self._metrics = metrics
        self._status = status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._segmenter: Segmenter | None = None
        self._composer: SubtitleComposer | None = None

        self._status.source = getattr(source, "name", "unknown")
        self._status.asr = getattr(asr, "name", "unknown")
        self._status.mt = getattr(mt, "name", "unknown")
        self._status.demo = "demo" in self._status.asr or "未加载模型" in self._status.asr

        self._log_dir = Path(log_dir)
        if not self._log_dir.is_absolute():
            self._log_dir = data_dir() / self._log_dir
        self._log_path = self._log_dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        self._wav_dir = self._log_dir / "wav"
        self._log_ready = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="livesub-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._source.close()
        except Exception:
            pass

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---------- 设置面板用 ----------

    @property
    def mt(self) -> Any:
        """翻译引擎（设置面板的"试译"要用）。"""
        return self._mt

    def reload_glossary(self) -> bool:
        """重新读词典并热替换。返回是否真的换上了。"""
        reload_fn = getattr(self._mt, "reload", None)
        if not callable(reload_fn):
            return False
        from .text.glossary import load_glossary

        reload_fn(load_glossary(self._cfg.glossary.dir))
        return True

    # ---------- 主循环 ----------

    def run(self) -> None:
        self._status.running = True
        self._status.started_at = time.perf_counter()
        self._publish_status()

        composer: SubtitleComposer | None = None
        try:
            vad = EnergyVAD(self._cfg.vad)
            seg = Segmenter(self._cfg.vad, self._source.sample_rate, self._cfg.audio.block_ms)
            self._segmenter = seg
            composer = SubtitleComposer(
                cfg=self._cfg.segment,
                vad_cfg=self._cfg.vad,
                transcribe=self._transcribe,
                emit=self._emit_line,
            )
            self._composer = composer

            for frame in self._source.frames():
                if self._stop.is_set():
                    break
                utterance = seg.feed(frame, vad.is_speech(frame))
                onset = seg.take_started()
                if onset is not None:
                    composer.on_speech_start(onset)
                if utterance is not None:
                    self._handle(utterance)
                # 用音频时间轴判断"暂缓的那句"是否该放出来了
                composer.tick(seg.position_ms, seg.active)
            tail = seg.flush()
            if tail is not None:
                self._handle(tail)
        except Exception as exc:  # 采集层异常（拔麦、设备被占用）
            self._status.last_error = f"{type(exc).__name__}: {exc}"
            self._pub.publish({"type": "error", "message": self._status.last_error})
        finally:
            try:
                if composer is not None:
                    composer.flush()  # 别把暂缓的最后一句弄丢
                    self._sync_segment_stats()
            finally:
                self._metrics.dropped_segments = seg.dropped if "seg" in locals() else 0
                self._status.running = False
                try:
                    self._source.close()
                    self._asr.close()
                    self._mt.close()
                except Exception:
                    pass
                self._publish_status()

    # ---------- 单段处理 ----------

    def _handle(self, utt: Utterance) -> None:
        """一段声学切句 → 识别 → 交给 composer 决定"现在出还是等一等"。"""
        if self._segmenter is not None:
            # 直播过程中也要能看到，否则排障时 dropped 永远是 0
            self._metrics.dropped_segments = self._segmenter.dropped
        ja, asr_ms = self._transcribe(utt.audio, utt.sample_rate)
        if self._composer is not None:
            self._composer.on_segment(utt, ja, asr_ms)
            self._sync_segment_stats()

    def _transcribe(self, audio: np.ndarray, sample_rate: int) -> tuple[str, float]:
        t_start = time.perf_counter()
        ja = ""
        try:
            result = self._asr.transcribe(audio, sample_rate)
            ja = (result.text or "").strip()
        except Exception as exc:
            self._metrics.errors += 1
            self._status.last_error = f"ASR: {type(exc).__name__}: {exc}"
            self._pub.publish({"type": "error", "message": self._status.last_error})
        return ja, (time.perf_counter() - t_start) * 1000.0

    def _emit_line(self, line: ComposedLine) -> None:
        """最终上屏：翻译、计时、广播、落盘。一句话只会走到这里一次。"""
        ja = (line.ja or "").strip()
        if not ja:
            return
        t_mt_start = time.perf_counter()
        zh = ""
        try:
            zh = (self._mt.translate(ja) or "").strip()
        except Exception as exc:
            self._metrics.errors += 1
            self._status.last_error = f"MT: {type(exc).__name__}: {exc}"
        t_mt_end = time.perf_counter()

        end_to_line_ms = (t_mt_end - line.speech_end_ts) * 1000.0
        trace = UtteranceTrace(
            utt_id=line.index,
            audio_ms=round(line.audio_ms, 1),
            asr_ms=round(line.asr_ms, 1),
            mt_ms=round((t_mt_end - t_mt_start) * 1000.0, 1),
            end_to_line_ms=round(end_to_line_ms, 1),
            text=ja,
            zh=zh,
            segments=line.segments,
            merged=line.merges,
        )
        self._metrics.add(trace)

        if self._cfg.debug.save_wav:
            self._save_wav(line)

        message = {
            "type": "line",
            "id": line.index,
            "ja": ja,
            "zh": zh,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "latency": {
                "end_to_line_ms": trace.end_to_line_ms,
                "asr_ms": trace.asr_ms,
                "mt_ms": trace.mt_ms,
                "audio_ms": trace.audio_ms,
                "segments": trace.segments,
            },
        }
        self._pub.publish(message)
        self._write_log(message)

    def _sync_segment_stats(self) -> None:
        if self._composer is None:
            return
        stats = self._composer.stats()
        self._metrics.merged_segments = stats["merged"]
        self._metrics.dropped_fillers = stats["dropped_fillers"]
        self._metrics.held_segments = stats["held"]
        self._metrics.hold_timeouts = stats["hold_timeouts"]

    # ---------- 落盘 ----------

    def _write_log(self, message: dict) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(message, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _save_wav(self, utt: ComposedLine) -> None:
        try:
            self._wav_dir.mkdir(parents=True, exist_ok=True)
            pcm = np.clip(utt.audio, -1.0, 1.0)
            pcm16 = (pcm * 32767.0).astype(np.int16)
            path = self._wav_dir / f"utt-{utt.index:04d}.wav"
            with wave.open(str(path), "wb") as fh:
                fh.setnchannels(1)
                fh.setsampwidth(2)
                fh.setframerate(utt.sample_rate)
                fh.writeframes(pcm16.tobytes())
        except OSError:
            pass

    def _publish_status(self) -> None:
        self._pub.publish({"type": "status", **self._status.to_dict()})
