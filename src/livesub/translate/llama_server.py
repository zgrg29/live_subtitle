"""用 llama.cpp 的 llama-server 跑小型指令模型（Qwen）做翻译。

为什么不用 CTranslate2/M2M100：通用平行语料模型学不到口语寒暄（`こんばんは` → "晚餐好"），
而指令微调的 LLM 在"翻译 + 保持口语"这种窄任务上表现明显更好。

运行方式：本进程拉起一个 llama-server 子进程（本地回环端口），用 HTTP 调它。
好处是**不需要编译任何 Python 绑定**，坏处是多一个进程和几秒启动时间。
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..config import bundle_dir, data_dir

SYSTEM_PROMPT = (
    "你是专业的日语到简体中文字幕翻译。把用户给的日语翻成自然、口语化的简体中文。\n"
    "规则：\n"
    "1. 只输出译文本身，不要解释、不要加引号、不要注音、不要输出原文。\n"
    "2. 按中文口语习惯翻译寒暄语，例如 こんばんは→晚上好，はじめまして→初次见面。\n"
    "3. 保留人名、游戏名、专有名词的常见译法，没有就音译。\n"
    "4. 译文尽量短，一行放得下，不要超过 40 个字。\n"
    "5. 遇到语气词（あ、えっと、まあ）可以用对应的中文口语词，不要漏掉情绪。"
)


class LlamaServerTranslator:
    def __init__(
        self,
        model_path: str | Path,
        server_exe: str | Path = "tools/llama/llama-server.exe",
        num_threads: int = 2,
        context: int = 512,
        startup_timeout_s: float = 120.0,
    ) -> None:
        self._model = _resolve(model_path)
        self._exe = _resolve(server_exe)
        if not self._model.exists():
            raise FileNotFoundError(f"翻訳モデルがありません：{self._model}")
        if not self._exe.exists():
            raise FileNotFoundError(f"llama-server がありません：{self._exe}")

        self._port = _free_port()
        self._base = f"http://127.0.0.1:{self._port}"
        self._lock = threading.Lock()
        creationflags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        self._proc = subprocess.Popen(
            [
                str(self._exe),
                "-m", str(self._model),
                "-c", str(context),
                "-t", str(max(1, int(num_threads))),
                "--host", "127.0.0.1",
                "--port", str(self._port),
                "-ngl", "0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        try:
            self._wait_ready(startup_timeout_s)
        except Exception:
            self.close()
            raise
        self.name = f"qwen:{self._model.stem}"

    # ---------- 生命周期 ----------

    def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"llama-server の起動に失敗しました（終了コード {self._proc.returncode}）")
            try:
                with urllib.request.urlopen(f"{self._base}/health", timeout=2) as rsp:
                    if rsp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.3)
        raise TimeoutError(f"llama-server が {timeout_s:.0f} 秒以内に準備できませんでした")

    def close(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------- 翻译 ----------

    def translate(self, ja: str) -> str:
        text = (ja or "").strip()
        if not text:
            return ""
        payload = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "top_p": 0.9,
            "max_tokens": 96,
            "stream": False,
            "cache_prompt": True,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with self._lock:
            try:
                with urllib.request.urlopen(request, timeout=120) as rsp:
                    body = json.loads(rsp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"翻訳サービスが応答しません：{exc}") from exc
        choice = (body.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        return _clean(content)


def _clean(text: str) -> str:
    """去掉模型偶尔加的解释、引号、多余换行。"""
    text = text.strip()
    for prefix in ("译文：", "译文:", "翻译：", "翻译:", "中文："):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.split("\n")[0].strip()
    if len(text) >= 2 and text[0] in "「“\"'" and text[-1] in "」”\"'":
        text = text[1:-1].strip()
    return text


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    for base in (data_dir(), bundle_dir()):
        candidate = base / p
        if candidate.exists():
            return candidate
    return data_dir() / p


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
