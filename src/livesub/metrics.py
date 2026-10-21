"""延迟与吞吐统计。所有数字都来自真实时间戳，不做估算。"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, asdict


@dataclass
class UtteranceTrace:
    utt_id: int
    audio_ms: float  # 这句话本身多长
    asr_ms: float  # 识别耗时（合并句包含被合并片段的识别）
    mt_ms: float  # 翻译耗时
    end_to_line_ms: float  # 人说完 → 字幕出现在屏幕上
    text: str = ""
    zh: str = ""
    segments: int = 1  # 这一行由几段音频合并而成
    merged: int = 0  # 合并次数（= segments - 1）

    def as_dict(self) -> dict:
        return asdict(self)


class Metrics:
    """滑动窗口统计，供 /api/metrics 和调试 HUD 使用。"""

    def __init__(self, window: int = 200) -> None:
        # pipeline 线程写、事件循环线程读，必须加锁：
        # 一边 append 一边迭代 deque 会抛 "deque mutated during iteration"。
        self._lock = threading.Lock()
        self.traces: deque[UtteranceTrace] = deque(maxlen=window)
        self.utterances = 0
        self.errors = 0
        self.dropped_segments = 0  # 被 VAD 判定为噪声而丢弃的片段
        # 半句合并的统计：合并了几次、暂缓了几段、等超时几段、丢掉几句纯语气词
        self.merged_segments = 0
        self.dropped_fillers = 0
        self.held_segments = 0
        self.hold_timeouts = 0

    def add(self, trace: UtteranceTrace) -> None:
        with self._lock:
            self.traces.append(trace)
            self.utterances += 1

    def summary(self) -> dict:
        with self._lock:
            traces = list(self.traces)
            utterances = self.utterances
            errors = self.errors
            dropped = self.dropped_segments
            merged = self.merged_segments
            fillers = self.dropped_fillers
            held = self.held_segments
            timeouts = self.hold_timeouts

        base = {
            "count": len(traces),
            "utterances": utterances,
            "errors": errors,
            "dropped_segments": dropped,
            "merged_segments": merged,
            "dropped_fillers": fillers,
            "held_segments": held,
            "hold_timeouts": timeouts,
        }
        if not traces:
            return base
        base.update(
            {
                "asr_ms": _stats([t.asr_ms for t in traces]),
                "mt_ms": _stats([t.mt_ms for t in traces]),
                "end_to_line_ms": _stats([t.end_to_line_ms for t in traces]),
                "audio_ms": _stats([t.audio_ms for t in traces]),
            }
        )
        return base


def _stats(values: list[float]) -> dict:
    ordered = sorted(values)
    n = len(ordered)

    def pick(p: float) -> float:
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return round(ordered[idx], 1)

    return {
        "avg": round(sum(ordered) / n, 1),
        "p50": pick(0.50),
        "p95": pick(0.95),
        "max": round(ordered[-1], 1),
    }
