"""本地 HTTP/WebSocket 服务：给 OBS 浏览器源提供字幕页。

安全约定：
  - 只绑定 127.0.0.1，不监听局域网；
  - 没有对外暴露的控制接口，唯一的写入口是配置文件和命令行。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import APP_NAME, __version__
from .config import Config
from .metrics import Metrics
from .pipeline import Pipeline, PipelineStatus, Publisher


@dataclass
class Runtime:
    cfg: Config
    pipeline: Pipeline
    publisher: Publisher
    metrics: Metrics
    status: PipelineStatus
    overlay_dir: Path


def create_app(runtime: Runtime) -> FastAPI:
    overlay_file = runtime.overlay_dir / "index.html"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime.publisher.attach_loop(asyncio.get_running_loop())
        runtime.pipeline.start()
        try:
            yield
        finally:
            runtime.pipeline.stop()
            runtime.pipeline.join(timeout=3.0)

    app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/overlay")

    @app.get("/overlay")
    async def overlay() -> Response:
        if not overlay_file.exists():
            return JSONResponse({"error": f"缺少字幕页文件：{overlay_file}"}, status_code=500)
        return FileResponse(
            overlay_file,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/config")
    async def api_config() -> dict:
        return asdict(runtime.cfg.overlay)

    @app.get("/api/health")
    async def api_health() -> dict:
        return {
            "app": APP_NAME,
            "version": __version__,
            "status": runtime.status.to_dict(),
            "pid": __import__("os").getpid(),
        }

    @app.get("/api/metrics")
    async def api_metrics() -> dict:
        return runtime.metrics.summary()

    @app.get("/api/history")
    async def api_history() -> dict:
        return {"lines": runtime.publisher.history()}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = runtime.publisher.add_client()
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "version": __version__,
                    "config": asdict(runtime.cfg.overlay),
                    "status": runtime.status.to_dict(),
                    "history": runtime.publisher.history(),
                }
            )
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    message = {"type": "ping", "t": time.time()}
                await websocket.send_json(message)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            runtime.publisher.remove_client(queue)

    return app
