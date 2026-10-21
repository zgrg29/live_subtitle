"""译前补标点：把 ASR 的无标点输出补成模型训练时见过的样子。

为什么值得做：同一批句子，去掉标点后 F1 从 0.605 掉到 0.456（docs/DEVLOG.md）。
Marian 是在带标点的语料上训的，没有「、」和「？」时，从句边界会丢，
反问句还会被读成陈述句（`この敵前より強くなってない` → "他们比敌人更强大"）。

原则：**宁可少插，不可插错**。实测（60 句，无标点输入）：

| 处理 | F1 | 明显错 |
|---|---|---|
| 什么都不补 | 0.456 | 13 |
| 只补逗号 | 0.485 | 13 |
| 只补句末 | **0.504** | **10** |
| 逗号 + 句末 | 0.503 | 10 |

所以**默认只补句末**：逗号没有净收益（改好 6 句、改坏 5 句，互相抵消），
却要多背 5 个回归风险。逗号逻辑保留在 `_insert_commas` 里，`commas=True` 才启用，
留给"句子更长、从句更多"的语料再验证。

句末规则：`か/かな/だろう/でしょ/じゃない/の` 结尾 → 「？」；否则 「。」。
已经带标点的句子原样返回——书写体输入上这个函数是 no-op。
"""

from __future__ import annotations

# 只允许出现在句首的寒暄/感叹词：插在它们后面几乎永远是对的
_LEAD_WORDS = (
    "えーと", "えっと", "えー", "あのー", "あの", "うーん", "うん", "まあ", "はい",
    "なるほど", "たしかに", "ちなみに", "というわけで", "とりあえず", "じゃあ",
    "では", "それで", "だから", "でも", "そして", "まず",
)

# 从句连接词：接在前一句后面，逗号插在它们**之后**
_CLAUSE_WORDS = ("けど", "けれど", "けれども", "ので", "のに", "ですが")

# 句末是这些 → 疑问句
_QUESTION_END = ("か", "かな", "だろう", "でしょ", "じゃない", "の", "っけ")

_END_PUNCT = "。！？!?"
_AFTER = "、。，,!?！？…"  # 这些字符后面不再补逗号


def punctuate(text: str, commas: bool = False) -> str:
    """给无标点的日文补句末标点（可选补逗号）；已带标点的原样返回。"""
    s = (text or "").strip()
    if not s:
        return s
    if commas:
        s = _insert_commas(s)
    return _add_terminal(s)


def _insert_commas(text: str) -> str:
    out = text
    # 从句连接词：任何位置都可以，插在词后
    for word in sorted(_CLAUSE_WORDS, key=len, reverse=True):
        out = _insert_after(out, word, require_head=False)
    # 句首寒暄：只认开头，避免「入ってはいけません」这种被误切
    for word in sorted(_LEAD_WORDS, key=len, reverse=True):
        out = _insert_after(out, word, require_head=True)
    return out


def _insert_after(text: str, word: str, require_head: bool) -> str:
    idx = 0
    while True:
        i = text.find(word, idx)
        if i < 0:
            return text
        end = i + len(word)
        nxt = text[end] if end < len(text) else ""
        head_ok = (i == 0) if require_head else True
        if head_ok and nxt and nxt not in _AFTER:
            text = text[:end] + "、" + text[end:]
            idx = end + 1
        else:
            idx = end


def _add_terminal(text: str) -> str:
    if not text or text[-1] in _END_PUNCT:
        return text
    if text.endswith(_QUESTION_END):
        return text + "？"
    return text + "。"
