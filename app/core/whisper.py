"""core/whisper.py — WhisperEngine：懒加载 / GPU→CPU 回退 / 看门狗 / to_srt（阶段②b）。

要点（方案 §6.2 / §10 / §13）：
  - GPU 独占：whisper_workers=1，单路串行队列，多路反而互相抢占显存。
  - 懒加载：首次使用时才载入模型，避免拖慢启动。
  - GPU→CPU 回退：缺 cublas64_12.dll 等 CUDA 库时自动降级 CPU（红线 G6）。
  - 看门狗：推理长时间无输出时判定挂起并中止。
  - 无声视频跳过转写直接放行（由 engine 判定）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

__all__ = [
    "TranscriptResult", "WhisperEngine", "segments_to_srt", "is_available",
    "resolve_local_model",
]


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str

# 最多让远程后端连续失败几次，之后本任务内固定回退本地 Whisper
_REMOTE_FAIL_CAP = 2


@dataclass
class TranscriptResult:
    """转写结果（阶段②b 产物）。"""

    text: str = ""
    segments: List[TranscriptSegment] = field(default_factory=list)
    language: str = ""
    elapsed_ms: int = 0
    device: str = ""
    model: str = ""
    skipped: bool = False
    error: str = ""

    @property
    def chars(self) -> int:
        return len(self.text.strip())

    def to_srt(self) -> str:
        return segments_to_srt(self.segments)


def _fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms_total = int(round(seconds * 1000))
    hours, rem = divmod(ms_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

_CJK_RE = None


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (0x3040 <= code <= 0x30FF) or (0x3400 <= code <= 0x4DBF) \
        or (0x4E00 <= code <= 0x9FFF) or (0xF900 <= code <= 0xFAFF) \
        or (0xAC00 <= code <= 0xD7AF)


def _join_segment_text(segments: List[TranscriptSegment]) -> str:
    """把片段拼成一整段文本。

    CJK 片段直接相连，西文片段之间补一个空格 —— 否则英文会把每个词粘在一起
    （Hello.Whereallthisisatest...）。
    """
    parts: List[str] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        if parts:
            prev = parts[-1]
            if not (_is_cjk(prev[-1]) or _is_cjk(text[0])):
                parts.append(" ")
        parts.append(text)
    return "".join(parts).strip()


# 行首角色标签：远程服务开角色识别时会拼 `[SPEAKER_00]`（服务端原文是 `[角色 SPEAKER_00]`）
_SPEAKER_TAG_RE = re.compile(r"^\s*\[[^\]]*SPEAKER_[^\]]*\]\s*")


def segments_to_srt(segments: List[TranscriptSegment]) -> str:
    """把片段列表渲染成 SRT 字幕文本。

    行首的 `[SPEAKER_xx]` 角色标签会被剥掉 —— 字幕是给人看的，标签占行首
    还容易被播放器当成正文；说话人信息只保留在给 AI 的转写文本里。
    """
    lines: List[str] = []
    for idx, seg in enumerate(segments, start=1):
        text = _SPEAKER_TAG_RE.sub("", (seg.text or "").strip(), count=1).strip()
        if not text:
            continue
        lines.append(str(idx))
        lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + ("\n" if lines else "")


def is_available() -> bool:
    """faster-whisper 是否可用（可导入）。"""
    import importlib.util

    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def _model_repo_slug(model_name: str) -> str:
    """把模型名映射成 HF 缓存目录名，如 large-v3-turbo →
    models--mobiuslabsgmbh--faster-whisper-large-v3-turbo。"""
    name = str(model_name or "").strip()
    if not name:
        return ""
    repo = ""
    try:
        from faster_whisper.utils import _MODELS  # type: ignore

        repo = _MODELS.get(name.lower()) or _MODELS.get(name) or ""
    except Exception:  # noqa: BLE001 - 映射表缺失时退化为按名字推导
        repo = ""
    if not repo:
        repo = name
    return "models--" + repo.replace("/", "--")


def resolve_local_model(model_name: str, download_root: Optional[str]) -> str:
    """在本地缓存里定位已下载完成的模型目录，找不到返回空串。

    为什么必须这么做：faster-whisper 收到「模型名」时会走
    `download_model → huggingface_hub.snapshot_download`，而 snapshot_download
    **无条件**先请求仓库接口（`api.repo_info`）。只要 HF_ENDPOINT 指向非
    HuggingFace 兼容端点（例如 www.modelscope.cn 只有网页接口），返回的 HTML
    会让 `r.json()` 抛 JSONDecodeError —— 哪怕模型已完整缓存也照样失败。
    改成直接传「目录」，faster-whisper 会跳过一切网络请求（transcribe.py: os.path.isdir
    分支），离线可用。

    支持的落盘布局（按优先级）：
      1. `<root>/<model_name>/`                 —— 本项目 download_whisper_model 的产物
      2. `<root>/models--<owner>--<repo>/snapshots/<sha>/` —— huggingface_hub 缓存
    """
    if not download_root:
        return ""
    root = Path(download_root)
    if not root.is_dir():
        return ""

    def _valid(snap: Path) -> bool:
        """目录是否是「能真的加载起来」的完整模型。

        除了 model.bin / config.json，**词表**也必须有——
        小模型是 vocabulary.txt，large-v3 / turbo 是 vocabulary.json。
        少了词表 faster-whisper 会报
        "Cannot load the vocabulary from the model directory"，
        而旧逻辑只看前两个文件，于是会把半成品目录当成可用模型。
        """
        try:
            if not (snap / "model.bin").is_file() or not (snap / "config.json").is_file():
                return False
            for name in ("vocabulary.txt", "vocabulary.json"):
                probe = snap / name
                if probe.is_file() and probe.stat().st_size > 0:
                    return True
            return False
        except OSError:
            return False

    # 1) 本项目自己的平铺目录布局（下载器的首选产物）
    for candidate in (root / str(model_name), root / _model_repo_slug(model_name)):
        if candidate.is_dir() and _valid(candidate):
            return str(candidate)

    # 2) huggingface_hub 的 snapshots 布局
    slug = _model_repo_slug(model_name)
    roots = []
    if slug:
        roots.append(root / slug)
    roots.extend(sorted((d for d in root.glob("models--*") if d.is_dir()),
                        key=lambda d: d.name))

    best = ""
    best_mtime = -1.0
    for repo_dir in roots:
        snaps = repo_dir / "snapshots"
        if not snaps.is_dir():
            continue
        for snap in snaps.iterdir():
            if not snap.is_dir() or not _valid(snap):
                continue
            try:
                mtime = snap.stat().st_mtime
            except OSError:
                mtime = 0.0
            if mtime > best_mtime:
                best, best_mtime = str(snap), mtime
        if best:
            break  # 命中精确 slug 就不再退而求其次
    return best


def _friendly_error(exc: Exception) -> str:
    """把底层异常翻译成用户能直接照做的说明。"""
    if isinstance(exc, json.JSONDecodeError):
        return (
            f"模型缓存接口返回的不是 JSON（{exc}）。原因通常是 runtime.hf_endpoint "
            "指向了非 HuggingFace 兼容的端点（如 www.modelscope.cn 只有网页接口，"
            "不提供 HF 的 /api/models 接口）。请改用 https://hf-mirror.com，"
            "或留空使用官方源。"
        )
    text = str(exc)
    low = text.lower()

    # 模型没下完：这是最常见的失败原因，必须给出可执行的动作，
    # 而不是把 huggingface_hub 的一长段英文原样抛给用户。
    if "incomplete" in low and ("missing" in low or "model.bin" in low):
        return (
            "本地模型文件不完整（缺少 model.bin），说明上一次下载没有完成。"
            "请到「设置 → 转写运行环境」点『下载 / 续传模型』把剩余部分下完"
            "（会从已下载的字节继续，不会从零开始），"
            "或把 whisper.backend 改成 api 直接使用远程转写服务。"
        )
    if "could not be reached" in low or "connection" in low or "ssl" in low:
        return (
            f"加载模型时连不上模型下载站（{text[:120]}）。"
            "请检查网络/代理，或确认模型已完整下载到本地"
            "（设置 → 转写运行环境可查看模型状态并续传）。"
        )
    if "invalid model size" in low or "not a local folder" in low:
        return f"模型名无法识别：{text[:160]}。请检查 whisper.model 是否拼写正确。"
    return text


def _is_source_error(exc: Exception) -> bool:
    """判定「模型来源类」错误：换设备重试也没用，不必再跑一次 CPU 加载。"""
    if isinstance(exc, (json.JSONDecodeError, FileNotFoundError)):
        return True
    text = str(exc)
    return "Invalid model size" in text or "not a local folder" in text


def _hf_endpoint_issue() -> str:
    """当前 HF_ENDPOINT 是否不兼容（返回提示语，空串表示没问题）。"""
    try:
        from ..infra import bootstrap

        return bootstrap.hf_endpoint_issue()
    except Exception:  # noqa: BLE001 - bootstrap 不可用时不阻断加载
        return ""


@contextmanager
def _hf_endpoint_guard(on_warn: "Callable[[str], None]") -> "Iterator[None]":
    """模型未本地缓存时必须联网；若 HF_ENDPOINT 不兼容则临时改用 hf-mirror。

    modelscope.cn 之类的站点没有 /api/models，会让 snapshot_download 拿回 HTML
    并抛 JSONDecodeError。这里只在本次加载期间改写环境变量，用完即还原。
    """
    current = os.environ.get("HF_ENDPOINT", "").strip()
    fallback = "https://hf-mirror.com"
    if not current or not _hf_endpoint_issue():
        yield
        return
    if current.rstrip("/").lower() == fallback:
        yield
        return
    try:
        on_warn(f"HF_ENDPOINT={current} 不是 HuggingFace 兼容端点，本次模型下载临时改用 {fallback}")
    except Exception:  # noqa: BLE001
        pass
    os.environ["HF_ENDPOINT"] = fallback
    try:
        yield
    finally:
        if current:
            os.environ["HF_ENDPOINT"] = current
        else:
            os.environ.pop("HF_ENDPOINT", None)


def _cuda_libs_present() -> bool:
    """检查 CUDA 运行库（cublas64_12.dll）是否存在。

    这是 faster-whisper 在 Windows 上 GPU 推理的硬依赖；
    缺失会导致推理挂起（而非报错），因此必须前置探测。

    判定顺序：libs/nvidia/*/bin 磁盘直查 → ctypes 加载 → PATH。
    """
    import ctypes
    import sys

    if sys.platform != "win32":
        return True
    # 1) 磁盘直查（pip 装的 nvidia-* 包落在 libs/nvidia/*/bin）
    try:
        from ..infra.paths import LIBS_DIR

        nvidia_root = LIBS_DIR / "nvidia"
        if nvidia_root.is_dir():
            for dll in nvidia_root.glob("*/bin/cublas64_12.dll"):
                return True
            for dll in nvidia_root.glob("*/bin/cublasLt64_12.dll"):
                return True
    except Exception:  # noqa: BLE001 - 探测失败不致命
        pass
    # 2) 直接尝试加载（PATH / 已注入的 DLL 目录）
    try:
        ctypes.WinDLL("cublas64_12.dll")
        return True
    except OSError:
        return False


