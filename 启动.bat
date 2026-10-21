@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%~dp0src

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [缺少运行环境] 没找到 .venv
    echo  请先按 README 的「快速开始」安装依赖，或运行：
    echo      python -m venv --without-pip .venv
    echo      python -m pip --python .venv\Scripts\python.exe install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "models\sherpa-ja\tokens.txt" (
    echo.
    echo  [提示] 还没下载识别模型，将进入演示模式（文字是预设台词）。
    echo  要下载真实模型，另开一个窗口运行：
    echo      .venv\Scripts\python.exe -m livesub download --repo csukuangfj/reazonspeech-k2-v2 --to models/sherpa-ja
    echo.
)

echo.
echo  正在启动 LiveSub，字幕页： http://127.0.0.1:8765/overlay
echo  关掉这个窗口即停止。
echo.

".venv\Scripts\python.exe" -m livesub run %*
echo.
echo  LiveSub 已退出。
pause
