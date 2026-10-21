"""翻译接口约定。

M1 会接入真实的 ja→zh 引擎（小 NMT 或量化 LLM），但接口保持不变：
输入一句日语，输出一句中文。翻译层自己负责"太长就压缩"这件事。
"""

from __future__ import annotations

from typing import Protocol


class Translator(Protocol):
    name: str

    def translate(self, ja: str) -> str: ...

    def close(self) -> None: ...
