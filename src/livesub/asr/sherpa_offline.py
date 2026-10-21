"""sherpa-onnx 的离线（整句）识别引擎 —— 纯 CPU，不需要 CUDA。

目录里需要这些文件（transducer 结构）：
    encoder*.onnx  decoder*.onnx  joiner*.onnx  tokens.txt

优先使用 int8 量化版本（快 2~3 倍、内存小一半，日语精度损失很小）。

注意：M0 只接 transducer。paraformer/ctc 模型的加载方式不同，
后续在 `_load` 里按文件名分支即可。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..config import data_dir
from .base import AsrResult


class SherpaOfflineASR:
    def __init__(
        self,
        model_dir: str | Path,
        num_threads: int = 2,
        lead_pad_ms: int = 200,
    ) -> None:
        try:
            import sherpa_onnx
        except Exception as exc:
            raise RuntimeError(
                "缺少 sherpa-onnx，请先执行：pip install -r requirements-asr.txt"
            ) from exc

        self._dir = Path(model_dir)
        if not self._dir.is_absolute():
            # 用 data_dir() 而不是 cwd：和 factory/config 的解析规则保持一致
            self._dir = data_dir() / self._dir
        if not self._dir.exists():
            raise FileNotFoundError(f"模型目录不存在：{self._dir}")

        encoder = _pick(self._dir, "encoder")
        decoder = _pick(self._dir, "decoder")
        joiner = _pick(self._dir, "joiner")
        tokens = self._dir / "tokens.txt"
        if not tokens.exists():
            raise FileNotFoundError(f"缺少 tokens.txt：{self._dir}")

        self.name = f"sherpa:{self._dir.name}"
        self._lead_pad_ms = max(0, int(lead_pad_ms))
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(encoder),
            decoder=str(decoder),
            joiner=str(joiner),
            tokens=str(tokens),
            num_threads=int(num_threads),
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
        )

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> AsrResult:
        # 段首补 200ms 静音：离线 transducer 在"几乎没有前导静音"时容易吞掉第一个短词
        # （实测 はやくおじいさん… 的「はやく」整段丢失，补静音后恢复）。
        if self._lead_pad_ms > 0 and audio.size:
            pad = np.zeros(
                int(sample_rate * self._lead_pad_ms / 1000), dtype=np.float32
            )
            audio = np.concatenate([pad, audio])
        stream = self._rec.create_stream()
        stream.accept_waveform(sample_rate, audio)
        self._rec.decode_stream(stream)
        text = getattr(stream.result, "text", "") or ""
        return AsrResult(text=text.strip())

    def close(self) -> None:
        return None


def _pick(model_dir: Path, stem: str) -> Path:
    """在模型目录里找 stem 对应的 onnx，优先 int8（快 2~3 倍、内存小一半）。

    文件名不统一：有的是 `encoder.int8.onnx`，有的是
    `encoder-epoch-99-avg-1.int8.onnx`，所以先精确匹配，再按 int8 优先排序。
    """
    for name in (f"{stem}.int8.onnx", f"{stem}.onnx"):
        candidate = model_dir / name
        if candidate.exists():
            return candidate

    candidates = sorted(model_dir.glob(f"{stem}*.onnx"))
    int8 = [c for c in candidates if ".int8." in c.name]
    if int8:
        return int8[0]
    if candidates:
        return candidates[0]
    raise FileNotFoundError(f"{model_dir} 里找不到 {stem}*.onnx")
