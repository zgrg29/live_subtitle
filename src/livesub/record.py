"""改善用记录：把麦克风音频和每句字幕存到本地（默认关闭）。

为什么要连续存音频
------------------
只存「每句话的 WAV」会丢掉句子之间的停顿，以后想做更细的分句评测、或者拿真实录音
重跑 ASR，都没法复现当时的上下文。所以这里存一条连续的 WAV，每句只记它在音频里的
起止偏移（`lines.jsonl` 的 `start_s` / `end_s`），需要切片时按偏移取即可。

目录结构
--------
    recordings/20261022-100345/
        meta.json      版本、模型、麦克风、配置快照、开始/结束时间
        lines.jsonl    每句：日语、中文、时间戳、音频文件名、起止秒
        audio-000.wav  16kHz / 16-bit / 单声道；超过 max_minutes 自动分段
        README.txt     给以后拿数据的人看的格式说明

线程
----
`write()` 在 pipeline 线程里被每 20ms 调一次，只做 int16 转换和入队；
真正的磁盘写入在独立线程，避免 ASR 推理 + 磁盘抖动一起卡住字幕。
"""

from __future__ import annotations

import json
import queue
import threading
import wave
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config, data_dir

_README = """LiveSub 記録データ
====================

このフォルダは「設定 → 記録」で記録をオンにしたときだけ作られます。
外部には送信されません。不要になったらフォルダごと削除してください。

audio-XXX.wav
  マイク入力の連続音声。16kHz / 16-bit / モノラル。
  max_minutes を超えると audio-001.wav のように分割されます。

lines.jsonl
  1 行 = 1 字幕。主なキー：
    id       字幕 ID（単調増加）
    ts       表示した時刻（ローカル時刻）
    audio    対応する WAV ファイル名
    start_s  その文の発話開始（WAV 先頭からの秒）
    end_s    その文の発話終了（WAV 先頭からの秒）
    ja       認識された日本語
    zh       翻訳された中国語（翻訳失敗時は空）
    latency  認識・翻訳・発話終了→表示の各 ms

meta.json
  アプリのバージョン、使用モデル、マイク、設定のスナップショット。
  認識結果を再現するときの条件として使えます。

注意
----
録音には配信者本人以外の声（通話・来客）も入る可能性があります。
配布・共有する前に、写っている人全員の同意を確認してください。
"""


def record_root(cfg: Config) -> Path:
    """记录根目录（相对路径按数据目录解析）。"""
    root = Path(cfg.record.dir)
    return root if root.is_absolute() else data_dir() / root


def config_snapshot(cfg: Config) -> dict[str, Any]:
    """配置快照。只放和识别/翻译/音频有关的段，避免把无关字段写进数据。"""
    names = ("audio", "vad", "segment", "asr", "mt")
    return {name: asdict(getattr(cfg, name)) for name in names}


