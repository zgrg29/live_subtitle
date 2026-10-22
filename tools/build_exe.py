"""把项目打包成绿色版 exe（单目录，可整个文件夹拷走）。

用法：
    python tools/build_exe.py              # 带模型一起打（推荐，约 300~500MB）
    python tools/build_exe.py --no-models  # 不带模型，首次运行自己下载

产物：
    dist/LiveSub/LiveSub.exe        双击启动
    dist/LiveSub/models/...         识别模型
    dist/LiveSub/_internal/...      运行时和依赖

为什么用单目录而不是单文件：
- 启动快（单文件每次要解压几百 MB）；
- 杀软误报少（单文件自解压行为很像木马）；
- 模型可以放外面随时替换，不用重新打包。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "LiveSub"


def _version() -> str:
    try:
        from livesub import __version__

        return __version__
    except Exception:
        return "0.5.1"


def run(cmd: list[str]) -> None:
    print("$ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"命令失败（{result.returncode}）：{cmd[0]}")


def build(with_models: bool, out_name: str = "LiveSub") -> None:
    dist = ROOT / "dist" / out_name
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", "LiveSub",
        "--console",
        # 静态资源：字幕页、术语表
        "--add-data", f"{ROOT / 'overlay'};overlay",
        "--add-data", f"{ROOT / 'glossary'};glossary",
        # 这些是延迟导入/动态导入，PyInstaller 静态分析看不到
        "--hidden-import", "sounddevice",
        "--collect-data", "sounddevice",
        "--hidden-import", "uvicorn",
        "--collect-submodules", "uvicorn",
        "--hidden-import", "websockets",
        "--hidden-import", "sherpa_onnx",
        "--collect-all", "sherpa_onnx",
        "--hidden-import", "onnxruntime",
        "--collect-all", "onnxruntime",
        "--hidden-import", "transformers",
        "--hidden-import", "tokenizers",
        "--collect-all", "tokenizers",
        "--hidden-import", "sentencepiece",
        "--collect-all", "sentencepiece",
        # torch 只在“把模型转成 ONNX”时用得到，运行时完全不碰。
        # 不排除会被 transformers/ctranslate2 的转换器模块带进来，凭空多 400MB+。
        "--exclude-module", "torch",
        "--exclude-module", "sympy",
        "--exclude-module", "ctranslate2",
        "--exclude-module", "hf_xet",
        # 先打到 build/ 里，成功后再搬到 dist/：dist/ 下可能有被占用或受保护的旧产物，
        # PyInstaller 直接往里写会在删除旧目录时失败（Windows 权限，实测踩过）。
        "--distpath", str(ROOT / "build" / "pkg"),
        "--workpath", str(ROOT / "build" / "pyi"),
        str(ROOT / "launcher.py"),
    ]
    run(cmd)

    built = ROOT / "build" / "pkg" / "LiveSub"
    if not built.exists():
        raise SystemExit(f"打包产物不存在：{built}")
    if dist.exists():
        shutil.rmtree(dist)
    shutil.move(str(built), str(dist))

    # 配置文件样例 + 说明
    example = ROOT / "config.example.toml"
    if example.exists():
        shutil.copy2(example, dist / "config.example.toml")
    # 有本地 config.toml 就一起带上，保证绿色版的行为和开发时一致；
    # 但如果它绑定了具体麦克风名，就不要带（别人电脑上没有那个设备）。
    local_config = ROOT / "config.toml"
    if local_config.exists():
        text = local_config.read_text(encoding="utf-8", errors="replace")
        if 'device = ""' in text:
            shutil.copy2(local_config, dist / "config.toml")
        else:
            print("[跳过] 本地 config.toml 绑定了具体麦克风，不复制进绿色版")
    (dist / "使い方.txt").write_text(
        f"LiveSub ポータブル版 {_version()}\n"
        "============================================================\n\n"
        "【1】起動\n"
        "  LiveSub.exe をダブルクリックすると黒いウィンドウが開きます。\n"
        "  このウィンドウを閉じると字幕が止まります。\n"
        "  起動すると設定ページがブラウザで自動的に開きます。\n"
        "  初回に「不明な発行元」と出たら「詳細情報」→「実行」を選んでください。\n\n"
        "【2】設定ページ（スタイル／音声／辞書／記録／診断）\n"
        "  URL： http://127.0.0.1:8765/admin\n"
        "  「スタイル」：フォント、文字サイズ、色、縁取り、背景、表示位置、行数、\n"
        "                無音のときに字幕を消すまでの時間（既定 5 秒）。\n"
        "  「音声」：マイクの選択（再起動不要）、入力チャンネル、入力ゲイン、\n"
        "            認識しきい値、入力レベルメーター（声が青い線を超えるように調整）。\n"
        "            2ch オーディオインターフェースでマイクが「入力2」なら、\n"
        "            入力チャンネルを 2 にしてください（レベルが動いても認識が空になる原因）。\n"
        "  「保存して適用」で OBS の字幕にすぐ反映されます（再読み込み不要）。\n\n"
        "【3】記録（改善用・既定オフ）\n"
        "  「記録」タブでオンにすると、マイク音声と認識/翻訳結果をこの PC に保存します。\n"
        "  外部には送信しません。保存先：このフォルダの recordings\\ 年月日-時刻\\\n"
        "  不要になったらフォルダごと削除してください。\n"
        "  録音には通話や来客など他の人の声も入る可能性があります。共有前に同意を確認してください。\n\n"
        "【4】辞書の追加（同じ設定ページの「辞書」タブ）\n"
        "  2 つの表があります：\n"
        "    ・定型フレーズ：文全体が一致したときだけ有効。例 こんばんは → 晚上好\n"
        "    ・用語：2 つのモード\n"
        "        pre  翻訳前に日本語の語を中国語へ置換（カタカナの固有名詞・ゲーム名・食べ物）\n"
        "        post 翻訳後に「誤訳パターン」を正しい語へ置換（原文にその語があるときだけ有効）\n"
        "  追加したら「辞書を保存して即時反映」→ 下の「試し翻訳」で確認できます。\n"
        "  辞書ファイルはこのフォルダの glossary\\ にもあります（UTF-8・Tab 区切りのテキスト）。\n\n"
        "【5】OBS への追加\n"
        "  ソース → ＋ →「ブラウザ」\n"
        "  URL： http://127.0.0.1:8765/overlay\n"
        "  幅 1920、高さ 1080、位置 0,0\n"
        "  「ソースが非表示のときにシャットダウン」はオフ\n"
        "  （オンにすると再読み込みで字幕がちらつきます）\n"
        "  背景は最初から透明です（追加設定は不要）。\n\n"
        "【6】字幕が出ないとき（「診断」タブ）\n"
        "  設定ページの「診断」を開くと、音声→声の検出→切句→認識→字幕→OBS 接続の\n"
        "  どこで止まっているかが表示されます。\n"
        "  「直近6秒で認識テスト」で文字が出れば認識は正常、出なければマイク側の問題です。\n"
        "  「サポート用ファイルを保存」で、この PC の設定・直近10秒の音声・ログを 1 つの\n"
        "  zip にまとめて保存できます（外部送信はしません。聞かれたときだけ送ってください）。\n\n"
        "【7】その他\n"
        "  デバッグ（遅延と接続状態）： http://127.0.0.1:8765/overlay?debug=1\n"
        "  配信せずレイアウト確認：     http://127.0.0.1:8765/overlay?preview=1\n"
        "  ポート変更：LiveSub.exe --port 9000（OBS の URL も変更）\n"
        "  設定ページを自動で開かない：LiveSub.exe --no-browser\n"
        "  マイクが違う/音が出ない：設定ページの「音声」タブで選択・調整\n\n"
        "モデルがない場合はデモモード（台本のテキスト）で起動します。\n"
        "モデルのダウンロード：このフォルダでターミナルを開いて\n"
        "    LiveSub.exe download --repo csukuangfj/reazonspeech-k2-v2 --to models/sherpa-ja\n",
        encoding="utf-8",
    )

    if with_models:
        src = ROOT / "models"
        if not src.exists():
            print("[跳过] 源目录没有 models/，产物将是纯演示版")
        else:
            print(f"复制模型 {src} → {dist / 'models'} …")
            shutil.copytree(src, dist / "models", dirs_exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-models", action="store_true", help="不把 models/ 复制进去")
    parser.add_argument("--out", default="LiveSub", help="dist/ 下的子目录名，默认 LiveSub")
    args = parser.parse_args()
    try:
        import PyInstaller  # noqa: F401
    except Exception:
        print("缺少 PyInstaller，请先： pip install pyinstaller")
        return 2
    build(with_models=not args.no_models, out_name=args.out)

    dist = ROOT / "dist" / args.out
    exe = dist / "LiveSub.exe"
    size = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file()) / 1048576
    print(f"\n完成：{exe}")
    print(f"整个目录 {size:.0f} MB，可以打包成 zip 发给别人")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
