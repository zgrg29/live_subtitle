"""日语化回归测试（不需要模型和麦克风）。

产品面向日本主播：用户能看到的文件必须是日语。这里只守"最容易被改回去"的几个入口，
不做逐字文案断言——文案会变，语言不该变。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from livesub.config import Config, to_toml  # noqa: E402


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


class GeneratedConfigTest(unittest.TestCase):
    def test_comments_are_japanese(self) -> None:
        text = to_toml(Config())
        self.assertIn("# LiveSub の設定ファイルです", text)
        self.assertIn("マイク", text)
        self.assertIn("入力ゲイン", text)
        for chinese in ("配置。", "系统默认麦克风", "音量增益"):
            self.assertNotIn(chinese, text)


class UserFacingPagesTest(unittest.TestCase):
    def test_admin_page_is_japanese(self) -> None:
        html = _read("overlay/admin.html")
        self.assertIn('lang="ja"', html)
        for word in ("保存して適用", "スタイル", "辞書", "試し翻訳", "縁取り"):
            self.assertIn(word, html)
        for word in ("保存并应用", "样式", "词典", "描边"):
            self.assertNotIn(word, html)

    def test_overlay_page_is_japanese(self) -> None:
        html = _read("overlay/index.html")
        self.assertIn('lang="ja"', html)
        self.assertIn("字幕未接続", html)
        self.assertIn("接続済み", html)
        self.assertNotIn("字幕未连接", html)


class DocsAndLauncherTest(unittest.TestCase):
    def test_readme_is_japanese_and_points_to_progress(self) -> None:
        readme = _read("README.md")
        self.assertIn("クイックスタート", readme)
        self.assertIn("docs/PROGRESS.md", readme)
        self.assertNotIn("项目进度", readme)

    def test_launcher_is_japanese(self) -> None:
        bat = _read("起動.bat")
        self.assertIn("LiveSub を起動しています", bat)
        self.assertFalse((ROOT / "启动.bat").exists())

    def test_glossary_headers_are_japanese(self) -> None:
        self.assertIn("定型フレーズ", _read("glossary/phrases.tsv"))
        self.assertIn("用語集", _read("glossary/terms.tsv"))


if __name__ == "__main__":
    unittest.main()
