"""语音识别引擎。"""

from .base import ASR, AsrResult
from .mock import EchoASR, ScriptedASR

__all__ = ["ASR", "AsrResult", "EchoASR", "ScriptedASR"]
