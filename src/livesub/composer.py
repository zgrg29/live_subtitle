"""把"声学切出来的碎片"合成"值得上屏的一句话"。

问题
----
能量 VAD 只能知道"这里静了 320ms"，不知道主播是说完了一句，还是只是换气。
真人（尤其主播）会在助词、接续词后面停半秒继续，硬切之后单独翻译：
    「このゲーム、」  → 「这个游戏，」
    「けっこう難しいんですよね。」 → 「真是很难玩，不是吗？」
既碎又难读。

做法
----
1. 声学层照旧快速切句（不增加识别延迟）；
2. 每段先识别，用 `text.ja.analyze()` 看句尾像不像说完了；
3. 明显没说完（助词/接续词结尾、太短）→ **先不上屏**，等最多 `merge_wait_ms`；
4. 后续语音真的来了 → 把音频拼起来；短片段**重新识别一次**补上下文，长片段直接拼文本；
   然后翻译一次（不是把两句译文硬拼）；
5. 等不到后续 → 到点照常上屏，不会永远憋着；
6. 整句只是「えーと」这种犹豫声 → 直接丢掉。

关键取舍
--------
- 不翻译半句、不上屏后再改写：ADR-003 的"不闪字幕"原则不变；
- 短片段合并会多花一次识别，但 ASR 的 RTF 只有 0.11，换来的是更准的分句；
- 合并判断用**音频时间轴**而不是墙钟，`selftest --wav` 离线回放也能复现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .audio.vad import Utterance
from .config import SegmentConfig, VADConfig
from .text.ja import Ending, analyze, is_filler_only, strip_fillers


@dataclass
class ComposedLine:
    """最终要上屏的一句（可能由多段音频合并而来）。"""

    index: int  # 用最后一段的序号，保证 id 单调递增
    audio: np.ndarray
    sample_rate: int
    ja: str
    speech_end_ts: float
    speech_end_ms: float
    start_ms: float
    end_ms: float
    asr_ms: float  # 这句话花掉的全部识别时间（含被合并片段的识别）
    segments: int = 1  # 由几段音频组成
    merges: int = 0  # 合并了几次

    @property
    def audio_ms(self) -> float:
        return len(self.audio) / self.sample_rate * 1000.0


@dataclass
class _Pending:
    audio: np.ndarray
    sample_rate: int
    ja: str
    index: int
    start_ms: float
    end_ms: float
    speech_end_ms: float
    speech_end_ts: float
    asr_ms: float
    segments: int = 1
    merges: int = 0
    continuation: bool = False  # 已经听到后续语音，正在等它收句

    @property
    def audio_ms(self) -> float:
        return len(self.audio) / self.sample_rate * 1000.0


Transcribe = Callable[[np.ndarray, int], tuple[str, float]]
Emit = Callable[[ComposedLine], None]

_SENTENCE_PUNCT = "、。，．,.!?！？…「」『』（）()"


def _join_text(left: str, right: str) -> str:
    """把两段识别文本接成一句。中间通常补「、」，给翻译模型一个分句提示。

    例外：て形后面接补助动词（待って + ください）、「と」后面接思う/言う，
    这些是同一个语法结构，塞逗号反而会把句子拆坏。
    """
    left = (left or "").strip()
    right = (right or "").strip()
    if not left:
        return right
    if not right:
        return left
    if left[-1] in _SENTENCE_PUNCT or right[0] in _SENTENCE_PUNCT:
        return left + right
    if left[-1] in "てで":
        return left + right
    if left[-1] == "と" and right[0] in "思言感考":
        return left + right
    return f"{left}、{right}"


class SubtitleComposer:
    """暂缓 / 合并 / 超时兜底。全部在 pipeline 线程里同步执行。"""

    def __init__(
        self,
        cfg: SegmentConfig,
        vad_cfg: VADConfig,
        transcribe: Transcribe,
        emit: Emit,
    ) -> None:
        self._cfg = cfg
        self._vad = vad_cfg
        self._transcribe = transcribe
        self._emit = emit
        self._pending: _Pending | None = None

        # 统计项，供自测和 /api/metrics 观察合并策略是否在正常工作
        self.merged = 0
        self.dropped = 0
        self.held = 0
        self.timeouts = 0

    # ---------- 事件 ----------

    def on_speech_start(self, onset_ms: float) -> None:
        """检测到新的一段开口。用来决定"上一段要不要继续等"。"""
        p = self._pending
        if p is None or p.continuation:
            return
        if onset_ms - p.speech_end_ms <= self._cfg.merge_wait_ms:
            p.continuation = True
        else:
            # 停顿已经超过合并窗口，上一段就是独立的一句，先出
            self._flush("far-start")

    def tick(self, pos_ms: float, segment_active: bool) -> None:
        """每帧调用一次。用音频位置判断暂缓是否到期。"""
        p = self._pending
        if p is None:
            return

        if p.continuation:
            if not segment_active:
                # 后续那"一小段"被 VAD 当噪声丢了（咳嗽/碰麦），别继续等
                self._flush("continuation-lost")
            elif pos_ms - p.speech_end_ms >= self._cfg.max_hold_ms:
                # 后续语音太长，再等下去字幕就空了；先把已有内容放出去
                self._flush("max-hold")
            return

        if segment_active:
            # 同一句话正在继续（例如到上限被切开），不算静音超时
            return
        if pos_ms - p.speech_end_ms >= self._cfg.merge_wait_ms + self._vad.min_speech_ms:
            self._flush("timeout")

    def on_segment(self, utt: Utterance, ja: str, asr_ms: float) -> None:
        """收到一段已识别完的音频。"""
        raw = (ja or "").strip()
        p = self._pending

        if not raw or is_filler_only(raw):
            # 纯呼吸/犹豫声：本身不上屏。但如果正等着后半句，把音频并进去继续等，
            # 否则「このゲームは」+「えーと」会让前半句提前超时。
            if is_filler_only(raw):
                self.dropped += 1
            if p is not None:
                self._absorb(p, utt, asr_ms)
            return

        text = strip_fillers(raw)
        if not text:
            self.dropped += 1
            if p is not None:
                self._absorb(p, utt, asr_ms)
            return

        if p is not None:
            gap = utt.speech_start_ms - p.speech_end_ms
            added_ms = utt.end_ms - max(utt.start_ms, p.end_ms)
            can_merge = (
                self._cfg.merge
                and p.merges < self._cfg.max_merge
                and gap <= self._cfg.merge_wait_ms
                and added_ms > 0
                and p.audio_ms + added_ms <= self._cfg.max_total_s * 1000.0
            )
            if can_merge:
                self._merge(p, utt, text, asr_ms)
                return
            self._flush("new-sentence")

        info = analyze(text)
        if self._cfg.merge and info.ending == Ending.CONTINUE:
            self._pending = _Pending(
                audio=utt.audio,
                sample_rate=utt.sample_rate,
                ja=text,
                index=utt.index,
                start_ms=utt.start_ms,
                end_ms=utt.end_ms,
                speech_end_ms=utt.speech_end_ms,
                speech_end_ts=utt.speech_end_ts,
                asr_ms=asr_ms,
            )
            self.held += 1
            return

        self._emit_line(
            ComposedLine(
                index=utt.index,
                audio=utt.audio,
                sample_rate=utt.sample_rate,
                ja=text,
                speech_end_ts=utt.speech_end_ts,
                speech_end_ms=utt.speech_end_ms,
                start_ms=utt.start_ms,
                end_ms=utt.end_ms,
                asr_ms=asr_ms,
            )
        )

    def flush(self) -> None:
        """停止录音/流水线退出时，把暂缓的那句放出来。"""
        self._flush("stop")

    def stats(self) -> dict[str, int]:
        return {
            "merged": self.merged,
            "dropped_fillers": self.dropped,
            "held": self.held,
            "hold_timeouts": self.timeouts,
        }

    # ---------- 内部 ----------

    def _absorb(self, p: _Pending, utt: Utterance, asr_ms: float) -> None:
        """把"没内容的音频"（犹豫声/呼吸）并进暂缓句，保持合并链不断。"""
        if not self._cfg.merge:
            return
        if utt.speech_start_ms - p.speech_end_ms > self._cfg.merge_wait_ms:
            return
        added_ms = utt.end_ms - max(utt.start_ms, p.end_ms)
        if added_ms <= 0 or p.audio_ms + added_ms > self._cfg.max_total_s * 1000.0:
            return
        p.audio = self._concat(p, utt)
        p.index = utt.index
        p.end_ms = utt.end_ms
        p.speech_end_ms = utt.speech_end_ms
        p.speech_end_ts = utt.speech_end_ts
        p.asr_ms += asr_ms
        p.segments += 1
        p.continuation = False

    def _merge(self, p: _Pending, utt: Utterance, text: str, asr_ms: float) -> None:
        merged_audio = self._concat(p, utt)
        joined_text = _join_text(p.ja, text)

        # 短片段重新识别（补上下文）；长片段直接拼文本（省一次长音频 ASR）。
        if p.audio_ms <= self._cfg.re_asr_under_ms:
            new_text, merged_asr_ms = self._transcribe(merged_audio, utt.sample_rate)
            candidate = strip_fillers(new_text)
            if not candidate or is_filler_only(candidate):
                candidate = joined_text  # 合并识别失败：退回文本拼接
        else:
            candidate = joined_text
            merged_asr_ms = 0.0

        p.audio = merged_audio
        p.index = utt.index
        p.end_ms = utt.end_ms
        p.speech_end_ms = utt.speech_end_ms
        p.speech_end_ts = utt.speech_end_ts
        p.asr_ms += asr_ms + merged_asr_ms
        p.segments += 1
        p.merges += 1
        p.continuation = False
        self.merged += 1
        p.ja = candidate

        info = analyze(p.ja)
        if (
            self._cfg.merge
            and info.ending == Ending.CONTINUE
            and p.merges < self._cfg.max_merge
        ):
            # 还是没说完（比如「〜は」+「〜が」+「〜です」），再等一轮
            return
        self._flush("merged")

    def _concat(self, p: _Pending, utt: Utterance) -> np.ndarray:
        """拼音频；两段的缓冲有重叠（pre-roll）时只保留一份，避免重复音节。"""
        overlap_ms = max(0.0, p.end_ms - utt.start_ms)
        skip = int(round(overlap_ms / 1000.0 * utt.sample_rate))
        skip = max(0, min(skip, len(utt.audio)))
        if skip >= len(utt.audio):
            return p.audio
        return np.concatenate([p.audio, utt.audio[skip:]])

    def _flush(self, reason: str) -> None:
        p = self._pending
        self._pending = None
        if p is None:
            return
        if reason in ("timeout", "max-hold", "continuation-lost", "far-start"):
            self.timeouts += 1
        self._emit_line(
            ComposedLine(
                index=p.index,
                audio=p.audio,
                sample_rate=p.sample_rate,
                ja=p.ja,
                speech_end_ts=p.speech_end_ts,
                speech_end_ms=p.speech_end_ms,
                start_ms=p.start_ms,
                end_ms=p.end_ms,
                asr_ms=p.asr_ms,
                segments=p.segments,
                merges=p.merges,
            )
        )

    def _emit_line(self, line: ComposedLine) -> None:
        self._emit(line)
