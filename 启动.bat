@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: =====================================================
:: 视频 AI 重命名助手 — 启动脚本（保留控制台）
:: 双击即可运行；首次启动会自动补齐依赖到 libs/
:: =====================================================

set "APP_ROOT=%~dp0"
set "PORT=8000"

:: ---------- 可选：局域网访问（手机）----------
:: 需要手机访问时把下面改成 1
set "LAN=0"

:: ---------- Python 环境检测 ----------
set "PY_CMD="
if exist "%APP_ROOT%python\python.exe" (
    set "PY_CMD=%APP_ROOT%python\python.exe"
    goto :py_found
)
if exist "%APP_ROOT%venv\Scripts\python.exe" (
    set "PY_CMD=%APP_ROOT%venv\Scripts\python.exe"
    goto :py_found
)
python --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PY_CMD=python"
    goto :py_found
)
py --version >nul 2>&1
if !errorlevel! equ 0 (
    set "PY_CMD=py"
    goto :py_found
)
echo.
echo  未找到 Python！请选择以下方式之一：
echo   1. 安装 Python 3.11+ 并勾选 "Add to PATH"
echo   2. 将便携版 Python 放到本目录的 python\ 文件夹中
echo.
pause >nul
exit /b 1
:py_found

:: ---------- 启动 ----------
set "ARGS=--port %PORT%"
if "%LAN%"=="1" set "ARGS=!ARGS! --lan"

echo.
"!PY_CMD!" "%APP_ROOT%run.py" !ARGS!

echo.
echo 服务已退出。按任意键关闭窗口...
pause >nul
