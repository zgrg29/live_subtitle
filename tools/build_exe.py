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
    (dist / "使用说明.txt").write_text(
        "LiveSub 绿色版\n"
        "====================\n\n"
        "1. 双击 LiveSub.exe，会开一个黑色窗口，别关它。\n"
        "2. 浏览器打开 http://127.0.0.1:8765/overlay 看效果。\n"
        "3. OBS 里添加「浏览器」源，URL 填 http://127.0.0.1:8765/overlay，\n"
        "   宽 1920 高 1080，不要勾「不可见时关闭源」。\n"
        "4. 想改字体/颜色/麦克风：编辑同目录下的 config.toml，改完重启 exe。\n"
        "5. 第一次运行如果弹出「未知发布者」提示，点「更多信息」→「仍要运行」。\n\n"
        "没有模型时会在演示模式（预设台词）下运行。\n"
        "下载模型：在本目录打开终端执行\n"
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
