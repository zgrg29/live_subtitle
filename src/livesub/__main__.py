"""命令行入口：python -m livesub [...]。

子命令：
    run       正常启动（麦克风 → 识别 → 翻译 → OBS 字幕页）
    selftest  不需要麦克风和模型，用合成音频跑通整条链路并打印延迟
    devices   列出可用麦克风
    doctor    环境自检
    download  下载识别模型

注意：这里所有面向用户的文字都是日语（产品面向日本主播）。
代码注释保持中文，方便维护。
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

    parser = argparse.ArgumentParser(prog="livesub", description=f"{APP_NAME} — ローカル・リアルタイム日本語字幕")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="起動する（既定のコマンド）")
    _add_common(p_run)
    p_run.add_argument("--port", type=int, default=0, help="設定ファイルのポートを上書きする")
    p_run.add_argument("--synthetic", type=float, default=0.0, help=argparse.SUPPRESS)
    p_run.add_argument("--wav", type=str, default="", help="マイクの代わりに WAV ファイルを使う（デバッグ用）")
    p_run.add_argument("--mock", action="store_true", help="デモモードを強制する（モデルを読み込まない）")
    p_run.add_argument("--normal-priority", action="store_true", help="プロセスの優先度を下げない")

    p_self = sub.add_parser("selftest", help="ハードウェアなしの自己テスト")
    p_self.add_argument("--seconds", type=float, default=8.0)
    p_self.add_argument("--config", type=str, default=CONFIG_FILENAME)
    p_self.add_argument("--wav", type=str, default="", help="合成音声の代わりに WAV を使う")
    p_self.add_argument("--real", action="store_true", help="実際の認識モデルを使う（既定はデモエンジン）")

    sub.add_parser("devices", help="使用可能なマイクの一覧")
    sub.add_parser("doctor", help="環境チェック")
    # download 只在这里露个名字，实际解析在 main() 开头
    sub.add_parser("download", help="認識モデルをダウンロード")

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
    parser.add_argument("--config", type=str, default=CONFIG_FILENAME, help="設定ファイルのパス")


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
        print(f"\n[起動失敗] 音声入力：{exc}\n")
        return 3

    asr = build_asr(cfg, force_mock=demo)
    mt = build_mt(cfg, force_mock=demo)
    if not demo and last_asr_error():
        print(f"[ヒント] 認識モデルを読み込めなかったため、デモモードに切り替えました：{last_asr_error()}")

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
        print(f"\n[起動失敗] ポート {cfg.server.port} は使用中です。")
        print("  すでに LiveSub が起動している可能性があります。先にそちらを閉じてください。")
        print("  別のポートを使う場合： LiveSub.exe --port 9000（OBS の URL も変更してください）\n")
        if sys.stdin and sys.stdin.isatty():
            input("Enter キーで終了…")
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
    print(f"  字幕ページ　：{base}/overlay")
    print(f"  設定ページ　：{base}/admin            ← フォント・色・背景・辞書")
    print(f"  デバッグ　　：{base}/overlay?debug=1     （遅延と接続状態を表示）")
    print(f"  プレビュー　：{base}/overlay?preview=1   （配信なしでレイアウト確認）")
    print(f"  マイク　　　：{status.source}")
    print(f"  認識　　　　：{status.asr}")
    print(f"  翻訳　　　　：{status.mt}")
    if status.demo:
        print(line)
        print("  ⚠ 現在はデモモードです：表示されるのは台本のテキストで、実際の認識ではありません。")
        print("    実際の認識を使うには： pip install -r requirements-asr.txt")
        print("                          python -m livesub download --search \"zipformer ja\"")
        print("                          モデルを配置して再起動してください（README 参照）。")
    print(line)
    print("  OBS：「ブラウザ」ソースを追加 → URL に上の字幕ページを入力")
    print("       幅 1920 / 高さ 1080 →「ソースが非表示のときにシャットダウン」はオフ")
    print("       背景は最初から透明です（追加の設定は不要）")
    print(line)
    print("  Ctrl+C で停止\n")


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

    label = args.wav or f"合成音声 {args.seconds:.1f}s"
    print(f"\n自己テスト：{label} → VAD 区切り → {status.asr} → {status.mt}\n")
    pipeline.run()

    print(f"\n{'idx':<5}{'audio s':>9}{'asr ms':>9}{'mt ms':>8}{'end->line ms':>15}  text")
    print("─" * 88)
    for trace in metrics.traces:
        merge_note = f"  [統合 {trace.segments} 断片]" if trace.segments > 1 else ""
        print(
            f"{trace.utt_id:<5}{trace.audio_ms / 1000:>9.2f}{trace.asr_ms:>9.1f}"
            f"{trace.mt_ms:>8.1f}{trace.end_to_line_ms:>15.1f}  {trace.text}{merge_note}"
        )
        if trace.zh:
            print(f"{'':<5}{'':>9}{'':>9}{'':>8}{'':>15}  → {trace.zh}")
    print("─" * 88)
    summary = metrics.summary()
    print(f"合計 {summary.get('count', 0)} 文、ノイズ断片を {summary.get('dropped_segments', 0)} 個破棄")
    if summary.get("merged_segments") or summary.get("dropped_fillers"):
        print(
            f"区切りの統合：統合 {summary.get('merged_segments', 0)} 回 / "
            f"保留 {summary.get('held_segments', 0)} 断片 / "
            f"タイムアウト {summary.get('hold_timeouts', 0)} 断片 / "
            f"フィラー破棄 {summary.get('dropped_fillers', 0)} 文"
        )
    if "end_to_line_ms" in summary:
        lat = summary["end_to_line_ms"]
        print(f"発話終了→表示：p50 {lat['p50']}ms / p95 {lat['p95']}ms / max {lat['max']}ms")
        print("（OBS の描画とモニターの遅延は含みません。通常さらに 30〜80ms 加わります）")
    print(f"\nログ：{(Path('logs')).resolve()}\n")
    return 0


# ------------------------------------------------------------------------ devices


def _cmd_devices() -> int:
    from .audio.capture import list_input_devices

    try:
        devices = list_input_devices()
    except Exception as exc:
        print(f"デバイスの読み取りに失敗しました：{exc}")
        return 1
    if not devices:
        print("入力デバイスが見つかりません。")
        return 1
    print("\n使用可能なマイク（名前の一部を config.toml の audio.device に入力）：\n")
    for index, name, channels in devices:
        print(f"  [{index:>2}] {name}  ({channels}ch)")
    print()
    return 0


# ------------------------------------------------------------------------- doctor


def _cmd_doctor() -> int:
    ok = True
    print(f"\n{APP_NAME} {__version__} 環境チェック\n")

    version = sys.version_info
    good = version >= (3, 10)
    ok &= good
    print(f"  {'✓' if good else '✗'} Python {version.major}.{version.minor}.{version.micro}"
          + ("" if good else "  （3.10 以上が必要）"))

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

        print("  ✓ sherpa-onnx（音声認識エンジン導入済み）")
    except Exception:
        print("  · sherpa-onnx 未導入（現在はデモモードのみ）→ pip install -r requirements-asr.txt")

    cfg = load(CONFIG_FILENAME)
    model_dir = Path(cfg.asr.model_dir)
    if not model_dir.is_absolute():
        model_dir = data_dir() / model_dir
    if (model_dir / "tokens.txt").exists():
        print(f"  ✓ 認識モデル {model_dir}")
    else:
        print(f"  · 認識モデルなし {model_dir}  → python -m livesub download --search \"zipformer ja\"")

    overlay = bundle_dir() / "overlay" / "index.html"
    print(f"  {'✓' if overlay.exists() else '✗'} 字幕ページ {overlay}")
    ok &= overlay.exists()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((cfg.server.host, cfg.server.port))
        print(f"  ✓ ポート {cfg.server.port} は使用可能")
    except OSError:
        print(f"  ✗ ポート {cfg.server.port} は使用中（config.toml の server.port を変更）")

    try:
        from .audio.capture import list_input_devices

        count = len(list_input_devices())
        print(f"  {'✓' if count else '✗'} マイク {count} 台")
    except Exception as exc:
        print(f"  ✗ マイクの検出に失敗しました：{exc}")

    print("\n結果：" + ("環境は準備完了です。" if ok else "未準備の項目があります。上のヒントを確認してください。") + "\n")
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
