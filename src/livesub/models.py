"""模型下载：只依赖标准库，走 HuggingFace 的公开 API。

国内网络默认直连 huggingface.co 往往不通，支持环境变量切换镜像：
    set LIVESUB_HF_MIRROR=https://hf-mirror.com

用法：
    python -m livesub download --search "zipformer ja"
    python -m livesub download --repo <owner>/<name> --to models/sherpa-ja
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PATTERNS = ("encoder", "decoder", "joiner", "tokens.txt", "*.md")


def endpoint() -> str:
    return (
        os.environ.get("LIVESUB_HF_MIRROR")
        or os.environ.get("HF_ENDPOINT")
        or "https://huggingface.co"
    ).rstrip("/")


def _get_json(url: str, timeout: float = 20.0) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": "livesub/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def search(query: str, limit: int = 20) -> list[str]:
    url = f"{endpoint()}/api/models?search={urllib.parse.quote(query)}&limit={limit}"
    try:
        data = _get_json(url)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"访问 {endpoint()} 失败：{exc}") from exc
    return [item.get("id", "") for item in data if item.get("id")]


def list_files(repo: str) -> list[str]:
    url = f"{endpoint()}/api/models/{repo}"
    data = _get_json(url)
    if not isinstance(data, dict):
        return []
    return [s.get("rfilename", "") for s in data.get("siblings", []) if s.get("rfilename")]


def download(repo: str, target: str | Path, patterns: tuple[str, ...] = DEFAULT_PATTERNS) -> list[Path]:
    """下载匹配的文件；已存在且大小一致则跳过（可重复执行）。"""
    target_dir = Path(target)
    target_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in list_files(repo) if _match(f, patterns)]
    files = _prefer_int8(files)
    if not files:
        raise RuntimeError(
            f"{repo} 里没有匹配 {patterns} 的文件。可以先用 `livesub download --repo {repo} --list` 看清单。"
        )

    saved: list[Path] = []
    for name in files:
        destination = target_dir / Path(name).name
        url = f"{endpoint()}/{repo}/resolve/main/{urllib.parse.quote(name)}"
        _fetch(url, destination)
        saved.append(destination)
    return saved


def download_test_wavs(repo: str, target: str | Path) -> list[Path]:
    """下载仓库里的 test_wavs/（含 transcript.txt），用于基准测试。"""
    target_dir = Path(target)
    target_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in list_files(repo) if f.startswith("test_wavs/")]
    if not files:
        raise RuntimeError(f"{repo} 里没有 test_wavs/ 目录")
    saved = []
    for name in files:
        url = f"{endpoint()}/{repo}/resolve/main/{urllib.parse.quote(name)}"
        saved.append(_fetch(url, target_dir / Path(name).name))
    return saved


def _prefer_int8(files: list[str]) -> list[str]:
    """同一套模型同时有 fp32 和 int8 时，只下 int8。

    否则一个 encoder 就能拖 500MB，而 int8 的日语精度损失几乎看不出来。
    """
    int8_stems = {f.rsplit(".int8.", 1)[0] for f in files if ".int8." in f}
    if not int8_stems:
        return files
    return [
        f
        for f in files
        if ".int8." in f or f.rsplit(".", 1)[0] not in int8_stems
    ]


def _match(name: str, patterns: tuple[str, ...]) -> bool:
    lowered = name.lower()
    if "/" in name and not lowered.endswith(".onnx") and not lowered.endswith(".txt"):
        return False
    for pattern in patterns:
        p = pattern.lower()
        if p.startswith("*"):
            if lowered.endswith(p[1:]):
                return True
        elif p in lowered:
            return True
    return False


def _fetch(url: str, destination: Path) -> Path:
    if destination.exists() and destination.stat().st_size > 0:
        print(f"  已存在，跳过：{destination.name}")
        return destination
    tmp = destination.with_suffix(destination.suffix + ".part")
    print(f"  下载 {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "livesub/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as fh:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r    {done / 1048576:.1f}/{total / 1048576:.1f} MB", end="")
    except (urllib.error.URLError, TimeoutError) as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败：{exc}") from exc
    if total:
        print()
    tmp.replace(destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo = ""
    target = "models/sherpa-ja"
    query = ""
    show_list = False
    test_wavs = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--repo":
            repo = argv[i + 1]
            i += 2
        elif arg == "--to":
            target = argv[i + 1]
            i += 2
        elif arg == "--search":
            query = argv[i + 1]
            i += 2
        elif arg == "--test-wavs":
            test_wavs = argv[i + 1]
            i += 2
        elif arg == "--list":
            show_list = True
            i += 1
        else:
            print(f"未知参数：{arg}")
            return 2

    if query:
        results = search(query)
        print(f"在 {endpoint()} 上搜索 {query!r}：")
        for name in results:
            print(f"  {name}")
        return 0

    if not repo:
        print("用法：livesub download --search <关键词> | --repo <owner>/<name> [--to 目录]")
        return 2

    if show_list:
        for name in list_files(repo):
            print(name)
        return 0

    if test_wavs:
        saved = download_test_wavs(repo, test_wavs)
        print(f"完成：{len(saved)} 个测试音频 → {Path(test_wavs).resolve()}")
        return 0

    saved = download(repo, target)
    print(f"完成：{len(saved)} 个文件 → {Path(target).resolve()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
