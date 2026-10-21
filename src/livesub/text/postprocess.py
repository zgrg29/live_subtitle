"""译文后处理：便宜、确定性、可回滚的清洗。

原则：只做"改坏了能一眼看出来"的事——折叠解码崩溃的重复、统一标点、清掉孤立引号。
**不做任何语义改写**，那是模型的工作；文本规则一旦越界，就会造出比原文更难查的 bug。
"""

from __future__ import annotations

import re

# 只在中文字符旁边转全角，避免把 "3,000" / "C++" 这种也改掉
_ASCII_PUNCT = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_OPEN = "「“\"『("
_CLOSE = {"「": "」", "“": "”", '"': '"', "『": "』", "(": ")"}


def clean(zh: str) -> str:
    """把模型输出收拾成能上屏的一行中文。"""
    text = (zh or "").strip()
    if not text:
        return ""
    text = _collapse_repeats(text)
    text = _strip_stray_quotes(text)
    text = _fullwidth_punct(text)
    text = re.sub(r"\.{3,}", "……", text)
    text = re.sub(r"。{2,}", "。", text)
    # 中文之间不该有空格（`录播 稍后` 这种是替换时带进来的）
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _collapse_repeats(text: str) -> str:
    """折叠"Flame Flame Flame"这类解码崩溃。

    注意：中文口语里的「哈哈哈哈哈」是合法的，所以单字重复要 6 次以上才动手，
    并且折叠成两次而不是一次（"哈哈"比"哈"自然）。
    """
    # 先处理"整句就是同一个词重复"：`帧数 帧数 帧数`，词之间带空格，正则抓不到
    parts = [p for p in re.split(r"[\s、,，]+", text.strip()) if p]
    if len(parts) >= 3 and len(set(parts)) == 1:
        return parts[0]

    # 开头在打转："麦克风 comming 麦克风 comming 麦克风 麦克风" —— 只看前缀周期，
    # 尾巴拖着的东西说明解码已经崩了，整句只留一个周期
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if len(words) >= 4:
        for period in range(1, 5):
            if len(words) > period * 2 and words[period:period * 2] == words[:period]:
                text = " ".join(words[:period])
                break

    # 英文短语打转："mike comming mike comming" → "mike comming"
    for _ in range(3):
        folded = re.sub(r"\b([A-Za-z][\w']*(?:\s+[A-Za-z][\w']*){1,2})\s+\1\b", r"\1", text)
        if folded == text:
            break
        text = folded

    for n in range(12, 1, -1):
        text = re.sub(r"(.{%d})\1{2,}" % n, r"\1", text)
    text = re.sub(r"(.)\1{5,}", r"\1\1", text)
    return text


def _strip_stray_quotes(text: str) -> str:
    """模型偶尔在开头吐一个没闭合的引号（`“对,就稍微老大一点。`）。"""
    while text and text[0] in _OPEN:
        opener = text[0]
        closer = _CLOSE[opener]
        if closer in text[1:]:
            break
        text = text[1:].lstrip()
    if text and text[-1] in "”」』":
        opener = {v: k for k, v in _CLOSE.items()}[text[-1]]
        if opener not in text[:-1]:
            text = text[:-1].rstrip()
    return text


def _fullwidth_punct(text: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch in _ASCII_PUNCT:
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if (prev and _CJK.match(prev)) or (nxt and _CJK.match(nxt)):
                out.append(_ASCII_PUNCT[ch])
                continue
        out.append(ch)
    return "".join(out)
