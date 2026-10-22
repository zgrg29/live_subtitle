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
from .audio.capture import default_input_name, list_input_devices_detailed
from .audio.level import db_to_rms, rms_to_db
from .config import (
    CONFIG_FILENAME,
    Config,
    apply_overlay,
    asdict_audio,
    asdict_overlay,
    save_section,
)
from .metrics import Metrics
from .pipeline import Pipeline, PipelineStatus, Publisher
from .record import open_dir
from .text.glossary import load_glossary, save_glossary


def _clamp(value, low: float, high: float, default: float) -> float:
    """面板传来的数字不可信：转不动就用默认值，越界就夹住。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


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

    # ---------------- マイク設定 ----------------

    @app.get("/api/audio")
    async def api_audio() -> dict:
        """麦克风列表 + 当前增益/门限。设备枚举失败也要把配置页撑起来。"""
        try:
            devices = await run_in_threadpool(list_input_devices_detailed)
            default_name = await run_in_threadpool(default_input_name)
            error = ""
        except Exception as exc:
            devices = []
            default_name = ""
            error = f"{type(exc).__name__}: {exc}"
        cfg = runtime.cfg
        return {
            "ok": True,
            "config": asdict_audio(cfg),
            "threshold_db": round(rms_to_db(cfg.vad.threshold), 1),
            "devices": devices,
            "default_device": default_name,
            "source": runtime.status.source,
            "error": error,
        }

    @app.post("/api/audio")
    async def api_save_audio(payload: dict) -> dict:
        """保存并立即应用麦克风 / 增益 / 门限，不需要重启。"""
        cfg = runtime.cfg
        has_device = "device" in payload or "device_index" in payload
        device = str(payload.get("device") or "") if has_device else cfg.audio.device
        try:
            device_index = int(payload.get("device_index", -1)) if has_device else cfg.audio.device_index
        except (TypeError, ValueError):
            device_index = -1
        device_index = max(-1, device_index)

        if has_device and (device, device_index) != (cfg.audio.device, cfg.audio.device_index):
            try:
                await run_in_threadpool(
                    runtime.pipeline.switch_device, device, device_index
                )
            except Exception as exc:
                # 打不开就保留旧设备（switch_device 内部已经回退），配置也不落盘
                return JSONResponse({"error": str(exc)}, status_code=400)
            cfg.audio.device = device
            cfg.audio.device_index = device_index

        if "gain" in payload:
            gain = _clamp(payload.get("gain"), 0.1, 8.0, cfg.audio.gain)
            runtime.pipeline.set_gain(gain)
        if "threshold_db" in payload:
            threshold_db = _clamp(payload.get("threshold_db"), -66.0, -14.0,
                                  rms_to_db(cfg.vad.threshold))
            runtime.pipeline.set_threshold(db_to_rms(threshold_db))

        try:
            save_section(
                runtime.config_path or CONFIG_FILENAME,
                "audio",
                {
                    "device": cfg.audio.device,
                    "device_index": cfg.audio.device_index,
                    "gain": cfg.audio.gain,
                },
            )
            save_section(
                runtime.config_path or CONFIG_FILENAME,
                "vad",
                {"threshold": cfg.vad.threshold},
            )
        except Exception as exc:
            return JSONResponse(
                {"error": f"config.toml への書き込みに失敗しました：{exc}"}, status_code=500
            )

        return {
            "ok": True,
            "config": asdict_audio(cfg),
            "threshold_db": round(rms_to_db(cfg.vad.threshold), 1),
            "source": runtime.status.source,
        }

    # ---------------- 記録（改善用） ----------------

    @app.get("/api/record")
    async def api_record() -> dict:
        return {
            "ok": True,
            "state": runtime.pipeline.recording_state(),
            "config": {
                "enabled": runtime.cfg.record.enabled,
                "dir": runtime.cfg.record.dir,
                "max_minutes": runtime.cfg.record.max_minutes,
            },
        }

    @app.post("/api/record")
    async def api_set_record(payload: dict) -> dict:
        enabled = bool(payload.get("enabled"))
        try:
            state = await run_in_threadpool(runtime.pipeline.set_recording, enabled)
        except Exception as exc:
            return JSONResponse(
                {"error": f"記録を開始できませんでした：{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        try:
            save_section(
                runtime.config_path or CONFIG_FILENAME, "record", {"enabled": enabled}
            )
        except Exception as exc:
            # 設定を永続化できなかったのに録音だけ続くのは不親切なので元に戻す
            try:
                await run_in_threadpool(runtime.pipeline.set_recording, not enabled)
            except Exception:
                pass
            return JSONResponse(
                {"error": f"config.toml への書き込みに失敗しました：{exc}"}, status_code=500
            )
        return {"ok": True, "state": state}

    @app.post("/api/record/open")
    async def api_open_record() -> dict:
        state = runtime.pipeline.recording_state()
        target = Path(state.get("dir") or ".")
        try:
            await run_in_threadpool(open_dir, target)
        except Exception as exc:
            return JSONResponse({"error": f"フォルダを開けませんでした：{exc}"}, status_code=500)
        return {"ok": True, "path": str(target)}

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
            "recording": runtime.pipeline.recording_state(),
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
        # 设置页的电平条用 /ws?levels=1；字幕页用 /ws，不接收 10Hz 的电平消息。
        allow_levels = websocket.query_params.get("levels") in ("1", "true", "yes")
        queue = runtime.publisher.add_client(allow_levels=allow_levels)
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
