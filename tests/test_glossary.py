"""术语表 / 后处理的自测（不需要模型和麦克风）。

这些用例的重点不是"翻得对不对"，而是**不把本来翻对的句子改坏**：
词典是热路径上的字符串手术，一个越界的正则就能让所有字幕变样。
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from livesub.text.glossary import Glossary, Term, load_glossary, normalize  # noqa: E402
from livesub.text.postprocess import clean  # noqa: E402
from livesub.text.punctuate import punctuate  # noqa: E402
from livesub.translate.glossary_mt import GlossaryTranslator  # noqa: E402


def _tmpdir() -> Path:
    base = ROOT / ".testtmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"glossary-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return path


class NormalizeTest(unittest.TestCase):
    def test_strips_punctuation_and_space(self):
        self.assertEqual(normalize("こんばんは。"), "こんばんは")
        self.assertEqual(normalize(" 今日は、いい天気 ですね！"), "今日はいい天気ですね")

    def test_fullwidth_alnum(self):
        self.assertEqual(normalize("ＰＳ５"), "PS5")


class PhraseTest(unittest.TestCase):
    def test_hit_ignores_punctuation(self):
        g = Glossary(phrases={"こんばんは": "晚上好"})
        self.assertEqual(g.phrase("こんばんは。"), "晚上好")
        self.assertEqual(g.phrase("こんばんは！"), "晚上好")

    def test_miss_when_not_exact(self):
        g = Glossary(phrases={"こんばんは": "晚上好"})
        self.assertIsNone(g.phrase("こんばんは、今日も"))  # 不是整句，不能套

    def test_long_input_not_looked_up(self):
        g = Glossary(phrases={"あ": "啊"})
        self.assertIsNone(g.phrase("あ" * 40))  # 超过 max_len


class TermMatchTest(unittest.TestCase):
    def test_longest_first_no_overlap(self):
        g = Glossary(terms=(
            Term("配信者", "主播"),
            Term("配信", "直播", mode="post", wrong=("发送",)),
        ))
        hits = g.match_terms("配信者の配信")
        self.assertEqual([t.zh for _, t in hits], ["主播", "直播"])

    def test_rewrite_only_pre(self):
        g = Glossary(terms=(
            Term("マイク", "麦克风", mode="pre"),
            Term("配信", "直播", mode="post", wrong=("发送",)),
        ))
        text = "マイクと配信"
        hits = g.match_terms(text)
        self.assertEqual(g.rewrite_source(text, hits), "麦克风と配信")

    def test_repair_only_when_term_present(self):
        g = Glossary(terms=(Term("配信", "直播", mode="post", wrong=("发送",)),))
        # 原句里没有「配信」时，就算译文里有"发送"也不许动
        self.assertEqual(g.repair_target("开始发送", []), "开始发送")
        hits = g.match_terms("配信を始めます")
        self.assertEqual(g.repair_target("开始发送", hits), "开始直播")

    def test_repair_longest_wrong_first(self):
        g = Glossary(terms=(Term("配信", "直播", mode="post", wrong=("发送", "发送邮件")),))
        hits = g.match_terms("配信を始めます")
        self.assertEqual(g.repair_target("今天开始发送邮件了", hits), "今天开始直播了")


class LoadTest(unittest.TestCase):
    def test_load_and_override(self):
        d = _tmpdir()
        (d / "phrases.tsv").write_text("こんばんは\t晚上好\n# 注释\n\n", encoding="utf-8")
        (d / "terms.tsv").write_text(
            "配信\t直播\tpost\t发送|发货\t\n"
            "マイク\t麦克风\tpre\t\t\n"
            "坏行\t没有模式\tpost\t\t\n",  # post 但没写错误写法 → 丢弃
            encoding="utf-8",
        )
        g = load_glossary(str(d))
        self.assertEqual(g.phrase("こんばんは"), "晚上好")
        self.assertEqual(len(g.terms), 2)  # 坏行被丢掉

    def test_builtin_glossary_loads(self):
        g = load_glossary("glossary")
        self.assertTrue(len(g) > 0)
        self.assertEqual(g.phrase("こんばんは。"), "晚上好")


class CleanTest(unittest.TestCase):
    def test_collapse_repeated_word_sequence(self):
        self.assertEqual(clean("帧数 帧数 帧数"), "帧数")
        self.assertEqual(clean("Flame Flame Flame Flame"), "Flame")

    def test_collapse_prefix_loop(self):
        # 解码打转常见形态：开头周期重复，尾巴还拖着半截
        self.assertEqual(clean("麦克风 comming 麦克风 comming 麦克风 麦克风"), "麦克风 comming")
        self.assertEqual(clean("はい はい はい"), "はい")

    def test_keep_legit_laughter(self):
        self.assertEqual(clean("哈哈哈哈哈"), "哈哈哈哈哈")  # 5 个以内不许动
        self.assertEqual(clean("哈哈哈哈哈哈哈哈"), "哈哈")  # 8 个明显是崩了

    def test_stray_leading_quote(self):
        self.assertEqual(clean("“嗯，今天不错。"), "嗯，今天不错。")

    def test_fullwidth_punct_next_to_cjk(self):
        self.assertEqual(clean("你好,世界?"), "你好，世界？")
        self.assertEqual(clean("3,000"), "3,000")  # 数字里的逗号不动

    def test_no_space_between_cjk(self):
        self.assertEqual(clean("录播 稍后上传"), "录播稍后上传")


class PunctuateTest(unittest.TestCase):
    """译前补标点：只补句末，且必须对已带标点的输入是 no-op。"""

    def test_terminal_by_ending(self):
        self.assertEqual(punctuate("こんばんは"), "こんばんは。")
        self.assertEqual(punctuate("聞こえましたか"), "聞こえましたか？")
        self.assertEqual(punctuate("そうだろう"), "そうだろう？")

    def test_noop_when_already_punctuated(self):
        self.assertEqual(punctuate("今日はいい天気ですね。"), "今日はいい天気ですね。")
        self.assertEqual(punctuate("本当に？"), "本当に？")
        self.assertEqual(punctuate(""), "")

    def test_commas_off_by_default(self):
        # 逗号没有净收益，默认不插
        self.assertEqual(punctuate("えーと今日は寒いので行きます"),
                         "えーと今日は寒いので行きます。")
        self.assertIn("、", punctuate("えーと今日は寒いので行きます", commas=True))


class _Fake:
    name = "fake"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def translate(self, ja: str) -> str:
        self.seen.append(ja)
        return "开始发送"

    def close(self) -> None:
        pass


class TranslatorTest(unittest.TestCase):
    def test_phrase_hit_skips_model(self):
        fake = _Fake()
        mt = GlossaryTranslator(fake, Glossary(phrases={"こんばんは": "晚上好"}))
        self.assertEqual(mt.translate("こんばんは。"), "晚上好")
        self.assertEqual(fake.seen, [])  # 根本没调模型

    def test_pre_rewrite_and_post_repair(self):
        fake = _Fake()
        g = Glossary(terms=(
            Term("マイク", "麦克风", mode="pre"),
            Term("配信", "直播", mode="post", wrong=("发送",)),
        ))
        mt = GlossaryTranslator(fake, g)
        mt.translate("マイクと配信を始めます")
        self.assertIn("麦克风", fake.seen[0])  # 原文被改写
        self.assertIn("配信", fake.seen[0])  # post 词不动原文

    def test_stats(self):
        mt = GlossaryTranslator(_Fake(), Glossary(phrases={"こんばんは": "晚上好"}))
        mt.translate("こんばんは")
        mt.translate("これはテストです")
        self.assertEqual(mt.stats(), {"phrase": 1, "term": 0, "plain": 1, "punct": 0})


if __name__ == "__main__":
    unittest.main()
