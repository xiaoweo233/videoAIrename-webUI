"""core/engine.py — RenameEngine：五阶段并发流水线（方案 §6）。

流水线
------
[① 探测] ffprobe：时长 / 音轨 / creation_time / 关键帧时间
    ├──► [②a 抽帧] 线程池并发 N 路
    └──► [②b 转写] Whisper 单路串行（GPU 独占）
[③ 合并] merge_map：视觉 + 字幕都到齐才放行
[④ AI 分析] 多模态模型 → title / plot / tags
[⑤ 落盘] ExifTool 写元数据 → 重命名 → 时间戳还原 → NFO / SRT

红线：
  G3  禁止 import UI 库
  G4  工作线程不直接操作 UI，一律走 EventBus
  G6  可选依赖缺失必须优雅降级（无 Whisper 时仍能纯画面分析）
"""
from __future__ import annotations

import os
import queue
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..infra.config import AppConfig
from ..infra.paths import TMP_DIR
from ..infra.tools import ToolPaths, resolve_tools
from . import media as media_mod
from . import metadata as meta_mod
from . import rename as rename_mod
from .ai import AIClient
from .base import EventBus, JobStats, Phase, StopToken
from .whisper import TranscriptResult, WhisperEngine

__all__ = ["RenameEngine", "EngineOptions", "TaskSummary"]


@dataclass
class EngineOptions:
    """一次运行的参数（由配置 + 请求参数合成）。"""

    paths: List[str] = field(default_factory=list)
    dry_run: bool = False
    recursive: bool = True
    task_id: str = ""
    # 并发配额（方案 §6.2）
    frame_workers: int = 8
    ai_workers: int = 1
    whisper_workers: int = 1


@dataclass
class TaskSummary:
    """任务结束统计。"""

    task_id: str = ""
    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    cancelled: int = 0
    elapsed_ms: int = 0
    avg_seconds: float = 0.0
    stopped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "taskId": self.task_id,
            "total": self.total,
            "stats": {
                "success": self.success, "skipped": self.skipped,
                "failed": self.failed, "cancelled": self.cancelled,
            },
            "elapsedMs": self.elapsed_ms,
            "avgSeconds": self.avg_seconds,
            "stopped": self.stopped,
        }


