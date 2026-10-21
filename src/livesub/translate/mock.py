"""未接入真实翻译模型时的实现。"""

from __future__ import annotations

DEMO_PAIRS = {
    "こんにちは、今日も配信を始めます。": "大家好，今天也开始直播了。",
    "このゲーム、けっこう難しいんですよね。": "这个游戏挺难的啊。",
    "ちょっと待ってください、今行きます。": "等一下，我马上来。",
    "みんな、コメントありがとう。": "大家，谢谢评论。",
    "あ、しまった、またやられちゃった。": "啊，糟了，又被干掉了。",
    "もう一回だけ挑戦してみようと思います。": "我想再挑战一次。",
}


class EchoTranslator:
    """把日语原样加个前缀，用来验证"翻译这一段"是否接通。"""

    name = "echo(翻訳未接続)"

    def translate(self, ja: str) -> str:
        return f"［翻訳未接続］{ja}"

    def close(self) -> None:
        return None


class NullTranslator:
    """只显示日语，不做翻译。"""

    name = "none"

    def translate(self, ja: str) -> str:
        return ""

    def close(self) -> None:
        return None


class DemoTranslator:
    """给预设台词配上中文，让 demo 看起来像真的在翻译。"""

    name = "demo(台本訳)"

    def translate(self, ja: str) -> str:
        return DEMO_PAIRS.get(ja, f"［デモ訳］{ja}")

    def close(self) -> None:
        return None
