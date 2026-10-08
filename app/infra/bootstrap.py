"""bootstrap.py — 环境重定向 + 依赖注入（必须早于任何第三方 import）。

方案红线：
  G1  env 重定向必须发生在导入任何第三方库之前，否则 HF / CTranslate2
      的缓存会写进用户目录（~/.cache/huggingface、%LOCALAPPDATA%）。
  G5  依赖装到 libs/，不写系统 site-packages。

调用顺序（run.py 中体现）：
    from app.infra import bootstrap
    bootstrap.early_init()      # 第一步：重定向 env + 注入 libs 到 sys.path
    # ... 之后才允许 import fastapi / openai / faster_whisper 等
    bootstrap.ensure_runtime()  # 第二步：探测/补齐运行期依赖

本模块只依赖标准库。
"""
from __future__ import annotations

import os
import sys
from typing import List

from . import paths

__all__ = [
    "early_init", "ensure_runtime", "probe_runtime", "download_hf_model",
    "RuntimeReport", "cuda_libs_status", "ensure_cuda_libs", "apply_mirrors",
    "mirror_config", "PIP_PRESETS", "HF_PRESETS", "install_whisper",
]

# ------------------------------------------------------------------
# 国内镜像（可自定义）
#   依赖下载走 pip -i；模型下载走 HF_ENDPOINT（huggingface_hub 官方支持）
# ------------------------------------------------------------------
PIP_PRESETS = {
    "official": "https://pypi.org/simple",
    "tsinghua": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "ustc": "https://pypi.mirrors.ustc.edu.cn/simple",
    "tencent": "https://mirrors.cloud.tencent.com/pypi/simple",
}
HF_PRESETS = {
    "official": "https://huggingface.co",
    "hf-mirror": "https://hf-mirror.com",
    "modelscope": "https://www.modelscope.cn",
}

_PIP_INDEX: str = ""
_HF_ENDPOINT: str = ""


def apply_mirrors(pip_index: "str | None" = None, hf_endpoint: "str | None" = None) -> dict:
    """设置依赖/模型下载镜像。空字符串表示用官方源。

    - pip_index → 影响 _pip_install（-i 参数）
    - hf_endpoint → 设置 HF_ENDPOINT，影响 huggingface_hub 模型下载
    """
    global _PIP_INDEX, _HF_ENDPOINT
    _PIP_INDEX = (pip_index or "").strip()
    _HF_ENDPOINT = (hf_endpoint or "").strip()

    if _HF_ENDPOINT:
        os.environ["HF_ENDPOINT"] = _HF_ENDPOINT
    else:
        os.environ.pop("HF_ENDPOINT", None)

    if _PIP_INDEX:
        os.environ["PIP_INDEX_URL"] = _PIP_INDEX
        # 部分镜像走 http 或自签证书时需要放行主机
        try:
            from urllib.parse import urlparse

            host = urlparse(_PIP_INDEX).hostname
            if host:
                os.environ["PIP_TRUSTED_HOST"] = host
        except Exception:  # noqa: BLE001
            pass
    else:
        os.environ.pop("PIP_INDEX_URL", None)
        os.environ.pop("PIP_TRUSTED_HOST", None)
    return mirror_config()


def mirror_config() -> dict:
    """当前生效的镜像配置。"""
    return {"pip_index": _PIP_INDEX, "hf_endpoint": _HF_ENDPOINT,
            "pip_presets": PIP_PRESETS, "hf_presets": HF_PRESETS}

_INITIALIZED = False


