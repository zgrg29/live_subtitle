"""术语表包装器：套在任意翻译引擎外面，对上层完全透明。

顺序很重要：
1. 整句查表（寒暄等固定说法）——命中就直接返回，连模型都不用跑，这部分是 0ms；
2. 术语命中 → 按 `pre` 改写日文原文 → 翻译；
3. 按 `post` 修译文里的错误写法；
4. 统一清洗（标点、重复、孤立引号）。

任何一步都不改变"一句只翻一次、不闪字幕"的约定（ADR-003）。
"""

from __future__ import annotations

import threading

from ..text.glossary import Glossary
from ..text.postprocess import clean
from ..text.punctuate import punctuate
from .base import Translator


class GlossaryTranslator:
    def __init__(
        self,
        inner: Translator,
        glossary: Glossary,
        max_phrase_len: int = 24,
        punctuate: bool = False,
    ) -> None:
        self._inner = inner
        self._glossary = glossary
        self._max_phrase = max(4, int(max_phrase_len))
        self._punctuate = bool(punctuate)
        self._lock = threading.Lock()
        self._stats = {"phrase": 0, "term": 0, "plain": 0, "punct": 0}
        self.name = f"glossary:{inner.name}"

    @property
    def inner(self) -> Translator:
        return self._inner

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def translate(self, ja: str) -> str:
        text = (ja or "").strip()
        if not text:
            return ""

        # 取一次本地引用：设置面板热加载时整体替换，不会出现"半新半旧"
        g = self._glossary

        phrase = g.phrase(text, self._max_phrase)
        if phrase is not None:
            self._bump("phrase")
            return clean(phrase)

        if self._punctuate:
            text = punctuate(text)
            self._bump("punct")

        hits = g.match_terms(text)
        source = g.rewrite_source(text, hits) if hits else text
        zh = self._inner.translate(source)
        if hits:
            zh = g.repair_target(zh, hits)
        self._bump("term" if hits else "plain")
        return clean(zh)

    def reload(self, glossary: Glossary) -> None:
        """设置面板保存词典后热加载：整体替换引用即可。"""
        self._glossary = glossary

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def _bump(self, key: str) -> None:
        with self._lock:
            self._stats[key] += 1