class WhisperEngine:
    """Whisper 转写引擎（懒加载 + 设备自动判定 + 看门狗）。

    线程安全：内部用锁保证模型只加载一次；推理本身由调用方串行化。
    """

    def __init__(
        self,
        *,
        model_name: str = "large-v3-turbo",
        device: str = "auto",
        compute_type: str = "int8_float16",
        vad_filter: bool = True,
        language: str = "auto",
        use_gpu: bool = True,
        download_root: Optional[str] = None,
        watchdog_seconds: float = 300.0,
        on_log: Optional[Callable[[str, str], None]] = None,
        # 远程转写后端（whisper.backend = "api" 时启用）
        backend: str = "local",
        api_config: Optional[Dict[str, Any]] = None,
        prefer_api: bool = False,
        # 本地模型缺失/不完整时是否允许联网下载。
        # 默认关：弱网下 faster-whisper 会去拉 1.6GB 并卡住整条流水线，
        # 而下载应由「转写运行环境 → 下载 / 续传模型」显式触发（那里可停可续）。
        allow_download: bool = False,
    ) -> None:
        self.model_name = model_name
        self.requested_device = device
        self.compute_type = compute_type
        self.vad_filter = vad_filter
        self.language = None if str(language).lower() in ("auto", "", "none") else language
        self.use_gpu = bool(use_gpu)
        self.download_root = download_root
        self.watchdog_seconds = watchdog_seconds
        self._on_log = on_log

        # 转写后端：local = 本地 faster-whisper，api = 自定义 HTTP 服务，
        # auto = 优先 API（prefer_api 且已填 base_url），失败回退本地
        self.backend = str(backend).lower() if str(backend).lower() in ("local", "api", "auto") else "local"
        self.prefer_api = bool(prefer_api)
        self.allow_download = bool(allow_download)
        self._api_config = dict(api_config or {})
        self._remote = None          # RemoteASREngine 惰性实例

        self._model = None
        self._actual_device = ""
        self._actual_compute = ""
        self._active_backend = ""    # 生效后端："api" 或 "local"（首次 load 后确定）
        self._load_lock = threading.Lock()
        self._load_error = ""
        self._load_failed = False   # 终态失败：避免每个视频都重试一次（含网络请求）
        self._local_model_path = ""
        # 远程连续失败计数（达上限后本任务内固定回退本地，避免每个文件都重试一次远程）
        self._remote_failures = 0

    # ---------- 日志 ----------
    def _log(self, level: str, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, msg)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 设备判定 ----------
    def _resolve_device(self) -> "tuple[str, str]":
        """返回 (device, compute_type)。auto 时按 GPU 可用性判定。"""
        requested = (self.requested_device or "auto").lower()
        compute = self.compute_type or "int8_float16"

        # 用户显式关闭 GPU → 一律 CPU
        if not self.use_gpu:
            return "cpu", "int8"
        if requested == "cpu":
            return "cpu", "int8"

        gpu_ok = False
        if requested in ("auto", "cuda"):
            # 1) ctranslate2 报告有 CUDA 设备
            try:
                import ctranslate2  # type: ignore

                gpu_ok = ctranslate2.get_cuda_device_count() > 0
            except Exception:  # noqa: BLE001
                gpu_ok = False
            # 2) CUDA 运行库必须就位，否则会「挂起」而非报错
            if gpu_ok and not _cuda_libs_present():
                self._log("WARN", "检测到 CUDA 设备但缺少 cublas64_12.dll，将回退 CPU 转写")
                gpu_ok = False

        if gpu_ok:
            if compute == "int8":
                compute = "int8_float16"
            return "cuda", compute
        return "cpu", "int8"

    # ---------- 远程后端 ----------
    def _get_remote(self):
        """惰性构造远程转写后端（prefer_api / backend 非 local 时使用）。"""
        if self._remote is None:
            from .asr_remote import ASRConfig, RemoteASREngine

            cfg = ASRConfig.from_dict(self._api_config)
            # 语言沿用 whisper.language，除非 api 段自己指定
            if not cfg.language or cfg.language == "auto":
                cfg.language = str(self.language or "auto")
            self._remote = RemoteASREngine(cfg, on_log=self._log)
        return self._remote

    def reset_task_state(self) -> None:
        """新任务开始时清掉「上一批」留下的失败终态。

        WhisperEngine 实例是跨任务复用的（engine._whisper 只构造一次），
        不重置的话：上一批里远程挂过一次，这一批即使服务已经恢复也永远不会再试 API。
        已加载好的本地模型与远程客户端不销毁，不重复付加载代价。
        """
        self._remote_failures = 0
        self._load_failed = False
        self._load_error = ""
        self._active_backend = ""
        if self._remote is not None:
            self._remote.reset()

    def set_api_config(self, api_config: Dict[str, Any]) -> None:
        """热更新远程配置（测试连接 / 配置修改后调用）。"""
        self._api_config = dict(api_config or {})
        self._remote = None          # 下次 _get_remote 重建

    def _effective_plan(self) -> str:
        """决定本次 load 走 "api" 还是 "local"。

        优先级：api > local（当 prefer_api 且配了 base_url，且远程尚未连续失败过多时）。
        backend="api" 则强制走 api（不回退，保留旧语义）。
        """
        if self.backend == "api":
            return "api"
        if self.backend == "local":
            return "local"
        # backend == "auto"
        if not self.prefer_api:
            return "local"
        if not self._api_config.get("base_url"):
            return "local"
        if self._remote_failures >= _REMOTE_FAIL_CAP:
            return "local"
        return "api"

    def load(self) -> bool:
        """惰性加载模型；成功返回 True。线程安全。

        backend=api 时不做任何本地模型加载，只校验远程配置与连通性。
        backend=auto 时优先尝试 API，失败后自动回退本地 faster-whisper。
        """
        plan = self._effective_plan()
        prev_backend = self._active_backend
        self._active_backend = plan
        if plan != "api":
            return self._load_local()

        # ---------- 远程 API ----------
        if self._load_failed:
            return False
        with self._load_lock:
            if self._load_failed:
                return False
            remote = self._get_remote()
            if remote.load():
                self._actual_device = "remote"
                self._actual_compute = remote.cfg.protocol
                self._load_error = ""
                self._active_backend = "api"
                # 只在后端真正切换时打一行，避免每个视频都重复刷屏
                if prev_backend != "api":
                    self._log("INFO", f"转写后端：API {remote.cfg.base_url}（{remote.cfg.protocol}）")
                return True
            self._load_error = remote.load_error
            self._remote_failures += 1
            remote_error = self._load_error

        # backend=api 是「只用远程」语义，不回退；auto 才回退
        if self.backend == "api":
            self._load_failed = True
            return False

        # 远程整体不可用：直接把失败计数打满，本次任务内不再试探远程
        self._remote_failures = _REMOTE_FAIL_CAP
        self._active_backend = "local"
        self._log("WARN", f"远程 ASR 不可用（{remote_error}），已回退本地 Whisper")
        if not self._load_local():
            # 两边都不可用：保留远程错误 + 本地错误，便于用户定位
            self._load_error = (f"远程 ASR 不可用（{remote_error}）；"
                                f"本地 Whisper 也不可用（{self._load_error}）")
            return False
        return True

    # ---------- 模型加载（本地 faster-whisper） ----------

    def _load_local(self) -> bool:
        """加载本地 faster-whisper（幂等，线程安全）。"""
        if self._model is not None:
            return True
        # 终态失败后不再重试：否则每个文件都会重新走一次（可能联网的）加载流程
        if self._load_failed:
            return False
        with self._load_lock:
            if self._model is not None:
                return True
            if self._load_failed:
                return False
            if not is_available():
                self._load_error = "faster-whisper 未安装"
                self._load_failed = True
                self._log("WARN", "Whisper 不可用：faster-whisper 未安装，已降级为纯画面分析")
                return False

            device, compute = self._resolve_device()
            # 本地缓存优先：直接把模型目录喂给 faster-whisper，彻底绕开网络
            self._local_model_path = resolve_local_model(self.model_name, self.download_root)
            source = self._local_model_path or self.model_name
            if self._local_model_path:
                self._log("INFO", f"发现本地模型缓存，离线加载：{self._local_model_path}")
            elif not self.allow_download:
                # 没有可用的本地模型，又没开「允许联网下载」：立刻失败，
                # 不要按模型名去 snapshot_download（弱网会卡几分钟且必然失败）。
                self._load_error = (
                    f"本地模型 {self.model_name} 未就绪（目录缺词表或未下载完）。"
                    "请在设置 → 转写运行环境里点「下载 / 续传模型」，"
                    "或改用 API 转写后端。"
                )
                self._load_failed = True
                self._log("WARN", f"Whisper 本地模型不可用：{self._load_error}")
                return False

            def _open(src: str, dev: str, cpt: str):
                from faster_whisper import WhisperModel  # type: ignore

                return WhisperModel(
                    src, device=dev, compute_type=cpt,
                    download_root=self.download_root,
                )

            def _guard():
                # 本地目录加载不走网络，无需换源；只有按「模型名」下载才需要
                if self._local_model_path:
                    return nullcontext()
                return _hf_endpoint_guard(lambda m: self._log("WARN", m))

            try:
                self._log("INFO", f"正在加载 Whisper 模型 {self.model_name}（device={device}, compute={compute}）...")
                with _guard():
                    self._model = _open(source, device, compute)
                self._actual_device = device
                self._actual_compute = compute
                self._log("INFO", f"Whisper 就绪：{self.model_name} · {device} · {compute}")
                return True
            except Exception as exc:  # noqa: BLE001 - 模型加载失败不致命
                self._load_error = _friendly_error(exc)
                self._log("WARN", f"Whisper 模型加载失败（{device}）：{self._load_error}")
                # 只有「设备相关」失败才值得换 CPU 再试；来源类错误重试必然同样失败
                if device == "cuda" and not _is_source_error(exc):
                    try:
                        self._log("INFO", "尝试回退 CPU 加载 Whisper 模型 ...")
                        with _guard():
                            self._model = _open(source, "cpu", "int8")
                        self._actual_device = "cpu"
                        self._actual_compute = "int8"
                        self._log("INFO", "Whisper 已回退 CPU 加载成功")
                        return True
                    except Exception as exc2:  # noqa: BLE001
                        self._load_error = _friendly_error(exc2)
                        self._log("ERROR", f"Whisper CPU 回退也失败：{self._load_error}")
                self._load_failed = True
                return False

    @property
    def device(self) -> str:
        return self._actual_device or (self.requested_device or "auto")

    @property
    def local_model_path(self) -> str:
        return self._local_model_path

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def active_backend(self) -> str:
        """当前实际生效的转写后端："api" / "local"（尚未 load 时为空串）。"""
        return self._active_backend

    # ---------- 转写 ----------
    def transcribe(
        self,
        audio_path: str,
        *,
        stop_token=None,
        beam_size: int = 5,
    ) -> TranscriptResult:
        """转写单个音频文件。

        看门狗：在独立线程内跑推理，超时未结束则判定挂起并放弃结果
        （不 kill 线程以避免 C++ 层崩溃，但会立刻返回并降级）。

        backend=auto 时优先走 API；单条转写失败（异常/超时/无结果）且尚未达到
        _REMOTE_FAIL_CAP 时，同一条音频自动回退本地重试一次。
        """
        result = TranscriptResult(model=self.model_name)
        if not self.load():
            result.skipped = True
            result.error = self._load_error or "Whisper 不可用"
            return result

        # 远程后端优先（backend=auto 且 prefer_api）：失败时同一条音频回退本地重试
        if self._effective_plan() == "api":
            remote = self._transcribe_remote(audio_path, result,
                                             watchdog=self.watchdog_seconds,
                                             result_name=self.model_name)
            if remote.error and not remote.skipped:
                self._remote_failures += 1
                if self.backend == "api":
                    # 「只用远程」语义：不回退，直接把错误交给上层
                    return remote
                if self._remote_failures >= _REMOTE_FAIL_CAP:
                    self._log("WARN", f"远程转写已连续失败 {self._remote_failures} 次，"
                                       "本次任务后续固定用本地 Whisper")
                else:
                    self._log("WARN", f"远程转写失败（{remote.error}），改回退本地 Whisper")
                # 本地模型可能还没加载过（此前一直是远程在跑），先补加载
                if self._load_local():
                    self._active_backend = "local"
                    local = self._transcribe_local(audio_path, stop_token=stop_token,
                                                   beam_size=beam_size)
                    if not local.error:
                        return local
                    local.error = f"远程失败：{remote.error}；本地也失败：{local.error}"
                    return local
                remote.error = f"{remote.error}；且本地 Whisper 不可用（{self._load_error}）"
                return remote
            return remote

        return self._transcribe_local(audio_path, stop_token=stop_token, beam_size=beam_size)

    # ---------- 本地转写（带看门狗） ----------
    def _transcribe_local(self, audio_path: str, *, stop_token=None,
                          beam_size: int = 5) -> TranscriptResult:
        result = TranscriptResult(model=self.model_name)
        if self._model is None:
            result.skipped = True
            result.error = self._load_error or "Whisper 不可用"
            return result

        started = time.time()
        result.device = self._actual_device
        holder: dict = {}

        def _work() -> None:
            try:
                segments, info = self._model.transcribe(  # type: ignore[union-attr]
                    audio_path,
                    beam_size=beam_size,
                    vad_filter=self.vad_filter,
                    language=self.language,
                    word_timestamps=False,
                )
                collected: List[TranscriptSegment] = []
                for seg in segments:
                    if stop_token is not None and stop_token.is_set():
                        break
                    text = (seg.text or "").strip()
                    if not text:
                        continue
                    collected.append(TranscriptSegment(
                        start=float(seg.start or 0.0),
                        end=float(seg.end or 0.0),
                        text=text,
                    ))
                holder["segments"] = collected
                holder["text"] = _join_segment_text(collected)
                holder["language"] = getattr(info, "language", "") or ""
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)

        worker = threading.Thread(target=_work, name="whisper-infer", daemon=True)
        worker.start()
        worker.join(timeout=self.watchdog_seconds)

        if worker.is_alive():
            result.error = f"转写看门狗超时（>{self.watchdog_seconds:.0f}s），判定推理挂起"
            result.elapsed_ms = int((time.time() - started) * 1000)
            self._log("ERROR", result.error)
            return result

        if "error" in holder:
            result.error = holder["error"]
            result.elapsed_ms = int((time.time() - started) * 1000)
            self._log("WARN", f"转写失败：{result.error}")
            return result

        result.segments = holder.get("segments", [])
        result.text = holder.get("text", "")
        result.language = holder.get("language", "")
        result.elapsed_ms = int((time.time() - started) * 1000)
        self._log("DEBUG", f"转写完成：字幕 {result.chars} 字 · 用时 {result.elapsed_ms} ms")
        return result

    # ---------- 远程转写（带看门狗） ----------
    def _transcribe_remote(self, audio_path: str, result: TranscriptResult,
                           *, watchdog: float, result_name: str = "") -> TranscriptResult:
        """在独立线程里跑远程转写，超时则放弃结果（与本地推理的看门狗语义一致）。

        远程 HTTP 请求本身有 timeout，但 gradio_client 的排队/上传阶段可能不受控，
        这里再加一层线程级超时兜底，避免一个卡死的请求拖住整条流水线。

        调用方（transcribe）在拿到失败结果后自行决定是否回退本地重试。
        """
        started = time.time()
        result.model = result_name or self.model_name
        result.device = "remote"
        holder: dict = {}
        remote = self._get_remote()

        def _work() -> None:
            try:
                sub = remote.transcribe(audio_path)
                holder["text"] = sub.text
                holder["segments"] = sub.segments
                holder["language"] = sub.language
                holder["error"] = sub.error
                holder["skipped"] = sub.skipped
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)

        worker = threading.Thread(target=_work, name="asr-remote", daemon=True)
        worker.start()
        worker.join(timeout=max(30.0, float(watchdog)))

        if worker.is_alive():
            result.error = f"远程转写超时（>{watchdog:.0f}s），已放弃本次结果"
            result.elapsed_ms = int((time.time() - started) * 1000)
            self._log("ERROR", result.error)
            return result

        result.elapsed_ms = int((time.time() - started) * 1000)
        if holder.get("skipped"):
            result.skipped = True
            result.error = holder.get("error", "") or "远程转写已跳过"
            return result
        if holder.get("error") and not holder.get("segments"):
            result.error = str(holder.get("error"))
            # 不再在这里记日志：RemoteASREngine 已经记过一条 WARN，
            # 调用方（transcribe）还会记一条带「回退/不回退」语义的日志，
            # 三处都记会让同一故障在日志里重复三次。
            return result

        result.segments = holder.get("segments", []) or []
        result.text = holder.get("text", "")
        result.language = holder.get("language", "")
        # 成功日志由 RemoteASREngine 自己记（与失败日志同一处），这里不再重复
        return result
