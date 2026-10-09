@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: =====================================================
:: 视频 AI 重命名助手 — 启动脚本（静默版，无控制台窗口）
:: 浏览器会自动打开 http://127.0.0.1:8000/
:: =====================================================

set "APP_ROOT=%~dp0"
set "PORT=8000"

:: 等待服务就绪后打开浏览器（后台延迟 3 秒）
start "" /min cmd /c "timeout /t 3 >nul & start http://127.0.0.1:%PORT%/"

:: 找到 python
set "PY_CMD="
if exist "%APP_ROOT%python\python.exe" set "PY_CMD=%APP_ROOT%python\python.exe"
if not defined PY_CMD if exist "%APP_ROOT%venv\Scripts\python.exe" set "PY_CMD=%APP_ROOT%venv\Scripts\python.exe"
if not defined PY_CMD (where python >nul 2>&1 && set "PY_CMD=python")
if not defined PY_CMD (where py >nul 2>&1 && set "PY_CMD=py")

if not defined PY_CMD (
    echo 未找到 Python，请安装 Python 3.11+ 或放入便携版到 python\ 目录。
    pause >nul
    exit /b 1
)

:: 用 pythonw 静默运行（无控制台）；失败则回退 python
"%PY_CMD%w" "%APP_ROOT%run.py" --port %PORT% 2>nul
if !errorlevel! neq 0 (
    "%PY_CMD%" "%APP_ROOT%run.py" --port %PORT%
)
