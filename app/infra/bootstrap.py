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

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import paths

__all__ = [
    "early_init", "ensure_runtime", "probe_runtime", "download_hf_model",
    "RuntimeReport", "cuda_libs_status", "ensure_cuda_libs", "apply_mirrors",
    "mirror_config", "PIP_PRESETS", "HF_PRESETS", "install_whisper",
    "hf_endpoint_issue", "nvidia_dll_roots", "download_whisper_model",
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
}
# 注意：www.modelscope.cn 不是 HuggingFace 兼容端点（只有网页接口，没有
# /api/models 系列 JSON 接口）。把它填进 HF_ENDPOINT 会让
# huggingface_hub.snapshot_download → api.repo_info 拿到 HTML 后抛
# JSONDecodeError("Expecting value: line 1 column 1 (char 0)")，即使模型已缓存。
# 这里保留识别，命中时给出明确提示而不是让用户对着 JSON 报错发懵。
_HF_INCOMPATIBLE_HOSTS = ("modelscope.cn", "modelscope.com")

_PIP_INDEX: str = ""
_HF_ENDPOINT: str = ""
_HF_ENDPOINT_WARNING: str = ""


def _hf_endpoint_warning(endpoint: str) -> str:
    host = ""
    try:
        from urllib.parse import urlparse

        host = (urlparse(endpoint).hostname or "").lower()
    except Exception:  # noqa: BLE001
        host = ""
    for bad in _HF_INCOMPATIBLE_HOSTS:
        if host.endswith(bad):
            return (f"{endpoint} 不是 HuggingFace 兼容端点，模型下载/缓存校验会失败"
                    "（报 Expecting value: line 1 column 1）。建议改用 https://hf-mirror.com")
    return ""


def hf_endpoint_issue(endpoint: "str | None" = None) -> str:
    """当前 HF_ENDPOINT 的兼容性提示；空串表示可用。

    供 whisper 引擎在「必须联网下载模型」时判断是否需要临时换源。
    """
    ep = (endpoint if endpoint is not None
          else (_HF_ENDPOINT or os.environ.get("HF_ENDPOINT", ""))).strip()
    return _hf_endpoint_warning(ep)


def apply_mirrors(pip_index: "str | None" = None, hf_endpoint: "str | None" = None) -> dict:
    """设置依赖/模型下载镜像。空字符串表示用官方源。

    - pip_index → 影响 _pip_install（-i 参数）
    - hf_endpoint → 设置 HF_ENDPOINT，影响 huggingface_hub 模型下载
    """
    global _PIP_INDEX, _HF_ENDPOINT, _HF_ENDPOINT_WARNING
    _PIP_INDEX = (pip_index or "").strip()
    _HF_ENDPOINT = (hf_endpoint or "").strip()
    _HF_ENDPOINT_WARNING = _hf_endpoint_warning(_HF_ENDPOINT)

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
            "hf_endpoint_warning": _HF_ENDPOINT_WARNING,
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
    # 关掉 Xet 分块传输：新版 huggingface_hub 走 Xet 时对 hf-mirror 等镜像
    # 会报 "CAS Client Error: Format error: I/O error"，退回普通 HTTP 更稳。
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    # Windows 未开开发者模式时建不了符号链接；而符号链接探测用的是
    # tempfile.mkdtemp，在某些安全策略下该目录不可写，会让下载在第 0 步就失败。
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

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


