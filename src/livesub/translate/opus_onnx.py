"""opus-mt-ja-zh（75M）的 ONNX Runtime 实现。

为什么不用 CTranslate2：这个 Marian 转 CT2 后 EOS 标记错位，输出退化成单字
（模型本身没问题，transformers 直接跑是好的）。ONNX 路线绕过了这个问题，
而且运行时只需要 onnxruntime，不需要 torch。

关键点：
- 第一步用 `decoder_model.onnx`（无 past），它会把 encoder 的 K/V 也算出来；
- 之后用 `decoder_with_past_model.onnx`，只更新 decoder 侧的 K/V，encoder 的复用；
- 不做 KV cache 的话每步都要重算整句，实测慢 3~5 倍。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from ..config import data_dir


class OpusOnnxTranslator:
    def __init__(
        self,
        model_dir: str | Path = "models/opus-mt-ja-zh-past",
        num_threads: int = 2,
        max_new_tokens: int = 32,
    ) -> None:
        try:
            import onnxruntime as ort
            from transformers import MarianTokenizer
        except Exception as exc:
            raise RuntimeError(
                "缺少翻译依赖，请执行：pip install -r requirements-mt.txt"
            ) from exc

        self._dir = Path(model_dir)
        if not self._dir.is_absolute():
            self._dir = data_dir() / self._dir
        for name in ("encoder_model.onnx", "decoder_model.onnx", "decoder_with_past_model.onnx"):
            if not (self._dir / name).exists():
                raise FileNotFoundError(f"ONNX 模型不完整，缺少 {name}：{self._dir}")

        cfg = json.loads((self._dir / "config.json").read_text(encoding="utf-8"))
        self._start = int(cfg.get("decoder_start_token_id") or 65000)
        self._eos = int(cfg.get("eos_token_id") or 0)
        self._max_new = max(4, int(max_new_tokens))

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(num_threads))
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        def session(name: str):
            return ort.InferenceSession(
                str(self._dir / name), options, providers=["CPUExecutionProvider"]
            )

        self._enc = session("encoder_model.onnx")
        self._dec0 = session("decoder_model.onnx")
        self._dec = session("decoder_with_past_model.onnx")
        self._dec0_out = [o.name for o in self._dec0.get_outputs()]
        self._dec_out = [o.name for o in self._dec.get_outputs()]
        self._past_names = [i.name for i in self._dec.get_inputs() if i.name.startswith("past_key_values")]

        self._tok = MarianTokenizer.from_pretrained(str(self._dir))
        self._lock = threading.Lock()
        self.name = f"opus-onnx:{self._dir.name}"

    def translate(self, ja: str) -> str:
        text = (ja or "").strip()
        if not text:
            return ""
        with self._lock:
            return self._greedy(text)

    def _greedy(self, text: str) -> str:
        ids = self._tok.encode(text)
        mask = np.ones((1, len(ids)), dtype=np.int64)
        hidden = self._enc.run(
            None, {"input_ids": np.array([ids], dtype=np.int64), "attention_mask": mask}
        )[0]

        # 第一步：算 encoder+decoder 的 K/V
        outputs = self._dec0.run(
            self._dec0_out,
            {
                "encoder_attention_mask": mask,
                "input_ids": np.array([[self._start]], dtype=np.int64),
                "encoder_hidden_states": hidden,
            },
        )
        logits = outputs[0]
        past = _as_past(self._dec0_out, outputs)
        tokens = [int(np.argmax(logits[0, -1]))]

        for _ in range(self._max_new - 1):
            if tokens[-1] == self._eos:
                break
            feeds = {
                "encoder_attention_mask": mask,
                "input_ids": np.array([[tokens[-1]]], dtype=np.int64),
            }
            feeds.update(past)
            outputs = self._dec.run(self._dec_out, feeds)
            # 只更新 decoder 侧的 K/V；encoder 侧沿用第一步的结果
            past.update(_as_past(self._dec_out, outputs))
            tokens.append(int(np.argmax(outputs[0][0, -1])))

        if tokens and tokens[-1] == self._eos:
            tokens = tokens[:-1]
        return self._tok.decode(tokens, skip_special_tokens=True).strip()

    def close(self) -> None:
        self._enc = self._dec0 = self._dec = None  # type: ignore[assignment]


def _as_past(out_names: list[str], outputs: list) -> dict:
    """把输出的 `present.i.xxx` 改名成下一步要喂的 `past_key_values.i.xxx`。"""
    return {
        name.replace("present.", "past_key_values.", 1): value
        for name, value in zip(out_names[1:], outputs[1:])
    }
