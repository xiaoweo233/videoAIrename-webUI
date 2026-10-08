"""core/whisper.py — WhisperEngine：懒加载 / GPU→CPU 回退 / 看门狗 / to_srt（阶段②b）。

要点（方案 §6.2 / §10 / §13）：
  - GPU 独占：whisper_workers=1，单路串行队列，多路反而互相抢占显存。
  - 懒加载：首次使用时才载入模型，避免拖慢启动。
  - GPU→CPU 回退：缺 cublas64_12.dll 等 CUDA 库时自动降级 CPU（红线 G6）。
  - 看门狗：推理长时间无输出时判定挂起并中止。
  - 无声视频跳过转写直接放行（由 engine 判定）。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

__all__ = ["TranscriptResult", "WhisperEngine", "segments_to_srt", "is_available"]


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
        with self._load_lock:
            if self._model is not None:
                return True
            if not is_available():
                self._load_error = "faster-whisper 未安装"
                self._log("WARN", "Whisper 不可用：faster-whisper 未安装，已降级为纯画面分析")
                return False

            device, compute = self._resolve_device()
            try:
                from faster_whisper import WhisperModel  # type: ignore

                self._log("INFO", f"正在加载 Whisper 模型 {self.model_name}（device={device}, compute={compute}）...")
                self._model = WhisperModel(
                    self.model_name,
                    device=device,
                    compute_type=compute,
                    download_root=self.download_root,
                )
                self._actual_device = device
                self._actual_compute = compute
                self._log("INFO", f"Whisper 就绪：{self.model_name} · {device} · {compute}")
                return True
            except Exception as exc:  # noqa: BLE001 - 模型加载失败不致命
                self._load_error = str(exc)
                self._log("WARN", f"Whisper 模型加载失败（{device}）：{exc}")
                # GPU 失败时再试一次 CPU
                if device == "cuda":
                    try:
                        from faster_whisper import WhisperModel  # type: ignore
                        self._log("INFO", "尝试回退 CPU 加载 Whisper 模型 ...")
                        self._model = WhisperModel(
                            self.model_name, device="cpu", compute_type="int8",
                            download_root=self.download_root,
                        )
                        self._actual_device = "cpu"
                        self._actual_compute = "int8"
                        self._log("INFO", "Whisper 已回退 CPU 加载成功")
                        return True
                    except Exception as exc2:  # noqa: BLE001
                        self._load_error = str(exc2)
                        self._log("ERROR", f"Whisper CPU 回退也失败：{exc2}")
                return False

    @property
    def device(self) -> str:
        return self._actual_device or (self.requested_device or "auto")

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