def _nvidia_dll_roots() -> List["Path"]:
    """返回所有可能存放 CUDA 运行库 DLL 的目录（去重、只保留存在的）。

    为什么要搜这么多地方：旧逻辑只认 libs/nvidia/*/bin，于是「用户已经用 pip
    在系统环境装过 nvidia-cublas-cu12」这种再常见不过的情况会被判成「缺失」，
    接着就触发一次 696MB 的重复下载 —— 这正是「依赖安装太慢」的真实原因。

    搜索顺序（就近优先）：
      1. libs/nvidia/*/bin|lib      —— 本项目 --target 安装的
      2. site-packages/nvidia/*/bin|lib —— 系统环境 pip 装的
      3. ctranslate2 包目录 —— 它自带 cudnn64_9.dll（cuDNN 9 的精简分发包）
      4. torch/lib —— 装了 PyTorch 时它自带全套 cublas/cudnn
    """
    if sys.platform != "win32":
        return []

    roots: List[Path] = []
    seen = set()

    def _add(path: "Path") -> None:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key in seen or not path.is_dir():
            return
        # 目录里没有任何 dll 就不必加入搜索路径
        try:
            if not any(f.suffix.lower() == ".dll" for f in path.iterdir()):
                return
        except OSError:
            return
        seen.add(key)
        roots.append(path)

    nvidia_bases: List[Path] = [paths.LIBS_DIR / "nvidia"]

    # 系统环境（含用户级）里 pip 装好的 nvidia-* 包
    try:
        import site

        for base in list(site.getsitepackages()) + [site.getusersitepackages()]:
            if base:
                nvidia_bases.append(Path(base) / "nvidia")
    except Exception:  # noqa: BLE001 - site 信息缺失不致命
        pass

    for nvidia_root in nvidia_bases:
        if not nvidia_root.is_dir():
            continue
        try:
            subs = sorted(d for d in nvidia_root.iterdir() if d.is_dir())
        except OSError:
            continue
        for sub in subs:
            for candidate in (sub / "bin", sub / "lib"):
                _add(candidate)

    # ctranslate2 自带 cudnn64_9.dll
    try:
        import ctranslate2  # type: ignore

        _add(Path(ctranslate2.__file__).parent)
    except Exception:  # noqa: BLE001 - 未安装则不参与
        pass

    # PyTorch 自带全套 CUDA 运行库（常见于别的项目共用同一个 Python）
    try:
        import torch  # type: ignore

        _add(Path(torch.__file__).parent / "lib")
    except Exception:  # noqa: BLE001
        pass

    return roots


def _inject_nvidia_dll_dirs() -> List[str]:
    """把所有能找到的 CUDA 运行库目录加入 DLL 搜索路径，返回加入的目录列表。

    faster-whisper(CTranslate2) 在 GPU 推理时需要 cublas64_12.dll / cudnn*.dll。
    缺库时 CTranslate2 不会报错而是**挂起**，因此这里必须尽可能把已有目录都用上。
    """
    if sys.platform != "win32":
        return []
    extra = [str(p) for p in _nvidia_dll_roots()]
    if not extra:
        return []

    # Python 3.8+ 推荐方式
    try:
        for d in extra:
            os.add_dll_directory(d)
    except (AttributeError, OSError):
        pass

    # PATH 兜底（部分库用 LoadLibrary 直接搜索 PATH）
    current = os.environ.get("PATH", "")
    missing = [d for d in extra if d not in current]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + current
    return extra


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