class RenameEngine:
    """五阶段流水线引擎（线程安全，单实例可复用多任务）。"""

    CIRCUIT_BREAKER_THRESHOLD = 5  # 连续失败阈值（方案 §6.3）

    def __init__(
        self,
        config: AppConfig,
        bus: EventBus,
        *,
        tool_paths: Optional[ToolPaths] = None,
        on_db_log=None,
        on_file_start=None,
        on_file_done=None,
        on_review=None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.tools = tool_paths or resolve_tools()
        self._on_db_log = on_db_log          # callable(level, message, file, task_id)
        self._on_file_start = on_file_start  # callable(task_id, path) -> file_id
        self._on_file_done = on_file_done    # callable(file_id, dict)
        self._on_review = on_review          # callable(dict) -> None

        self.stop_token = StopToken()
        self.stats = JobStats()
        self._ai_client: Optional[AIClient] = None
        self._whisper: Optional[WhisperEngine] = None
        self._ai_lock = threading.Lock()
        self._whisper_lock = threading.Lock()  # 保证转写单路串行

        self._consecutive_failures = 0
        self._circuit_lock = threading.Lock()
        self._context_warned = False
        self._warn_lock = threading.Lock()
        self._error_files: List[Tuple[str, str]] = []

        # 阶段③合并：视觉与字幕都到齐才放行
        self._merge_lock = threading.Lock()
        self._merge_registry: Dict[str, Dict[str, Any]] = {}

        self.task_id = ""
        self._started_at = 0.0

    # ==================================================================
    # 日志
    # ==================================================================
    def _log(self, level: str, message: str, file: str = "") -> None:
        self.bus.emit_log(level, message, file=file, source="engine")
        if self._on_db_log:
            try:
                self._on_db_log(level, message, file, self.task_id)
            except Exception:  # noqa: BLE001
                pass

    # ==================================================================
    # 惰性构造 AI / Whisper
    # ==================================================================
    def _get_ai(self) -> AIClient:
        with self._ai_lock:
            if self._ai_client is None:
                ai_cfg = self.config.ai
                self._ai_client = AIClient(
                    provider=ai_cfg.get("provider", "openai"),
                    base_url=ai_cfg.get("base_url", ""),
                    api_key=ai_cfg.get("api_key", ""),
                    model=ai_cfg.get("model", ""),
                    timeout=ai_cfg.get("timeout", 60),
                    retry_times=ai_cfg.get("retry_times", 2),
                    max_tokens=ai_cfg.get("max_tokens", 5000),
                    temperature=ai_cfg.get("temperature", 0.6),
                    top_p=ai_cfg.get("top_p", 0.8),
                    enforce_json_mode=ai_cfg.get("enforce_json_mode", False),
                    system_prompt=ai_cfg.get("system_prompt", ""),
                    prompt=ai_cfg.get("prompt", ""),
                    on_log=lambda lv, msg: self._log(lv, msg),
                )
            return self._ai_client

    def _get_whisper(self) -> WhisperEngine:
        with self._whisper_lock:
            if self._whisper is None:
                w_cfg = self.config.whisper
                from ..infra.paths import MODELS_DIR

                self._whisper = WhisperEngine(
                    model_name=w_cfg.get("model", "large-v3-turbo"),
                    device=w_cfg.get("device", "auto"),
                    compute_type=w_cfg.get("compute_type", "int8_float16"),
                    vad_filter=w_cfg.get("vad_filter", True),
                    language=w_cfg.get("language", "auto"),
                    use_gpu=bool(w_cfg.get("use_gpu", True)),
                    download_root=str(MODELS_DIR / "whisper"),
                    on_log=lambda lv, msg: self._log(lv, msg),
                )
            return self._whisper

    # ==================================================================
    # 统计 / 熔断
    # ==================================================================
    def _inc(self, key: str) -> None:
        self.stats.inc(key)

    def _record_failure(self, scenario: str = "处理失败") -> bool:
        with self._circuit_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
                self._log("ERROR", f"🚨 连续 {self.CIRCUIT_BREAKER_THRESHOLD} 次{scenario}，已自动停止处理")
                self.stop_token.set()
                return True
        return False

    def _record_success(self) -> None:
        with self._circuit_lock:
            self._consecutive_failures = 0

    def _warn_context_once(self, frames: int) -> None:
        with self._warn_lock:
            if self._context_warned:
                return
            self._context_warned = True
        self._log("WARN", "⚠️ 检测到上下文窗口溢出，建议：1) 降低关键帧数 "
                          f"(当前 {frames}) 2) 降低抽帧分辨率 "
                          f"(当前 {self.config.frames.get('max_side', 520)})")

    def _progress(self, phase: str, current_file: str = "") -> None:
        snap = self.stats.as_dict()
        self.bus.emit_progress(
            task_id=self.task_id,
            phase=phase,
            current_file=current_file,
            done=snap["done"],
            total=snap["total"],
            stats=snap["stats"],
            avg_seconds=snap["avgSeconds"],
        )

    # ==================================================================
    # 主流程
    # ==================================================================
    def run(self, options: EngineOptions) -> TaskSummary:
        """执行一次完整流水线（阻塞直至完成/停止）。"""
        self.task_id = options.task_id or f"t_{int(time.time() * 1000)}"
        self.stop_token.clear()
        self._started_at = time.time()
        self._consecutive_failures = 0
        self._context_warned = False
        self._error_files.clear()
        self.stats = JobStats()

        # ---------- 扫描 ----------
        self.bus.emit_phase(Phase.PROBE, "start")
        self._log("INFO", f"开始扫描：{len(options.paths)} 个入口路径（递归={'开' if options.recursive else '关'}）")
        videos = media_mod.collect_videos(options.paths, recursive=options.recursive)
        if not videos:
            self._log("WARN", "未找到可处理的视频文件")
            self.bus.emit_phase(Phase.PROBE, "done")
            return self._finish(stopped=False)

        # ---------- 跳过已处理 ----------
        # 打开（默认）：文件名含标记 **或** 已写入 ExifTool 软水印 → 直接跳过
        # 关闭：全部重新处理（同名 .nfo / .srt 由写入端直接覆盖）
        pre_skipped = 0
        naming = self.config.naming
        if naming.get("enable_skip"):
            videos, pre_skipped = self._filter_processed(videos, naming.get("marker", "AI"))
            for _ in range(pre_skipped):
                self._inc("skipped")

        total = len(videos)
        self.stats.total = total
        if total == 0:
            self._log("INFO", "所有视频均已处理，无需操作。")
            self.bus.emit_phase(Phase.PROBE, "done")
            return self._finish(stopped=False)

        self.bus.emit_task("started", self.task_id, total=total)
        self._log("INFO", f"待处理 {total} 个视频"
                          f"（共扫描到 {len(videos) + pre_skipped} 个，已跳过 {pre_skipped} 个）")
        self._log("INFO", f"并发配置：抽帧 {options.frame_workers} / 转写 {options.whisper_workers} / "
                          f"AI {options.ai_workers}{'（预览模式）' if options.dry_run else ''}")
        self._progress(Phase.PROBE)

        # ---------- ②a 抽帧生产者 + ④ AI 消费者 ----------
        task_queue: "queue.Queue[Optional[Tuple[str, Any, Any]]]" = queue.Queue(
            maxsize=max(1, options.ai_workers) * 4
        )
        frame_workers = max(1, min(options.frame_workers, total))

        producer = threading.Thread(
            target=self._frame_producer,
            args=(videos, task_queue, frame_workers),
            name="producer", daemon=True,
        )
        consumers = [
            threading.Thread(
                target=self._ai_consumer,
                args=(task_queue,),
                name=f"ai-{i}", daemon=True,
            )
            for i in range(max(1, options.ai_workers))
        ]

        producer.start()
        for c in consumers:
            c.start()
        for c in consumers:
            c.join()

        # ---------- 失败文件归集 ----------
        if self._error_files and self.config.output.get("move_failed") and not options.dry_run:
            moved = rename_mod.move_to_failed(self._error_files)
            for src, dest in moved:
                self._log("INFO", f"📦 失败文件已移至：{os.path.basename(os.path.dirname(dest))}/"
                                  f"{os.path.basename(dest)}", file=src)

        return self._finish(stopped=self.stop_token.is_set())

    def _finish(self, *, stopped: bool) -> TaskSummary:
        elapsed_ms = int((time.time() - self._started_at) * 1000)
        snap = self.stats.as_dict()
        summary = TaskSummary(
            task_id=self.task_id,
            total=snap["total"],
            success=snap["stats"]["success"],
            skipped=snap["stats"]["skipped"],
            failed=snap["stats"]["failed"],
            cancelled=snap["stats"]["cancelled"],
            elapsed_ms=elapsed_ms,
            avg_seconds=snap["avgSeconds"],
            stopped=stopped,
        )
        self._log("INFO", f"本批完成：成功 {summary.success} · 跳过 {summary.skipped} · "
                          f"失败 {summary.failed} · 耗时 {elapsed_ms / 1000:.0f}s")
        self.bus.emit_task("stopped" if stopped else "completed", self.task_id)
        self._progress(Phase.IDLE)
        return summary

    def stop(self) -> None:
        """请求停止（已完成文件保留）。"""
        self.stop_token.set()

    # ==================================================================
    # 跳过已处理
    # ==================================================================
    def _filter_processed(self, videos: List[str], marker: str) -> Tuple[List[str], int]:
        """筛掉「已处理」视频。

        判定「已处理」的两个条件（满足任一即跳过）：
          1) 文件名（不含扩展名）中含处理标记，如 `xxx_AI.mp4`；
          2) 视频内部已写入 ExifTool 软水印（Software=AIVideoRenameV1）。
        marker 为空时只按软水印判定。
        """
        import re as _re

        pattern = None
        if marker:
            pattern = _re.compile(r"(?:^|_)" + _re.escape(marker) + r"(?:_|$)")
        kept: List[str] = []
        skipped = 0
        for path in videos:
            stem = os.path.splitext(os.path.basename(path))[0]
            by_name = bool(pattern and pattern.search(stem))
            if by_name or meta_mod.has_watermark(self.tools, path):
                skipped += 1
                self._log("DEBUG",
                          f"⏭ 跳过（{'文件名标记' if by_name else '软水印'}）：{os.path.basename(path)}",
                          file=path)
                continue
            kept.append(path)
        if skipped:
            self._log("INFO", f"⏩ 跳过 {skipped} 个已处理视频（标记/水印：{marker or '仅水印'}）")
        return kept, skipped

    # ==================================================================
    # ②a 生产者：探测 + 抽帧（CPU 线程池并发）
    # ==================================================================
    def _frame_producer(
        self,
        videos: List[str],
        task_queue: "queue.Queue",
        frame_workers: int,
    ) -> None:
        try:
            frames_cfg = self.config.frames
            max_keyframes = int(frames_cfg.get("max_keyframes", 35))
            max_side = int(frames_cfg.get("max_side", 520))
            hwaccel = str(frames_cfg.get("hwaccel", "none"))
            max_pending = frame_workers * 4

            with ThreadPoolExecutor(max_workers=frame_workers, thread_name_prefix="frame") as pool:
                futures: Dict[Any, str] = {}
                for path in videos:
                    if self.stop_token.is_set():
                        break
                    fut = pool.submit(
                        media_mod.probe_and_extract,
                        self.tools, path,
                        max_keyframes=max_keyframes, max_side=max_side,
                        hwaccel=hwaccel, stop_token=self.stop_token,
                    )
                    futures[fut] = path
                    if len(futures) >= max_pending:
                        done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
                        for f in done:
                            path_ = futures.pop(f, None)
                            self._handle_frame_result(f, path_, task_queue)

                if futures:
                    if not self.stop_token.is_set():
                        done, _ = wait(list(futures.keys()))
                        for f in done:
                            path_ = futures.pop(f, None)
                            self._handle_frame_result(f, path_, task_queue)
                    else:
                        for f in futures:
                            f.cancel()
        except Exception as exc:  # noqa: BLE001
            self._log("ERROR", f"抽帧生产者异常：{exc}")
        finally:
            # 发送结束哨兵
            for _ in range(max(1, self.config.runtime.get("ai_workers", 1))):
                try:
                    task_queue.put(None, timeout=2.0)
                except queue.Full:
                    break

    def _handle_frame_result(self, future, path: Optional[str], task_queue: "queue.Queue") -> None:
        if path is None:
            return
        try:
            info, frames = future.result()
        except Exception as exc:  # noqa: BLE001
            info, frames = None, None
            self._log("ERROR", f"抽帧线程异常：{exc}", file=os.path.basename(path))

        # ②b 转写：有音轨且启用时，串行处理（GPU 独占）
        transcript: Optional[TranscriptResult] = None
        if (
            frames is not None
            and info is not None and info.has_audio
            and self.config.whisper.get("enable", True)
            and not self.stop_token.is_set()
        ):
            transcript = self._transcribe_one(path)

        self._enqueue(task_queue, (path, info, frames, transcript))

    # ==================================================================
    # ②b 转写（单路串行）
    # ==================================================================
    def _transcribe_one(self, video_path: str) -> TranscriptResult:
        name = os.path.basename(video_path)
        self.bus.emit_phase(Phase.TRANSCRIBE, "start", file=name)
        started = time.time()

        audio_path = str(TMP_DIR / f"audio_{os.getpid()}_{threading.get_ident()}.wav")
        try:
            from ..infra import tools as tool_mod

            ok = tool_mod.extract_audio(self.tools, video_path, audio_path)
            if not ok:
                result = TranscriptResult(skipped=True, error="音频抽取失败")
            else:
                engine = self._get_whisper()
                result = engine.transcribe(audio_path, stop_token=self.stop_token)
        finally:
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except OSError:
                pass

        elapsed = int((time.time() - started) * 1000)
        if result.skipped:
            self.bus.emit_phase(Phase.TRANSCRIBE, "skip", file=name, elapsed_ms=0)
            if result.error:
                self._log("DEBUG", f"跳过转写：{result.error}", file=name)
        else:
            self.bus.emit_phase(Phase.TRANSCRIBE, "done", file=name, elapsed_ms=elapsed)
            self._log("DEBUG", f"转写完成：字幕 {result.chars} 字", file=name)
        return result

    def _enqueue(self, task_queue: "queue.Queue", item) -> None:
        while not self.stop_token.is_set():
            try:
                task_queue.put(item, timeout=1.0)
                return
            except queue.Full:
                self.stop_token.wait(0.1)

    # ==================================================================
    # ④ AI 消费者
    # ==================================================================
    def _ai_consumer(self, task_queue: "queue.Queue") -> None:
        while not self.stop_token.is_set():
            try:
                item = task_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if item is None:
                    return
                self._process_one(item)
            except Exception as exc:  # noqa: BLE001
                self._log("ERROR", f"AI 消费者异常：{exc}")
            finally:
                try:
                    task_queue.task_done()
                except ValueError:
                    pass

    def _process_one(self, item) -> None:
        path, info, frames, transcript = item
        name = os.path.basename(path)
        started = time.time()

        file_id = None
        if self._on_file_start:
            try:
                file_id = self._on_file_start(self.task_id, path)
            except Exception:  # noqa: BLE001
                file_id = None

        # ---------- 探测结果落账 ----------
        if info is None or frames is None:
            self._finish_file(file_id, path, name, status="error",
                              error="抽帧失败", elapsed_ms=int((time.time() - started) * 1000))
            self._inc("failed")
            self._record_failure("抽帧失败")
            self._progress(Phase.AI, name)
            return
        if not frames.frames_b64:
            self._finish_file(file_id, path, name, status="error",
                              error=frames.error or "无有效帧",
                              info=info, elapsed_ms=int((time.time() - started) * 1000))
            self._inc("failed")
            self._record_failure("抽帧失败")
            self._progress(Phase.AI, name)
            return

        # ---------- ③ 合并 & ④ AI 分析 ----------
        self.bus.emit_phase(Phase.MERGE, "start", file=name)
        self.bus.emit_phase(Phase.MERGE, "done", file=name, elapsed_ms=0)

        self.bus.emit_phase(Phase.AI, "start", file=name)
        self._progress(Phase.AI, name)

        transcript_text = transcript.text if transcript and not transcript.skipped else ""
        client = self._get_ai()
        result = client.analyze(
            frames.frames_b64,
            video_name=name,
            duration=info.duration,
            transcript=transcript_text,
            stop_token=self.stop_token,
        )
        self.bus.emit_phase(Phase.AI, "done", file=name, elapsed_ms=result.elapsed_ms)

        if not result.ok:
            if result.context_error:
                self._warn_context_once(len(frames.frames_b64))
            self._log("ERROR", f"AI 分析失败：{name} —— {result.error}", file=name)
            self._finish_file(file_id, path, name, status="error",
                              error=result.error or "AI 分析失败",
                              info=info, frames=len(frames.frames_b64),
                              subtitle_chars=(transcript.chars if transcript else 0),
                              model=result.model or self.config.ai.get("model", ""),
                              elapsed_ms=int((time.time() - started) * 1000))
            self._inc("failed")
            self._record_failure("AI 分析失败")
            self._progress(Phase.AI, name)
            return

        # ---------- ⑤ 落盘 ----------
        self.bus.emit_phase(Phase.WRITE, "start", file=name)
        self._persist(file_id, path, info, frames, transcript, result, name,
                      started=started)
        self._record_success()
        self._progress(Phase.WRITE, name)

    # ==================================================================
    # ⑤ 落盘
    # ==================================================================
    def _persist(self, file_id, path, info, frames, transcript, result, name,
                 *, started: float) -> None:
        output_cfg = self.config.output
        naming_cfg = self.config.naming
        dry_run = bool(output_cfg.get("dry_run"))

        new_stem = rename_mod.build_new_stem(
            path, title=result.title,
            creation_time=info.creation_time,
            naming=naming_cfg,
        )

        stat_before = meta_mod.preserve_stat(path)
        new_path, status = rename_mod.rename_file(path, new_stem, dry_run=dry_run)
        new_name = os.path.basename(new_path)

        if status == "ok" and not dry_run:
            meta_mod.restore_timestamps(new_path, stat_before)
            if output_cfg.get("metadata", True):
                ok, err = meta_mod.write_video_metadata(
                    self.tools, new_path,
                    title=result.title, plot=result.plot, tags=result.tags,
                    duration=info.duration, original_name=name,
                    allow_remux=bool(output_cfg.get("remux_fragmented", True)),
                    on_log=lambda lv, msg: self._log(lv, msg, file=name),
                )
                if not ok and err:
                    self._log("WARN", f"元数据写入失败：{err}", file=name)
                # ExifTool 覆写会刷新 mtime（含重封装），写回改名前的原始时间戳
                meta_mod.restore_timestamps(new_path, stat_before)
            if output_cfg.get("nfo", True):
                meta_mod.write_nfo(
                    new_path, title=result.title, plot=result.plot,
                    tags=result.tags, duration=info.duration, original_name=name,
                )
            if output_cfg.get("srt", True) and transcript is not None and not transcript.skipped:
                meta_mod.write_srt(new_path, transcript)

        elapsed_ms = int((time.time() - started) * 1000)
        self.bus.emit_phase(Phase.WRITE, "done", file=name, elapsed_ms=elapsed_ms)

        if status == "ok":
            self._inc("success")
            self.stats.record_duration(elapsed_ms)
            action = "预览" if dry_run else "重命名"
            self._log("INFO", f"{action}：{name} → {new_name}", file=name)
        elif status == "skipped":
            self._inc("skipped")
            self._log("INFO", f"⏭️ 文件名未变化：{name}", file=name)
        else:
            self._inc("failed")
            self._error_files.append((path, name))
            self._log("ERROR", f"❌ 重命名失败：{name}", file=name)

        self.bus.emit_inference(
            file=new_name if status == "ok" else name,
            model=result.model or self.config.ai.get("model", ""),
            frames=len(frames.frames_b64),
            subtitle_chars=(transcript.chars if transcript else 0),
            elapsed_ms=elapsed_ms,
        )

        # 文件收尾后立即推进一次进度，让外壳（SSE / DB）实时拿到 done 增量
        self._progress(Phase.WRITE, name)

        if status == "ok" and not dry_run:
            review_item = self._build_review(name, new_name, result)
            self.bus.emit_review("add", item=review_item)
            if self._on_review:
                try:
                    self._on_review(review_item)
                except Exception:  # noqa: BLE001
                    pass

        self._finish_file(file_id, path, name, status=status, new_path=new_path,
                          new_name=new_name, info=info, frames=len(frames.frames_b64),
                          subtitle_chars=(transcript.chars if transcript else 0),
                          model=result.model or self.config.ai.get("model", ""),
                          elapsed_ms=elapsed_ms)

    def _build_review(self, old_name: str, new_name: str, result) -> Dict[str, Any]:
        import uuid

        # 置信度：AI 成功即高；标签越多表示理解越充分
        confidence = 0.9 if result.tags else 0.8
        return {
            "id": f"r_{uuid.uuid4().hex[:10]}",
            "taskId": self.task_id,
            "oldName": old_name,
            "newName": new_name,
            "confidence": round(confidence, 2),
            "tags": result.tags or [],
            "file": old_name,
        }

    def _finish_file(
        self, file_id, path, name, *, status: str, error: str = "",
        new_path: str = "", new_name: str = "", info=None,
        frames: int = 0, subtitle_chars: int = 0, model: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        if not self._on_file_done:
            return
        payload = {
            "status": status, "error": error,
            "new_path": new_path, "new_name": new_name,
            "duration": getattr(info, "duration", 0.0) if info else 0.0,
            "has_audio": bool(getattr(info, "has_audio", False)) if info else False,
            "resolution": getattr(info, "resolution", "") if info else "",
            "frames": frames, "subtitle_chars": subtitle_chars,
            "model": model, "elapsed_ms": elapsed_ms,
        }
        try:
            self._on_file_done(file_id, payload)
        except Exception:  # noqa: BLE001
            pass

    # ==================================================================
    # 资源释放
    # ==================================================================
    def close(self) -> None:
        if self._ai_client is not None:
            self._ai_client.close()
            self._ai_client = None
