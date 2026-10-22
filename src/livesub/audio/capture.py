"""音频源。

三种实现共用同一个鸭子类型接口：

    source.sample_rate -> int
    source.frames()    -> 逐块产出 float32 单声道 numpy 数组
    source.close()

MicSource 用 sounddevice（PortAudio），延迟设为 low；blocksize 固定，
这样下游的 VAD/切句逻辑看到的是稳定的 20ms 网格。
"""

from __future__ import annotations

import queue
import threading
import time
import wave
from pathlib import Path
from typing import Iterator, Protocol

import numpy as np


class AudioSource(Protocol):
    name: str
    sample_rate: int

    def frames(self) -> Iterator[np.ndarray]: ...

    def close(self) -> None: ...


def list_input_devices() -> list[tuple[int, str, int]]:
    """返回 (索引, 名称, 最大输入通道数)，用于 `livesub devices`。"""
    import sounddevice as sd

    out: list[tuple[int, str, int]] = []
    for idx, dev in enumerate(sd.query_devices()):
        channels = int(dev["max_input_channels"])
        if channels > 0:
            out.append((idx, str(dev["name"]), channels))
    return out


def list_input_devices_detailed() -> list[dict]:
    """给设置页用的设备列表：索引、名称、通道数、是否系统默认输入。

    设备名可能重复（同型号 USB 麦克风插两个），所以同时返回索引；
    但配置里保存的是名字（PortAudio 的索引重启后会变）。
    """
    import sounddevice as sd

    try:
        default_input = sd.default.device[0]
    except Exception:
        default_input = -1
    out: list[dict] = []
    for idx, dev in enumerate(sd.query_devices()):
        channels = int(dev["max_input_channels"])
        if channels <= 0:
            continue
        out.append(
            {
                "index": idx,
                "name": str(dev["name"]),
                "channels": channels,
                "default": idx == default_input,
            }
        )
    return out


def default_input_name() -> str:
    """系统默认输入设备名；没有设备时返回空串。"""
    import sounddevice as sd

    try:
        info = sd.query_devices(kind="input")
    except Exception:
        return ""
    if int(info["max_input_channels"]) <= 0:
        return ""
    return str(info["name"])


def resolve_input_device(device: str, device_index: int = -1) -> int | None:
    """把配置里的设备解析成 sounddevice 的设备索引。

    `device_index` 优先（同名麦克风靠它区分）；索引对应的设备已经不存在、
    或者名字对不上（重启后设备顺序变了）时，退回按名字匹配，避免配置一改就崩。
    留空返回 None，表示交给 PortAudio 用系统默认设备。
    """
    import sounddevice as sd

    if device_index is not None and device_index >= 0:
        try:
            info = sd.query_devices(device_index)
            name = str(info["name"])
            same_name = not device or device.strip().lower() in name.lower()
            if int(info["max_input_channels"]) > 0 and same_name:
                return int(device_index)
        except Exception:
            pass  # 索引失效：继续按名字找

    if not device or not device.strip():
        try:
            default = sd.query_devices(kind="input")
        except Exception as exc:  # 没有可用输入设备
            raise RuntimeError(f"使用可能なマイクが見つかりません：{exc}") from exc
        if int(default["max_input_channels"]) <= 0:
            raise RuntimeError("システム既定のデバイスが入力デバイスではありません。config.toml でマイクを指定してください")
        return None

    needle = device.strip().lower()
    matches = [
        (idx, str(dev["name"]))
        for idx, dev in enumerate(sd.query_devices())
        if needle in str(dev["name"]).lower() and int(dev["max_input_channels"]) > 0
    ]
    if not matches:
        names = "\n".join(f"  [{i}] {n}" for i, n, _ in list_input_devices())
        raise RuntimeError(f"名前に {device!r} を含むマイクが見つかりません。使用可能なデバイス：\n{names}")
    return matches[0][0]