# ------------------------------------------------------------------
# 第一步：环境重定向
# ------------------------------------------------------------------
def early_init() -> None:
    """重定向所有缓存/临时目录到 APP_ROOT，并把 libs/ 注入 sys.path。

    幂等：重复调用只生效一次。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    paths.ensure_dirs()

    # --- 临时目录：TMP / TEMP / TMPDIR ---
    tmp = str(paths.TMP_DIR)
    os.environ["TMP"] = tmp
    os.environ["TEMP"] = tmp
    os.environ["TMPDIR"] = tmp

    # --- HuggingFace / 模型缓存 ---
    models = str(paths.MODELS_DIR)
    hf_home = os.path.join(models, "huggingface")
    os.environ["HF_HOME"] = hf_home
    os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(hf_home, "hub")
    os.environ["TRANSFORMERS_CACHE"] = os.path.join(hf_home, "transformers")
    # faster-whisper 走 CTranslate2，模型缓存在 HF_HOME 下
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    # --- XDG（Linux 兼容，Windows 上无害）---
    os.environ["XDG_CACHE_HOME"] = os.path.join(str(paths.CACHE_DIR), "xdg")
    os.environ["XDG_DATA_HOME"] = os.path.join(str(paths.DATA_DIR), "xdg")

    # --- 强制子进程 UTF-8，规避中文路径 GBK 乱码 ---
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

    # --- libs/ 注入 sys.path（零污染：不装进系统 site-packages）---
    libs = str(paths.LIBS_DIR)
    if paths.LIBS_DIR.is_dir() and libs not in sys.path:
        sys.path.insert(0, libs)

    # --- 让本机 CUDA 依赖库（pip 装的 nvidia-* 包）可被 ctranslate2 找到 ---
    _inject_nvidia_dll_dirs()

    # --- Windows：控制台 UTF-8 ---
    if sys.platform == "win32":
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            try:
                if stream is not None and hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _inject_nvidia_dll_dirs() -> None:
    """把 libs/nvidia/*/bin 加入 DLL 搜索路径。

    faster-whisper(CTranslate2) 在 GPU 推理时需要 cublas64_12.dll / cudnn*.dll，
    这些由 pip 包 nvidia-cublas-cu12 / nvidia-cudnn-cu12 提供。
    """
    if sys.platform != "win32" or not paths.LIBS_DIR.is_dir():
        return
    nvidia_root = paths.LIBS_DIR / "nvidia"
    if not nvidia_root.is_dir():
        return
    extra: List[str] = []
    try:
        for sub in nvidia_root.iterdir():
            for candidate in (sub / "bin", sub / "lib"):
                if candidate.is_dir():
                    extra.append(str(candidate))
    except OSError:
        return
    if not extra:
        return

    # Python 3.8+ 推荐方式
    try:
        for d in extra:
            os.add_dll_directory(d)
    except (AttributeError, OSError):
        pass

    # PATH 兜底（部分库用 LoadLibrary 直接搜索 PATH）
    os.environ["PATH"] = os.pathsep.join(extra) + os.pathsep + os.environ.get("PATH", "")


# ------------------------------------------------------------------
# 第二步：运行期依赖探测
# ------------------------------------------------------------------
class RuntimeReport:
    """运行期依赖探测结果。"""

    def __init__(self) -> None:
        self.python: str = sys.version.split()[0]
        self.executable: str = sys.executable
        self.ffmpeg: str = ""
        self.ffprobe: str = ""
        self.exiftool: str = ""
        self.openai: bool = False
        self.faster_whisper: bool = False
        self.fastapi: bool = False
        self.uvicorn: bool = False
        self.sqlalchemy: bool = False
        self.cuda_available: bool = False
        self.cuda_reason: str = ""
        self.autofixed: List[str] = []      # 自动补齐成功的包
        self.missing: List[str] = []        # 仍缺失的必需依赖

    def as_dict(self) -> dict:
        return {
            "python": self.python,
            "executable": self.executable,
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
            "exiftool": self.exiftool,
            "openai": self.openai,
            "faster_whisper": self.faster_whisper,
            "fastapi": self.fastapi,
            "uvicorn": self.uvicorn,
            "sqlalchemy": self.sqlalchemy,
            "cuda_available": self.cuda_available,
            "cuda_reason": self.cuda_reason,
            "autofixed": list(self.autofixed),
            "missing": list(self.missing),
        }


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _find_tool(name: str) -> str:
    """在 ffmpeg/ → bin/ → PATH → winget → 常见安装目录中查找可执行文件。

    exiftool 特例：优先返回 exiftool_files/exiftool.pl（配合 perl.exe 调用），
    因为分发包里常见的 `exiftool(-k).exe` 名字带 -k，执行后会「-- press ENTER --」
    等待输入，在 subprocess 中表现为永久挂起（本项目实测）。
    """
    import shutil

    ext = ".exe" if sys.platform == "win32" else ""
    candidates = []

    names = [name + ext]
    if name == "exiftool":
        names.append("exiftool(-k).exe")

    # exiftool 优先：先找 exiftool.pl（可配合 perl.exe 干净调用）
    if name == "exiftool":
        for base in (paths.FFMPEG_DIR, paths.BIN_DIR):
            script = base / "exiftool_files" / "exiftool.pl"
            if script.exists():
                return str(script)
            script = base / "exiftool.pl"
            if script.exists():
                return str(script)

    for base in (paths.FFMPEG_DIR, paths.BIN_DIR):
        for n in names:
            candidates.append(base / n)

    for p in candidates:
        if p.exists():
            return str(p)

    for n in names:
        found = shutil.which(n)
        if found:
            return found
        # 去掉扩展名再试一次
        found = shutil.which(n.replace(".exe", ""))
        if found:
            return found

    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            winget_links = os.path.join(local_appdata, "Microsoft", "WinGet", "Links")
            for n in names:
                p = os.path.join(winget_links, n)
                if os.path.exists(p):
                    return p
        for base in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
                     r"C:\Program Files (x86)\ffmpeg\bin"):
            for n in names:
                p = os.path.join(base, n)
                if os.path.exists(p):
                    return p
    else:
        for base in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"):
            for n in names:
                p = os.path.join(base, n)
                if os.path.exists(p):
                    return p
    return ""


def _probe_cuda(report: RuntimeReport) -> None:
    """冒烟式探测 GPU 是否可用（结果缓存到 cache/whisper_device.json）。"""
    import json

    cache_file = paths.CACHE_DIR / "whisper_device.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            report.cuda_available = bool(data.get("cuda_available"))
            report.cuda_reason = str(data.get("reason", ""))
            return
        except (OSError, ValueError):
            pass

    available = False
    reason = "未检测（跳过）"
    if _importable("ctranslate2"):
        try:
            import ctranslate2  # type: ignore

            n = ctranslate2.get_cuda_device_count()
            available = n > 0
            reason = f"ctranslate2 报告 {n} 个 CUDA 设备"
        except Exception as exc:  # noqa: BLE001 - 探测失败不致命
            reason = f"GPU 探测失败：{exc}"
    else:
        reason = "未安装 ctranslate2（faster-whisper 未就绪）"

    report.cuda_available = available
    report.cuda_reason = reason
    try:
        paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"cuda_available": available, "reason": reason}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def probe_runtime() -> RuntimeReport:
    """探测运行期依赖，不做任何安装/修改。"""
    report = RuntimeReport()
    report.ffmpeg = _find_tool("ffmpeg")
    report.ffprobe = _find_tool("ffprobe")
    report.exiftool = _find_tool("exiftool")
    report.openai = _importable("openai")
    report.faster_whisper = _importable("faster_whisper")
    report.fastapi = _importable("fastapi")
    report.uvicorn = _importable("uvicorn")
    report.sqlalchemy = _importable("sqlalchemy")
    _probe_cuda(report)

    if not report.ffmpeg or not report.ffprobe:
        report.missing.append("ffmpeg/ffprobe")
    for pkg, ok in (
        ("openai", report.openai),
        ("fastapi", report.fastapi),
        ("uvicorn", report.uvicorn),
        ("sqlalchemy", report.sqlalchemy),
    ):
        if not ok:
            report.missing.append(pkg)
    return report


def _pip_install(packages: List[str], on_log=None, index_url: str = "") -> bool:
    """把依赖安装到 libs/（--target），不污染系统环境。

    index_url 为空时回退到 apply_mirrors() 设置的全局 pip 源，再为空则用官方源。
    """
    import subprocess

    if not packages:
        return True
    index = (index_url or _PIP_INDEX or "").strip()
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(paths.LIBS_DIR),
        "--disable-pip-version-check", "--no-warn-script-location",
        "--upgrade",
    ]
    if index:
        cmd += ["-i", index]
        try:
            from urllib.parse import urlparse

            host = urlparse(index).hostname
            if host and urlparse(index).scheme == "http":
                cmd += ["--trusted-host", host]
        except Exception:  # noqa: BLE001
            pass
    cmd += list(packages)
    if on_log:
        src = f"（源：{index}）" if index else "（官方源）"
        on_log(f"正在安装依赖到 libs/：{' '.join(packages)} {src}")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if on_log:
            on_log(f"依赖安装失败：{exc}")
        return False
    if proc.returncode != 0:
        if on_log:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            on_log("依赖安装失败：" + " | ".join(tail))
        return False
    # 安装后刷新 sys.path 让新包立即可用
    libs = str(paths.LIBS_DIR)
    if libs not in sys.path:
        sys.path.insert(0, libs)
    import importlib

    importlib.invalidate_caches()
    return True


# 必需依赖（缺失则服务无法启动）
_REQUIRED = {
    "openai": "openai>=1.0",
    "fastapi": "fastapi>=0.110",
    "uvicorn": "uvicorn>=0.27",
    "sqlalchemy": "sqlalchemy>=2.0",
}
# 按需依赖（缺失时优雅降级，见红线 G6）
_OPTIONAL = {
    "faster_whisper": "faster-whisper>=1.0",
}
# Whisper 的 CUDA 运行库
_CUDA_PKGS = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def cuda_libs_status() -> dict:
    """检查 faster-whisper 在 Windows 上 GPU 推理所需的 CUDA 运行库。

    关键依赖是 cublas64_12.dll（还有 cudnn 系列）。缺失时 CTranslate2 不会报错，
    而是「挂起」，所以必须能独立判定。

    返回 {"present": bool, "cublas": path|"", "cudnn": [paths], "checked": [...]}
    """
    report = {"present": False, "cublas": "", "cudnn": [], "checked": []}
    if sys.platform != "win32":
        report["present"] = True  # 非 Windows 不做此检查
        return report

    nvidia_root = paths.LIBS_DIR / "nvidia"
    cublas = ""
    cudnn: List[str] = []
    try:
        if nvidia_root.is_dir():
            for sub in nvidia_root.iterdir():
                for base in (sub / "bin", sub / "lib"):
                    if not base.is_dir():
                        continue
                    for dll in base.glob("*.dll"):
                        name = dll.name.lower()
                        report["checked"].append(str(dll))
                        if name.startswith("cublas64_12") or name.startswith("cublaslt64_12"):
                            cublas = cublas or str(dll)
                        elif name.startswith("cudnn"):
                            cudnn.append(str(dll))
    except OSError:
        pass
    report["cublas"] = cublas
    report["cudnn"] = cudnn

    if cublas:
        # 尝试真正加载一次，确保位数/依赖链没问题
        try:
            import ctypes

            ctypes.WinDLL(cublas)
            report["present"] = True
        except OSError:
            # 文件在磁盘上但加载失败：仍视为「已安装」（可能是缺下级依赖），
            # 交由 CTranslate2 决定，避免误判为「未安装」而反复下载。
            report["present"] = True
        return report

    # 磁盘没有时，再看系统 PATH 里是否本来就有
    try:
        import ctypes

        ctypes.WinDLL("cublas64_12.dll")
        report["present"] = True
        report["cublas"] = "PATH"
    except OSError:
        report["present"] = False
    return report


def ensure_cuda_libs(on_log=None) -> dict:
    """确保 CUDA 运行库就位；缺失则 pip 安装到 libs/ 并刷新 DLL 搜索路径。"""
    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    status = cuda_libs_status()
    if status["present"]:
        log("CUDA 运行库已就位，无需补装")
        return {"ok": True, "installed": False, "status": status}

    if sys.platform != "win32":
        return {"ok": False, "installed": False, "status": status,
                "error": "非 Windows 平台，跳过 CUDA 补装"}

    log("正在补装 CUDA 运行库（nvidia-cublas-cu12 / nvidia-cudnn-cu12）到 libs/ ...")
    ok = _pip_install(_CUDA_PKGS, on_log=on_log)
    if not ok:
        return {"ok": False, "installed": False, "status": cuda_libs_status(),
                "error": "CUDA 运行库安装失败（请检查网络/代理后重试）"}

    _inject_nvidia_dll_dirs()
    status = cuda_libs_status()
    log("CUDA 运行库补装完成" if status["present"] else "CUDA 运行库已下载，但校验仍未通过")
    return {"ok": status["present"], "installed": True, "status": status}


def ensure_runtime(auto_install_cuda: bool = True, on_log=None) -> RuntimeReport:
    """探测并补齐运行期依赖。

    必需依赖缺失 → 自动 pip 安装到 libs/。
    Whisper 依赖按用户选择「强制安装并启用」，因此默认也补齐。
    CUDA 运行库仅在 auto_install_cuda 且系统确实装了 nvidia cublas 缺失时补齐。
    """
    report = probe_runtime()

    def log(msg: str) -> None:
        report.autofixed.append(msg)
        if on_log:
            on_log(msg)

    # 1) 必需依赖
    missing_required = [spec for mod, spec in _REQUIRED.items() if not _importable(mod)]
    if missing_required:
        if _pip_install(missing_required, on_log=log):
            log(f"已补齐必需依赖：{', '.join(missing_required)}")

    # 2) Whisper（用户要求强制启用）
    if not _importable("faster_whisper"):
        if _pip_install([_OPTIONAL["faster_whisper"], "tqdm"], on_log=log):
            log("已补齐 Whisper 依赖：faster-whisper")

    # 3) CUDA 运行库：机器有 GPU 但缺 cublas/cudnn 时自动补齐
    #    注意：原判据「has_cuda 为假才补」会漏掉最典型的故障场景——
    #    GPU 存在（device_count>0）但 cublas64_12.dll 缺失 → 推理挂起。
    #    这里改为独立检查 DLL 是否就位。
    if auto_install_cuda and sys.platform == "win32" and _importable("ctranslate2"):
        status = cuda_libs_status()
        if not status["present"]:
            gpu_present = False
            try:
                import ctranslate2  # type: ignore

                gpu_present = ctranslate2.get_cuda_device_count() > 0
            except Exception:  # noqa: BLE001
                gpu_present = False
            if gpu_present:
                res = ensure_cuda_libs(on_log=log)
                if res.get("installed"):
                    log("已补齐 CUDA 运行库：nvidia-cublas-cu12 / nvidia-cudnn-cu12")

    return probe_runtime()


def install_whisper(on_log=None) -> dict:
    """安装 faster-whisper 依赖到 libs/（走 apply_mirrors 设置的 pip 源）。"""
    if _importable("faster_whisper"):
        return {"ok": True, "installed": False, "already": True}
    ok = _pip_install(["faster-whisper>=1.0", "tqdm"], on_log=on_log)
    return {"ok": bool(ok and _importable("faster_whisper")), "installed": bool(ok),
            "already": False,
            "error": "" if ok else "安装失败（请检查网络或改用国内镜像后重试）"}


def download_hf_model(repo_id: str, on_log=None) -> bool:
    """按需下载 HuggingFace 模型到 models/（Whisper 用）。

    走 HF_HOME（已重定向到 models/huggingface），规避代理导致的缓存污染。
    镜像：apply_mirrors(hf_endpoint=...) 设置的 HF_ENDPOINT 会被 huggingface_hub 自动采用。
    """
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError:
        if _pip_install(["huggingface_hub"], on_log=on_log):
            from huggingface_hub import snapshot_download  # type: ignore
        else:
            return False
    if on_log:
        ep = _HF_ENDPOINT or "https://huggingface.co"
        on_log(f"正在下载模型 {repo_id} 到 models/（端点：{ep}）...")
    try:
        snapshot_download(
            repo_id=repo_id,
            cache_dir=str(paths.MODELS_DIR / "huggingface" / "hub"),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 网络/代理问题不致命
        if on_log:
            on_log(f"模型下载失败：{exc}")
        return False
