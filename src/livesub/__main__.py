"""命令行入口：python -m livesub [...]。

子命令：
    run       正常启动（麦克风 → 识别 → 翻译 → OBS 字幕页）
    selftest  不需要麦克风和模型，用合成音频跑通整条链路并打印延迟
    devices   列出可用麦克风
    doctor    环境自检
    download  下载识别模型
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .config import CONFIG_FILENAME, Config, bundle_dir, data_dir, load
from .audio.capture import WavSource
from .factory import build_asr, build_mt, build_source, last_asr_error, project_root
from .metrics import Metrics
from .pipeline import Pipeline, PipelineStatus, Publisher
from .server import Runtime, create_app


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    argv_list = list(sys.argv[1:] if argv is None else argv)

    # download 的参数（--repo/--to/--search）不做成 argparse 子命令：
    # nargs=REMAINDER 遇到 --repo 这种未知选项会直接报错，交给 models 自己解析更省事。
    if argv_list and argv_list[0] == "download":
        from . import models

        return models.main(argv_list[1:])

    parser = argparse.ArgumentParser(prog="livesub", description=f"{APP_NAME} — 本地实时日语字幕")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="启动服务（默认命令）")
    _add_common(p_run)
    p_run.add_argument("--port", type=int, default=0, help="覆盖配置里的端口")
    p_run.add_argument("--synthetic", type=float, default=0.0, help=argparse.SUPPRESS)
    p_run.add_argument("--wav", type=str, default="", help="用 WAV 文件代替麦克风（调试用）")
    p_run.add_argument("--mock", action="store_true", help="强制演示模式（不加载模型）")
    p_run.add_argument("--normal-priority", action="store_true", help="不降低进程优先级")

    p_self = sub.add_parser("selftest", help="无硬件自测")
    p_self.add_argument("--seconds", type=float, default=8.0)
    p_self.add_argument("--config", type=str, default=CONFIG_FILENAME)
    p_self.add_argument("--wav", type=str, default="", help="用 WAV 代替合成音频")
    p_self.add_argument("--real", action="store_true", help="用真实识别模型（默认演示引擎）")

    sub.add_parser("devices", help="列出可用麦克风")
    sub.add_parser("doctor", help="环境自检")
    # download 只在这里露个名字，实际解析在 main() 开头
    sub.add_parser("download", help="下载识别模型")

    # 不带子命令时默认按 run 处理：livesub --port 9000 等价于 livesub run --port 9000。
    # 必须在解析前补 "run"：否则 argparse 会把 "--mock" 后面那个裸值当成子命令。
    subcommands = {"run", "selftest", "devices", "doctor", "download"}
    tokens = argv_list
    if not tokens or tokens[0] not in subcommands | {"-h", "--help", "--version"}:
        tokens = ["run", *tokens]
    args = parser.parse_args(tokens)
    command = args.command

    if command == "run":
        return _cmd_run(args)
    if command == "selftest":
        return _cmd_selftest(args)
    if command == "devices":
        return _cmd_devices()
    if command == "doctor":
        return _cmd_doctor()
    if command == "download":
        from . import models

        return models.main(getattr(args, "rest", []) or [])
    parser.print_help()
    return 2


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=str, default=CONFIG_FILENAME, help="配置文件路径")


# --------------------------------------------------------------------------- run


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    if args.port:
        cfg.server.port = int(args.port)
    if not args.normal_priority:
        _set_below_normal_priority()

    demo = bool(args.mock or args.synthetic)
    try:
        source = build_source(cfg, wav=args.wav or None,
                              synthetic_seconds=args.synthetic or None)
    except Exception as exc:
        print(f"\n[启动失败] 音频输入：{exc}\n")
        return 3

    asr = build_asr(cfg, force_mock=demo)
    mt = build_mt(cfg, force_mock=demo)
    if not demo and last_asr_error():
        print(f"[提示] 识别模型未加载，已回退到演示模式：{last_asr_error()}")

    publisher = Publisher()
    metrics = Metrics()
    status = PipelineStatus()
    pipeline = Pipeline(cfg, source, asr, mt, publisher, metrics, status)

    app = create_app(
        Runtime(
            cfg=cfg,
            pipeline=pipeline,
            publisher=publisher,
            metrics=metrics,
            status=status,
            overlay_dir=bundle_dir() / "overlay",
        )
    )

    _print_banner(cfg, status)
    import uvicorn

    if not _port_available(cfg.server.host, cfg.server.port):
        print(f"\n[启动失败] 端口 {cfg.server.port} 已被占用。")
        print("  多半是已经有一个 LiveSub 在运行，先关掉它的窗口再试；")
        print(f"  或者换端口：LiveSub.exe --port 9000（OBS 的 URL 也要跟着改）\n")
        if sys.stdin and sys.stdin.isatty():
            input("按回车退出…")
        return 4

    try:
        uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


def _print_banner(cfg: Config, status: PipelineStatus) -> None:
    base = f"http://{cfg.server.host}:{cfg.server.port}"
    line = "─" * 58
    print(f"\n{line}")
    print(f"  {APP_NAME} {__version__}")
    print(line)
    print(f"  字幕页   {base}/overlay")
    print(f"  调试页   {base}/overlay?debug=1     （显示延迟和连接状态）")
    print(f"  预排版   {base}/overlay?preview=1   （不推流也能调字体）")
    print(f"  麦克风   {status.source}")
    print(f"  识别     {status.asr}")
    print(f"  翻译     {status.mt}")
    if status.demo:
        print(line)
        print("  ⚠ 当前是演示模式：文字是预设台词，不是真识别。")
        print("    要接入真实识别： pip install -r requirements-asr.txt")
        print("                    python -m livesub download --search \"zipformer ja\"")
        print("                    按 README 的步骤放好模型后重启。")
    print(line)
    print("  OBS：添加「浏览器」源 → 勾选「本地文件」关掉 → URL 填上面的字幕页")
    print("       宽 1920 高 1080 → 勾选「关闭源时关闭」不要开 → 背景透明无需设置")
    print(line)
    print("  按 Ctrl+C 停止\n")


def _port_available(host: str, port: int) -> bool:
    """启动前探一下端口，给用户一句人话，而不是黑窗一闪而过。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _set_below_normal_priority() -> None:
    """把整个进程降到"低于正常"优先级，避免和 OBS/游戏抢 CPU。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


# ----------------------------------------------------------------------- selftest


def _cmd_selftest(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    if args.wav:
        source = WavSource(args.wav, sample_rate=cfg.audio.sample_rate,
                           block_ms=cfg.audio.block_ms, realtime=False)
    else:
        source = build_source(cfg, synthetic_seconds=args.seconds)
    demo = not args.real
    asr = build_asr(cfg, force_mock=demo)
    mt = build_mt(cfg, force_mock=demo)
    publisher = Publisher()
    metrics = Metrics()
    status = PipelineStatus()
    pipeline = Pipeline(cfg, source, asr, mt, publisher, metrics, status, log_dir="logs")

    label = args.wav or f"合成音频 {args.seconds:.1f}s"
    print(f"\n自测：{label} → VAD 切句 → {status.asr} → {status.mt}\n")
    pipeline.run()

    print(f"\n{'idx':<5}{'audio s':>9}{'asr ms':>9}{'mt ms':>8}{'end->line ms':>15}  text")
    print("─" * 88)
    for trace in metrics.traces:
        merge_note = f"  [合并 {trace.segments} 段]" if trace.segments > 1 else ""
        print(
            f"{trace.utt_id:<5}{trace.audio_ms / 1000:>9.2f}{trace.asr_ms:>9.1f}"
            f"{trace.mt_ms:>8.1f}{trace.end_to_line_ms:>15.1f}  {trace.text}{merge_note}"
        )
        if trace.zh:
            print(f"{'':<5}{'':>9}{'':>9}{'':>8}{'':>15}  → {trace.zh}")
    print("─" * 88)
    summary = metrics.summary()
    print(f"共 {summary.get('count', 0)} 句，丢弃噪声片段 {summary.get('dropped_segments', 0)} 个")
    if summary.get("merged_segments") or summary.get("dropped_fillers"):
        print(
            f"断句合并：合并 {summary.get('merged_segments', 0)} 次 / "
            f"暂缓 {summary.get('held_segments', 0)} 段 / "
            f"等超时 {summary.get('hold_timeouts', 0)} 段 / "
            f"丢弃语气词 {summary.get('dropped_fillers', 0)} 句"
        )
    if "end_to_line_ms" in summary:
        lat = summary["end_to_line_ms"]
        print(f"说完→上屏：p50 {lat['p50']}ms / p95 {lat['p95']}ms / max {lat['max']}ms")
        print("（这里不含 OBS 渲染与显示器延迟，通常再加 30~80ms）")
    print(f"\n日志：{(Path('logs')).resolve()}\n")
    return 0


# ------------------------------------------------------------------------ devices


def _cmd_devices() -> int:
    from .audio.capture import list_input_devices

    try:
        devices = list_input_devices()
    except Exception as exc:
        print(f"读取设备失败：{exc}")
        return 1
    if not devices:
        print("没有找到任何输入设备。")
        return 1
    print("\n可用的麦克风（把名字的一部分填到 config.toml 的 audio.device）：\n")
    for index, name, channels in devices:
        print(f"  [{index:>2}] {name}  ({channels}ch)")
    print()
    return 0


# ------------------------------------------------------------------------- doctor


def _cmd_doctor() -> int:
    ok = True
    print(f"\n{APP_NAME} {__version__} 环境自检\n")

    version = sys.version_info
    good = version >= (3, 10)
    ok &= good
    print(f"  {'✓' if good else '✗'} Python {version.major}.{version.minor}.{version.micro}"
          + ("" if good else "  （需要 3.10+）"))

    for module, hint in (
        ("numpy", "pip install -r requirements.txt"),
        ("sounddevice", "pip install -r requirements.txt"),
        ("fastapi", "pip install -r requirements.txt"),
        ("uvicorn", "pip install -r requirements.txt"),
    ):
        try:
            __import__(module)
            print(f"  ✓ {module}")
        except Exception:
            ok = False
            print(f"  ✗ {module}  → {hint}")

    try:
        import sherpa_onnx  # noqa: F401

        print("  ✓ sherpa-onnx（真实识别引擎已安装）")
    except Exception:
        print("  · sherpa-onnx 未安装（当前只能演示模式）→ pip install -r requirements-asr.txt")

    cfg = load(CONFIG_FILENAME)
    model_dir = Path(cfg.asr.model_dir)
    if not model_dir.is_absolute():
        model_dir = data_dir() / model_dir
    if (model_dir / "tokens.txt").exists():
        print(f"  ✓ 识别模型 {model_dir}")
    else:
        print(f"  · 识别模型缺失 {model_dir}  → python -m livesub download --search \"zipformer ja\"")

    overlay = bundle_dir() / "overlay" / "index.html"
    print(f"  {'✓' if overlay.exists() else '✗'} 字幕页 {overlay}")
    ok &= overlay.exists()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((cfg.server.host, cfg.server.port))
        print(f"  ✓ 端口 {cfg.server.port} 可用")
    except OSError:
        print(f"  ✗ 端口 {cfg.server.port} 被占用（改 config.toml 的 server.port）")

    try:
        from .audio.capture import list_input_devices

        count = len(list_input_devices())
        print(f"  {'✓' if count else '✗'} 麦克风 {count} 个")
    except Exception as exc:
        print(f"  ✗ 麦克风检测失败：{exc}")

    print("\n结论：" + ("环境就绪。" if ok else "有项目未就绪，见上面提示。") + "\n")
    return 0 if ok else 1


def _utf8_console() -> None:
    """Windows 控制台默认 GBK，中文和 ✓ 会乱码；能改就改成 UTF-8。"""
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