class MicSource:
    """麦克风实时采集。"""

    def __init__(
        self,
        device: str = "",
        sample_rate: int = 16000,
        block_ms: int = 20,
        gain: float = 1.0,
        device_index: int = -1,
    ) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:  # pragma: no cover - 依赖缺失时给清晰提示
            raise RuntimeError("sounddevice が見つかりません。pip install -r requirements.txt を実行してください") from exc

        self._sd = sd
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.gain = float(gain)
        self.device = device  # 配置里的原始写法（空 = 系统默认），切换设备时用来回退
        self.device_index = int(device_index)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._stop = threading.Event()
        self._last_cb = time.monotonic()
        self.dropped_blocks = 0

        device_index = resolve_input_device(device, self.device_index)
        info = sd.query_devices(device_index, kind="input")
        self.name = str(info["name"])

        blocksize = max(1, int(sample_rate * block_ms / 1000))
        self._stream = sd.InputStream(
            device=device_index,
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=blocksize,
            latency="low",
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            # 溢出/欠载只记录，不打断
            pass
        self._last_cb = time.monotonic()
        chunk = np.asarray(indata[:, 0], dtype=np.float32).copy()
        if self.gain != 1.0:
            chunk *= self.gain
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            self.dropped_blocks += 1

    def frames(self) -> Iterator[np.ndarray]:
        while not self._stop.is_set():
            try:
                yield self._queue.get(timeout=0.25)
            except queue.Empty:
                # 设备被拔出 / USB 休眠 / 被独占时，PortAudio 不再回调。
                # 不检测的话这里会永远空转，界面停在最后一句且"看起来一切正常"。
                if time.monotonic() - self._last_cb > 5.0:
                    raise RuntimeError(
                        "マイクから 5 秒以上音声データが届きません"
                        "（デバイスが抜かれた／スリープした／他のアプリが占有している可能性があります）"
                    )
                continue

    def close(self) -> None:
        self._stop.set()
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


class WavSource:
    """回放一个 WAV 文件，用于离线调试（不需要麦克风）。"""

    def __init__(self, path: str | Path, sample_rate: int = 16000, block_ms: int = 20,
                 realtime: bool = False) -> None:
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.realtime = realtime
        self.name = f"wav:{self.path.name}"
        self._stop = threading.Event()
        self._data = self._read()

    def _read(self) -> np.ndarray:
        with wave.open(str(self.path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise RuntimeError("16-bit モノラルの WAV のみ対応しています。変換してから実行してください")
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if rate != self.sample_rate:
            # 简易重采样：线性插值，够用
            n = int(len(data) * self.sample_rate / rate)
            src = np.linspace(0, len(data) - 1, num=n)
            data = np.interp(src, np.arange(len(data)), data).astype(np.float32)
        return data

    def frames(self) -> Iterator[np.ndarray]:
        step = max(1, int(self.sample_rate * self.block_ms / 1000))
        started = time.perf_counter()
        sent = 0
        for start in range(0, len(self._data), step):
            if self._stop.is_set():
                return
            yield self._data[start : start + step]
            sent += step
            if self.realtime:
                target = started + sent / self.sample_rate
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)

    def close(self) -> None:
        self._stop.set()


class SyntheticSource:
    """合成"人声节奏"的信号：几段响 → 静，用于在没有任何硬件/模型时验证整条链路。"""

    def __init__(self, seconds: float = 6.0, sample_rate: int = 16000, block_ms: int = 20,
                 seed: int = 7, realtime: bool = False) -> None:
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.realtime = realtime
        self.name = "synthetic"
        self._stop = threading.Event()
        self._data = self._build(seconds, seed)

    def _build(self, seconds: float, seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        total = int(seconds * self.sample_rate)
        out = np.zeros(total, dtype=np.float32)
        amplitude = 0.03  # RMS 约 0.02，明显高于默认 VAD 门限 0.008
        pos = int(0.4 * self.sample_rate)
        while pos < total - int(1.2 * self.sample_rate):
            dur = int(rng.uniform(1.0, 1.8) * self.sample_rate)
            end = min(total, pos + dur)
            t = np.arange(end - pos) / self.sample_rate
            tone = np.sin(2 * np.pi * 220 * t) * 0.7 + np.sin(2 * np.pi * 660 * t) * 0.3
            tone += rng.normal(0, 0.15, size=tone.shape)
            env = np.ones_like(t)
            edge = min(len(t), int(0.03 * self.sample_rate))
            if edge > 1:
                env[:edge] = np.linspace(0, 1, edge)
                env[-edge:] = np.linspace(1, 0, edge)
            out[pos:end] = (tone * env * amplitude).astype(np.float32)
            pos = end + int(rng.uniform(0.8, 1.1) * self.sample_rate)
        return out

    def frames(self) -> Iterator[np.ndarray]:
        step = max(1, int(self.sample_rate * self.block_ms / 1000))
        started = time.perf_counter()
        sent = 0
        for start in range(0, len(self._data), step):
            if self._stop.is_set():
                return
            block = self._data[start : start + step]
            yield block
            sent += len(block)
            if self.realtime:
                delay = started + sent / self.sample_rate - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)

    def close(self) -> None:
        self._stop.set()
