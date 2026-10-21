"""翻译引擎。"""

from .base import Translator
from .mock import DemoTranslator, EchoTranslator, NullTranslator

__all__ = ["Translator", "EchoTranslator", "NullTranslator", "DemoTranslator"]
