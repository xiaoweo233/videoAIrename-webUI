"""infra/dialogs.py — 调用系统资源管理器原生文件/文件夹选择器。

为什么必须在服务端弹窗：
    浏览器 <input type="file"> / showDirectoryPicker 出于沙箱安全，只能拿到
    文件名，**拿不到绝对路径**；而本工具需要把绝对路径交给服务端引擎去扫描。
    因此由服务端（用户本机）调起系统原生对话框，才能拿到真实绝对路径。

实现：
    Windows → PowerShell + System.Windows.Forms（OpenFileDialog / FolderBrowserDialog）。
    脚本以 -EncodedCommand（UTF-16LE + base64）传入，彻底规避中文路径与引号转义；
    初始目录通过环境变量传递，同样避免转义。
    为避免对话框「弹在浏览器后面」导致用户以为没反应，脚本会创建一个
    Opacity=0 的 TopMost 宿主窗体作为 owner，强制对话框前置并获得焦点。

健壮性：
    - 同一时刻只允许一个对话框；重复请求返回 DialogBusyError（HTTP 409）。
    - 记录活动状态与截止时间，超期未结束自动判定为「卡死」并回收，避免永久锁死。
    - 支持 cancel_dialog() 主动杀掉挂起的进程。
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
    "cancel_dialog",
    "dialog_state",
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

# 默认等待时长：超过即视为用户未操作 / 卡死。可由调用方覆盖。
DEFAULT_TIMEOUT = 120.0
# 超期后额外宽限，超过则回收锁
_STALE_GRACE = 20.0

_FLAG_LOCK = threading.Lock()
_ACTIVE: Dict[str, Any] = {
    "active": False, "kind": "", "started": 0.0,
    "deadline": 0.0, "proc": None, "token": None,
}


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

# TopMost 宿主窗体：保证对话框前置（避免弹在浏览器后面被误认为「没反应」）
_OWNER_BEGIN = (
    "$owner=New-Object System.Windows.Forms.Form;"
    "$owner.FormBorderStyle='None';"
    "$owner.ShowInTaskbar=$false;"
    "$owner.StartPosition='CenterScreen';"
    "$owner.Size=New-Object System.Drawing.Size(1,1);"
    "$owner.Opacity=0;"
    "$owner.TopMost=$true;"
    "$owner.Show();"
    "$owner.Activate();"
)
_OWNER_END = "$owner.Close();"


# ------------------------------------------------------------------
# 并发控制：抢占 / 回收 / 取消
# ------------------------------------------------------------------
def _kill_active_locked() -> None:
    """（需持有 _FLAG_LOCK）杀掉当前挂起进程。"""
    proc = _ACTIVE.get("proc")
    if proc is not None and getattr(proc, "poll", lambda: 0)() is None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    _ACTIVE.update({"active": False, "kind": "", "started": 0.0,
                    "deadline": 0.0, "proc": None, "token": None})


def _try_begin(kind: str, timeout: float) -> object:
    """抢占对话框使用权。冲突抛 DialogBusyError；卡死则自动回收后放行。"""
    now = time.time()
    with _FLAG_LOCK:
        if _ACTIVE["active"]:
            deadline = float(_ACTIVE.get("deadline") or 0.0)
            if deadline and now > deadline + _STALE_GRACE:
                # 之前的进程已超期很久，判定卡死并回收
                _kill_active_locked()
            else:
                remain = max(0, int(deadline - now)) if deadline else 0
                raise DialogBusyError(
                    f"已有{_ACTIVE['kind'] or '选择'}对话框正在等待操作"
                    + (f"（剩余约 {remain}s）" if remain else "")
                    + "；若看不到窗口，请点「取消等待」后重试"
                )
        token = object()
        _ACTIVE.update({
            "active": True, "kind": kind, "started": now,
            "deadline": now + timeout, "proc": None, "token": token,
        })
        return token


def _end(token: object) -> None:
    with _FLAG_LOCK:
        if _ACTIVE.get("token") is token:
            _ACTIVE.update({"active": False, "kind": "", "started": 0.0,
                            "deadline": 0.0, "proc": None, "token": None})


def cancel_dialog() -> Dict[str, Any]:
    """取消当前挂起的对话框（杀掉进程并释放占用）。"""
    with _FLAG_LOCK:
        if not _ACTIVE["active"]:
            return {"ok": True, "cancelled": False, "reason": "当前没有等待中的对话框"}
        kind = _ACTIVE["kind"]
        _kill_active_locked()
    return {"ok": True, "cancelled": True, "kind": kind}


def dialog_state() -> Dict[str, Any]:
    """查询当前对话框状态（供前端轮询 / 诊断）。"""
    with _FLAG_LOCK:
        active = bool(_ACTIVE["active"])
        started = float(_ACTIVE.get("started") or 0.0)
        deadline = float(_ACTIVE.get("deadline") or 0.0)
        return {
            "active": active,
            "kind": _ACTIVE["kind"] if active else "",
            "elapsedSec": round(time.time() - started, 1) if active else 0.0,
            "remainingSec": max(0, int(deadline - time.time())) if active and deadline else 0,
            "supported": is_supported(),
        }


# ------------------------------------------------------------------
# 执行
# ------------------------------------------------------------------
def _parse_paths(stdout: str) -> "tuple[bool, List[str]]":
    """解析 PowerShell 输出。返回 (是否用户确认, 路径列表)。"""
    lines = [ln.strip() for ln in (stdout or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if _SENTINEL not in lines:
        return False, []  # 用户取消
    idx = lines.index(_SENTINEL)
    return True, [ln for ln in lines[idx + 1:] if ln]


def _initial_dir_block() -> str:
    """初始目录经环境变量交给脚本，规避引号/中文转义。"""
    return (
        "$init=$env:VAIR_INIT_DIR;"
        "if($init -and (Test-Path -LiteralPath $init)){"
        "try{$dlg.InitialDirectory=$init}catch{}};"
    )


def _guarded(what: str, body: str, timeout: float,
             initial_dir: str = "") -> Dict[str, Any]:
    if not is_supported():
        return {"ok": False, "cancelled": False, "paths": [],
                "error": "当前系统不支持原生选择器，请手动输入路径"}

    exe = _powershell_exe()
    if not exe:
        return {"ok": False, "cancelled": False, "paths": [],
                "error": "未找到 PowerShell，无法弹出系统选择器"}

    token = _try_begin(what, timeout)  # 可能抛 DialogBusyError
    try:
        script = _PRELUDE + _OWNER_BEGIN + body + _OWNER_END
        cmd = [exe, "-NoProfile", "-NonInteractive", "-STA",
               "-EncodedCommand", _encode_ps(script)]
        env = dict(os.environ)
        if initial_dir:
            env["VAIR_INIT_DIR"] = initial_dir
        flags = _NO_WINDOW if sys.platform == "win32" else 0

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=flags, env=env,
            )
        except OSError as exc:
            return {"ok": False, "cancelled": False, "paths": [],
                    "error": f"启动选择器失败：{exc}"}

        with _FLAG_LOCK:
            if _ACTIVE.get("token") is token:
                _ACTIVE["proc"] = proc

        try:
            out_b, err_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            proc.communicate()
            return {"ok": False, "cancelled": False, "paths": [],
                    "error": f"等待选择超时（{int(timeout)}s），已自动取消"}

        rc = proc.returncode
        stdout = (out_b or b"").decode("utf-8", "replace")
        stderr = (err_b or b"").decode("utf-8", "replace")
        confirmed, paths = _parse_paths(stdout)
        if not confirmed:
            # 进程被 cancel 杀死 / 异常退出 / 用户点取消
            if rc not in (0,) and stderr.strip():
                tail = stderr.strip().splitlines()[-1][:300]
                return {"ok": False, "cancelled": False, "paths": [], "error": tail}
            return {"ok": True, "cancelled": True, "paths": []}
        return {"ok": True, "cancelled": False, "paths": paths}
    finally:
        _end(token)


def pick_files(*, multi: bool = True, initial_dir: str = "",
               timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """弹出「选择视频文件」原生多选对话框。

    返回 {"ok": bool, "cancelled": bool, "paths": [绝对路径...], "error": str}
    """
    exts = ";".join(f"*.{e}" for e in VIDEO_EXTS)
    body = (
        "$dlg=New-Object System.Windows.Forms.OpenFileDialog;"
        "$dlg.Title='选择视频文件（可多选）';"
        f"$dlg.Multiselect=${'true' if multi else 'false'};"
        "$dlg.CheckFileExists=$true;"
        "$dlg.RestoreDirectory=$true;"
        "$dlg.DereferenceLinks=$true;"
        f"$dlg.Filter='视频文件|{exts}|所有文件|*.*';"
        "$dlg.FilterIndex=1;"
        + _initial_dir_block() +
        "$res=$dlg.ShowDialog($owner);"
        "if($res -eq [System.Windows.Forms.DialogResult]::OK){"
        f"Write-Output '{_SENTINEL}';"
        "foreach($f in $dlg.FileNames){Write-Output $f}}"
    )
    return _guarded("文件选择", body, timeout, initial_dir)


def pick_folder(*, initial_dir: str = "", timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """弹出「选择文件夹」原生对话框。"""
    body = (
        "$dlg=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$dlg.Description='选择包含视频的文件夹';"
        "$dlg.ShowNewFolderButton=$true;"
        + _initial_dir_block() +
        "$res=$dlg.ShowDialog($owner);"
        "if($res -eq [System.Windows.Forms.DialogResult]::OK){"
        f"Write-Output '{_SENTINEL}';"
        "Write-Output $dlg.SelectedPath}"
    )
    return _guarded("文件夹选择", body, timeout, initial_dir)


def selftest() -> Dict[str, Any]:
    """不弹窗，仅验证 PowerShell + WinForms 是否可用（供诊断）。"""
    if not is_supported():
        return {"ok": False, "error": "非 Windows 平台"}
    exe = _powershell_exe()
    if not exe:
        return {"ok": False, "error": "未找到 PowerShell"}
    script = _PRELUDE + "Write-Output 'VAIR_WINFORMS_OK'"
    cmd = [exe, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", _encode_ps(script)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60,
                              creationflags=_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exe": exe, "error": str(exc)}
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return {"ok": proc.returncode == 0 and "VAIR_WINFORMS_OK" in out,
            "exe": exe, "rc": proc.returncode, "out": out.strip(), "error": err.strip()[:300]}