class SessionRecorder:
    """一次「开启记录」对应一个会话目录。"""

    def __init__(
        self,
        cfg: Config,
        *,
        version: str,
        source_name: str,
        asr_name: str,
        mt_name: str,
        sample_rate: int,
    ) -> None:
        root = record_root(cfg)
        root.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        session_dir = root / stamp
        suffix = 2
        while session_dir.exists():
            session_dir = root / f"{stamp}-{suffix}"
            suffix += 1
        session_dir.mkdir(parents=True)

        self.dir = session_dir
        self.sample_rate = int(sample_rate)
        self.session_index = -1
        self.started_at = datetime.now()
        self.dropped_frames = 0
        self.last_error = ""

        self._max_samples = (
            int(cfg.record.max_minutes * 60 * self.sample_rate)
            if cfg.record.max_minutes > 0
            else 0
        )
        self._queue: queue.Queue[tuple] = queue.Queue(maxsize=4000)
        self._lock = threading.Lock()
        self._closed = False

        self._part = 0  # 已经开了几个 part（0 = 还没开）
        self._part_name = ""
        self._part_offset_ms = 0.0  # 这个 part 对应音频时间轴的起点
        self._part_samples = 0
        self._total_samples = 0
        self._lines = 0
        self._bytes = 0

        self._meta: dict[str, Any] = {
            "app": "LiveSub",
            "format": 1,  # 目录结构变了就加 1，方便以后写迁移脚本
            "version": version,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "sample_rate": self.sample_rate,
            "channels": 1,
            "sample_width": 2,
            "source": source_name,
            "asr": asr_name,
            "mt": mt_name,
            "max_minutes": cfg.record.max_minutes,
            "settings": config_snapshot(cfg),
        }
        self._write_meta()

        (self.dir / "README.txt").write_text(_README, encoding="utf-8")
        self._thread = threading.Thread(
            target=self._writer_loop, name="livesub-recorder", daemon=True
        )
        self._thread.start()

    # ---------- 写入（pipeline 线程调用） ----------

    def start_part(self, offset_ms: float = 0.0) -> str:
        """开一个新的 WAV 分段。offset_ms 是该分段在音频时间轴上的起点。"""
        with self._lock:
            self._start_part_locked(float(offset_ms))
            return self._part_name

    def _start_part_locked(self, offset_ms: float) -> None:
        if self._closed:
            return
        name = f"audio-{self._part:03d}.wav"
        self._part += 1
        self._part_name = name
        self._part_offset_ms = max(0.0, offset_ms)
        self._part_samples = 0
        self._enqueue(("open", name))

    def write(self, frame: np.ndarray) -> None:
        """追加一帧音频（float32, -1..1）。"""
        if self._closed:
            return
        data = np.asarray(frame, dtype=np.float32)
        if data.size == 0:
            return
        with self._lock:
            if self._part == 0:
                # 正常路径不会走到这里（开记录时会先 start_part），兜底防丢数据
                self._start_part_locked(0.0)
            if self._max_samples and self._part_samples + data.size > self._max_samples:
                next_offset = self._part_offset_ms + self._part_samples / self.sample_rate * 1000.0
                self._start_part_locked(next_offset)
            pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16)
            self._part_samples += data.size
            self._total_samples += data.size
            self._enqueue(("audio", pcm.tobytes()))

    def add_line(self, message: dict[str, Any], start_ms: float, end_ms: float) -> None:
        """记一句已经上屏的字幕（含音频偏移）。"""
        if self._closed:
            return
        with self._lock:
            if self._part == 0:
                return
            part = self._part_name
            offset = self._part_offset_ms
            payload = {
                "id": message.get("id"),
                "ts": message.get("ts") or datetime.now().isoformat(timespec="seconds"),
                "audio": part,
                "start_s": round(max(0.0, start_ms - offset) / 1000.0, 3),
                "end_s": round(max(0.0, end_ms - offset) / 1000.0, 3),
                "ja": message.get("ja", ""),
                "zh": message.get("zh", ""),
                "latency": message.get("latency", {}),
            }
            try:
                with (self.dir / "lines.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._lines += 1
            except OSError as exc:
                self.last_error = f"lines.jsonl: {exc}"

    # ---------- 状态 / 关闭 ----------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": not self._closed,
                "dir": str(self.dir),
                "session": self.dir.name,
                "parts": self._part,
                "lines": self._lines,
                "duration_s": round(self._total_samples / self.sample_rate, 1),
                "bytes": self._bytes,
                "dropped_frames": self.dropped_frames,
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "last_error": self.last_error,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._enqueue(("stop", None))
        self._thread.join(timeout=5.0)
        self._write_meta(ended=True)

    # ---------- 内部 ----------

    def _enqueue(self, item: tuple) -> None:
        try:
            self._queue.put_nowait(item)
            return
        except queue.Full:
            pass
        # 控制消息不能被丢掉；音频满了可以丢帧（下面 write 会记 dropped）
        try:
            self._queue.put(item, timeout=2.0)
        except queue.Full:
            if item[0] == "audio":
                self.dropped_frames += 1
            else:
                self.last_error = "recorder queue full"

    def _writer_loop(self) -> None:
        fh: wave.Wave_write | None = None
        try:
            while True:
                kind, payload = self._queue.get()
                if kind == "stop":
                    break
                if kind == "open":
                    if fh is not None:
                        fh.close()
                        fh = None
                    try:
                        fh = wave.open(str(self.dir / payload), "wb")
                        fh.setnchannels(1)
                        fh.setsampwidth(2)
                        fh.setframerate(self.sample_rate)
                    except OSError as exc:
                        self.last_error = f"wav: {exc}"
                        fh = None
                elif kind == "audio" and fh is not None:
                    try:
                        fh.writeframes(payload)
                        with self._lock:
                            self._bytes += len(payload)
                    except OSError as exc:
                        self.last_error = f"wav: {exc}"
                        fh = None
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass

    def _write_meta(self, ended: bool = False) -> None:
        with self._lock:
            data = dict(self._meta)
            data.update(
                {
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "parts": self._part,
                    "lines": self._lines,
                    "duration_s": round(self._total_samples / self.sample_rate, 1),
                    "bytes": self._bytes,
                    "dropped_frames": self.dropped_frames,
                }
            )
            if ended:
                data["ended_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            (self.dir / "meta.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass


def open_dir(path: str | Path) -> None:
    """在文件管理器里打开目录（Windows 绿色版的主要目标平台）。"""
    import os
    import subprocess
    import sys

    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
