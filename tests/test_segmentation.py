"""断句合并（M2.3）的单元测试：不需要模型、不需要麦克风。

覆盖三件事：
    1. 日语句尾判断（哪些结尾算"说完了"）；
    2. composer 的暂缓 / 合并 / 超时兜底；
    3. 语气词清理与吸收。
"""

from __future__ import annotations

import contextlib
import sys
import unittest
import uuid
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from livesub.asr.base import AsrResult  # noqa: E402
from livesub.audio.vad import Segmenter, Utterance  # noqa: E402
from livesub.composer import ComposedLine, SubtitleComposer  # noqa: E402
from livesub.config import Config, SegmentConfig, VADConfig, load, to_toml  # noqa: E402
from livesub.metrics import Metrics  # noqa: E402
from livesub.pipeline import Pipeline, PipelineStatus, Publisher  # noqa: E402
from livesub.text.ja import Ending, analyze, is_filler_only, strip_fillers  # noqa: E402
from livesub.translate.mock import NullTranslator  # noqa: E402

SR = 16000


@contextlib.contextmanager
def _temp_file():
    base = ROOT / ".testtmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"config-{uuid.uuid4().hex[:8]}.toml"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@contextlib.contextmanager
def _temp_dir():
    base = ROOT / ".testtmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"run-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def _utt(
    index: int,
    start_ms: float,
    end_ms: float,
    speech_start_ms: float | None = None,
    speech_end_ms: float | None = None,
) -> Utterance:
    samples = int(round((end_ms - start_ms) / 1000.0 * SR))
    return Utterance(
        index=index,
        audio=np.zeros(samples, dtype=np.float32),
        sample_rate=SR,
        start_ms=start_ms,
        end_ms=end_ms,
        speech_start_ms=start_ms if speech_start_ms is None else speech_start_ms,
        speech_end_ms=end_ms if speech_end_ms is None else speech_end_ms,
        end_ts=0.0,
        speech_end_ts=0.0,
    )


class _Harness:
    """组一个 composer，记录上屏结果和 ASR 调用。"""

    def __init__(self, replies: list[str] | None = None, **segment_overrides) -> None:
        self.out: list[ComposedLine] = []
        self.calls: list[float] = []
        self.replies = list(replies or [])
        cfg = SegmentConfig()
        for key, value in segment_overrides.items():
            setattr(cfg, key, value)
        self.composer = SubtitleComposer(
            cfg=cfg,
            vad_cfg=VADConfig(),
            transcribe=self._transcribe,
            emit=self.out.append,
        )

    def _transcribe(self, audio: np.ndarray, sample_rate: int) -> tuple[str, float]:
        self.calls.append(len(audio) / sample_rate * 1000.0)
        return (self.replies.pop(0) if self.replies else ""), 5.0


class _FixedSource:
    """按固定节奏吐音频：[响 1s, 静 0.6s, 响 1.5s, 静 1s]。"""

    name = "fixed"
    sample_rate = SR
    block_ms = 20

    def __init__(self) -> None:
        parts = [(0.05, 1000), (0.0, 600), (0.05, 1500), (0.0, 1000)]
        chunks = [
            np.full(int(SR * ms / 1000), amp, dtype=np.float32) for amp, ms in parts
        ]
        self._data = np.concatenate(chunks)
        self._closed = False

    def frames(self):
        step = int(SR * self.block_ms / 1000)
        for start in range(0, len(self._data), step):
            if self._closed:
                return
            yield self._data[start : start + step]

    def close(self) -> None:
        self._closed = True


class _SequenceASR:
    name = "sequence"

    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(lines)
        self.calls = 0

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> AsrResult:
        self.calls += 1
        return AsrResult(text=next(self._lines))

    def close(self) -> None:
        return None


