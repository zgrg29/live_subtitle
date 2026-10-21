"""日语句尾判断：这段 ASR 文本像不像"说完了"。

为什么需要它
------------
ReazonSpeech 出来的日语没有标点，光看文本看不出句子边界。但嘴上的停顿会骗人：
主播在助词、接续词后面停 0.5 秒继续说是常态。所以切句不能只看静音长度，
还要看"这句话是否停在一个能收尾的位置"。

判断只做两档，宁可保守：
    COMPLETE  可以单独上屏
    CONTINUE  几乎肯定没说完，先别翻译/上屏，等下一段合并

这里刻意不引入模型：规则表几十行、零延迟、出错也能一眼看懂。以后要换标点模型，
只需要让本模块的 analyze() 换实现，上层不用动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Ending(str, Enum):
    COMPLETE = "complete"  # 可以收句
    CONTINUE = "continue"  # 还没说完，等待合并


@dataclass(frozen=True)
class EndingInfo:
    ending: Ending
    reason: str  # 命中的规则，排障/调参时看日志用
    text: str  # 去掉开头语气词后的文本


# 句号/问号/感叹号：ASR 自带标点（有的模型会带）时，直接信它
_HARD_END = "。．.!?！？…"
_COMMA_END = "、，,;；:"

# 这些词本身以助词结尾，但它们是完整的一句话，不能按助词判成"没说完"
_COMPLETE_WORDS = {
    "こんにちは", "こんばんは", "おはよう", "おやすみ",
    "ありがとう", "ありがとうございます", "すみません", "ごめん", "ごめんなさい",
    "はい", "いいえ", "うん", "ううん", "そう", "そうそう", "なるほど",
    "了解", "りょうかい", "おつかれ", "おつかれさま", "お疲れ様",
    "はじめまして", "よろしく", "ただいま", "おかえり", "おかえりなさい", "いらっしゃい",
    "いただきます", "ごちそうさま", "いってきます", "いってらっしゃい",
}

# 以这些结尾 = 句子还没完。按"最长匹配优先"排列。
# 注意：不要把单个「し」放进来（「寿司」这类名词会误判）；
# 也不要把「ん」放进来（「ません」「んです」会被误伤）。
_CONTINUE_SUFFIXES = (
    "とともに", "と共に", "ながら", "けれども", "けれど", "けど",
    "ので", "のに", "から", "まで", "より", "だけ", "しか", "ほど",
    "など", "ばかり", "って", "という", "たり", "だり", "つつ",
    "ても", "でも", "なら", "たら", "れば", "ば",
    "や", "の", "と", "も", "へ", "を", "に", "で", "が", "は", "て",
)

# 以这些结尾 = 基本说完了。长的写在前面，避免「です」抢在「でした」前面。
_COMPLETE_SUFFIXES = (
    "でしょうか", "でしょう", "ましょう", "ください", "ません",
    "でした", "ました", "ですか", "ますか", "ましたか", "ませんか",
    "くださいね", "んですよね", "んですね", "んです", "のです", "んだ", "のだ",
    "と思います", "と思う", "じゃない", "じゃん", "よね", "かな", "かしら",
    "ですね", "ですよ", "ますね", "ますよ",
    "です", "ます",
    "ね", "よ", "な", "わ", "ぞ", "ぜ", "さ", "か",
    "た", "だ", "い",
)

# 动词/形容词的辞书形结尾（行く、食べる、高い…）。放在助词判断之后。
_DICT_END = frozenset("うくぐすつぬぶむる")

# 这一组是"看起来像收句、但口语里经常只是喘口气"的结尾：
# 「今日はですね、」后面往往还有话。短句时多等一会儿，长句就当说完了。
_SOFT_DISCOURSE = ("ですね", "ですよ", "ますね", "ますよ")

# 开头的语气词：只删"带长音/明确是犹豫"的形式。
# 「あの」「その」「まあ」不算——它们可能是"那个游戏"里的"那个"。
_LEADING_FILLER = re.compile(
    r"^(?:(?:えーと|ええと|えっと|えー|あー|うーん|んーと|んー|そのー|そー|はー|ふーん)"
    r"[、,，.。！!？?\s]*)"
)

# 只由犹豫声组成的整句（可以直接丢掉，不上屏）
_FILLER_ONLY = re.compile(
    r"^(?:(?:えーと|えー+|ええと|えっと|あー+|あのー+|うーん+|んー+|そのー+|はー+|ふーん+))+$"
)

_NON_WORD = re.compile(r"[\s、。，．,.!?！？…「」『』（）()【】\[\]・〜~\-—]")


def strip_fillers(text: str) -> str:
    """删掉句子开头的犹豫声，例如「えーと、今日は」→「今日は」。

    只处理开头，不碰句中（句中删除会改变语义，而且 ASR 本来也经常吞掉）。
    """
    out = (text or "").strip()
    while True:
        new = _LEADING_FILLER.sub("", out, count=1)
        if new == out:
            break
        out = new
    return out.strip()


def is_filler_only(text: str) -> bool:
    """整句只有「えーと」「あー」「うーん」这类声音，没有任何内容。"""
    core = _NON_WORD.sub("", text or "")
    return bool(core) and bool(_FILLER_ONLY.fullmatch(core))


def _core_length(text: str) -> int:
    return len(_NON_WORD.sub("", text))


def analyze(text: str, short_chars: int = 8) -> EndingInfo:
    """判断一句日语是否说完了。

    规则顺序（后面的不会覆盖前面的）：
        1. 结尾标点；
        2. 固定短语白名单（こんにちは、はい…）；
        3. 短句 + ですね / ですよ → 先当没说完；
        4. 助词 / 接续词 / て形 → 没说完；
        5. です・ます・た・ね… → 说完了；
        6. 动词辞书形 → 说完了；
        7. 太短（默认 < 8 字）→ 当没说完（「このゲーム」这种）；
        8. 其余（长名词句）→ 说完了。
    """
    cleaned = strip_fillers(text)
    if not cleaned:
        return EndingInfo(Ending.COMPLETE, "empty", cleaned)

    probe = cleaned.rstrip()
    last = probe[-1]

    if last in _HARD_END:
        return EndingInfo(Ending.COMPLETE, "punct:end", cleaned)
    if last in _COMMA_END:
        return EndingInfo(Ending.CONTINUE, "punct:comma", cleaned)
    if probe in _COMPLETE_WORDS:
        return EndingInfo(Ending.COMPLETE, "word", cleaned)

    core_len = _core_length(probe)

    if core_len <= 10 and probe.endswith(_SOFT_DISCOURSE):
        return EndingInfo(Ending.CONTINUE, "discourse", cleaned)

    for suffix in _CONTINUE_SUFFIXES:
        if probe.endswith(suffix):
            return EndingInfo(Ending.CONTINUE, f"particle:{suffix}", cleaned)

    for suffix in _COMPLETE_SUFFIXES:
        if probe.endswith(suffix):
            return EndingInfo(Ending.COMPLETE, f"end:{suffix}", cleaned)

    if last in _DICT_END:
        return EndingInfo(Ending.COMPLETE, "dict", cleaned)

    if core_len <= short_chars:
        return EndingInfo(Ending.CONTINUE, f"short:{core_len}", cleaned)

    return EndingInfo(Ending.COMPLETE, "long", cleaned)
