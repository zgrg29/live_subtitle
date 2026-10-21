@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%~dp0src

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [実行環境がありません] .venv が見つかりません。
    echo  README の「クイックスタート」に従って依存関係を入れてください：
    echo      python -m venv --without-pip .venv
    echo      python -m pip --python .venv\Scripts\python.exe install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist "models\sherpa-ja\tokens.txt" (
    echo.
    echo  [ヒント] 認識モデルが未ダウンロードのため、デモモードで起動します（台本のテキスト）。
    echo  実際のモデルを使うには、別ウィンドウで：
    echo      .venv\Scripts\python.exe -m livesub download --repo csukuangfj/reazonspeech-k2-v2 --to models/sherpa-ja
    echo.
)

echo.
echo  LiveSub を起動しています。字幕ページ： http://127.0.0.1:8765/overlay
echo  このウィンドウを閉じると停止します。
echo.

".venv\Scripts\python.exe" -m livesub run %*
echo.
echo  LiveSub を終了しました。
pause
