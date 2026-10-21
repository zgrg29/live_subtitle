"""核心链路自测（不需要麦克风、不需要模型）。

跑法：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import codecs
import contextlib
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@contextlib.contextmanager
def _tmpdir():
    """给测试找一个真的能写的临时目录。

    注意：用 Path.mkdir() 而不是 tempfile.mkdtemp()。mkdtemp 建的目录权限是 0700，
    在受限环境（CI 沙箱/AppContainer）里后续写入会被拒绝，而普通 mkdir 不会。
    系统临时目录不可写时回退到仓库内的 .testtmp（已 gitignore）。
    """
    bases = [Path(tempfile.gettempdir()), ROOT / ".testtmp"]
    chosen: Path | None = None
    for base in bases:
        try:
            base.mkdir(parents=True, exist_ok=True)
            candidate = base / f"livesub-test-{uuid.uuid4().hex[:8]}"
            candidate.mkdir()
            (candidate / ".probe").write_text("ok", encoding="utf-8")
            chosen = candidate
            break
        except OSError:
            continue
    if chosen is None:
        raise RuntimeError("找不到可写的临时目录，测试无法运行")

    try:
        yield str(chosen)
    finally:
        shutil.rmtree(chosen, ignore_errors=True)

from livesub.asr.mock import ScriptedASR  # noqa: E402
from livesub.audio.capture import SyntheticSource, WavSource  # noqa: E402
from livesub.audio.vad import EnergyVAD, Segmenter  # noqa: E402
from livesub.config import Config, load, to_toml  # noqa: E402
from livesub.metrics import Metrics  # noqa: E402
from livesub.pipeline import Pipeline, PipelineStatus, Publisher  # noqa: E402
from livesub.translate.mock import DemoTranslator  # noqa: E402


class ConfigTest(unittest.TestCase):
    def test_defaults_are_sane(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.audio.sample_rate, 16000)
        self.assertEqual(cfg.server.host, "127.0.0.1")
        self.assertGreater(cfg.overlay.zh_size, cfg.overlay.ja_size)

    def test_roundtrip(self) -> None:
        with _tmpdir() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.overlay.zh_size = 64
            cfg.audio.device = "My Mic"
            path.write_text(to_toml(cfg), encoding="utf-8")
            loaded = load(path)
            self.assertEqual(loaded.overlay.zh_size, 64)
            self.assertEqual(loaded.audio.device, "My Mic")

    def test_missing_file_is_created(self) -> None:
        with _tmpdir() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = load(path)
            self.assertTrue(path.exists())
            self.assertEqual(cfg.server.port, 8765)

    def test_bom_and_wrong_types_are_tolerated(self) -> None:
        """记事本存的 UTF-8 带 BOM、手改类型写错，都不该让程序起不来。"""
        with _tmpdir() as tmp:
            path = Path(tmp) / "config.toml"
            body = (
                "[server]\n"
                'port = "abc"\n'
                "[overlay]\n"
                'bilingual = "false"\n'
                'zh_size = "not a number"\n'
                "[audio]\n"
                'block_ms = "20"\n'
            )
            path.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))
            cfg = load(path)
            self.assertEqual(cfg.server.port, 8765)
            self.assertFalse(cfg.overlay.bilingual)
            self.assertEqual(cfg.overlay.zh_size, 56)
            self.assertEqual(cfg.audio.block_ms, 20)

    def test_broken_toml_falls_back_to_defaults(self) -> None:
        with _tmpdir() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("this is not toml [[[", encoding="utf-8")
            cfg = load(path)  # 不抛异常
            self.assertEqual(cfg.server.port, 8765)


class SegmenterTest(unittest.TestCase):
    def test_splits_on_silence(self) -> None:
        cfg = Config()
        sr = 16000
        frame_len = int(sr * 0.02)
        vad = EnergyVAD(cfg.vad)
        seg = Segmenter(cfg.vad, sr, 20)

        speech = [0.05] * frame_len
        silence = [0.0] * frame_len
        utterances = []
        # 1.2 秒说话 + 0.8 秒静音，重复三次
        for _ in range(3):
            for _ in range(60):
                u = seg.feed(_frame(speech), vad.is_speech(_frame(speech)))
                if u:
                    utterances.append(u)
            for _ in range(40):
                u = seg.feed(_frame(silence), vad.is_speech(_frame(silence)))
                if u:
                    utterances.append(u)
        self.assertEqual(len(utterances), 3)
        for u in utterances:
            self.assertGreater(u.audio_ms, 1000)
            self.assertGreater(u.speech_end_ts, 0)

    def test_short_blip_is_dropped(self) -> None:
        cfg = Config()
        sr = 16000
        vad = EnergyVAD(cfg.vad)
        seg = Segmenter(cfg.vad, sr, 20)
        blip = _frame([0.05] * int(sr * 0.02))
        silence = _frame([0.0] * int(sr * 0.02))
        for _ in range(3):  # 60ms，小于 min_speech_ms
            seg.feed(blip, vad.is_speech(blip))
        for _ in range(40):
            seg.feed(silence, vad.is_speech(silence))
        self.assertEqual(seg.dropped, 0)  # 根本没开句，不算丢弃
        self.assertIsNone(seg.flush())

    def test_cap_splits_instead_of_truncating(self) -> None:
        cfg = Config()
        cfg.vad.max_utterance_s = 3.0
        sr = 16000
        frame_len = int(sr * 0.02)
        vad = EnergyVAD(cfg.vad)
        seg = Segmenter(cfg.vad, sr, 20)

        loud = _frame([0.05] * frame_len)
        quiet = _frame([0.002] * frame_len)
        pieces = []
        # 8 秒连续"说话"，每 0.5 秒有一个极低能量的缝
        for i in range(400):
            frame = quiet if i % 25 == 24 else loud
            u = seg.feed(frame, True)
            if u:
                pieces.append(u)
        tail = seg.flush()
        if tail:
            pieces.append(tail)

        self.assertGreaterEqual(len(pieces), 3, "超过上限应当切分而不是丢弃")
        total = sum(p.audio_ms for p in pieces)
        self.assertGreater(total, 7800, "切分后音频不应丢失")
        for piece in pieces:
            # 会在上限之前就切，而不是硬切在 3.0 秒
            self.assertLessEqual(piece.audio_ms, 3000 + 40)
            self.assertGreater(piece.audio_ms, 800)

class PipelineTest(unittest.TestCase):
    def test_synthetic_end_to_end(self) -> None:
        cfg = Config()
        cfg.mt.engine = "echo"  # 默认是 none（翻译未接入）；这里要验证翻译这一段被调用
        source = SyntheticSource(6.0, sample_rate=cfg.audio.sample_rate, block_ms=cfg.audio.block_ms)
        publisher = Publisher()
        metrics = Metrics()
        status = PipelineStatus()
        with _tmpdir() as tmp:
            pipeline = Pipeline(
                cfg, source, ScriptedASR(), DemoTranslator(), publisher, metrics, status, log_dir=tmp
            )
            pipeline.run()
            log_files = list(Path(tmp).glob("session-*.jsonl"))
        self.assertGreater(metrics.utterances, 0, "整条链路没有产出任何一句话")
        self.assertFalse(status.running)
        self.assertFalse(status.last_error, status.last_error)
        self.assertEqual(len(publisher.history()), metrics.utterances)
        self.assertTrue(log_files, "没有写出会话日志")

        first = publisher.history()[0]
        self.assertEqual(first["type"], "line")
        self.assertTrue(first["ja"])
        self.assertTrue(first["zh"])
        self.assertGreaterEqual(first["latency"]["end_to_line_ms"], 0)

        summary = metrics.summary()
        self.assertGreater(summary["end_to_line_ms"]["p50"], 0)


class WavSourceTest(unittest.TestCase):
    def test_wav_roundtrip(self) -> None:
        import wave

        import numpy as np

        synthetic = SyntheticSource(4.0, sample_rate=16000, block_ms=20)
        frames = list(synthetic.frames())
        pcm = np.concatenate(frames)
        with _tmpdir() as tmp:
            path = Path(tmp) / "sample.wav"
            with wave.open(str(path), "wb") as fh:
                fh.setnchannels(1)
                fh.setsampwidth(2)
                fh.setframerate(16000)
                fh.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())

            source = WavSource(path, sample_rate=16000, block_ms=20)
            publisher = Publisher()
            metrics = Metrics()
            with _tmpdir() as log_dir:
                pipeline = Pipeline(
                    Config(), source, ScriptedASR(), DemoTranslator(),
                    publisher, metrics, PipelineStatus(), log_dir=log_dir,
                )
                pipeline.run()

        self.assertGreater(metrics.utterances, 0)
        self.assertTrue(publisher.history()[0]["ja"])


class OverlayTest(unittest.TestCase):
    def test_overlay_exists(self) -> None:
        html = (ROOT / "overlay" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="stage"', html)
        self.assertIn("WebSocket", html)
        self.assertIn("background: transparent", html)


def _frame(values: list[float]):
    import numpy as np

    return np.asarray(values, dtype="float32")


if __name__ == "__main__":
    unittest.main()
