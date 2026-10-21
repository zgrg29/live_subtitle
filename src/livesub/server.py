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
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from . import APP_NAME, __version__
from .config import CONFIG_FILENAME, Config, apply_overlay, asdict_overlay, save_section
from .metrics import Metrics
from .pipeline import Pipeline, PipelineStatus, Publisher
from .text.glossary import load_glossary, save_glossary


@dataclass
class Runtime:
    cfg: Config
    pipeline: Pipeline
    publisher: Publisher
    metrics: Metrics
    status: PipelineStatus
    overlay_dir: Path
    config_path: Path | None = None


def create_app(runtime: Runtime) -> FastAPI:
    overlay_file = runtime.overlay_dir / "index.html"
    admin_file = runtime.overlay_dir / "admin.html"

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
            return JSONResponse({"error": f"字幕ページのファイルがありません：{overlay_file}"}, status_code=500)
        return FileResponse(
            overlay_file,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/admin")
    async def admin() -> Response:
        if not admin_file.exists():
            return JSONResponse({"error": f"設定ページのファイルがありません：{admin_file}"}, status_code=500)
        return FileResponse(
            admin_file,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/config")
    async def api_config() -> dict:
        return asdict_overlay(runtime.cfg)

    @app.post("/api/config")
    async def api_save_config(payload: dict) -> dict:
        """设置面板保存样式：改内存 → 写 config.toml → 广播给字幕页（无需重启）。"""
        values = apply_overlay(runtime.cfg, payload)
        try:
            save_section(runtime.config_path or CONFIG_FILENAME, "overlay", values)
        except Exception as exc:  # 写盘失败也要把内存里的改动告诉用户
            return JSONResponse({"error": f"config.toml への書き込みに失敗しました：{exc}"}, status_code=500)
        runtime.publisher.publish({"type": "config", "config": values})
        return {"ok": True, "config": values}

    @app.get("/api/glossary")
    async def api_glossary() -> dict:
        glossary = load_glossary(runtime.cfg.glossary.dir)
        return {"ok": True, **glossary.to_rows()}

    @app.post("/api/glossary")
    async def api_save_glossary(payload: dict) -> dict:
        """保存词典：写 TSV → 热加载 → 返回重新读进来的内容。"""
        try:
            paths = save_glossary(
                runtime.cfg.glossary.dir,
                list(payload.get("phrases") or []),
                list(payload.get("terms") or []),
            )
        except Exception as exc:
            return JSONResponse({"error": f"辞書の書き込みに失敗しました：{exc}"}, status_code=500)
        runtime.pipeline.reload_glossary()
        glossary = load_glossary(runtime.cfg.glossary.dir)
        return {
            "ok": True,
            "written": [str(p) for p in paths],
            "reloaded": isinstance(runtime.pipeline.mt, object),
            **glossary.to_rows(),
        }

    @app.post("/api/translate")
    async def api_translate(payload: dict) -> dict:
        """试译：用当前引擎（含词典）翻一句，给面板做即时验证。"""
        ja = str(payload.get("ja") or "").strip()
        if not ja:
            return {"ok": True, "zh": ""}
        try:
            zh = await run_in_threadpool(runtime.pipeline.mt.translate, ja)
        except Exception as exc:
            # 常见于用 --wav 跑完一遍之后：引擎已经 close 了
            return JSONResponse(
                {"error": f"翻訳エンジンが現在使用できません（{type(exc).__name__}）：{exc}"},
                status_code=503,
            )
        return {"ok": True, "zh": zh}

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
