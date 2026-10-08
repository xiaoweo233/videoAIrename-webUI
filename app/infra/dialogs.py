"""infra/dialogs.py — 调用系统资源管理器原生文件/文件夹选择器。

为什么必须在服务端弹窗：
    浏览器 <input type="file"> / showDirectoryPicker 出于沙箱安全，只能拿到
    文件名，**拿不到绝对路径**；而本工具需要把绝对路径交给服务端引擎去扫描。
    因此由服务端（用户本机）调起系统原生对话框，才能拿到真实绝对路径。

实现：
    Windows → PowerShell + System.Windows.Forms（OpenFileDialog / FolderBrowserDialog）。
    脚本以 -EncodedCommand（UTF-16LE + base64）传入，彻底规避中文路径与引号转义问题；
    初始目录通过环境变量传递，同样避免转义。

约束：
    - 同一时刻只允许一个对话框（全局锁），避免多请求抢焦点。
    - 任何失败都以结构化结果返回，不向调用方抛异常。
    - 仅依赖标准库。
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

__all__ = [
    "DialogBusyError",
    "pick_files",
    "pick_folder",
    "selftest",
    "is_supported",
    "VIDEO_EXTS",
]

# 常见视频扩展名（用于 OpenFileDialog 过滤器）
VIDEO_EXTS: List[str] = [
    "mp4", "mov", "mkv", "avi", "wmv", "flv", "webm", "m4v",
    "mpg", "mpeg", "ts", "m2ts", "mts", "3gp", "vob", "rmvb",
]

_SENTINEL = "__VAIR_BEGIN__"
_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW
_LOCK = threading.Lock()


class DialogBusyError(RuntimeError):
    """已有选择器对话框在前台，拒绝重复弹出。"""


def is_supported() -> bool:
    """当前平台是否支持原生选择器。"""
    return sys.platform == "win32"


def _powershell_exe() -> str:
    """定位 powershell / pwsh 可执行文件。"""
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    # 兜底：Windows 默认路径
    win = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return win if os.path.exists(win) else ""


def _encode_ps(script: str) -> str:
    """PowerShell -EncodedCommand 要求 UTF-16LE 的 base64。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


_PRELUDE = (
    "$ErrorActionPreference='Stop';"
    "$ProgressPreference='SilentlyContinue';"
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
    "Add-Type -AssemblyName System.Drawing | Out-Null;"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)


