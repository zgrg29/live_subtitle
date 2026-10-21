"""ASR 接口约定。

只要求一次调用把一段音频转成文本：M0 是"VAD 切句 + 整句识别"，
所以引擎不需要支持流式。以后接流式引擎时，在 pipeline 里加一个
"partial 通道"即可，不影响这里的契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class AsrResult:
    text: str
    lang: str = "ja"
    confidence: float = 0.0


class ASR(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> AsrResult: ...

    def close(self) -> None: ...
