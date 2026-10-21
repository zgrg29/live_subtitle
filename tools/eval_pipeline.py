"""整条切句→识别流水线的离线评估：一句话被切成几行、错多少字、延迟多少。

和 tools/bench_asr.py 的区别：
    bench_asr  直接喂整段音频，只测 ASR 本身；
    本工具     走真实的 VAD + 断句合并 + ASR + 翻译，测"最终上屏"的效果。

用法：
    python tools/eval_pipeline.py --wav-dir models/test_wavs
    python tools/eval_pipeline.py --wav-dir models/test_wavs --no-merge
    python tools/eval_pipeline.py --wav-dir models/test_wavs --no-merge --end-silence 420  # 旧行为
    python tools/eval_pipeline.py --wav-dir models/test_wavs --no-mt

指标：
    lines    最终上了几行字幕
    merged   发生了多少次半句合并
    CER      把上屏文本拼起来和 transcript.txt 比（字错率，已去标点）
    p50/p95  "说完→上屏"（不含 OBS 渲染）
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_asr import edit_distance, load_transcripts, normalise  # noqa: E402

from livesub.audio.capture import WavSource  # noqa: E402
from livesub.config import Config, load  # noqa: E402
from livesub.factory import build_asr, build_mt  # noqa: E402
from livesub.metrics import Metrics  # noqa: E402
from livesub.pipeline import Pipeline, PipelineStatus, Publisher  # noqa: E402
from livesub.translate.mock import NullTranslator  # noqa: E402


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav-dir", default="models/test_wavs")
    parser.add_argument("--wav", action="append", default=[])
    parser.add_argument("--config", default="")
    parser.add_argument("--end-silence", type=int, default=0, help="覆盖 vad.end_silence_ms")
    parser.add_argument("--no-merge", action="store_true", help="关掉半句合并（对照）")
    parser.add_argument("--no-mt", action="store_true", help="不加载翻译模型（只看 CER/断句）")
    args = parser.parse_args()

    if args.wav:
        wavs = [Path(p) for p in args.wav]
        wav_dir = wavs[0].parent
    else:
        wav_dir = Path(args.wav_dir)
        wavs = sorted(wav_dir.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"没有找到 WAV：{wav_dir}")

    refs = load_transcripts(wav_dir)
    cfg = load(args.config) if args.config else Config()
    if args.end_silence:
        cfg.vad.end_silence_ms = args.end_silence
    if args.no_merge:
        cfg.segment.merge = False

    print(
        f"\nVAD end_silence={cfg.vad.end_silence_ms}ms  merge={'off' if args.no_merge else 'on'}"
        f"  ASR={cfg.asr.engine}  MT={'none' if args.no_mt else cfg.mt.engine}\n"
    )
    header = (
        f"{'file':<12}{'dur s':>7}{'lines':>6}{'merged':>7}{'CER':>7}"
        f"{'p50':>7}{'p95':>7}{'asr p50':>9}{'mt p50':>8}  text"
    )
    print(header)
    print("─" * 120)

    total_err = 0.0
    total_ref = 0
    all_latency: list[float] = []
    all_asr: list[float] = []
    all_mt: list[float] = []
    total_lines = 0
    total_merged = 0

    for path in wavs:
        # 每条重建引擎：Pipeline.run() 结束时会 close()，模型对象不能跨文件复用
        asr = build_asr(cfg)
        mt = NullTranslator() if args.no_mt else build_mt(cfg)
        source = WavSource(
            path,
            sample_rate=cfg.audio.sample_rate,
            block_ms=cfg.audio.block_ms,
            realtime=False,
        )
        with wave.open(str(path), "rb") as fh:
            duration = fh.getnframes() / fh.getframerate()
        metrics = Metrics()
        pipeline = Pipeline(
            cfg, source, asr, mt, Publisher(), metrics, PipelineStatus(), log_dir="logs/eval"
        )
        pipeline.run()

        traces = list(metrics.traces)
        text = "".join(t.text for t in traces)
        display = " ‖ ".join(t.text for t in traces)
        cer_text = "-"
        ref = refs.get(path.name, "")
        if ref:
            r, h = normalise(ref), normalise(text)
            err = edit_distance(r, h)
            cer = err / max(1, len(r))
            total_err += err
            total_ref += len(r)
            cer_text = f"{cer * 100:5.1f}%"

        lat = [t.end_to_line_ms for t in traces]
        asr_ms = [t.asr_ms for t in traces]
        mt_ms = [t.mt_ms for t in traces]
        all_latency += lat
        all_asr += asr_ms
        all_mt += mt_ms
        total_lines += len(traces)
        total_merged += metrics.merged_segments

        print(
            f"{path.name:<12}{duration:>7.2f}{len(traces):>6}{metrics.merged_segments:>7}"
            f"{cer_text:>7}{_pct(lat, 0.5):>7.0f}{_pct(lat, 0.95):>7.0f}"
            f"{_pct(asr_ms, 0.5):>9.0f}{_pct(mt_ms, 0.5):>8.0f}  {display}"
        )

    print("─" * 120)
    total_cer = (total_err / total_ref * 100) if total_ref else 0.0
    print(
        f"合计 {len(wavs)} 条 / {total_lines} 行 / 合并 {total_merged} 次，"
        f"CER {total_cer:.2f}%"
    )
    if all_latency:
        print(
            f"说完→上屏 p50 {_pct(all_latency, 0.5):.0f}ms / p95 {_pct(all_latency, 0.95):.0f}ms"
            f"（ASR p50 {_pct(all_asr, 0.5):.0f}ms，MT p50 {_pct(all_mt, 0.5):.0f}ms）"
        )
    print("\n提示：CER 只反映识别，不反映翻译；长音频的 ASR 时间天然更长。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
