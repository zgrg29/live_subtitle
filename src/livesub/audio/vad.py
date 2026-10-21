"""语音活动检测与切句。

M0 用能量 VAD：零依赖、够快、对"干净麦克风"这个前提足够。
Silero VAD（ONNX）留作后续可选增强，接口保持一致。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from ..config import VADConfig


class EnergyVAD:
    """RMS 能量门限 + 缓慢跟随的噪声底。

    判定为说话需要同时满足：
      rms > 绝对门限  且  rms > 噪声底 * noise_ratio
    后者让它在风扇、空调、键盘声里不会一直"在说话"。
    """

    def __init__(self, cfg: VADConfig) -> None:
        self.cfg = cfg
        self.last_rms = 0.0
        self.reset()

    def reset(self) -> None:
        self._floor = 0.0015

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float32))))
        self.last_rms = rms
        threshold = max(self.cfg.threshold, self._floor * self.cfg.noise_ratio)
        speech = rms > threshold
        if not speech:
            # 只在非语音时更新噪声底，避免把说话声吸收进去
            self._floor = 0.995 * self._floor + 0.005 * rms
        return speech


@dataclass
class Utterance:
    index: int
    audio: np.ndarray
    sample_rate: int
    start_ms: float  # 这段缓冲的起点（含 pre-roll，可能早于开口）
    end_ms: float
    speech_start_ms: float  # 真正开口的音频时间轴位置
    speech_end_ms: float  # 真正说完的音频时间轴位置
    end_ts: float  # 判定收句的时刻（含尾部静音等待）
    speech_end_ts: float  # 真正的"人说完"时刻，用于算用户感知延迟

    @property
    def audio_ms(self) -> float:
        return len(self.audio) / self.sample_rate * 1000.0


class Segmenter:
    """把连续的 20ms 帧拼成一句话。

    规则：
      - 连续说话 >= min_speech_ms 才开一句（挡掉咳嗽、碰麦）；
      - 之后静音 >= end_silence_ms 就收句；
      - 单句超过 max_utterance_s 强收（防止不停顿导致延迟无限涨）。
    """

    def __init__(self, cfg: VADConfig, sample_rate: int, frame_ms: int = 20) -> None:
        self.cfg = cfg
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self._pre: deque[np.ndarray] = deque(maxlen=max(1, int(cfg.pre_roll_ms / frame_ms) + 12))
        self._buf: list[np.ndarray] = []
        self._active = False
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._dur_ms = 0.0
        self._pos_ms = 0.0
        self._index = 0
        self._seg_start_ms = 0.0  # 当前这句"真正开口"的位置
        self._started_ms: float | None = None  # 供上层做合并判断的一次性事件
        self.dropped = 0

    @property
    def position_ms(self) -> float:
        """当前处理到的音频时间轴位置（毫秒）。离线跑 WAV 时也是单调的。"""
        return self._pos_ms

    @property
    def active(self) -> bool:
        """正在攒一句话（还没收句）。"""
        return self._active

    def take_started(self) -> float | None:
        """取走"刚开口"事件；返回真正开口的音频时间轴位置。

        用它而不是墙钟：离线回放 WAV 时音频跑得比墙钟快，按墙钟等合并会全错。
        """
        value = self._started_ms
        self._started_ms = None
        return value

    def feed(self, frame: np.ndarray, is_speech: bool) -> Utterance | None:
        dur = len(frame) / self.sample_rate * 1000.0
        self._pos_ms += dur

        if not self._active:
            self._pre.append(frame)
            if not is_speech:
                self._speech_ms = 0.0
                return None
            self._speech_ms += dur
            if self._speech_ms < self.cfg.min_speech_ms:
                return None
            # 先记下"连续说了多久"，下面会把它覆盖成整段缓冲时长
            onset_speech_ms = self._speech_ms
            self._active = True
            self._buf = list(self._pre)
            self._dur_ms = sum(len(f) for f in self._buf) / self.sample_rate * 1000.0
            self._speech_ms = self._dur_ms
            self._silence_ms = 0.0
            # 开头位置 = 当前位置 - 连续说话时长（不是缓冲起点，缓冲含 pre-roll）
            self._seg_start_ms = max(0.0, self._pos_ms - onset_speech_ms)
            self._started_ms = self._seg_start_ms
            return None

        self._buf.append(frame)
        self._dur_ms += dur
        if is_speech:
            self._speech_ms += dur
            self._silence_ms = 0.0
        else:
            self._silence_ms += dur

        hit_end = self._silence_ms >= self.cfg.end_silence_ms
        if hit_end:
            return self._close()
        if self._dur_ms >= self.cfg.max_utterance_s * 1000.0:
            return self._close_at_pause()
        return None

    def _close_at_pause(self) -> Utterance | None:
        """到达单句上限时，退回到最近的低能量点切，别把词切成两半。

        只在前 70% 之后找切点，并且切完至少留 0.8 秒给下一句；
        找不到合适切点就老实按上限硬切。
        """
        frames = self._buf
        if not frames:
            return self._close()
        min_keep = max(1, int(800 / self.frame_ms))
        start_idx = max(1, int(len(frames) * 0.7))
        end_idx = len(frames) - min_keep
        if end_idx <= start_idx:
            return self._close()

        best_index = -1
        best_energy = float("inf")
        for i in range(start_idx, end_idx):
            energy = float(np.sqrt(np.mean(np.square(frames[i], dtype=np.float32))))
            if energy < best_energy:
                best_energy = energy
                best_index = i
        if best_index <= 0:
            return self._close()

        head = np.concatenate(frames[:best_index])
        tail = frames[best_index:]
        start_ms = max(0.0, self._pos_ms - self._dur_ms)
        head_end_ms = start_ms + len(head) / self.sample_rate * 1000.0
        end_ts = time.perf_counter()

        head_speech_start = max(start_ms, min(self._seg_start_ms, head_end_ms))

        # 剩下的音频继续算作下一句的开头，不丢内容
        self._buf = tail
        kept_ms = sum(len(f) for f in tail) / self.sample_rate * 1000.0
        self._dur_ms = kept_ms
        self._speech_ms = kept_ms
        self._silence_ms = 0.0
        # 尾巴是同一段连续说话，开口位置就是切点
        self._seg_start_ms = head_end_ms

        self._index += 1
        return Utterance(
            index=self._index,
            audio=head,
            sample_rate=self.sample_rate,
            start_ms=start_ms,
            end_ms=head_end_ms,
            speech_start_ms=head_speech_start,
            speech_end_ms=head_end_ms,
            end_ts=end_ts,
            speech_end_ts=end_ts,
        )

    def _close(self, trailing_silence_ms: float | None = None) -> Utterance | None:
        audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)
        voiced_ms = self._speech_ms
        start_ms = max(0.0, self._pos_ms - self._dur_ms)
        end_ts = time.perf_counter()
        end_ms = start_ms + len(audio) / self.sample_rate * 1000.0
        silence_ms = self._silence_ms if trailing_silence_ms is None else trailing_silence_ms
        speech_end_ts = end_ts - max(0.0, silence_ms) / 1000.0
        speech_end_ms = max(start_ms, end_ms - max(0.0, silence_ms))
        speech_start_ms = max(start_ms, min(self._seg_start_ms, speech_end_ms))
        self._active = False
        self._buf = []
        self._silence_ms = 0.0
        self._speech_ms = 0.0
        self._dur_ms = 0.0
        self._seg_start_ms = 0.0
        self._started_ms = None
        self._pre.clear()

        if voiced_ms < self.cfg.min_utterance_ms:
            self.dropped += 1
            return None

        self._index += 1
        return Utterance(
            index=self._index,
            audio=audio,
            sample_rate=self.sample_rate,
            start_ms=start_ms,
            end_ms=end_ms,
            speech_start_ms=speech_start_ms,
            speech_end_ms=speech_end_ms,
            end_ts=end_ts,
            speech_end_ts=speech_end_ts,
        )

    def flush(self) -> Utterance | None:
        """停止录音时把最后一句吐出来。"""
        if self._active and self._buf:
            return self._close(trailing_silence_ms=0.0)
        return None