def _pip_install(
    packages: List[str],
    on_log=None,
    index_url: str = "",
    retries: int = 3,
    timeout: float = 7200,
) -> bool:
    """把依赖安装到 libs/（--target），不污染系统环境。

    index_url 为空时回退到 apply_mirrors() 设置的全局 pip 源，再为空则用官方源。

    为什么不是「跑一次 pip 就完事」：CUDA 运行库单个 wheel 就有几百 MB，
    走官方源在国内经常几十 KB/s 甚至断流，pip 默认只有 15s 超时和 5 次重试，
    一旦断开就整包重来（本项目实测卡在 1.7MB/696MB 的第 3 次续传）。
    因此这里显式放大超时/重试次数，并在失败后**整体重跑**——
    pip 会复用临时目录里的 .incomplete 文件续传，不会从零开始。
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
        # 大 wheel（cublas 单包 600MB+）必须放大超时与重试，否则必然中断
        "--timeout", "120",
        "--retries", "10",
    ]
    if index:
        cmd += ["-i", index]
        try:
            from urllib.parse import urlparse

            host = urlparse(index).hostname
            # 镜像站常见 http 或自签证书，需要显式放行
            if host:
                cmd += ["--trusted-host", host]
        except Exception:  # noqa: BLE001
            pass
    cmd += list(packages)

    src = f"（源：{index}）" if index else "（官方源）"
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        if on_log:
            suffix = "" if attempt == 1 else f" · 第 {attempt}/{attempts} 次尝试（已下载部分会续传）"
            on_log(f"正在安装依赖到 libs/：{' '.join(packages)} {src}{suffix}")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            if on_log:
                on_log(f"依赖安装超时（>{timeout:.0f}s），第 {attempt}/{attempts} 次")
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            if on_log:
                on_log(f"依赖安装失败：{exc}")
            continue

        if proc.returncode == 0:
            # 安装后刷新 sys.path 让新包立即可用
            libs = str(paths.LIBS_DIR)
            if libs not in sys.path:
                sys.path.insert(0, libs)
            import importlib

            importlib.invalidate_caches()
            return True

        if on_log:
            raw = (proc.stderr or proc.stdout or "").strip()
            tail = [ln for ln in raw.splitlines() if ln.strip()][-3:]
            on_log(f"依赖安装失败（第 {attempt}/{attempts} 次）：" + " | ".join(tail)[:400])

    if on_log:
        on_log(
            "依赖安装多次失败。可尝试：1) 设置页把 pip 源换成清华/阿里镜像；"
            "2) 检查代理/VPN；3) 手动执行 pip install --target libs " + " ".join(packages)
        )
    return False


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
    "gradio_client": "gradio_client>=1.0",
}
# Whisper 的 CUDA 运行库
_CUDA_PKGS = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]


def nvidia_dll_roots() -> List[str]:
    """对外暴露的 CUDA 运行库目录列表（供 whisper 引擎与诊断使用）。"""
    return [str(p) for p in _nvidia_dll_roots()]


def cuda_libs_status() -> dict:
    """检查 faster-whisper 在 Windows 上 GPU 推理所需的 CUDA 运行库。

    关键依赖是 cublas64_12.dll（还有 cudnn 系列）。缺失时 CTranslate2 不会报错，
    而是「挂起」，所以必须能独立判定。

    **搜索范围**：本项目 libs/、系统 site-packages、ctranslate2 自带目录、torch/lib。
    只要任意一处能找到即可，绝不因为「不在 libs/」就误判缺失并重复下载 696MB。

    返回 {"present": bool, "cublas": path|"", "cudnn": [paths], "checked": [...],
          "sources": [dirs], "reused": bool}
      reused=True 表示命中的系统环境已有的库（本次无需下载）
    """
    report = {
        "present": False, "cublas": "", "cudnn": [], "checked": [],
        "sources": [], "reused": False,
    }
    if sys.platform != "win32":
        report["present"] = True  # 非 Windows 不做此检查
        return report

    roots = _nvidia_dll_roots()
    report["sources"] = [str(p) for p in roots]
    libs_prefix = str(paths.LIBS_DIR).lower()

    cublas = ""
    cudnn: List[str] = []
    cublas_in_libs = False
    for root in roots:
        try:
            dlls = sorted(root.glob("*.dll"))
        except OSError:
            continue
        in_libs = str(root).lower().startswith(libs_prefix)
        for dll in dlls:
            name = dll.name.lower()
            report["checked"].append(str(dll))
            if name.startswith("cublas64_12") or name.startswith("cublaslt64_12"):
                if not cublas:
                    cublas = str(dll)
                    cublas_in_libs = in_libs
            elif name.startswith("cudnn"):
                cudnn.append(str(dll))

    report["cublas"] = cublas
    report["cudnn"] = cudnn

    if cublas:
        # 磁盘上有：尽可能真正加载一次，确认位数与依赖链没问题
        try:
            import ctypes

            ctypes.WinDLL(cublas)
        except OSError:
            # 文件在磁盘上但加载失败（可能缺下级依赖）：仍视为「已安装」，
            # 交由 CTranslate2 决定，避免误判为「未安装」而反复下载。
            pass
        report["present"] = True
        report["reused"] = not cublas_in_libs
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
    ok = _pip_install(_CUDA_PKGS, on_log=on_log, retries=3)
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

    # 2b) gradio 协议的远程 ASR 需要 gradio_client。
    # 只在配置确实指向 gradio 服务时才装 —— openai 协议 / 纯本地用户不必付这份代价。
    if _uses_gradio_asr() and not _importable("gradio_client"):
        if _pip_install([_OPTIONAL["gradio_client"]], on_log=log):
            log("已补齐远程 ASR 依赖：gradio_client")

    # 3) CUDA 运行库：机器有 GPU 但缺 cublas/cudnn 时自动补齐
    #    注意：原判据「has_cuda 为假才补」会漏掉最典型的故障场景——
    #    GPU 存在（device_count>0）但 cublas64_12.dll 缺失 → 推理挂起。
    #    这里改为独立检查 DLL 是否就位。
    if auto_install_cuda and sys.platform == "win32" and _importable("ctranslate2"):
        status = cuda_libs_status()
        if status["present"]:
            # 系统环境里已有（如系统 Python 装过 nvidia-cublas-cu12）→ 直接复用，
            # 不重复下载 696MB。这是「依赖安装太慢」的主要来源。
            if status.get("reused"):
                log(f"复用系统已有的 CUDA 运行库：{status.get('cublas', '')}")
        else:
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


def _uses_gradio_asr() -> bool:
    """配置里的转写后端是否会用到 gradio 协议。

    直接读 config.json 而不 import app.infra.config —— 这个模块在 run.py 里
    是第一顺位导入的（注入 libs/ 之前），不能反向依赖配置层。读失败就当作「不用」，
    这样离线/未初始化场景不会因为探测而多花一次 pip 安装。
    """
    try:
        path = paths.APP_ROOT / "config.json"
        if not path.is_file():
            return False
        cfg = json.loads(path.read_text(encoding="utf-8"))
        w = (cfg or {}).get("whisper") or {}
        if str(w.get("backend") or "auto").lower() == "local":
            return False
        api = w.get("api") or {}
        return str(api.get("protocol") or "") == "gradio" and bool(str(api.get("base_url") or "").strip())
    except Exception:  # noqa: BLE001 - 探测失败不应阻断启动
        return False


def _hf_download_env() -> dict:
    """构造一份适合「国内弱网 + Windows」的 HF 下载环境变量。

    两个坑必须显式处理：
      1. 默认的 HF 缓存用符号链接落盘。Windows 未开开发者模式时建不了符号链接，
         huggingface_hub 会去探测「是否支持符号链接」——而探测用的是
         tempfile.mkdtemp，在部分安全策略下创建出的目录**不可写**，
         于是 touch() 直接 PermissionError，整个下载在第 0 步就崩掉。
         关掉符号链接（HF_HUB_DISABLE_SYMLINKS=1）即可完全跳过这段探测。
      2. 断点续传需要保留 .incomplete 文件，所以不能被清理。
    """
    return {
        "HF_HUB_DISABLE_SYMLINKS": "1",
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        # 新版 huggingface_hub 默认走 Xet 分块传输，对 hf-mirror 这类镜像
        # 常常直接报 "CAS Client Error: Format error: I/O error"。
        # 关掉它退回普通 HTTP 下载，兼容性最好，也保留断点续传。
        "HF_HUB_DISABLE_XET": "1",
        "HF_HUB_DOWNLOAD_TIMEOUT": "120",
        "HF_HUB_ETAG_TIMEOUT": "30",
    }


# Whisper 模型仓库的文件清单**并不统一**，实测（2026-11）：
#   Systran/faster-whisper-{tiny,base,small,medium}
#       → config.json / model.bin / tokenizer.json / vocabulary.txt
#   Systran/faster-whisper-large-v3、mobiuslabsgmbh/faster-whisper-large-v3-turbo
#       → config.json / model.bin / tokenizer.json /
#         vocabulary.json / preprocessor_config.json
# 关键差异：小模型用 vocabulary.txt，大模型用 vocabulary.json。
# 只认其中一种会把另一种模型下残（faster-whisper 报
# "Cannot load the vocabulary from the model directory"）。
_WHISPER_MODEL_FILES = (
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
    "vocabulary.json",
    "preprocessor_config.json",
)

# 任何 Whisper 模型都必须有的文件。词表二选一（.txt 或 .json），见 _has_vocabulary。
_WHISPER_REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")

# 仓库里与推理无关、不必下载的文件
_WHISPER_SKIP_FILES = (".gitattributes", "README.md", "LICENSE", ".gitignore")


def _has_vocabulary(model_dir: Path) -> bool:
    """模型目录里是否已有词表。

    faster-whisper 的词表有两个来源：小模型是 vocabulary.txt，
    large-v3 / turbo 是 vocabulary.json，二者有一个即可。
    """
    for name in ("vocabulary.txt", "vocabulary.json"):
        try:
            if (model_dir / name).is_file() and (model_dir / name).stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _http_json(url: str, timeout: float = 30) -> Any:
    """GET 一个 JSON 端点。"""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "vair/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _download_one_file(
    url: str,
    target: Path,
    *,
    expected_size: int,
    log,
    stop_check=None,
    max_retries: int = 0,
    stall_seconds: float = 15.0,
) -> Tuple[bool, str]:
    """断点续传下载单个文件。

    为什么不用 huggingface_hub：本机实测走 hf-mirror 时传输层会反复
    "peer closed connection"（每几百 KB 断一次），hf_hub 内部重试次数用尽后整包失败，
    而 1.6GB 的 model.bin 在这种网络下永远下不完。
    这里改用最朴素的 HTTP Range 续传：断了就从已有字节继续，无限重试，
    并把进度写进日志，用户能看到「已下载 763MB / 1542MB」而不是干等。
    """
    import urllib.error
    import urllib.request

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".incomplete")

    # 已有完整文件就直接用
    try:
        _sz = target.stat().st_size if target.is_file() else -1
        # expected_size 未知（清单取不到）时也必须拒绝 0 字节文件：
        # 那正是「建好文件就断线」留下的空壳，放行会让模型缺文件却报成功。
        if _sz > 0 and (expected_size <= 0 or _sz == expected_size):
            return True, ""
    except OSError:
        pass

    # 关键：上一次可能已经写出一个「不完整但很大」的 target（例如旧版本/中断后
    # 直接保留了 model.bin）。只认 .incomplete 会把这份已有进度白白丢掉，
    # 于是每次续传都从头下 1.6GB。这里把两者当中较大的那份接过来继续。
    # 注意：0 字节的 target 是上一次「刚建好文件就断线」留下的空壳，
    # 它没有任何续传价值，直接删掉，免得被误报成「已转为续传起点」。
    try:
        t_size = target.stat().st_size if target.is_file() else 0
        p_size = part.stat().st_size if part.is_file() else 0
        if t_size > 0 and (expected_size <= 0 or t_size < expected_size):
            if t_size > p_size:
                target.replace(part)
                log(f"    发现未完成的 {target.name}（{t_size / 1048576:.0f}MB），"
                    "已转为续传起点")
            else:
                target.unlink()
        elif target.is_file() and t_size == 0:
            target.unlink()
    except OSError:
        pass

    tries = 0
    last_report = 0.0
    last_progress_bytes = -1
    last_progress_at = time.time()

    while max_retries <= 0 or tries < max_retries:
        if stop_check is not None and stop_check():
            return False, "已取消"
        tries += 1
        try:
            pos = part.stat().st_size if part.is_file() else 0
        except OSError:
            pos = 0
        if pos >= expected_size and expected_size > 0:
            break

        headers = {"User-Agent": "vair/1.0"}
        if pos > 0:
            headers["Range"] = f"bytes={pos}-"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=stall_seconds) as resp:
                # 服务端不支持 Range 时必须从头写，否则文件会损坏
                if pos > 0 and resp.status != 206:
                    pos = 0
                mode = "ab" if pos > 0 else "wb"
                with open(part, mode) as fh:
                    while True:
                        if stop_check is not None and stop_check():
                            return False, "已取消"
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                        pos += len(chunk)

                        now = time.time()
                        if now - last_report >= 3.0:
                            last_report = now
                            if expected_size > 0:
                                pct = pos * 100.0 / expected_size
                                log(f"    下载中 {pos / 1048576:.0f}MB / "
                                    f"{expected_size / 1048576:.0f}MB（{pct:.0f}%）")
                            else:
                                log(f"    下载中 {pos / 1048576:.0f}MB")
        except urllib.error.HTTPError as exc:
            # 永久性错误重试多少次都是同一个结果，必须立刻失败。
            # 之前这里一律按「网络抖动」处理，遇到 404 会白白重试 200 次
            # （实测每次约 17s，累计近一小时），最后还报出一个无关的 WinError。
            code = int(getattr(exc, "code", 0) or 0)
            # 416 = Range 起点超出文件长度 → 说明本地这份已经下完了
            # （典型场景：清单取不到时 expected_size=0，无法用大小判断完成，
            #   于是一直发 bytes=<EOF>- 的请求，服务器只能回 416）。
            if code == 416:
                log(f"    {target.name} 已达文件末尾，按已完成处理")
                break
            if code in (401, 403):
                return False, (f"无权限访问（HTTP {code}）：该模型仓库可能需要登录"
                               "或已设置访问限制")
            if code == 404:
                return False, (f"仓库里不存在文件 {target.name}（HTTP 404）："
                               "模型名或文件清单对不上，请换一个模型")
            if 400 <= code < 500 and code != 429:
                return False, f"请求被拒绝（HTTP {code}），不再重试"
            # 429 / 5xx 属于临时性问题，走退避后重试
            progressed = pos > last_progress_bytes
            last_progress_bytes = pos
            if not progressed:
                time.sleep(min(2.0 ** min(tries, 5), 5.0))
            last_progress_at = time.time()
            if tries % 10 == 0:
                log(f"    服务端暂时不可用（HTTP {code}，第 {tries} 次），"
                    f"已下载 {pos / 1048576:.0f}MB，继续续传 ...")
            continue
        except Exception as exc:  # noqa: BLE001 - 断流是常态，继续续传
            # http.client.IncompleteRead 会把「已经收到但没读完」的字节放在 .partial 里。
            # 弱网镜像（hf-mirror）几乎每次都在 response body 中途断开，这几十~几百 KB
            # 如果不落盘就等于白下 —— 而正是这个原因让 1.6GB 的 model.bin 永远下不完。
            partial = getattr(exc, "partial", None)
            if partial:
                try:
                    with open(part, "ab") as fh:
                        fh.write(partial)
                    pos += len(partial)
                except OSError:
                    pass
            # 只要有进展就立刻重连，不睡觉；确实完全没进展才退避
            progressed = pos > last_progress_bytes
            last_progress_bytes = pos
            if not progressed:
                backoff = min(2.0 ** min(tries, 4), 15.0)
                time.sleep(backoff)
            last_progress_at = time.time()
            if tries % 20 == 0:
                pct = f"（{pos * 100.0 / expected_size:.1f}%）" if expected_size > 0 else ""
                log(f"    网络中断（第 {tries} 次），已下载 {pos / 1048576:.0f}MB{pct}，继续续传 ..."
                    f"（{type(exc).__name__}）")
            continue

        try:
            size = part.stat().st_size if part.is_file() else 0
        except OSError:
            size = 0
        if expected_size <= 0 or size >= expected_size:
            # expected_size 未知时（清单取不到），只要这一轮没有报错且流读到了
            # 自然结束，就认为文件已完整；随后由外层按响应头/大小校验兜底。
            if expected_size <= 0 and pos > 0:
                break
            if size >= expected_size > 0:
                break

    try:
        size = part.stat().st_size if part.is_file() else 0
    except OSError:
        size = 0

    # 一个字节都没拿到：绝不能走到 rename（会抛 WinError 2，
    # 把「服务器一直拒绝」伪装成「重命名失败」，非常误导）。
    if size <= 0:
        return False, (f"未能下载到任何数据：{target.name}"
                       "（服务端没有返回内容，请检查网络或换下载源）")

    if expected_size > 0 and size < expected_size:
        return False, (f"下载未完成：{size / 1048576:.0f}MB / "
                       f"{expected_size / 1048576:.0f}MB（重试会继续续传）")

    if size <= 0:
        return False, f"下载得到空文件：{target.name}（服务端未返回内容）"

    try:
        if target.exists():
            target.unlink()
        part.replace(target)
    except OSError as exc:
        return False, f"保存文件失败（{target.name}）：{exc}"
    return True, ""


def download_whisper_model(
    model_name: str = "large-v3-turbo",
    on_log=None,
    *,
    download_root: "str | None" = None,
    force: bool = False,
    stop_check=None,
) -> dict:
    """下载（或续传）faster-whisper 模型到本地目录，返回 {"ok", "path", "error"}。

    落盘布局：`<download_root>/<model_name>/model.bin` 等平铺文件。
    faster-whisper 收到「目录」时会跳过一切网络请求直接加载（transcribe.py 的
    os.path.isdir 分支），因此下载完就能彻底离线，也不会再被 HF 缓存接口拖累。

    与 huggingface_hub 的 snapshot_download 相比，这里自己实现逐文件断点续传：
    弱网镜像经常每几百 KB 断一次，只有「断了就从已有字节继续」才可能下完 1.6GB。
    """
    import urllib.error
    import urllib.request

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    root = Path(download_root) if download_root else (paths.MODELS_DIR / "whisper")
    target_dir = root / str(model_name)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "path": "", "error": f"无法创建模型目录 {target_dir}：{exc}"}

    # 已完整则直接返回，不联网
    if not force:
        try:
            from ..core.whisper import resolve_local_model

            existing = resolve_local_model(model_name, str(root))
            if existing:
                log(f"本地已有完整模型，跳过下载：{existing}")
                return {"ok": True, "path": existing, "error": "", "already": True}
        except Exception:  # noqa: BLE001 - 循环导入等异常时继续走下载
            pass

    # 解析仓库名（large-v3-turbo → mobiuslabsgmbh/faster-whisper-large-v3-turbo）
    repo_id = str(model_name)
    try:
        from faster_whisper.utils import _MODELS  # type: ignore

        repo_id = _MODELS.get(str(model_name).lower()) or _MODELS.get(model_name) or repo_id
    except Exception:  # noqa: BLE001
        pass

    # 端点优先级：配置的 → hf-mirror → 官方
    endpoints: List[str] = []
    configured = (_HF_ENDPOINT or os.environ.get("HF_ENDPOINT", "")).strip()
    if configured and not _hf_endpoint_warning(configured):
        endpoints.append(configured.rstrip("/"))
    for candidate in ("https://hf-mirror.com", "https://huggingface.co"):
        if candidate not in endpoints:
            endpoints.append(candidate)

    log(f"正在下载模型 {repo_id} → {target_dir}")
    size_hint = {
        "tiny": "约 75MB", "tiny.en": "约 75MB",
        "base": "约 145MB", "base.en": "约 145MB",
        "small": "约 480MB", "small.en": "约 480MB",
        "medium": "约 1.5GB", "medium.en": "约 1.5GB",
        "large-v1": "约 3GB", "large-v2": "约 3GB", "large-v3": "约 3GB",
        "large-v3-turbo": "约 1.6GB",
        "distil-large-v3": "约 1.5GB",
    }.get(str(model_name).lower(), "")
    hint = f"（{size_hint}）" if size_hint else ""
    log(f"提示：{model_name} {hint}；弱网下会反复续传，进度会打印在下面。")

    # 1) 取文件清单（含每个文件的确切大小，用于校验）
    revision = "main"
    file_list: List[Dict[str, Any]] = []
    last_error = ""
    for ep in endpoints:
        try:
            info = _http_json(f"{ep}/api/models/{repo_id}", timeout=30)
            revision = str(info.get("sha") or "main")
            tree = _http_json(f"{ep}/api/models/{repo_id}/tree/{revision}", timeout=30)
            # 清单拿到了就按清单办事：只用它过滤掉 .gitattributes / README 这类
            # 非模型文件，但绝不因此漏掉某个模型特有的必要文件。
            file_list = [
                {"path": item.get("path"), "size": int(item.get("size") or 0)}
                for item in tree
                if item.get("type") == "file"
                and str(item.get("path") or "") not in _WHISPER_SKIP_FILES
                and not str(item.get("path") or "").startswith(".")
            ]
            if file_list:
                log(f"文件清单已获取（端点：{ep}，revision={revision[:8]}，共 {len(file_list)} 个文件）")
                break
        except Exception as exc:  # noqa: BLE001
            last_error = f"{ep} → {exc}"
            log(f"获取文件清单失败（{ep}）：{exc}")

    if not file_list:
        # 清单拿不到时**不能**凭空假设文件名：
        # 不同模型的清单本就不一样（小模型 vocabulary.txt、大模型 vocabulary.json），
        # 硬塞一份通用清单会把不存在的文件请求成 404，并把下载卡在那里。
        # 这里只下「任何 Whisper 模型都必须有」的三个文件，其余等清单可用时再补。
        log("无法获取文件清单（网络或镜像不可用），本次仅下载基础文件："
            + ", ".join(_WHISPER_REQUIRED_FILES))
        file_list = [{"path": name, "size": 0} for name in _WHISPER_REQUIRED_FILES]

    # 2) 逐文件续传
    total_files = len(file_list)
    for index, item in enumerate(file_list, start=1):
        name = str(item["path"])
        expected = int(item["size"] or 0)
        target = target_dir / name
        log(f"[{index}/{total_files}] {name}"
            + (f"（{expected / 1048576:.0f}MB）" if expected else ""))

        ok = False
        err = ""
        for ep in endpoints:
            url = f"{ep}/{repo_id}/resolve/{revision}/{name}"
            ok, err = _download_one_file(
                url, target, expected_size=expected, log=log, stop_check=stop_check,
            )
            if ok:
                break
            if err == "已取消":
                return {"ok": False, "path": str(target_dir), "error": "已取消"}
            log(f"  换端点重试：{ep} 失败（{err}）")

        if not ok:
            return {"ok": False, "path": str(target_dir),
                    "error": f"下载 {name} 失败：{err}"}

        # 大小校验（清单给不出大小时跳过）
        if expected > 0:
            try:
                actual = target.stat().st_size
                if actual != expected:
                    return {"ok": False, "path": str(target_dir),
                            "error": f"{name} 大小不符：{actual} != {expected}"}
            except OSError as exc:
                return {"ok": False, "path": str(target_dir), "error": f"无法读取 {name}：{exc}"}

    # 3) 最终校验：必备文件 + 词表二选一
    missing = [n for n in _WHISPER_REQUIRED_FILES if not (target_dir / n).is_file()]
    if missing:
        return {"ok": False, "path": str(target_dir),
                "error": f"模型文件不完整，缺少：{', '.join(missing)}"}
    if not _has_vocabulary(target_dir):
        return {"ok": False, "path": str(target_dir),
                "error": ("模型缺少词表文件（vocabulary.txt 或 vocabulary.json）。"
                          "小模型用 .txt、大模型用 .json，请重新下载补齐。")}

    log(f"模型下载完成：{target_dir}")
    return {"ok": True, "path": str(target_dir), "error": ""}
