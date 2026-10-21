"""离线识别基准：不用麦克风、不用 VAD，直接量"识别一段音频要多久、错多少字"。

用法：
    python tools/bench_asr.py --model-dir models/sherpa-ja --wav-dir models/test_wavs
    python tools/bench_asr.py --model-dir models/sherpa-ja --wav models/test_wavs/1.wav --threads 4

指标：
    dur     音频时长
    ms      识别耗时
    RTF     耗时 / 音频时长（越小越快，<1 才能实时）
    CER     字错率（有 transcript.txt 时计算，已去掉标点和空格）
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livesub.asr.sherpa_offline import SherpaOfflineASR  # noqa: E402

KANA_PUNCT = re.compile(r"[\s、。，．,.!?！？「」『』（）()…・\-—~〜]")


def read_wav(path: Path, target_sr: int = 16000) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise SystemExit(f"{path.name}: 只支持 16-bit PCM")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != target_sr:
        n = int(len(data) * target_sr / rate)
        data = np.interp(
            np.linspace(0, len(data) - 1, num=n), np.arange(len(data)), data
        ).astype(np.float32)
    return data


def load_transcripts(directory: Path) -> dict[str, str]:
    path = directory / "transcript.txt"
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        key = ""
        text = line
        if "\t" in line:
            key, text = line.split("\t", 1)
        elif re.match(r"^\S+\.wav\b", line):
            parts = line.split(None, 1)
            key, text = parts[0], (parts[1] if len(parts) > 1 else "")
        if key:
            result[Path(key.strip()).name] = text.strip()
        else:
            order.append(text.strip())
    if order:
        for index, text in enumerate(order, start=1):
            result.setdefault(f"{index}.wav", text)
    return result


def edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def normalise(text: str) -> str:
    return KANA_PUNCT.sub("", text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="models/sherpa-ja")
    parser.add_argument("--wav-dir", default="")
    parser.add_argument("--wav", action="append", default=[])
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--repeat", type=int, default=1, help="每条重复几次取平均")
    args = parser.parse_args()

    if args.wav:
        wavs = [Path(p) for p in args.wav]
    elif args.wav_dir:
        wavs = sorted(Path(args.wav_dir).glob("*.wav"))
    else:
        raise SystemExit("请给 --wav-dir 或 --wav")

    refs = load_transcripts(Path(args.wav_dir)) if args.wav_dir else {}

    print(f"\n加载模型 {args.model_dir}（{args.threads} 线程）…")
    asr = SherpaOfflineASR(args.model_dir, num_threads=args.threads)
    print("就绪。\n")

    print(f"{'file':<12}{'dur s':>7}{'ms':>8}{'RTF':>7}{'CER':>7}  text")
    print("─" * 100)
    total_ms = 0.0
    total_audio = 0.0
    cers: list[float] = []

    for path in wavs:
        audio = read_wav(path)
        duration = len(audio) / 16000.0
        times: list[float] = []

        text = ""
        for _ in range(max(1, args.repeat)):
            start = time.perf_counter()
            result = asr.transcribe(audio, 16000)
            times.append((time.perf_counter() - start) * 1000)
            text = result.text
        elapsed = min(times)

        total_ms += elapsed
        total_audio += duration
        cer_text = "-"
        ref = refs.get(path.name, "")
        if ref:
            ref_n, hyp_n = normalise(ref), normalise(text)
            cer = edit_distance(ref_n, hyp_n) / max(1, len(ref_n))
            cers.append(cer)
            cer_text = f"{cer * 100:5.1f}%"

        print(
            f"{path.name:<12}{duration:>7.2f}{elapsed:>8.1f}"
            f"{elapsed / max(1e-6, duration * 1000):>7.3f}{cer_text:>7}  {text}"
        )
        if ref:
            print(f"{'':<12}{'参考':<6}{'':>20}  {ref}")

    print("─" * 100)
    print(f"合计 {len(wavs)} 条，音频 {total_audio:.1f}s，推理 {total_ms:.0f}ms，"
          f"平均 RTF {total_ms / max(1e-6, total_audio * 1000):.3f}")
    if cers:
        print(f"平均 CER {sum(cers) / len(cers) * 100:.1f}%（字级，已去标点空格）")
    os_note = "RTF<1 表示比实时快，越大越省 CPU 余量"
    print(os_note + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