class JapaneseEndingTest(unittest.TestCase):
    def test_complete_endings(self) -> None:
        cases = [
            "けっこう難しいんですよね",
            "行きます",
            "今日は晴れです。",
            "こんにちは",
            "はい",
            "うん",
            "ありがとうございます",
            "わかった",
            "難しい",
            "行く",
            "雪や路面の凍結による交通への影響",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(analyze(text).ending, Ending.COMPLETE)

    def test_continuation_endings(self) -> None:
        cases = [
            "このゲーム",
            "気象庁は",
            "暴風雪や高波に警戒するとともに",
            "ちょっと待って",
            "行くけど",
            "今日は、",
            "それでは",
            "へえ",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(analyze(text).ending, Ending.CONTINUE)

    def test_short_discourse_is_held(self) -> None:
        # 「今日はですね、」后面往往还有话；长句以「ですね」结尾则正常收句
        self.assertEqual(analyze("今日はですね").ending, Ending.CONTINUE)
        self.assertEqual(analyze("そうですね").ending, Ending.CONTINUE)
        self.assertEqual(
            analyze("けっこう難しいんですよね", short_chars=4).ending, Ending.COMPLETE
        )

    def test_fillers(self) -> None:
        self.assertEqual(strip_fillers("えーと、今日は"), "今日は")
        self.assertEqual(strip_fillers("あー、うーん、このゲーム"), "このゲーム")
        self.assertEqual(strip_fillers("あの、今日は"), "あの、今日は")  # 可能是"那个"
        self.assertTrue(is_filler_only("えーと"))
        self.assertTrue(is_filler_only("あー、うーん"))
        self.assertFalse(is_filler_only("えーと、今日は"))
        self.assertFalse(is_filler_only("はい"))
        self.assertEqual(strip_fillers("えーと"), "")


class ComposerTest(unittest.TestCase):
    def test_complete_goes_straight_out(self) -> None:
        h = _Harness()
        h.composer.on_segment(_utt(1, 0, 1200, 100, 1000), "今日は晴れです", 12.0)
        self.assertEqual(len(h.out), 1)
        self.assertEqual(h.out[0].ja, "今日は晴れです")
        self.assertEqual(h.out[0].segments, 1)
        self.assertEqual(h.composer.held, 0)

    def test_incomplete_is_merged_with_next(self) -> None:
        h = _Harness(["このゲーム、けっこう難しいです"])
        h.composer.on_segment(_utt(1, 0, 1200, 100, 1000), "このゲーム", 10.0)
        self.assertEqual(h.out, [])
        self.assertEqual(h.composer.held, 1)

        h.composer.on_speech_start(1500)  # 停 500ms 就继续说
        h.composer.on_segment(
            _utt(2, 1200, 3000, 1300, 2800), "けっこう難しいです", 10.0
        )
        self.assertEqual(len(h.out), 1)
        line = h.out[0]
        self.assertEqual(line.ja, "このゲーム、けっこう難しいです")
        self.assertEqual(line.segments, 2)
        self.assertEqual(line.merges, 1)
        self.assertAlmostEqual(line.audio_ms, 3000.0, places=1)
        self.assertEqual(len(h.calls), 1)  # 短片段重新识别了一次
        self.assertAlmostEqual(h.calls[0], 3000.0, places=1)
        self.assertEqual(h.composer.merged, 1)

    def test_timeout_publishes_fragment(self) -> None:
        h = _Harness()
        h.composer.on_segment(_utt(1, 0, 1200, 100, 1000), "このゲームは", 10.0)
        h.composer.tick(1959, False)
        self.assertEqual(h.out, [])
        h.composer.tick(1960, False)  # 1000 + 800 + 160
        self.assertEqual(len(h.out), 1)
        self.assertEqual(h.out[0].ja, "このゲームは")
        self.assertEqual(h.composer.timeouts, 1)

    def test_long_pending_uses_text_join(self) -> None:
        h = _Harness()
        h.composer.on_segment(_utt(1, 0, 3000, 100, 2900), "長い前置きは", 10.0)
        h.composer.on_speech_start(3400)
        h.composer.on_segment(_utt(2, 3000, 5000, 3100, 4800), "今日は晴れです", 10.0)
        self.assertEqual(len(h.out), 1)
        self.assertEqual(h.out[0].ja, "長い前置きは、今日は晴れです")
        self.assertEqual(h.out[0].segments, 2)
        self.assertEqual(h.calls, [])  # 长片段不再重识别，省一次 ASR

    def test_filler_only_is_dropped(self) -> None:
        h = _Harness()
        h.composer.on_segment(_utt(1, 0, 600, 100, 500), "えーと", 10.0)
        self.assertEqual(h.out, [])
        self.assertEqual(h.composer.dropped, 1)
        self.assertFalse(h.composer._pending)
        h.composer.flush()
        self.assertEqual(h.out, [])

    def test_filler_is_absorbed_while_waiting(self) -> None:
        h = _Harness(["このゲームは、けっこう難しいです"])
        h.composer.on_segment(_utt(1, 0, 1200, 100, 1000), "このゲームは", 10.0)
        h.composer.on_speech_start(1300)
        h.composer.on_segment(_utt(2, 1200, 1800, 1300, 1700), "えーと", 10.0)
        self.assertEqual(h.out, [])  # 只是"えーと"，继续等
        self.assertEqual(h.composer.dropped, 1)

        h.composer.on_speech_start(2300)
        h.composer.on_segment(_utt(3, 2200, 3500, 2300, 3300), "けっこう難しいです", 10.0)
        self.assertEqual(len(h.out), 1)
        self.assertEqual(h.out[0].segments, 3)
        self.assertAlmostEqual(h.out[0].audio_ms, 3100.0, places=1)

    def test_merge_count_is_capped(self) -> None:
        h = _Harness(re_asr_under_ms=0, max_merge=2)
        for index, (start, end, text) in enumerate(
            [(0, 1000, "これは"), (1000, 2000, "それも"), (2000, 3000, "あれは")], start=1
        ):
            h.composer.on_speech_start(start + 300)
            h.composer.on_segment(_utt(index, start, end, start + 100, end - 100), text, 10.0)
        self.assertEqual(len(h.out), 1)
        self.assertEqual(h.out[0].segments, 3)
        self.assertEqual(h.composer.merged, 2)

    def test_clear_sentence_does_not_wait_for_next_speech(self) -> None:
        h = _Harness()
        h.composer.on_segment(_utt(1, 0, 1000, 100, 900), "行きます", 10.0)
        self.assertEqual(len(h.out), 1)

    def test_text_join_keeps_grammar_together(self) -> None:
        from livesub.composer import _join_text

        # て形 + 补助动词、引用「と」+ 思う：不能塞逗号
        self.assertEqual(_join_text("ちょっと待って", "ください"), "ちょっと待ってください")
        self.assertEqual(_join_text("そうだなと", "思います"), "そうだなと思います")
        # 独立小句之间补「、」有助于翻译
        self.assertEqual(_join_text("このゲーム", "難しいです"), "このゲーム、難しいです")


class SegmenterSignalTest(unittest.TestCase):
    def test_take_started_is_one_shot(self) -> None:
        cfg = Config().vad
        seg = Segmenter(cfg, SR, 20)
        frame = np.full(int(SR * 0.02), 0.05, dtype=np.float32)
        starts = []
        for _ in range(20):
            seg.feed(frame, True)
            starts.append(seg.take_started())
        found = [value for value in starts if value is not None]
        self.assertEqual(len(found), 1)
        self.assertTrue(seg.active)
        self.assertGreater(seg.position_ms, 0)


class PipelineMergeTest(unittest.TestCase):
    def test_pipeline_merges_and_publishes_once(self) -> None:
        cfg = Config()
        cfg.vad.end_silence_ms = 320
        cfg.segment.merge_wait_ms = 800
        asr = _SequenceASR(
            [
                "このゲーム",  # 第一段：像没说完
                "けっこう難しいです",  # 第二段：说完了
                "このゲーム、けっこう難しいです",  # 合并后重识别
            ]
        )
        publisher = Publisher()
        metrics = Metrics()
        with _temp_dir() as tmp:
            pipeline = Pipeline(
                cfg,
                _FixedSource(),
                asr,
                NullTranslator(),
                publisher,
                metrics,
                PipelineStatus(),
                log_dir=tmp,
            )
            pipeline.run()

        self.assertEqual(metrics.utterances, 1)
        history = publisher.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ja"], "このゲーム、けっこう難しいです")
        self.assertEqual(history[0]["latency"]["segments"], 2)
        self.assertEqual(metrics.merged_segments, 1)
        self.assertEqual(asr.calls, 3)


class SegmentConfigTest(unittest.TestCase):
    def test_segment_section_roundtrip(self) -> None:
        cfg = Config()
        cfg.segment.merge_wait_ms = 900
        cfg.segment.merge = False
        text = to_toml(cfg)
        self.assertIn("[segment]", text)
        with _temp_file() as path:
            path.write_text(text, encoding="utf-8")
            loaded = load(path)
            self.assertEqual(loaded.segment.merge_wait_ms, 900)
            self.assertFalse(loaded.segment.merge)

    def test_bad_segment_values_are_clamped(self) -> None:
        # 复用 load() 里的校验：写一份坏配置，不应让程序起不来
        with _temp_file() as path:
            path.write_text(
                '[segment]\nmerge_wait_ms = "abc"\nmax_merge = 999\n', encoding="utf-8"
            )
            loaded = load(path)
            self.assertEqual(loaded.segment.merge_wait_ms, 800)
            self.assertEqual(loaded.segment.max_merge, 6)


if __name__ == "__main__":
    unittest.main()
