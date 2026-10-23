"""本地 HTTP/WebSocket 服务：给 OBS 浏览器源提供字幕页。

安全约定：
  - 只绑定 127.0.0.1，不监听局域网；
  - 没有对外暴露的控制接口，唯一的写入口是配置文件和命令行。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import platform
import sys
import time
import wave
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
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


def _wav_bytes(audio, sample_rate: int) -> bytes:
    """float32 单声道 → 16bit PCM WAV 字节（诊断页打包/试听用）。"""
    pcm = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(int(sample_rate))
        fh.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _diag_verdict(diag: dict) -> str:
    """按"断在哪一步"给一句人话。顺序就是信号链的顺序。"""
    counters = diag.get("counters") or {}
    frames = int(counters.get("frames", 0))
    asr_calls = int(counters.get("asr_calls", 0))
    asr_empty = int(counters.get("asr_empty", 0))
    emitted = int(counters.get("emitted", 0))
    age = diag.get("last_frame_age_s")
    clients = diag.get("clients") or {}

    if not diag.get("running"):
        return "パイプラインが止まっています（起動直後か、マイクでエラーが出ています）"
    if frames == 0 or (age is not None and age > 3):
        return "音声が届いていません（マイクが他のアプリに使われている／抜けている）"
    if float(diag.get("speech_ratio") or 0) <= 0.0:
        return ("音声は届いていますが、まだ VAD が声を検出していません"
                "（しきい値が高い／ミュート／まだ話していない、のいずれか）")
    if int(counters.get("utterances", 0)) == 0:
        return "声は拾えていますが、文の区切りができていません（短い声だけ／しきい値ぎりぎり）"
    if asr_calls > 0 and asr_empty >= asr_calls:
        return ("認識結果が空です。マイクの入力チャンネル違い（2ch の入力2 に挿している等）か、"
                "音楽・ノイズしか入っていない可能性が高いです")
    if emitted == 0:
        return "認識はできていますが、字幕として配信されていません（統合待ち／翻訳で停止）"
    if int(clients.get("subtitle", 0)) == 0:
        return "字幕は生成されていますが、OBS の字幕ページが接続していません（URL／ポート／キャッシュ）"
    return "正常に動いています"


def _diag_summary_text(diag: dict) -> str:
    """人が読む 1 枚のレポート。サポートにそのまま貼れる形にする。"""
    counters = diag.get("counters") or {}
    clients = diag.get("clients") or {}
    channels = diag.get("channels_db") or []
    channel_text = " / ".join(f"ch{i + 1} {db:+.0f} dB" for i, db in enumerate(channels)) or "-"
    selected = diag.get("active_channel")
    mode = str(diag.get("channel_mode") or "manual")
    if mode == "auto":
        if diag.get("auto_state") == "locked" and selected is not None:
            channel_label = f"ch{int(selected) + 1}（自動判定）"
        elif diag.get("auto_state") == "listening" and selected is not None:
            channel_label = f"ch{int(selected) + 1}（自動判定中）"
        else:
            channel_label = "自動"
    elif selected is None:
        channel_label = "-"
    elif int(selected) < 0:
        channel_label = "全chミックス"
    else:
        channel_label = f"ch{int(selected) + 1}"
    return "\n".join(
        [
            "LiveSub 診断レポート",
            f"version: {diag.get('version')}   稼働: {diag.get('uptime_s')} 秒",
            f"マイク: {diag.get('source')}（{diag.get('channel_count')}ch、使用 {channel_label}）",
            f"チャンネル別レベル: {channel_text}",
            f"入力: フレーム {counters.get('frames', 0)} / 最終 {diag.get('last_frame_age_s')} 秒前 / 現在 {diag.get('level_db')} dB",
            f"VAD: 発話 {float(diag.get('speech_ratio') or 0) * 100:.0f}% / しきい値 {diag.get('threshold_db')} dB",
            f"切句: {counters.get('utterances', 0)} 回",
            f"認識: {counters.get('asr_calls', 0)} 回 / 空 {counters.get('asr_empty', 0)} 回 / 最終「{counters.get('last_asr_text', '')}」",
            f"字幕: {counters.get('emitted', 0)} 行 / 最終 {diag.get('last_line_age_s')} 秒前",
            f"接続: 字幕ページ {clients.get('subtitle', 0)} / 設定ページ {clients.get('levels', 0)}",
            f"直近のエラー: {diag.get('last_error') or 'なし'}",
            "",
            f"判定: {_diag_verdict(diag)}",
            "",
        ]
    )


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
        diag = runtime.pipeline.diag()
        return {
            "ok": True,
            "config": asdict_audio(cfg),
            "threshold_db": round(rms_to_db(cfg.vad.threshold), 1),
            "devices": devices,
            "default_device": default_name,
            "source": runtime.status.source,
            "channel_count": int(diag.get("channel_count", 1)),
            "channel_mode": diag.get("channel_mode", "manual"),
            "auto_state": diag.get("auto_state", "manual"),
            "active_channel": diag.get("active_channel"),
            "detected_channel": diag.get("detected_channel"),
            "error": error,
        }

    @app.post("/api/audio")
    async def api_save_audio(payload: dict) -> dict:
        """保存并立即应用麦克风 / 通道 / 增益 / 门限，不需要重启。"""
        cfg = runtime.cfg
        has_device = "device" in payload or "device_index" in payload
        device = str(payload.get("device") or "") if has_device else cfg.audio.device
        try:
            device_index = int(payload.get("device_index", -1)) if has_device else cfg.audio.device_index
        except (TypeError, ValueError):
            device_index = -1
        device_index = max(-1, device_index)
        channel = (
            int(_clamp(payload.get("channel"), -2, 7, cfg.audio.channel))
            if "channel" in payload
            else cfg.audio.channel
        )

        if (device, device_index, channel) != (cfg.audio.device, cfg.audio.device_index, cfg.audio.channel):
            try:
                await run_in_threadpool(
                    runtime.pipeline.switch_device, device, device_index, channel
                )
            except Exception as exc:
                # 打不开就保留旧设备（switch_device 内部已经回退），配置也不落盘
                return JSONResponse({"error": str(exc)}, status_code=400)
            cfg.audio.device = device
            cfg.audio.device_index = device_index
            cfg.audio.channel = channel

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
                    "channel": cfg.audio.channel,
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

        diag = runtime.pipeline.diag()
        return {
            "ok": True,
            "config": asdict_audio(cfg),
            "threshold_db": round(rms_to_db(cfg.vad.threshold), 1),
            "source": runtime.status.source,
            "channel_count": int(diag.get("channel_count", 1)),
            "channel_mode": diag.get("channel_mode", "manual"),
            "auto_state": diag.get("auto_state", "manual"),
            "active_channel": diag.get("active_channel"),
            "detected_channel": diag.get("detected_channel"),
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

    # ---------------- 診断（0.5.1） ----------------

    @app.get("/api/diag")
    async def api_diag() -> dict:
        """一台机器上"字幕が出ない"ときの一次情報。全部ローカル、外部送信なし。"""
        cfg = runtime.cfg
        diag = runtime.pipeline.diag()
        return {
            "ok": True,
            "diag": diag,
            "metrics": runtime.metrics.summary(),
            "config": {
                "audio": asdict_audio(cfg),
                "asr": {
                    "engine": cfg.asr.engine,
                    "model_dir": cfg.asr.model_dir,
                    "num_threads": cfg.asr.num_threads,
                },
                "mt": {"engine": cfg.mt.engine, "model_dir": cfg.mt.model_dir},
                "segment": {"merge": cfg.segment.merge, "merge_wait_ms": cfg.segment.merge_wait_ms},
                "overlay": {"clear_after_ms": cfg.overlay.clear_after_ms},
                "record": {"enabled": cfg.record.enabled},
            },
            "system": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "frozen": bool(getattr(sys, "frozen", False)),
                "pid": os.getpid(),
            },
            "verdict": _diag_verdict(diag),
            "summary": _diag_summary_text(diag),
        }

    @app.post("/api/diag/recent")
    async def api_diag_recent(payload: dict | None = None) -> dict:
        """直近数秒を認識し直す。「音声が悪い」のか「表示が悪い」のかを切り分ける。

        音声は pipeline が実際に受け取ったものを使うので、OBS や WS とは無関係に
        「認識エンジンがこの音声から文字を出せるか」だけを測れる。
        """
        body = payload or {}
        seconds = _clamp(body.get("seconds", 6.0), 1.0, 20.0, 6.0)
        audio, rate = await run_in_threadpool(runtime.pipeline.recent_audio, seconds)
        if audio.size == 0:
            return JSONResponse(
                {"error": "まだ音声が届いていません。マイクに向かって話してから、もう一度試してください。"},
                status_code=409,
            )

        rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float32))))
        peak = float(np.max(np.abs(audio)))
        started = time.perf_counter()
        text = ""
        error = ""
        try:
            result = await run_in_threadpool(runtime.pipeline.transcribe_now, audio, rate)
            text = (result.text or "").strip()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        asr_ms = (time.perf_counter() - started) * 1000.0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        wav_path: Path | None = runtime.pipeline.log_path.parent / f"diag-recent-{stamp}.wav"
        try:
            wav_path.parent.mkdir(parents=True, exist_ok=True)
            wav_path.write_bytes(_wav_bytes(audio, rate))
        except OSError:
            wav_path = None

        return {
            "ok": True,
            "seconds": round(len(audio) / rate, 2),
            "level_db": round(rms_to_db(rms), 1),
            "peak_db": round(rms_to_db(peak), 1),
            "text": text,
            "chars": len(text),
            "asr_ms": round(asr_ms, 1),
            "error": error,
            "demo": bool(runtime.status.demo),
            "wav": str(wav_path) if wav_path else "",
        }

    @app.get("/api/diag/bundle")
    async def api_diag_bundle() -> Response:
        """サポート用の 1 ファイル（zip）。ユーザーはこれを送るだけでよい。"""
        diag = runtime.pipeline.diag()
        snapshot = {
            "ok": True,
            "diag": diag,
            "metrics": runtime.metrics.summary(),
            "summary": _diag_summary_text(diag),
            "system": {
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "frozen": bool(getattr(sys, "frozen", False)),
                "pid": os.getpid(),
            },
            "config": {
                "audio": asdict_audio(runtime.cfg),
                "asr": {"engine": runtime.cfg.asr.engine, "model_dir": runtime.cfg.asr.model_dir},
                "mt": {"engine": runtime.cfg.mt.engine, "model_dir": runtime.cfg.mt.model_dir},
            },
        }
        audio, rate = runtime.pipeline.recent_audio(10.0)
        log_lines: list[str] = []
        try:
            if runtime.pipeline.log_path.exists():
                log_lines = runtime.pipeline.log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[-200:]
        except OSError:
            pass

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("diag.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
            zf.writestr("summary.txt", snapshot["summary"])
            zf.writestr("session.jsonl", "\n".join(log_lines))
            if audio.size:
                zf.writestr("recent-10s.wav", _wav_bytes(audio, rate))
            config_file = Path(runtime.config_path or CONFIG_FILENAME)
            try:
                if config_file.exists():
                    zf.writestr("config.toml", config_file.read_text(encoding="utf-8-sig"))
            except OSError:
                pass

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="livesub-diag-{stamp}.zip"'},
        )

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
