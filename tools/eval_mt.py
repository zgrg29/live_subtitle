"""翻译质量评测：把评测集跑一遍当前 MT 引擎，出一份可读报告。

和 `eval_pipeline.py` 的分工：
- `eval_pipeline.py` 量的是**整条链路**（切句 / 合并 / CER / 端到端延迟），输入是 WAV；
- 本脚本量的是**翻译本身**，输入是纯句子，不碰麦克风和识别，一次只要几秒。

用法：
    python tools/eval_mt.py
    python tools/eval_mt.py --limit 10
    python tools/eval_mt.py --set eval/mt_eval.jsonl --out-md logs/eval/mt.md

评测集格式（JSONL，一行一句）：
    {"id": "A01", "cat": "寒暄", "ja": "こんばんは。", "ref": "晚上好。", "note": "考点"}

`ref` 是人工参考译文，也是唯一的评分基准；改 `ref` 就是改标准，别改脚本。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

# 控制台是 GBK：不要让一个特殊字符把整个评测搞崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - 老终端
        pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = ROOT / "eval" / "mt_eval.jsonl"

# 日文标点/空白：ASR 输出里没有这些东西
_NO_PUNCT = str.maketrans("", "", "、。，．,.!?！？…‥・「」『』（）()　 ")

# 只在"输出明显坏了"时才报警，避免满屏 False Positive
KANA = set("ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴ"
           "ふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろゎわゐゑをん"
           "ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノ"
           "ハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロヮワヰヱヲンヴヵヶ")
CANTONESE = set("哋唔係喺咗啲嘅咁乜睇嚟畀冇嘢哋")

FLAG_ORDER = ["空输出", "未翻译", "假名残留", "粤语", "重复", "长度异常", "超长"]


def main() -> int:
    ap = argparse.ArgumentParser(description="评测 ja→zh 翻译质量")
    ap.add_argument("--set", default=str(DEFAULT_SET), help="评测集 JSONL")
    ap.add_argument("--config", default=str(ROOT / "config.toml"), help="配置文件")
    ap.add_argument("--out-md", default="", help="报告输出路径（默认 logs/eval/mt-<时间>.md）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--no-glossary", action="store_true", help="绕过术语表，跑裸模型（A/B 用）")
    ap.add_argument("--strip-punct", action="store_true",
                    help="去掉日文标点再翻，模拟 ASR 实际输出（线上就是这个形态）")
    ap.add_argument("--punctuate", action="store_true",
                    help="译前补标点（配合 --strip-punct 才是真实链路）")
    ap.add_argument("--no-punct", action="store_true", help="关掉译前补标点（真·裸模型）")
    ap.add_argument("--tag", default="", help="报告文件名后缀，例如 before / after")
    ap.add_argument("--all", action="store_true",
                    help="把 out_of_scope 的句子也算进去（默认跳过：唱歌/歌词不翻译）")
    args = ap.parse_args()

    items = _load_set(Path(args.set))
    if args.strip_punct:
        # 模拟 ASR 输出：流式 transducer 不吐标点，这是线上真实形态
        items = [{**it, "ja": it["ja"].translate(_NO_PUNCT)} for it in items]
    if args.limit > 0:
        items = items[: args.limit]

    # 唱歌/歌词不在产品范围内（主播明确说不翻译歌词），默认排除
    skipped = 0
    if not args.all:
        kept = [it for it in items if not it.get("out_of_scope")]
        skipped = len(items) - len(kept)
        items = kept
    if skipped:
        print(f"skipped {skipped} out-of-scope sentences (no lyric translation)")

    from livesub.config import load as load_config
    from livesub.factory import build_mt, last_mt_error

    cfg = load_config(Path(args.config))
    if args.no_glossary:
        cfg.glossary.enabled = False
    if args.punctuate:
        cfg.mt.punctuate = True
    if args.no_punct:
        cfg.mt.punctuate = False
    mt = build_mt(cfg)
    err = last_mt_error()
    if err:
        print(f"[warn] mt engine fell back: {err}", file=sys.stderr)
    # 控制台编码不可靠（GBK/UTF-8 混着来），进度只用 ASCII，正文一律写文件
    print(f"engine={mt.name}  sentences={len(items)}")

    # 第一次推理要建 session、JIT 一下，别算进延迟
    mt.translate("今日はいい天気ですね。")

    rows: list[dict] = []
    for it in items:
        t0 = time.perf_counter()
        try:
            out = mt.translate(it["ja"])
        except Exception as exc:  # 单句失败不该中断整轮评测
            out = f"<ERROR {type(exc).__name__}: {exc}>"
        ms = (time.perf_counter() - t0) * 1000.0
        rows.append({**it, "out": out, "ms": round(ms, 1),
                     **_score(out, it["ja"], it.get("ref", ""), it.get("alt", ()))})

    close = getattr(mt, "close", None)
    if callable(close):
        close()

    report = _render(rows, mt.name, args.set)
    out_md = Path(args.out_md) if args.out_md else _default_out(args.tag)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report, encoding="utf-8")

    out_json = out_md.with_suffix(".json")
    out_json.write_text(
        json.dumps({"engine": mt.name, "ts": datetime.now().isoformat(timespec="seconds"), "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lat = [r["ms"] for r in rows]
    f1 = [r["f1"] for r in rows]
    flagged = sum(1 for r in rows if r["flags"])
    low = sum(1 for r in rows if r["f1"] < 0.2)
    print(f"F1 mean {statistics.fmean(f1):.3f}  (F1<0.2: {low}/{len(rows)})")
    print(f"latency p50 {statistics.median(lat):.0f}ms  max {max(lat):.0f}ms")
    print(f"flagged {flagged}/{len(rows)}")
    print(f"report: {out_md}")
    print(f"data:   {out_json}")
    return 0


# ---------- 打分 ----------

def _score(out: str, ja: str, ref: str, alts: list[str] | tuple[str, ...] = ()) -> dict:
    """字符 bigram F1 + 一组"看起来就不对"的标记。

    一句可以有多个同样正确的译法（`alt`），取其中最高的 F1——否则评分会变成
    "谁背下了我的措辞"，而不是"翻得对不对"。

    bigram F1 不追求和 COMET 对齐，只用来在同一套句子上做前后对比：
    改之前 0.62、改之后 0.71，比"感觉好点了"可靠。
    """
    out = (out or "").strip()
    flags: list[str] = []
    candidates = [ref] + [a for a in (alts or []) if a]
    f1 = max((_bigram_f1(out, c) for c in candidates if c), default=0.0)

    if not out:
        flags.append("空输出")
    if out == ja.strip():
        flags.append("未翻译")
    if any(ch in KANA for ch in out):
        flags.append("假名残留")
    if any(ch in CANTONESE for ch in out):
        flags.append("粤语")
    if _has_repeat(out):
        flags.append("重复")
    if ref and (len(out) > len(ref) * 2.5 or len(out) < len(ref) * 0.4):
        flags.append("长度异常")
    if len(out) > 40:
        flags.append("超长")

    flags.sort(key=lambda f: FLAG_ORDER.index(f) if f in FLAG_ORDER else 99)
    return {"f1": round(f1, 3), "flags": flags, "chars": len(out)}


def _bigram_f1(out: str, ref: str) -> float:
    if not out or not ref:
        return 0.0
    a = _grams(out)
    b = _grams(ref)
    if not a or not b:
        return 0.0
    hit = sum(min(a[g], b[g]) for g in a.keys() & b.keys())
    p = hit / sum(a.values())
    r = hit / sum(b.values())
    return 0.0 if hit == 0 else 2 * p * r / (p + r)


def _grams(text: str) -> dict[str, int]:
    text = "".join(ch for ch in text if not ch.isspace())
    out: dict[str, int] = {}
    for i in range(len(text) - 1):
        g = text[i : i + 2]
        out[g] = out.get(g, 0) + 1
    if len(text) == 1:
        out[text] = 1
    return out


def _has_repeat(text: str) -> bool:
    """4 字以上的片段连续出现 3 次，基本就是解码崩了。"""
    for n in (4, 5, 6):
        for i in range(len(text) - n * 3 + 1):
            g = text[i : i + n]
            if g * 3 in text:
                return True
    return False


# ---------- 报告 ----------

def _render(rows: list[dict], engine: str, set_path: str) -> str:
    lat = [r["ms"] for r in rows]
    f1 = [r["f1"] for r in rows]
    flagged = [r for r in rows if r["flags"]]
    lines = [
        "# MT 评测报告",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 引擎：`{engine}`",
        f"- 评测集：`{set_path}`（{len(rows)} 句）",
        f"- 延迟：p50 **{statistics.median(lat):.0f}ms** / p95 {_pct(lat, 95):.0f}ms / max {max(lat):.0f}ms",
        f"- 字符 bigram F1 均值：**{statistics.fmean(f1):.3f}**",
        f"- 可疑输出：**{len(flagged)}** 句" + (f"（{', '.join(sorted({f for r in flagged for f in r['flags']}))}）" if flagged else ""),
        "",
        "> F1 只用于同一套句子的前后对比，不代表绝对质量。`ref` 改了，分数就会变，这是有意的。",
        "",
        "## 分类汇总",
        "",
        "| 类别 | 句数 | 平均 F1 | p50 延迟 | 可疑 |",
        "|---|---|---|---|---|",
    ]
    for cat in dict.fromkeys(r["cat"] for r in rows):
        sub = [r for r in rows if r["cat"] == cat]
        lines.append(
            f"| {cat} | {len(sub)} | {statistics.fmean(r['f1'] for r in sub):.3f} "
            f"| {statistics.median(r['ms'] for r in sub):.0f}ms "
            f"| {sum(1 for r in sub if r['flags'])} |"
        )

    if flagged:
        lines += ["", "## 可疑句", "",
                  "| id | 标记 | 日本語 | 模型输出 |", "|---|---|---|---|"]
        for r in flagged:
            lines.append(f"| {r['id']} | {', '.join(r['flags'])} | {r['ja']} | {r['out']} |")

    lines += ["", "## 逐句明细", "",
              "| id | 类别 | 日本語 | 参考 | 模型输出 | F1 | ms |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        mark = " ⚠️" if r["flags"] else ""
        lines.append(
            f"| {r['id']} | {r['cat']} | {r['ja']} | {r.get('ref', '')} "
            f"| {r['out']}{mark} | {r['f1']:.2f} | {r['ms']:.0f} |"
        )
    lines.append("")
    return "\n".join(lines)


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q / 100 * (len(ordered) - 1))))
    return ordered[idx]


def _default_out(tag: str = "") -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    return ROOT / "logs" / "eval" / f"mt-{stamp}{suffix}.md"


def _load_set(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"评测集不存在：{path}")
    items: list[dict] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{n} JSON 解析失败：{exc}") from exc
        if "ja" not in item:
            raise SystemExit(f"{path}:{n} 缺少 ja 字段")
        item.setdefault("id", f"L{n}")
        item.setdefault("cat", "未分类")
        item.setdefault("ref", "")
        items.append(item)
    return items


if __name__ == "__main__":
    raise SystemExit(main())
