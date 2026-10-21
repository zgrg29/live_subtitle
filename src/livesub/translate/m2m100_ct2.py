"""M2M100-418M 的 CTranslate2 实现：纯 CPU、int8、单句几十毫秒。

为什么选它：
- **MIT 许可**（NLLB 是 CC-BY-NC，不能进 MIT 项目）；
- 418M 参数、int8 后约 470MB，不需要 torch；
- 专用翻译模型，同尺寸下比小 LLM 更稳。

输入格式（实测出来的，别乱改）：
    tokens = ["__ja__", *sentencepiece_pieces, "</s>"]
    target_prefix = [["__zh__"]]
少了结尾的 `</s>` 会退化成「您您您您…」；target_prefix 写 `zh` 而不是 `__zh__` 会输出乱码。
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..config import data_dir


class M2M100CT2:
    def __init__(
        self,
        model_dir: str | Path,
        src_lang: str = "ja",
        tgt_lang: str = "zh",
        num_threads: int = 2,
    ) -> None:
        try:
            import ctranslate2
            import sentencepiece
        except Exception as exc:
            raise RuntimeError(
                "缺少翻译依赖，请执行：pip install -r requirements-mt.txt"
            ) from exc

        self._dir = Path(model_dir)
        if not self._dir.is_absolute():
            self._dir = data_dir() / self._dir
        spm_file = self._dir / "sentencepiece.bpe.model"
        if not (self._dir / "model.bin").exists() or not spm_file.exists():
            raise FileNotFoundError(f"翻译模型不完整：{self._dir}")

        self._sp = sentencepiece.SentencePieceProcessor(model_file=str(spm_file))
        self._src_token = f"__{src_lang}__"
        self._tgt_token = f"__{tgt_lang}__"
        self._lock = threading.Lock()
        self._tr = ctranslate2.Translator(
            str(self._dir),
            device="cpu",
            compute_type="int8",
            intra_threads=max(1, int(num_threads)),
            inter_threads=1,
        )
        self.name = f"m2m100:{self._dir.name}"

    def translate(self, ja: str) -> str:
        text = (ja or "").strip()
        if not text:
            return ""
        tokens = [self._src_token, *self._sp.encode(text, out_type=str), "</s>"]
        with self._lock:
            result = self._tr.translate_batch(
                [tokens],
                target_prefix=[[self._tgt_token]],
                beam_size=1,  # 贪心：字幕要延迟稳定，不追求最高分
                max_decoding_length=64,
            )
        return _decode(result[0].hypotheses[0])

    def close(self) -> None:
        self._tr = None  # type: ignore[assignment]


def _decode(tokens: list[str]) -> str:
    """去掉语言标记和 EOS，再拼回中文（中文不需要词间空格）。"""
    kept = [t for t in tokens if not t.startswith("__") and t != "</s>"]
    return "".join(kept).replace("▁", "").strip()