def _run_ps(script: str, timeout: float) -> "tuple[int, str, str]":
    """执行 PowerShell 脚本，返回 (returncode, stdout, stderr)。"""
    exe = _powershell_exe()
    if not exe:
        return 127, "", "未找到 PowerShell，无法弹出系统选择器"
    cmd = [exe, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", _encode_ps(script)]
    flags = _NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"选择器等待超时（{int(timeout)}s）"
    except OSError as exc:
        return 126, "", f"启动 PowerShell 失败：{exc}"
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out, err


def _parse_paths(stdout: str) -> "tuple[bool, List[str]]":
    """解析 PowerShell 输出。返回 (是否用户确认, 路径列表)。"""
    lines = [ln.strip() for ln in (stdout or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if _SENTINEL not in lines:
        return False, []  # 用户取消
    idx = lines.index(_SENTINEL)
    paths = [ln for ln in lines[idx + 1:] if ln]
    return True, paths


def _initial_dir_block(initial_dir: str) -> str:
    """把初始目录经环境变量交给脚本，规避引号/中文转义。"""
    if not initial_dir:
        return ""
    return (
        "$init=$env:VAIR_INIT_DIR;"
        "if($init -and (Test-Path -LiteralPath $init)){"
        "$dlg.InitialDirectory=$init};"
    )


def _acquire(what: str) -> None:
    if not _LOCK.acquire(blocking=False):
        raise DialogBusyError(f"已有{what}对话框打开，请先完成或关闭它")


def _guarded(what: str, script: str, timeout: float,
             initial_dir: str = "") -> Dict[str, Any]:
    if not is_supported():
        return {"ok": False, "cancelled": False, "paths": [],
                "error": "当前系统不支持原生选择器，请手动输入路径"}
    _acquire(what)
    try:
        env = dict(os.environ)
        if initial_dir:
            env["VAIR_INIT_DIR"] = initial_dir
        exe = _powershell_exe()
        if not exe:
            return {"ok": False, "cancelled": False, "paths": [],
                    "error": "未找到 PowerShell"}
        cmd = [exe, "-NoProfile", "-NonInteractive", "-STA",
               "-EncodedCommand", _encode_ps(_PRELUDE + script)]
        flags = _NO_WINDOW if sys.platform == "win32" else 0
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  creationflags=flags, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "cancelled": False, "paths": [],
                    "error": f"等待选择超时（{int(timeout)}s）"}
        except OSError as exc:
            return {"ok": False, "cancelled": False, "paths": [],
                    "error": f"启动选择器失败：{exc}"}
        stdout = (proc.stdout or b"").decode("utf-8", "replace")
        stderr = (proc.stderr or b"").decode("utf-8", "replace")
        confirmed, paths = _parse_paths(stdout)
        if not confirmed:
            if proc.returncode != 0 and stderr.strip():
                return {"ok": False, "cancelled": False, "paths": [],
                        "error": stderr.strip().splitlines()[-1][:300]}
            return {"ok": True, "cancelled": True, "paths": []}
        return {"ok": True, "cancelled": False, "paths": paths}
    finally:
        _LOCK.release()


def pick_files(*, multi: bool = True, initial_dir: str = "",
               timeout: float = 900.0) -> Dict[str, Any]:
    """弹出「选择视频文件」原生多选对话框。

    返回 {"ok": bool, "cancelled": bool, "paths": [绝对路径...], "error": str}
    """
    exts = ";".join(f"*.{e}" for e in VIDEO_EXTS)
    script = (
        "$dlg=New-Object System.Windows.Forms.OpenFileDialog;"
        "$dlg.Title='选择视频文件（可多选）';"
        f"$dlg.Multiselect=${'true' if multi else 'false'};"
        "$dlg.CheckFileExists=$true;"
        "$dlg.RestoreDirectory=$true;"
        "$dlg.DereferenceLinks=$true;"
        f"$dlg.Filter='视频文件|{exts}|所有文件|*.*';"
        "$dlg.FilterIndex=1;"
        + _initial_dir_block(initial_dir) +
        "if($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
        f"Write-Output '{_SENTINEL}';"
        "foreach($f in $dlg.FileNames){Write-Output $f}}"
    )
    return _guarded("文件选择", script, timeout, initial_dir)


def pick_folder(*, initial_dir: str = "", timeout: float = 900.0) -> Dict[str, Any]:
    """弹出「选择文件夹」原生对话框。"""
    script = (
        "$dlg=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$dlg.Description='选择包含视频的文件夹';"
        "$dlg.ShowNewFolderButton=$true;"
        + _initial_dir_block(initial_dir) +
        "if($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){"
        f"Write-Output '{_SENTINEL}';"
        "Write-Output $dlg.SelectedPath}"
    )
    return _guarded("文件夹选择", script, timeout, initial_dir)


def selftest() -> Dict[str, Any]:
    """不弹窗，仅验证 PowerShell + WinForms 是否可用（供诊断）。"""
    if not is_supported():
        return {"ok": False, "error": "非 Windows 平台"}
    exe = _powershell_exe()
    if not exe:
        return {"ok": False, "error": "未找到 PowerShell"}
    rc, out, err = _run_ps(_PRELUDE + "Write-Output 'VAIR_WINFORMS_OK'", timeout=60)
    return {
        "ok": rc == 0 and "VAIR_WINFORMS_OK" in out,
        "exe": exe,
        "rc": rc,
        "out": out.strip(),
        "error": err.strip()[:300],
    }
