"""术语表 / 固定表达表：用文本补模型不会的东西，零延迟。

两张表：
- `phrases.tsv`：整句精确匹配（忽略标点和空格）。只放说法固定的句子，比如寒暄。
- `terms.tsv`：词级替换，三种模式（都实测过，别凭感觉选）：
    * `pre`  —— 译前把日文词换成中文再送模型。片假名专名/食物名走这条：
                实测 Marian 会原样保留注入的中文，而且周围的助词、动词也能跟着变顺。
    * `post` —— 正常翻译，译后把「错误写法」换成正确词。适合"句子结构对、只有这个词错"
                （典型 `配信 → 发送/发货/传送`）。只在日文原句真的出现该词时才替换。
    * 不加   —— 模型本来就翻对的，加了只会引入风险。

设计取舍：不做模糊匹配、不做正则、不做语义判断。宁可漏，不可错——词典改坏一句
本来翻对的字幕，比修好一句更伤。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# 匹配时忽略的字符：全角/半角标点、空白。日文原句里这些不承载语义。
_IGNORED = str.maketrans("", "", "、。，．,.!?！？…‥・「」『』（）()　 \t\n\r")


def normalize(text: str) -> str:
    """去掉标点和空白，全角字母数字转半角（用于整句匹配）。

    还会削掉**结尾**的拖音符号（`こんばんはー` / `おつかれ〜`）：
    口语里几乎每句寒暄都带拖音，不削就永远匹配不上整句表。
    """
    return (
        unicodedata.normalize("NFKC", text or "")
        .translate(_IGNORED)
        .strip()
        .rstrip("ー〜～")
    )


@dataclass(frozen=True)
class Phrase:
    ja: str
    zh: str
    note: str = ""


@dataclass(frozen=True)
class Term:
    ja: str
    zh: str
    mode: str = "pre"  # pre | post
    wrong: tuple[str, ...] = ()
    note: str = ""


@dataclass
class Glossary:
    phrases: dict[str, str] = field(default_factory=dict)
    terms: tuple[Term, ...] = ()
    sources: tuple[Path, ...] = ()
    # 给设置面板用的原始行（phrases 的键是归一化后的，丢掉了原写法）
    phrase_rows: tuple[Phrase, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.phrases or self.terms)

    def __len__(self) -> int:
        return len(self.phrases) + len(self.terms)

    def to_rows(self) -> dict[str, list[dict]]:
        """给设置面板：把当前词典还原成可编辑的行。"""
        return {
            "phrases": [
                {"ja": p.ja, "zh": p.zh, "note": p.note} for p in self.phrase_rows
            ],
            "terms": [
                {
                    "ja": t.ja,
                    "zh": t.zh,
                    "mode": t.mode,
                    "wrong": "|".join(t.wrong),
                    "note": t.note,
                }
                for t in self.terms
            ],
        }

    # ---------- 查询 ----------

    def phrase(self, ja: str, max_len: int = 24) -> str | None:
        """整句命中返回译文；太长的句子不查表（长句按整句套用风险太高）。"""
        key = normalize(ja)
        if not key or len(key) > max_len:
            return None
        return self.phrases.get(key)

    def match_terms(self, ja: str) -> list[tuple[int, Term]]:
        """最长优先、互不重叠地找出句子里命中的术语。"""
        if not self.terms or not ja:
            return []
        occupied = [False] * len(ja)
        found: list[tuple[int, Term]] = []
        for term in self.terms:  # 已按词长倒序排好
            start = 0
            while True:
                i = ja.find(term.ja, start)
                if i < 0:
                    break
                end = i + len(term.ja)
                if not any(occupied[i:end]):
                    found.append((i, term))
                    for k in range(i, end):
                        occupied[k] = True
                start = i + 1
        found.sort(key=lambda pair: pair[0])
        return found

    # ---------- 应用 ----------

    def rewrite_source(self, ja: str, hits: list[tuple[int, Term]]) -> str:
        """把 `pre` 模式的词换成中文，拼回一句送模型。"""
        pre = [(i, t) for i, t in hits if t.mode == "pre"]
        if not pre:
            return ja
        pieces: list[str] = []
        cursor = 0
        for start, term in pre:
            pieces.append(ja[cursor:start])
            pieces.append(term.zh)
            cursor = start + len(term.ja)
        pieces.append(ja[cursor:])
        return "".join(pieces)

    def repair_target(self, zh: str, hits: list[tuple[int, Term]]) -> str:
        """按 `post` 规则修译文里的错误写法（只在原句真的出现过该词时）。"""
        out = zh
        for _, term in hits:
            if term.mode != "post":
                continue
            # 长串优先：`发送邮件` 要比 `发送` 先匹配，否则会剩下一个孤零零的"邮件"
            for wrong in sorted(term.wrong, key=len, reverse=True):
                if not wrong:
                    continue
                # 忽略大小写：模型可能吐 `Mike` 也可能吐 `mike`
                out = re.sub(re.escape(wrong), lambda _m, z=term.zh: z, out, flags=re.IGNORECASE)
        return out


# ---------- 加载 ----------

def load_glossary(dir_name: str = "glossary") -> Glossary:
    """从内置目录 + 用户目录加载；用户文件按行覆盖同名的内置条目。"""
    from ..config import bundle_dir, data_dir

    dirs: list[Path] = []
    explicit = Path(dir_name)
    if explicit.is_absolute():
        # 允许用户指到任意目录：`dir = "D:/我的词典"`
        if explicit.is_dir():
            dirs.append(explicit)
    else:
        for base in (bundle_dir(), data_dir()):
            path = base / dir_name if dir_name else base
            if path.is_dir() and path not in dirs:
                dirs.append(path)

    phrases: dict[str, str] = {}
    phrase_rows: dict[str, Phrase] = {}
    terms: dict[str, Term] = {}
    used: list[Path] = []

    for directory in dirs:
        for path in (directory / "phrases.tsv", directory / "terms.tsv"):
            if not path.is_file():
                continue
            used.append(path)
            rows = _read_tsv(path)
            if path.name == "phrases.tsv":
                for row in rows:
                    key = normalize(row[0])
                    if key:
                        note = row[2] if len(row) > 2 else ""
                        phrases[key] = row[1]
                        phrase_rows[key] = Phrase(row[0], row[1], note)
            else:
                for row in rows:
                    term = _to_term(row)
                    if term is not None:
                        terms[term.ja] = term  # 后加载的覆盖先加载的

    ordered = tuple(sorted(terms.values(), key=lambda t: (-len(t.ja), t.ja)))
    return Glossary(
        phrases=phrases,
        terms=ordered,
        sources=tuple(used),
        phrase_rows=tuple(phrase_rows.values()),
    )


def _read_tsv(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) >= 2 and cols[0] and cols[1]:
            rows.append(cols)
    return rows


def _to_term(row: list[str]) -> Term | None:
    zh = row[1]
    mode = (row[2].lower() if len(row) > 2 and row[2] else "pre")
    if mode not in ("pre", "post"):
        mode = "pre"
    wrong = tuple(w for w in (row[3].split("|") if len(row) > 3 else []) if w.strip())
    note = row[4] if len(row) > 4 else ""
    if mode == "post" and not wrong:
        return None  # post 没有错误写法就没有意义，当成写错了
    return Term(ja=row[0], zh=zh, mode=mode, wrong=wrong, note=note)


_PHRASE_HEADER = (
    "# 定型フレーズ：文全体が一致したときだけ使います（句読点・空白・語尾の「ー」は無視）\n"
    "# 形式：日本語 <TAB> 中国語 <TAB> メモ\n"
    "# 設定ページから書き出されます。手で編集する場合も Tab 区切り・UTF-8 で保存してください。\n"
)
_TERM_HEADER = (
    "# 用語集：日本語 <TAB> 中国語 <TAB> モード(pre/post) <TAB> 誤訳パターン(縦棒区切り・post のみ) <TAB> メモ\n"
    "# pre  = 翻訳前に日本語の語を中国語へ置換してからモデルに渡す（カタカナの固有名詞・ゲーム名）\n"
    "# post = 通常どおり翻訳し、訳文の誤訳パターンを置換（原文にその語があるときだけ有効）\n"
    "# 設定ページから書き出されます。手で編集する場合も Tab 区切り・UTF-8 で保存してください。\n"
)


def _clean_cell(value: object) -> str:
    return str(value or "").replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()


def save_glossary(
    dir_name: str,
    phrases: list[dict],
    terms: list[dict],
) -> list[Path]:
    """把面板里的词典写回 TSV（写到**用户目录**，覆盖内置条目）。

    返回写出的文件路径。格式非法（缺 ja/zh）的行会被跳过，不写垃圾进文件。
    """
    from ..config import data_dir

    target = Path(dir_name)
    if not target.is_absolute():
        target = data_dir() / dir_name
    target.mkdir(parents=True, exist_ok=True)

    phrase_lines = [_PHRASE_HEADER]
    for row in phrases:
        ja, zh = _clean_cell(row.get("ja")), _clean_cell(row.get("zh"))
        if not ja or not zh:
            continue
        phrase_lines.append(f"{ja}\t{zh}\t{_clean_cell(row.get('note'))}")

    term_lines = [_TERM_HEADER]
    for row in terms:
        ja, zh = _clean_cell(row.get("ja")), _clean_cell(row.get("zh"))
        if not ja or not zh:
            continue
        mode = "post" if str(row.get("mode", "pre")).lower() == "post" else "pre"
        wrong = "|".join(w.strip() for w in str(row.get("wrong", "")).split("|") if w.strip())
        if mode == "post" and not wrong:
            continue  # post 必须有错误写法，否则等于没写
        term_lines.append(f"{ja}\t{zh}\t{mode}\t{wrong}\t{_clean_cell(row.get('note'))}")

    written = []
    for name, lines in (("phrases.tsv", phrase_lines), ("terms.tsv", term_lines)):
        path = target / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(path)
    return written
