"""端到端连通性检查：HTTP + WebSocket 都要通。

用法（先另开一个窗口跑起来）：
    python -m livesub run --synthetic 30 --mock --port 8799
    python tools/check_server.py --port 8799
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request

import websockets

try:  # Windows 控制台默认 GBK，中文会乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


async def check_ws(port: int, expect_lines: int) -> int:
    url = f"ws://127.0.0.1:{port}/ws"
    seen = {"hello": 0, "line": 0, "status": 0}
    deadline = time.monotonic() + 25
    async with websockets.connect(url, open_timeout=5) as ws:
        while time.monotonic() < deadline:
            if seen["hello"] and seen["line"] >= expect_lines:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind in seen:
                seen[kind] += 1
            if kind == "line":
                print(f"  字幕 #{msg['id']}: {msg['ja']}  →  {msg['zh']}")
                print(f"    延迟 {msg['latency']['end_to_line_ms']} ms")
            elif kind == "hello":
                print(f"  收到 hello，历史 {len(msg.get('history', []))} 条")
    print(f"  WebSocket 统计：{seen}")
    return 0 if seen["line"] >= expect_lines and seen["hello"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lines", type=int, default=1, help="至少收到几条字幕")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    print(f"检查 {base}")
    health = http_json(f"{base}/api/health")
    print(f"  /api/health  {health['app']} {health['version']}  运行中={health['status']['running']}")

    with urllib.request.urlopen(f"{base}/overlay", timeout=5) as response:
        html = response.read().decode("utf-8")
    print(f"  /overlay     {len(html)} 字节，含 stage={'id=\"stage\"' in html}")

    metrics = http_json(f"{base}/api/metrics")
    print(f"  /api/metrics 已产生 {metrics.get('utterances', 0)} 句")

    code = asyncio.run(check_ws(args.port, args.lines))
    print("结果：" + ("通过" if code == 0 else "失败"))
    return code


if __name__ == "__main__":
    sys.exit(main())
