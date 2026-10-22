"""マイク設定・音量メーター・記録・字幕の自動消去の回帰テスト。

マイクもモデルも使わない。録音は合成フレームを SessionRecorder に直接書いて検証する。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from livesub.asr.mock import ScriptedASR  # noqa: E402
from livesub.audio.capture import SyntheticSource  # noqa: E402
from livesub.audio.level import db_to_rms, level_percent, rms_to_db  # noqa: E402
from livesub.audio.vad import EnergyVAD  # noqa: E402
from livesub.config import Config, load, save_section, to_toml  # noqa: E402
from livesub.metrics import Metrics  # noqa: E402
from livesub.pipeline import Pipeline, PipelineStatus, Publisher  # noqa: E402
from livesub.record import SessionRecorder  # noqa: E402
from livesub.translate.mock import DemoTranslator  # noqa: E402


@contextlib.contextmanager
def _tmpdir():
    """和 test_core 一样：不用 mkdtemp（沙箱里 0700 无法写入）。"""
    bases = [Path(tempfile.gettempdir()), ROOT / ".testtmp"]
    chosen: Path | None = None
    for base in bases:
        try:
            base.mkdir(parents=True, exist_ok=True)
            candidate = base / f"livesub-audio-{uuid.uuid4().hex[:8]}"
            candidate.mkdir()
            (candidate / ".probe").write_text("ok", encoding="utf-8")
            chosen = candidate
            break
        except OSError:
            continue
    if chosen is None:
        raise RuntimeError("找不到可写的临时目录，测试无法运行")
    try:
        yield chosen
    finally:
        shutil.rmtree(chosen, ignore_errors=True)


class AudioSettingConfigTest(unittest.TestCase):
    def test_new_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.overlay.clear_after_ms, 5000)
        self.assertTrue(cfg.ui.open_admin)
        self.assertFalse(cfg.record.enabled)
        self.assertEqual(cfg.record.max_minutes, 60)
        self.assertEqual(cfg.audio.channel, 0)

    def test_channel_is_clamped(self) -> None:
        from livesub.config import _validate

        cfg = Config()
        cfg.audio.channel = 99  # type: ignore[assignment]
        _validate(cfg)
        self.assertEqual(cfg.audio.channel, 7)

        cfg.audio.channel = -9  # type: ignore[assignment]
        _validate(cfg)
        self.assertEqual(cfg.audio.channel, -1)

    def test_out_of_range_values_are_clamped(self) -> None:
        cfg = Config()
        cfg.overlay.clear_after_ms = 999999
        cfg.vad.threshold = 0.0
        cfg.vad.noise_ratio = 99.0
        cfg.record.max_minutes = 99999
        cfg.ui.open_admin = "no"  # type: ignore[assignment]
        from livesub.config import _validate

        _validate(cfg)
        self.assertEqual(cfg.overlay.clear_after_ms, 60000)
        self.assertEqual(cfg.vad.threshold, 0.0005)
        self.assertEqual(cfg.vad.noise_ratio, 8.0)
        self.assertEqual(cfg.record.max_minutes, 1440)
        self.assertFalse(cfg.ui.open_admin)

    def test_generated_config_has_new_sections(self) -> None:
        text = to_toml(Config())
        for needle in ("clear_after_ms = 5000", "[ui]", "[record]", "open_admin = true",
                       "enabled = false", "マイク", "channel = 0"):
            self.assertIn(needle, text)

    def test_save_section_keeps_other_sections(self) -> None:
        with _tmpdir() as tmp:
            path = tmp / "config.toml"
            path.write_text(to_toml(Config()), encoding="utf-8")
            save_section(path, "audio", {"device": "USB Mic", "gain": 1.7})
            save_section(path, "record", {"enabled": True})
            cfg = load(path)
            self.assertEqual(cfg.audio.device, "USB Mic")
            self.assertAlmostEqual(cfg.audio.gain, 1.7)
            self.assertTrue(cfg.record.enabled)
            self.assertIn("[overlay]", path.read_text(encoding="utf-8"))


class LevelTest(unittest.TestCase):
    def test_db_roundtrip(self) -> None:
        self.assertAlmostEqual(rms_to_db(0.01), -40.0, places=4)
        self.assertAlmostEqual(db_to_rms(-40.0), 0.01, places=6)
        self.assertLess(rms_to_db(0.0), -80)

    def test_percent_mapping(self) -> None:
        self.assertEqual(level_percent(-60), 0)
        self.assertEqual(level_percent(0), 100)
        self.assertAlmostEqual(level_percent(-30), 50, places=3)

    def test_vad_exposes_effective_threshold(self) -> None:
        cfg = Config()
        cfg.vad.threshold = 0.005
        cfg.vad.noise_ratio = 3.0
        vad = EnergyVAD(cfg.vad)
        # 先喂一段较响的"噪声"：非语音时噪声底会跟上去，实际门限应高于绝对门限
        for _ in range(300):
            vad.is_speech(np.full(320, 0.002, dtype="float32"))
        self.assertGreater(vad.effective_threshold, 0.005)
        # 门限用的是「这一帧开始时」的噪声底，之后才更新，所以允许一个很小的差
        self.assertAlmostEqual(vad.effective_threshold, vad._floor * 3.0, delta=1e-4)


class RecorderTest(unittest.TestCase):
    def _recorder(self, tmp: Path, max_minutes: int = 0) -> SessionRecorder:
        cfg = Config()
        cfg.record.dir = str(tmp / "recordings")
        cfg.record.max_minutes = max_minutes
        recorder = SessionRecorder(
            cfg,
            version="test",
            source_name="synthetic",
            asr_name="mock",
            mt_name="mock",
            sample_rate=16000,
        )
        recorder.start_part(0.0)
        return recorder

    def test_writes_wav_lines_and_meta(self) -> None:
        with _tmpdir() as tmp:
            recorder = self._recorder(tmp)
            for _ in range(50):  # 1 秒
                recorder.write(np.full(320, 0.05, dtype="float32"))
            recorder.add_line(
                {"id": 7, "ts": "2026-10-22T10:00:00", "ja": "こんにちは", "zh": "你好",
                 "latency": {"end_to_line_ms": 123.4}},
                start_ms=0.0,
                end_ms=900.0,
            )
            recorder.close()

            session = recorder.dir
            wav = session / "audio-000.wav"
            self.assertTrue(wav.exists())
            with wave.open(str(wav), "rb") as fh:
                self.assertEqual(fh.getnchannels(), 1)
                self.assertEqual(fh.getsampwidth(), 2)
                self.assertEqual(fh.getframerate(), 16000)
                self.assertEqual(fh.getnframes(), 16000)

            lines = [json.loads(x) for x in
                     (session / "lines.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["ja"], "こんにちは")
            self.assertEqual(lines[0]["zh"], "你好")
            self.assertEqual(lines[0]["audio"], "audio-000.wav")
            self.assertAlmostEqual(lines[0]["end_s"], 0.9)

            meta = json.loads((session / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["lines"], 1)
            self.assertEqual(meta["parts"], 1)
            self.assertAlmostEqual(meta["duration_s"], 1.0)
            self.assertIn("settings", meta)
            self.assertTrue((session / "README.txt").exists())

    def test_splits_long_recording(self) -> None:
        with _tmpdir() as tmp:
            recorder = self._recorder(tmp, max_minutes=1)
            # 61 秒 → 应分成两个文件，且音频不丢
            for _ in range(3050):
                recorder.write(np.zeros(320, dtype="float32"))
            recorder.close()
            files = sorted(p.name for p in recorder.dir.glob("audio-*.wav"))
            self.assertEqual(files, ["audio-000.wav", "audio-001.wav"])
            total = 0
            for name in files:
                with wave.open(str(recorder.dir / name), "rb") as fh:
                    total += fh.getnframes()
            self.assertEqual(total, 3050 * 320)
            meta = json.loads((recorder.dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["parts"], 2)


class RecordingPipelineTest(unittest.TestCase):
    def test_pipeline_records_every_emitted_line(self) -> None:
        cfg = Config()
        cfg.mt.engine = "echo"
        with _tmpdir() as tmp:
            cfg.record.enabled = True
            cfg.record.dir = str(tmp / "recordings")
            cfg.record.max_minutes = 0
            source = SyntheticSource(5.0, sample_rate=cfg.audio.sample_rate,
                                     block_ms=cfg.audio.block_ms)
            publisher = Publisher()
            metrics = Metrics()
            status = PipelineStatus()
            pipeline = Pipeline(cfg, source, ScriptedASR(), DemoTranslator(),
                                publisher, metrics, status, log_dir=tmp / "logs")
            pipeline.run()
            self.assertFalse(status.last_error, status.last_error)
            self.assertGreater(metrics.utterances, 0)

            sessions = list((tmp / "recordings").iterdir())
            self.assertEqual(len(sessions), 1)
            lines = (sessions[0] / "lines.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), len(publisher.history()))
            with wave.open(str(sessions[0] / "audio-000.wav"), "rb") as fh:
                self.assertGreater(fh.getnframes(), 0)
            meta = json.loads((sessions[0] / "meta.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(meta["duration_s"], 4.9)
            self.assertFalse(pipeline.recording_state()["active"])


class MicChannelSelectionTest(unittest.TestCase):
    """2ch インターフェースで「入力2」を選べるか（0.5.1 の本命の修正）。

    ここが壊れると「レベルは動くのに字幕が出ない」に戻る。
    """

    def _fake_sounddevice(self, channels: int = 2):
        import types

        class FakeStream:
            def __init__(self, **kwargs) -> None:
                self.channels = int(kwargs["channels"])
                self.callback = kwargs["callback"]
                self.started = False

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.started = False

            def close(self) -> None:
                pass

        mod = types.ModuleType("sounddevice")
        mod.InputStream = lambda **kw: FakeStream(**kw)
        mod.query_devices = lambda device=None, kind=None: {
            "name": "Fake 2ch Interface",
            "max_input_channels": channels,
        }
        mod.default = types.SimpleNamespace(device=(0, 0))
        return mod

    def _source(self, channel: int, channels: int = 2, gain: float = 1.0):
        import sys

        fake = self._fake_sounddevice(channels)
        original = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = fake
        try:
            from livesub.audio.capture import MicSource

            return MicSource(device="", channel=channel, gain=gain)
        finally:
            if original is None:
                sys.modules.pop("sounddevice", None)
            else:
                sys.modules["sounddevice"] = original

    @staticmethod
    def _feed(source, values: list[float]) -> None:
        data = np.zeros((320, len(values)), dtype=np.float32)
        for i, value in enumerate(values):
            data[:, i] = value
        source._callback(data, 320, None, None)

    def test_second_channel_is_used(self) -> None:
        src = self._source(channel=1)
        self.assertEqual(src.channel_count, 2)
        self._feed(src, [0.0, 0.5])
        frame = next(src.frames())
        self.assertAlmostEqual(float(np.max(np.abs(frame))), 0.5, places=3)
        levels = src.channel_levels()
        self.assertEqual(len(levels), 2)
        self.assertGreater(levels[1], levels[0])
        src.close()

    def test_gain_applies_to_selected_channel(self) -> None:
        src = self._source(channel=1, gain=2.0)
        self._feed(src, [0.0, 0.25])
        self.assertAlmostEqual(float(np.max(np.abs(next(src.frames())))), 0.5, places=3)
        src.close()

    def test_mix_mode_averages_channels(self) -> None:
        src = self._source(channel=-1)
        self.assertEqual(src.channel_count, 2)
        self._feed(src, [0.2, 0.4])
        self.assertAlmostEqual(float(next(src.frames())[0]), 0.3, places=3)
        src.close()

    def test_mono_device_still_opens_one_channel(self) -> None:
        src = self._source(channel=0, channels=1)
        self.assertEqual(src.channel_count, 1)
        self.assertEqual(len(src.channel_levels()), 1)
        src.close()


class PipelineDiagTest(unittest.TestCase):
    def _pipeline(self, tmp, asr=None):
        cfg = Config()
        cfg.mt.engine = "echo"
        source = SyntheticSource(4.0, sample_rate=cfg.audio.sample_rate,
                                 block_ms=cfg.audio.block_ms)
        publisher = Publisher()
        pipeline = Pipeline(cfg, source, asr or ScriptedASR(), DemoTranslator(),
                            publisher, Metrics(), PipelineStatus(), log_dir=tmp / "logs")
        return pipeline, cfg

    def test_every_stage_is_counted(self) -> None:
        with _tmpdir() as tmp:
            pipeline, cfg = self._pipeline(tmp)
            pipeline.run()
            diag = pipeline.diag()
            counters = diag["counters"]
            from livesub import __version__

            self.assertEqual(diag["version"], __version__)
            self.assertGreater(counters["frames"], 0)
            self.assertGreater(counters["speech_frames"], 0)
            self.assertGreater(counters["utterances"], 0)
            self.assertGreater(counters["asr_calls"], 0)
            self.assertEqual(counters["asr_empty"], 0)
            self.assertGreater(counters["emitted"], 0)
            self.assertGreater(diag["speech_ratio"], 0)
            self.assertIsNotNone(diag["last_frame_age_s"])
            self.assertEqual(diag["clients"]["subtitle"], 0)

            audio, rate = pipeline.recent_audio(2.0)
            self.assertEqual(rate, cfg.audio.sample_rate)
            self.assertGreater(len(audio), 0)
            self.assertLessEqual(len(audio), 2 * rate)

    def test_empty_recognition_is_visible(self) -> None:
        """ASR が空文字を返すケースを"無音"と取り違えない（最重要の診断項目）。"""
        from livesub.asr.base import AsrResult

        class EmptyASR:
            name = "empty"
            is_demo = False

            def transcribe(self, audio, sample_rate):  # noqa: ANN001
                return AsrResult(text="")

            def close(self) -> None:
                pass

        with _tmpdir() as tmp:
            pipeline, _ = self._pipeline(tmp, asr=EmptyASR())
            pipeline.run()
            counters = pipeline.diag()["counters"]
            self.assertGreater(counters["asr_calls"], 0)
            self.assertEqual(counters["asr_empty"], counters["asr_calls"])
            self.assertEqual(counters["emitted"], 0)


class DiagVerdictTest(unittest.TestCase):
    def _diag(self, **overrides):
        base = {
            "running": True,
            "last_frame_age_s": 0.2,
            "speech_ratio": 0.3,
            "counters": {"frames": 100, "utterances": 3, "asr_calls": 3, "asr_empty": 0,
                         "emitted": 3},
            "clients": {"subtitle": 1, "levels": 1},
        }
        base.update(overrides)
        return base

    def test_verdict_follows_the_signal_chain(self) -> None:
        from livesub.server import _diag_verdict

        self.assertIn("音声が届いていません", _diag_verdict(self._diag(
            last_frame_age_s=10)))
        self.assertIn("VAD", _diag_verdict(self._diag(speech_ratio=0.0)))
        self.assertIn("区切り", _diag_verdict(self._diag(
            counters={"frames": 100, "utterances": 0, "asr_calls": 0, "asr_empty": 0, "emitted": 0})))
        self.assertIn("チャンネル", _diag_verdict(self._diag(
            counters={"frames": 100, "utterances": 3, "asr_calls": 3, "asr_empty": 3, "emitted": 0})))
        self.assertIn("OBS", _diag_verdict(self._diag(clients={"subtitle": 0, "levels": 0})))
        self.assertIn("正常", _diag_verdict(self._diag()))


class PublisherLevelFilterTest(unittest.TestCase):
    def test_level_only_goes_to_subscribers(self) -> None:
        """字幕页不能被 10Hz 的电平消息塞满队列。"""
        import asyncio

        from livesub.pipeline import Publisher

        async def scenario() -> None:
            pub = Publisher()
            plain = pub.add_client()
            meter = pub.add_client(allow_levels=True)
            pub._fanout({"type": "level", "db": -30})
            pub._fanout({"type": "line", "id": 1})
            self.assertEqual(plain.get_nowait()["type"], "line")
            self.assertTrue(plain.empty())
            self.assertEqual(meter.get_nowait()["type"], "level")
            self.assertEqual(meter.get_nowait()["type"], "line")

        asyncio.run(scenario())


class OpenAdminTest(unittest.TestCase):
    def test_waits_until_health_is_ready_then_opens_admin(self) -> None:
        """起動直後にブラウザで設定ページを開く（/api/health が 200 になってから）。"""
        import threading
        import webbrowser
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from livesub.__main__ import _open_admin_when_ready

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/api/health":
                    self.send_error(404)
                    return
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # noqa: ANN002
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        opened: list[str] = []
        done = threading.Event()

        def fake_open(url: str, *args, **kwargs) -> bool:
            opened.append(url)
            done.set()
            return True

        original = webbrowser.open
        webbrowser.open = fake_open
        try:
            _open_admin_when_ready("127.0.0.1", port, timeout=10.0)
            self.assertTrue(done.wait(5.0), "health が 200 になったら開くはず")
            self.assertEqual(opened, [f"http://127.0.0.1:{port}/admin"])
        finally:
            webbrowser.open = original
            server.shutdown()
            server.server_close()

    def test_does_not_open_when_http_is_not_ready(self) -> None:
        """端口能连上但 HTTP 还没起来时，不能打开浏览器（否则会看到连接失败页）。"""
        import socket
        import threading
        import webbrowser

        from livesub.__main__ import _open_admin_when_ready

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        opened: list[str] = []
        done = threading.Event()

        def fake_open(url: str, *args, **kwargs) -> bool:
            opened.append(url)
            done.set()
            return True

        original = webbrowser.open
        webbrowser.open = fake_open
        try:
            _open_admin_when_ready("127.0.0.1", port, timeout=0.3)
            self.assertFalse(done.wait(2.0), "HTTP 未就绪时不应打开浏览器")
            self.assertEqual(opened, [])
        finally:
            webbrowser.open = original
            server.close()


class GlossaryLayoutTest(unittest.TestCase):
    def test_sections_are_collapsed_and_add_button_is_on_top(self) -> None:
        """词典两个表默认收起；展开后「＋ 追加」在表格上面。"""
        html = (ROOT / "overlay" / "admin.html").read_text(encoding="utf-8")
        for fold_id, body_id, add_id in (
            ("fold-phrases", "phrases-body", "add-phrase"),
            ("fold-terms", "terms-body", "add-term"),
        ):
            # 没有 open 属性 = 页面加载时默认收起
            self.assertIn(f'<details class="card fold" id="{fold_id}">', html)
            block = html.split(f'id="{fold_id}"', 1)[1].split("</details>", 1)[0]
            self.assertLess(
                block.index(f'id="{add_id}"'),
                block.index(f'id="{body_id}"'),
                "「＋ 追加」按钮应该在表格上方",
            )


class OverlayAutoHideTest(unittest.TestCase):
    def test_overlay_has_auto_hide(self) -> None:
        html = (ROOT / "overlay" / "index.html").read_text(encoding="utf-8")
        self.assertIn("clear_after_ms", html)
        self.assertIn("scheduleClear", html)
        self.assertIn("fadeOutAll", html)
        # 预排版页面不能自动把文字消掉，否则调样式时看不到
        self.assertIn("if (PREVIEW) return;", html)

    def test_admin_has_audio_and_record_ui(self) -> None:
        html = (ROOT / "overlay" / "admin.html").read_text(encoding="utf-8")
        for needle in ("panel-audio", "panel-record", "audio_device", "audio_gain",
                       "audio_threshold", "meter-fill", "record_on", "open-record",
                       "認識しきい値", "保存フォルダを開く", "既定オフ"):
            self.assertIn(needle, html)

    def test_admin_has_diagnostics_and_channel_picker(self) -> None:
        """0.5.1：字幕が出ないときにどこで止まったかを画面だけで切り分けられる。"""
        html = (ROOT / "overlay" / "admin.html").read_text(encoding="utf-8")
        for needle in ("panel-diag", "diag-test", "diag-bundle", "diag-list",
                       "audio_channel", "channel-field", "meter-channels",
                       "サポート用ファイルを保存", "入力チャンネル"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
