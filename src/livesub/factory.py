"""按配置组装具体实现（音频源 / ASR / 翻译）。"""

from __future__ import annotations

from pathlib import Path

from .asr.base import ASR
from .asr.mock import EchoASR, ScriptedASR
from .asr.sherpa_offline import SherpaOfflineASR
from .audio.capture import AudioSource, MicSource, SyntheticSource, WavSource
from .config import Config, data_dir, project_root  # noqa: F401  (project_root 供外部沿用)
from .translate.base import Translator
from .translate.mock import DemoTranslator, EchoTranslator, NullTranslator


def build_source(
    cfg: Config,
    wav: str | None = None,
    synthetic_seconds: float | None = None,
) -> AudioSource:
    if wav:
        # 实时回放：这样测出来的延迟才是真实延迟
        return WavSource(wav, sample_rate=cfg.audio.sample_rate,
                         block_ms=cfg.audio.block_ms, realtime=True)
    if synthetic_seconds:
        return SyntheticSource(synthetic_seconds, sample_rate=cfg.audio.sample_rate,
                               block_ms=cfg.audio.block_ms, realtime=True)
    return MicSource(
        device=cfg.audio.device,
        sample_rate=cfg.audio.sample_rate,
        block_ms=cfg.audio.block_ms,
        gain=cfg.audio.gain,
    )


def build_asr(cfg: Config, force_mock: bool = False) -> ASR:
    engine = cfg.asr.engine
    if force_mock or engine == "mock":
        return ScriptedASR()

    model_dir = Path(cfg.asr.model_dir)
    if not model_dir.is_absolute():
        model_dir = data_dir() / model_dir

    if engine in ("auto", "sherpa"):
        try:
            return SherpaOfflineASR(
                model_dir,
                num_threads=cfg.asr.num_threads,
                lead_pad_ms=cfg.asr.lead_pad_ms,
            )
        except Exception as exc:
            if engine == "sherpa" or not cfg.asr.fallback_to_mock:
                raise
            # 这里不打印，交给 CLI 统一提示，保持库代码安静
            _LAST_ASR_ERROR[0] = f"{type(exc).__name__}: {exc}"
            return EchoASR()

    raise ValueError(f"未知的 ASR 引擎：{engine}")


def build_mt(cfg: Config, force_mock: bool = False) -> Translator:
    engine = cfg.mt.engine
    if force_mock:
        return DemoTranslator()
    if engine == "none":
        return NullTranslator()
    if engine == "echo":
        return EchoTranslator()

    if engine in ("auto", "opus", "onnx"):
        try:
            from .translate.opus_onnx import OpusOnnxTranslator

            return OpusOnnxTranslator(
                cfg.mt.model_dir,
                num_threads=cfg.mt.num_threads,
                max_new_tokens=cfg.mt.max_new_tokens,
            )
        except Exception as exc:
            _LAST_MT_ERROR[0] = f"{type(exc).__name__}: {exc}"
            if engine == "opus" and not cfg.mt.fallback_to_none:
                raise
            return NullTranslator()

    if engine in ("qwen", "llama"):
        try:
            from .translate.llama_server import LlamaServerTranslator

            return LlamaServerTranslator(
                cfg.mt.model_path,
                server_exe=cfg.mt.llama_server,
                num_threads=cfg.mt.num_threads,
                context=cfg.mt.context,
            )
        except Exception as exc:
            _LAST_MT_ERROR[0] = f"{type(exc).__name__}: {exc}"
            if engine in ("qwen", "llama"):
                if not cfg.mt.fallback_to_none:
                    raise
                # 退回 M2M100 也比没有翻译强
                if _m2m100_path(cfg).exists():
                    try:
                        return _build_m2m100(cfg)
                    except Exception:
                        pass
                return NullTranslator()

    if engine in ("auto", "m2m100", "ct2"):
        try:
            return _build_m2m100(cfg)
        except Exception as exc:
            _LAST_MT_ERROR[0] = f"{type(exc).__name__}: {exc}"
            if engine == "m2m100" and not cfg.mt.fallback_to_none:
                raise
            return NullTranslator()

    raise ValueError(f"未知的翻译引擎：{engine}（支持 qwen / m2m100 / none / echo）")


def _m2m100_path(cfg: Config) -> Path:
    path = Path(cfg.mt.model_dir)
    return path if path.is_absolute() else data_dir() / path


def _build_m2m100(cfg: Config) -> Translator:
    from .translate.m2m100_ct2 import M2M100CT2

    return M2M100CT2(
        cfg.mt.model_dir,
        tgt_lang=cfg.mt.target_lang,
        num_threads=cfg.mt.num_threads,
    )


# ASR 回退原因，给 CLI 提示用
_LAST_ASR_ERROR: list[str] = [""]
_LAST_MT_ERROR: list[str] = [""]


def last_asr_error() -> str:
    return _LAST_ASR_ERROR[0]


def last_mt_error() -> str:
    return _LAST_MT_ERROR[0]
