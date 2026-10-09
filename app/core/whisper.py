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
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, List, Optional

__all__ = [
    "TranscriptResult", "WhisperEngine", "segments_to_srt", "is_available",
    "resolve_local_model",
]


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


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


def segments_to_srt(segments: List[TranscriptSegment]) -> str:
    """把片段列表渲染成 SRT 字幕文本。"""
    lines: List[str] = []
    for idx, seg in enumerate(segments, start=1):
        text = (seg.text or "").strip()
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
    """
    if not download_root:
        return ""
    root = Path(download_root)
    if not root.is_dir():
        return ""

    def _valid(snap: Path) -> bool:
        return (snap / "model.bin").is_file() and (snap / "config.json").is_file()

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
    return str(exc)


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

        self._model = None
        self._actual_device = ""
        self._actual_compute = ""
        self._load_lock = threading.Lock()
        self._load_error = ""
        self._load_failed = False   # 终态失败：避免每个视频都重试一次（含网络请求）
        self._local_model_path = ""

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

    # ---------- 模型加载 ----------
    def load(self) -> bool:
        """惰性加载模型；成功返回 True。线程安全。"""
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
        """
        result = TranscriptResult(model=self.model_name)
        if not self.load():
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
                texts: List[str] = []
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
                    texts.append(text)
                holder["segments"] = collected
                holder["text"] = "".join(texts)
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
