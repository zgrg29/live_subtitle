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

from ..config import AUTO_CHANNEL


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
    """麦克风实时采集。

    多通道设备（2ch 音频接口）默认只取 `channel` 指定的那一路；
    `channel=-1` 时把所有通道平均混成一路。采集时尽量按设备的通道数打开，
    这样设置页能分开显示每一路的电平——"电平在动但一个字都识别不出来"
    最常见的原因就是把没插麦的那一路当成了输入。
    """

    def __init__(
        self,
        device: str = "",
        sample_rate: int = 16000,
        block_ms: int = 20,
        gain: float = 1.0,
        device_index: int = -1,
        channel: int = 0,
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
        self.requested_channel = int(channel)
        self.auto = self.requested_channel == AUTO_CHANNEL
        self.channel = 0 if self.auto else int(channel)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._stop = threading.Event()
        self._last_cb = time.monotonic()
        self.dropped_blocks = 0

        # 自動判定用の状態。音声コールバックと pipeline スレッドの両方から触るので、
        # 共有する数字は短いロックで守る。重い処理は入れない。
        self._auto_lock = threading.Lock()
        self._auto_output: int | None = None
        self._auto_locked = False
        self._auto_speech_ms = 0.0
        self._auto_speech_sum: list[float] = []
        self._auto_noise: list[float] = []

        device_index = resolve_input_device(device, self.device_index)
        info = sd.query_devices(device_index, kind="input")
        self.name = str(info["name"])
        self.max_channels = max(1, int(info["max_input_channels"]))

        blocksize = max(1, int(sample_rate * block_ms / 1000))
        # 先按"最多能开几个通道"预分配：回调在 stream.start() 之后可能立刻触发，
        # 那时下面的 channel_count 还没赋值，不能在这里出现 AttributeError。
        slots = max(1, min(4, self.max_channels))
        self.channel_rms: list[float] = [0.0] * slots
        self._auto_speech_sum = [0.0] * slots
        self._auto_noise = [0.0015] * slots
        self._stream = self._open_stream(sd, device_index, sample_rate, blocksize)
        self.channel_count = int(getattr(self._stream, "channels", 1) or 1)
        del self.channel_rms[self.channel_count:]
        del self._auto_speech_sum[self.channel_count:]
        del self._auto_noise[self.channel_count:]
        if self.auto and self.channel_count <= 1:
            # 1ch マイクでは選ぶ余地がない。UI も「自動」を出さない。
            self.channel = 0

    def _open_stream(self, sd, device_index, sample_rate, blocksize):  # noqa: ANN001
        """优先按 2ch 打开（能分通道看电平）；失败就退回单声道。

        某些驱动在 16kHz/2ch 组合下会拒绝打开，而 1ch 是能用的，
        所以这里必须能退，不能因为"想看电平"把麦克风弄成打不开。
        """
        if self.auto:
            # 自動判定では、まず候補を全部開いて各チャンネルのレベルを見たい。
            # 4ch までに制限（それ以上の業務用インターフェースは手動選択で対応）。
            want = min(4, self.max_channels)
        elif self.channel > 0:
            want = min(4, self.max_channels, self.channel + 1)
        else:
            want = min(2, self.max_channels)

        attempts: list[int] = []
        candidate_channels = [want, 2, 1] if self.auto else [want, 1]
        for channels in candidate_channels:
            if 1 <= channels <= self.max_channels and channels not in attempts:
                attempts.append(channels)
        last_error: Exception | None = None
        for channels in attempts:
            try:
                stream = sd.InputStream(
                    device=device_index,
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="float32",
                    blocksize=blocksize,
                    latency="low",
                    callback=self._callback,
                )
                stream.start()
                return stream
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"マイクを開けませんでした：{last_error}") from last_error

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            # 溢出/欠载只记录，不打断
            pass
        self._last_cb = time.monotonic()
        data = np.asarray(indata, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(-1, 1)

        # 各通道的电平（加增益之前）：用来判断"声音到底在哪一路"
        for i in range(min(len(self.channel_rms), data.shape[1])):
            block_rms = float(np.sqrt(np.mean(np.square(data[:, i], dtype=np.float32))))
            self.channel_rms[i] = 0.85 * self.channel_rms[i] + 0.15 * block_rms
            # ゆっくりした雑音底。声が乗っている間も少し上がるが、判定には
            # 「声の平均 / 雑音底」ではなく、まず平均 RMS の大小を使う。
            self._auto_noise[i] = 0.995 * self._auto_noise[i] + 0.005 * block_rms

        active = self.channel
        if self.auto and not self._auto_locked:
            # まだ判定前。声が入っている側をそのまま通す。ここで mix にすると
            # 入力2の声が半分の音量になり、VAD が拾いにくくなる。
            active = self._auto_pick_output(data.shape[1])

        if active < 0:
            mono = data.mean(axis=1, dtype=np.float32)
        elif active < data.shape[1]:
            mono = data[:, active]
        else:
            mono = data[:, 0]
        chunk = np.array(mono, dtype=np.float32, copy=True)
        if self.gain != 1.0:
            chunk *= self.gain
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            self.dropped_blocks += 1

    def _auto_pick_output(self, channels: int) -> int:
        """判定前の出力チャンネル。

        一番大きいチャンネルをそのまま使う。切り替えは 1.35 倍以上の差が
        出たときだけにして、20ms ごとの細かな揺れで音が途切れないようにする。
        """
        if channels <= 1:
            return 0
        levels = self.channel_rms
        best = max(range(min(channels, len(levels))), key=levels.__getitem__)
        current = self._auto_output
        if current is None or current >= channels:
            self._auto_output = best
        elif best != current:
            if levels[best] > levels[current] * 1.35 or levels[current] < 1e-4:
                self._auto_output = best
        return self._auto_output

    def note_speech(self, speech: bool) -> None:
        """pipeline の VAD 結果を受け取り、十分な発話が溜まったらチャンネルを固定する。

        VAD は「いま出力しているチャンネル」に対して走るので、入力2にマイクが
        あっても判定前から声を通せる。600ms 相当の発話が溜まった時点で、
        各チャンネルの平均 RMS が一番大きいものを選ぶ。
        """
        if not self.auto or not speech:
            return
        with self._auto_lock:
            if self._auto_locked:
                return
            self._auto_speech_ms += float(self.block_ms)
            for i in range(min(self.channel_count, len(self._auto_speech_sum))):
                self._auto_speech_sum[i] += float(self.channel_rms[i])

            if self._auto_speech_ms < 600:
                return

            frames = max(1, int(self._auto_speech_ms / max(1, self.block_ms)))
            scores = [
                value / frames for value in self._auto_speech_sum[: self.channel_count]
            ]
            if not scores:
                return
            best = max(range(len(scores)), key=scores.__getitem__)
            # -54 dBFS 未満は「声」ではなく雑音とみなして、もう少し待つ。
            if scores[best] < 0.002:
                self._auto_speech_ms = 0.0
                self._auto_speech_sum = [0.0] * len(self._auto_speech_sum)
                return

            self.channel = best
            self._auto_output = best
            self._auto_locked = True

    def set_channel(self, channel: int) -> None:
        """手動でチャンネルを切り替える。AUTO_CHANNEL を渡すと自動判定に戻る。"""
        channel = int(channel)
        with self._auto_lock:
            self.requested_channel = channel
            self.auto = channel == AUTO_CHANNEL
            self._auto_output = None
            self._auto_locked = False
            self._auto_speech_ms = 0.0
            self._auto_speech_sum = [0.0] * max(1, len(self.channel_rms))
            if self.auto:
                self.channel = 0
            else:
                self.channel = channel

    @property
    def channel_mode(self) -> str:
        return "auto" if self.auto else "manual"

    @property
    def auto_state(self) -> str:
        if not self.auto:
            return "manual"
        if self.channel_count <= 1:
            return "mono"
        return "locked" if self._auto_locked else "listening"

    @property
    def active_channel(self) -> int:
        """いま実際に ASR へ渡しているチャンネル。自動判定前は動的に変わる。"""
        if self.auto and not self._auto_locked:
            return int(self._auto_output if self._auto_output is not None else 0)
        return int(self.channel)

    @property
    def detected_channel(self) -> int | None:
        """自動判定で確定したチャンネル。未確定なら None。"""
        if self.auto and self._auto_locked:
            return int(self.channel)
        return None

    def channel_levels(self) -> list[float]:
        """每一路的线性 RMS 快照（回调线程在写，复制一份再返回）。"""
        return list(self.channel_rms)[: self.channel_count]

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
