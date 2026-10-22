"""主流水线：音频帧 → VAD → 切句 → 识别 → 翻译 → 广播/落盘。

线程模型（刻意保持简单）：
    [采集回调线程] --queue--> [pipeline 线程: VAD/切句/ASR/MT] --call_soon_threadsafe--> [uvicorn 事件循环] --> WebSocket

ASR/MT 都是同步阻塞调用，所以整条推理放在一个独立线程里，
既不阻塞 WebSocket 推送，也不会让事件循环卡住。

设置页的热切换：
    - 麦克风：`switch_device()` 在调用线程里打开新设备，再关掉旧的；pipeline 线程
      发现 `_source` 变了就换过去。打开新设备失败会回到旧设备，所以不会因为点错
      设备而把正在直播的字幕搞停。
    - 增益/门限：直接改共享的 Config 对象，下一次回调/下一帧就生效。
    - 记录：`set_recording()` 只做开关，音频写盘在 recorder 自己的线程里。
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
from typing import Any, Callable

import numpy as np

from . import __version__
from .audio.capture import AudioSource
from .audio.level import rms_to_db
from .audio.vad import EnergyVAD, Segmenter, Utterance
from .composer import ComposedLine, SubtitleComposer
from .config import Config, data_dir
from .metrics import Metrics, UtteranceTrace
from .record import SessionRecorder, record_root


@dataclass
class PipelineStatus:
    running: bool = False
    source: str = ""
    asr: str = ""
    mt: str = ""
    demo: bool = False
    recording: bool = False
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
            "recording": self.recording,
            "uptime_s": uptime,
            "last_error": self.last_error,
        }


class Publisher:
    """把 pipeline 线程产生的事件安全地推给所有 WebSocket 客户端。"""

    def __init__(self, history: int = 30) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        # queue -> 是否接收 level 消息。字幕页不需要电平，而且 OBS 的 CEF
        # 被挂起时 10Hz 的电平会把队列塞满，挤掉真正要显示的字幕。
        self._clients: dict[asyncio.Queue, bool] = {}
        self._history: deque[dict] = deque(maxlen=history)
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_client(self, allow_levels: bool = False) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        with self._lock:
            self._clients[queue] = allow_levels
        return queue

    def remove_client(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._clients.pop(queue, None)

    def history(self) -> list[dict]:
        # 与 publish() 共用锁：否则 WS 握手时可能撞上 append，
        # 抛出 "deque mutated during iteration" 并被上层静默吞掉。
        with self._lock:
            return list(self._history)

    def client_counts(self) -> dict[str, int]:
        """当前连着几个客户端。诊断页用它回答"OBS 到底连上没有"。"""
        with self._lock:
            total = len(self._clients)
            levels = sum(1 for allow in self._clients.values() if allow)
        return {"total": total, "levels": levels, "subtitle": total - levels}

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
        is_level = message.get("type") == "level"
        with self._lock:
            clients = list(self._clients.items())
        for queue, allow_levels in clients:
            if is_level and not allow_levels:
                continue
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
        source_factory: Callable[[str, int], AudioSource] | None = None,
    ) -> None:
        self._cfg = cfg
        self._source = source
        self._asr = asr
        self._mt = mt
        self._pub = publisher
        self._metrics = metrics
        self._status = status
        self._source_factory = source_factory
        self._last_device: tuple[str, int, int] = (
            cfg.audio.device, cfg.audio.device_index, cfg.audio.channel
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._segmenter: Segmenter | None = None
        self._composer: SubtitleComposer | None = None
        self._vad: EnergyVAD | None = None
        # 诊断页会从 HTTP 线程重跑一次识别；sherpa 的同一个 recognizer 不能两线程同时解码
        self._asr_lock = threading.Lock()

        self._source_lock = threading.Lock()
        self._recorder_lock = threading.Lock()
        self._recorder: SessionRecorder | None = None
        self._current_session = 0
        self._last_level_at = 0.0

        # ---- 诊断用（0.5.1）：字幕が出ないとき、どこで止まったかを数える ----
        # 这些计数是"证据"：帧数增长 → 音频到了；speech_frames → VAD 认为有人说话；
        # utterances → 切句成功；asr_empty → 识别出来是空字符串（最容易被误当成没字幕）。
        self._diag_lock = threading.Lock()
        self._counters: dict[str, Any] = {
            "frames": 0,
            "speech_frames": 0,
            "utterances": 0,
            "asr_calls": 0,
            "asr_empty": 0,
            "emitted": 0,
            "last_frame_ts": 0.0,
            "last_asr_text": "",
            "last_asr_ts": 0.0,
            "last_line_ts": 0.0,
            "last_empty_audio_ms": 0.0,
        }
        # 最近 20 秒的音频：诊断页可以拿它重跑一次识别，判断是"音频不对"还是"显示不对"
        self._recent: deque[np.ndarray] = deque(maxlen=max(1, int(20000 / cfg.audio.block_ms)))
        self._recent_lock = threading.Lock()

        self._status.source = getattr(source, "name", "unknown")
        self._status.asr = getattr(asr, "name", "unknown")
        self._status.mt = getattr(mt, "name", "unknown")
        self._status.demo = bool(getattr(asr, "is_demo", False)) or "demo" in self._status.asr
        self._status.recording = False

        self._log_dir = Path(log_dir)
        if not self._log_dir.is_absolute():
            self._log_dir = data_dir() / self._log_dir
        self._log_path = self._log_dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        self._wav_dir = self._log_dir / "wav"
        self._log_ready = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="livesub-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._source.close()

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

    def set_gain(self, gain: float) -> None:
        """输入增益：MicSource 的回调每块都读 `self.gain`，改完立刻生效。"""
        self._cfg.audio.gain = float(gain)
        with self._source_lock:
            source = self._source
        if hasattr(source, "gain"):
            try:
                source.gain = float(gain)
            except Exception:
                pass

    def set_threshold(self, threshold: float) -> None:
        """VAD 门限：EnergyVAD 持有同一个 Config 对象，改完下一帧生效。"""
        self._cfg.vad.threshold = float(threshold)

    def set_channel(self, channel: int) -> None:
        """只改通道不重开设备（设备已经按足够多的通道采集时有效）。"""
        self._cfg.audio.channel = int(channel)
        with self._source_lock:
            source = self._source
        if hasattr(source, "channel"):
            try:
                source.channel = int(channel)
            except Exception:
                pass

    def switch_device(self, device: str, device_index: int = -1,
                      channel: int | None = None) -> str:
        """热切换麦克风（也可以只换输入通道）。返回实际打开的设备名；失败时抛异常并尽量恢复旧设备。

        先关旧设备再开新设备：Windows 上很多麦克风是独占的，同时开两个会失败。
        打开失败会立刻用旧设备重建，保证直播不中断（只是可能有几十毫秒的空隙）。
        """
        if self._source_factory is None:
            raise RuntimeError("WAV／合成音声モードではマイクを切り替えられません")

        want_channel = self._cfg.audio.channel if channel is None else int(channel)
        with self._source_lock:
            old = self._source
            previous = self._last_device
            try:
                old.close()
            except Exception:
                pass
            try:
                new = self._source_factory(str(device or ""), int(device_index), want_channel)
            except Exception as exc:
                # 回退：用旧设备重建，别把正在跑的 pipeline 留在「没有输入」的状态
                try:
                    restored = self._source_factory(*previous)
                    self._source = restored
                    self._status.source = getattr(restored, "name", "unknown")
                except Exception:
                    pass
                raise RuntimeError(f"マイクを開けませんでした：{exc}") from exc

            self._source = new
            self._last_device = (str(device or ""), int(device_index), want_channel)
            self._status.source = getattr(new, "name", "unknown")
            self._status.last_error = ""
        self._publish_status()
        return self._status.source

    def recording_state(self) -> dict[str, Any]:
        with self._recorder_lock:
            rec = self._recorder
        state: dict[str, Any] = {
            "enabled": bool(self._cfg.record.enabled),
            "active": rec is not None,
            "dir": str(record_root(self._cfg)),
            "max_minutes": self._cfg.record.max_minutes,
        }
        if rec is not None:
            state.update(rec.stats())
        return state

    def set_recording(self, enabled: bool) -> dict[str, Any]:
        """打开/关闭记录。打开时从当前音频位置开始，之前的音频不会补录。"""
        enabled = bool(enabled)
        if enabled and not self._status.running:
            raise RuntimeError("字幕パイプラインが動作していないため記録を開始できません")
        closing: SessionRecorder | None = None
        with self._recorder_lock:
            if enabled and self._recorder is None:
                recorder = SessionRecorder(
                    self._cfg,
                    version=__version__,
                    source_name=self._status.source,
                    asr_name=self._status.asr,
                    mt_name=self._status.mt,
                    sample_rate=self._cfg.audio.sample_rate,
                )
                # 如果是在直播中途打开的，当前时间轴已经走了很远；
                # 记录从这个位置开始，字幕偏移要减掉它。
                position = self._segmenter.position_ms if self._segmenter is not None else 0.0
                recorder.session_index = self._current_session
                recorder.start_part(position)
                self._recorder = recorder
                self._status.recording = True
            elif not enabled and self._recorder is not None:
                closing = self._recorder
                self._recorder = None
                self._status.recording = False
            self._cfg.record.enabled = enabled

        if closing is not None:
            closing.close()
        self._publish_status()
        return self.recording_state()

    # ---------- 主循环 ----------

    def run(self) -> None:
        self._status.running = True
        self._status.started_at = time.perf_counter()
        self._publish_status()

        if self._cfg.record.enabled:
            try:
                self.set_recording(True)
            except Exception as exc:
                self._status.last_error = f"REC: {type(exc).__name__}: {exc}"
                self._pub.publish({"type": "error", "message": self._status.last_error})

        source = self._source
        try:
            while not self._stop.is_set():
                try:
                    self._run_session(source, self._current_session)
                except Exception as exc:  # 采集层异常（拔麦、设备被占用）
                    self._status.last_error = f"{type(exc).__name__}: {exc}"
                    self._pub.publish({"type": "error", "message": self._status.last_error})
                finally:
                    try:
                        source.close()
                    except Exception:
                        pass

                if self._stop.is_set():
                    break

                # 设置页刚刚换了设备：switch_device() 已经建好新 source
                if self._source is not source:
                    source = self._source
                    self._current_session += 1
                    continue

                # WAV / 合成音源没有「重连」的概念，播完就结束
                if self._source_factory is None:
                    break

                # 麦克风出错（拔出/休眠/被独占）：每隔一会儿重试同一个设备
                source = self._reconnect(source)
                if source is None:
                    break
                self._current_session += 1
        finally:
            self._close_recorder()
            self._metrics.dropped_segments = (
                self._segmenter.dropped if self._segmenter is not None else 0
            )
            self._status.running = False
            for engine in (self._source, self._asr, self._mt):
                try:
                    engine.close()
                except Exception:
                    pass
            self._status.recording = False
            self._publish_status()

    def _run_session(self, source: AudioSource, session_index: int) -> None:
        """跑一段音源，直到它结束/出错/被关闭。换设备时会重新进入这里。"""
        with self._recorder_lock:
            recorder = self._recorder
            if recorder is not None and recorder.session_index != session_index:
                recorder.session_index = session_index
                recorder.start_part(0.0)

        vad = EnergyVAD(self._cfg.vad)
        self._vad = vad
        seg = Segmenter(self._cfg.vad, source.sample_rate, self._cfg.audio.block_ms)
        self._segmenter = seg
        composer = SubtitleComposer(
            cfg=self._cfg.segment,
            vad_cfg=self._cfg.vad,
            transcribe=self._transcribe,
            emit=self._emit_line,
        )
        self._composer = composer

        try:
            for frame in source.frames():
                if self._stop.is_set():
                    break
                self._push_recent(frame)
                speech = vad.is_speech(frame)
                self._note_frame(speech)
                self._publish_level(vad, speech)
                utterance = seg.feed(frame, speech)
                recorder = self._recorder
                if recorder is not None:
                    recorder.write(frame)
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
        finally:
            composer.flush()  # 别把暂缓的最后一句弄丢
            self._sync_segment_stats()
            self._metrics.dropped_segments = seg.dropped

    def _reconnect(self, previous: AudioSource) -> AudioSource | None:
        """麦克风断流后的重连；返回新 source，或 None 表示要退出。"""
        while not self._stop.is_set():
            if self._source is not previous:
                return self._source  # 等待期间用户换了设备
            self._stop.wait(1.5)
            if self._stop.is_set():
                return None
            if self._source is not previous:
                return self._source
            try:
                source = self._source_factory(*self._last_device)  # type: ignore[misc]
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if message != self._status.last_error:
                    self._status.last_error = message
                    self._pub.publish({"type": "error", "message": message})
                continue
            self._source = source
            self._status.source = getattr(source, "name", "unknown")
            self._status.last_error = ""
            self._publish_status()
            return source
        return None

    # ---------- 单段处理 ----------

    def _handle(self, utt: Utterance) -> None:
        """一段声学切句 → 识别 → 交给 composer 决定"现在出还是等一等"。"""
        self._bump("utterances")
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
            with self._asr_lock:
                result = self._asr.transcribe(audio, sample_rate)
            ja = (result.text or "").strip()
        except Exception as exc:
            self._metrics.errors += 1
            self._status.last_error = f"ASR: {type(exc).__name__}: {exc}"
            self._pub.publish({"type": "error", "message": self._status.last_error})
        audio_ms = round(len(audio) / max(1, sample_rate) * 1000.0, 1)
        with self._diag_lock:
            self._counters["asr_calls"] += 1
            self._counters["last_asr_text"] = ja
            self._counters["last_asr_ts"] = time.perf_counter()
            if not ja:
                self._counters["asr_empty"] += 1
                self._counters["last_empty_audio_ms"] = audio_ms
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
        with self._diag_lock:
            self._counters["emitted"] += 1
            self._counters["last_line_ts"] = time.perf_counter()

        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.add_line(message, line.start_ms, line.end_ms)
            except Exception as exc:
                self._status.last_error = f"REC: {type(exc).__name__}: {exc}"

    def _publish_level(self, vad: EnergyVAD, speech: bool) -> None:
        """给设置页的电平条：最多 10 次/秒，别把 WebSocket 灌满。"""
        now = time.perf_counter()
        if now - self._last_level_at < 0.1:
            return
        self._last_level_at = now
        rms = float(vad.last_rms)
        # 分通道电平（加增益之前）：2ch 接口上"声音在哪一路"一眼就能看出来。
        # 字幕页收不到 level，所以多这几个字段不影响 OBS。
        channels_db: list[float] = []
        levels_fn = getattr(self._source, "channel_levels", None)
        if callable(levels_fn):
            try:
                channels_db = [round(rms_to_db(v), 1) for v in levels_fn()]
            except Exception:
                channels_db = []
        self._pub.publish(
            {
                "type": "level",
                "rms": round(rms, 6),
                "db": round(rms_to_db(rms), 1),
                "threshold_db": round(rms_to_db(vad.effective_threshold), 1),
                "speech": bool(speech),
                "channels": len(channels_db),
                "channels_db": channels_db,
            }
        )

    def _sync_segment_stats(self) -> None:
        if self._composer is None:
            return
        stats = self._composer.stats()
        self._metrics.merged_segments = stats["merged"]
        self._metrics.dropped_fillers = stats["dropped_fillers"]
        self._metrics.held_segments = stats["held"]
        self._metrics.hold_timeouts = stats["hold_timeouts"]

    # ---------- 诊断（0.5.1） ----------
    #
    # 设计目标：不用看控制台、不用远程桌面，也能回答"字幕在哪一步断的"。
    # 计数器全部在 pipeline 线程里更新，HTTP 线程只读快照。

    def _bump(self, key: str, n: int = 1) -> None:
        with self._diag_lock:
            self._counters[key] = int(self._counters.get(key, 0)) + n

    def _note_frame(self, speech: bool) -> None:
        with self._diag_lock:
            self._counters["frames"] += 1
            self._counters["last_frame_ts"] = time.perf_counter()
            if speech:
                self._counters["speech_frames"] += 1

    def _push_recent(self, frame: np.ndarray) -> None:
        with self._recent_lock:
            self._recent.append(frame)

    def recent_audio(self, seconds: float = 6.0) -> tuple[np.ndarray, int]:
        """最近 N 秒的音频（诊断页拿它重跑识别）。返回 (audio, sample_rate)。"""
        rate = int(getattr(self._source, "sample_rate", self._cfg.audio.sample_rate))
        need = max(1, int(seconds * rate))
        with self._recent_lock:
            frames = list(self._recent)
        picked: list[np.ndarray] = []
        total = 0
        for frame in reversed(frames):
            picked.append(frame)
            total += len(frame)
            if total >= need:
                break
        if not picked:
            return np.zeros(0, dtype=np.float32), rate
        audio = np.concatenate(list(reversed(picked)))
        return audio[-need:], rate

    def diag(self) -> dict[str, Any]:
        """给 /api/diag 和诊断页用的快照。"""
        src = self._source
        vad = self._vad
        now = time.perf_counter()
        with self._diag_lock:
            counters = dict(self._counters)
        with self._recent_lock:
            recent_frames = list(self._recent)
        rate = max(1, int(getattr(src, "sample_rate", self._cfg.audio.sample_rate)))
        recent_s = sum(len(f) for f in recent_frames) / rate

        channels_db: list[float] = []
        levels_fn = getattr(src, "channel_levels", None)
        if callable(levels_fn):
            try:
                channels_db = [round(rms_to_db(v), 1) for v in levels_fn()]
            except Exception:
                channels_db = []

        frames = int(counters.get("frames", 0))
        speech = int(counters.get("speech_frames", 0))
        last_frame_ts = float(counters.get("last_frame_ts", 0.0))
        last_asr_ts = float(counters.get("last_asr_ts", 0.0))
        last_line_ts = float(counters.get("last_line_ts", 0.0))
        return {
            "version": __version__,
            "uptime_s": round(now - self._status.started_at, 1) if self._status.started_at else 0.0,
            "running": self._status.running,
            "source": self._status.source,
            "device": self._cfg.audio.device,
            "device_index": self._cfg.audio.device_index,
            "channel": getattr(src, "channel", self._cfg.audio.channel),
            "channel_count": int(getattr(src, "channel_count", 1) or 1),
            "channels_db": channels_db,
            "sample_rate": rate,
            "gain": self._cfg.audio.gain,
            "level_db": round(rms_to_db(vad.last_rms), 1) if vad is not None else None,
            "threshold_db": round(
                rms_to_db(vad.effective_threshold if vad is not None else self._cfg.vad.threshold), 1
            ),
            "asr": self._status.asr,
            "mt": self._status.mt,
            "demo": self._status.demo,
            "counters": counters,
            "speech_ratio": round(speech / frames, 3) if frames else 0.0,
            "last_frame_age_s": round(now - last_frame_ts, 1) if last_frame_ts else None,
            "last_asr_age_s": round(now - last_asr_ts, 1) if last_asr_ts else None,
            "last_line_age_s": round(now - last_line_ts, 1) if last_line_ts else None,
            "clients": self._pub.client_counts(),
            "recent_audio_s": round(recent_s, 1),
            "last_error": self._status.last_error,
            "recording": bool(self._status.recording),
        }

    @property
    def asr(self) -> Any:
        """识别引擎（诊断页的"重跑一次识别"要用）。"""
        return self._asr

    def transcribe_now(self, audio: np.ndarray, sample_rate: int) -> Any:
        """从别的线程单独跑一次识别（诊断用），与流水线互斥。"""
        with self._asr_lock:
            return self._asr.transcribe(audio, sample_rate)

    @property
    def log_path(self) -> Path:
        return self._log_path

    # ---------- 记录 ----------

    def _close_recorder(self) -> None:
        with self._recorder_lock:
            recorder = self._recorder
            self._recorder = None
        if recorder is not None:
            try:
                recorder.close()
            except Exception:
                pass

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
