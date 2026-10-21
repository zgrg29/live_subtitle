"""没有模型时用的假引擎。

它们的存在不是为了"假装能用"，而是为了让整条链路（采集→切句→翻译→OBS 上屏）
在没有任何模型、任何网络的情况下也能被验证和演示。产品启动时如果模型缺失，
会明确提示"当前是演示模式"。
"""

from __future__ import annotations

from itertools import cycle

import numpy as np

from .base import AsrResult

DEMO_LINES = [
    "こんにちは、今日も配信を始めます。",
    "このゲーム、けっこう難しいんですよね。",
    "ちょっと待ってください、今行きます。",
    "みんな、コメントありがとう。",
    "あ、しまった、またやられちゃった。",
    "もう一回だけ挑戦してみようと思います。",
]


class EchoASR:
    """回显：只说"识别到多长的语音"，用来确认音频链路通不通。"""

    name = "echo(モデル未読込)"
    is_demo = True

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> AsrResult:
        seconds = len(audio) / sample_rate
        return AsrResult(text=f"（日本語音声 {seconds:.1f} 秒・認識モデル未読込）")

    def close(self) -> None:
        return None


class ScriptedASR:
    """按顺序吐预设日语，用于 demo 和自测。"""

    name = "demo(台本)"
    is_demo = True

    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines = cycle(lines or DEMO_LINES)
        self._count = 0

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> AsrResult:
        self._count += 1
        return AsrResult(text=next(self._lines))

    def close(self) -> None:
        return None
