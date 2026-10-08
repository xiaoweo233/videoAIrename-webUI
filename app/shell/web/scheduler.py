"""shell/web/scheduler.py — 任务调度器（单任务串行）。

职责：
  - 接收「创建任务」请求 → 落库 → 在后台线程跑引擎
  - 把引擎回调翻译成 DB 写入（任务/文件/日志/审批）
  - 管理停止语义（StopToken）
  - 单例，进程内只允许一个任务在跑（与桌面壳一致，避免 GPU 争抢）
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from ...core.base import EventBus, Phase
from ...core.engine import EngineOptions, RenameEngine, TaskSummary
from ...db import repo
from ...infra.config import AppConfig
from ...infra.tools import resolve_tools
from .sse import SSEBroker

__all__ = ["TaskScheduler", "TaskConflictError"]


class TaskConflictError(RuntimeError):
    """已有任务在跑。"""


class TaskScheduler:
    """进程内单任务调度器。"""

    def __init__(self, config: AppConfig, bus: EventBus, broker: SSEBroker) -> None:
        self.config = config
        self.bus = bus
        self.broker = broker
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._engine: Optional[RenameEngine] = None
        self._current_task_id: str = ""
        self._running = False
        self._last_summary: Optional[TaskSummary] = None
        # 运行期进度回写节流状态（避免高频 emit_progress 打爆 sqlite）
        self._last_persist: Dict[str, int] = {"done": -1, "total": -1}
        # 订阅事件总线：把引擎运行期的 total/done/phase 持续回写 DB，
        # 使 GET /api/tasks/{id} 在任务进行中也能返回真实进度（红线 G4：只订阅、不反向依赖）。
        self._unsubscribe = bus.subscribe(self._on_bus_event)

    def _on_bus_event(self, event: Dict[str, Any]) -> None:
        """把 task/progress 事件中的进度同步到 tasks 表（节流 + 容错）。"""
        etype = event.get("type")
        if etype not in ("task", "progress"):
            return
        tid = event.get("taskId") or self.current_task_id
        if not tid:
            return
        try:
            if etype == "task":
                if event.get("action") == "started" and event.get("total") is not None:
                    total = int(event.get("total") or 0)
                    if total != self._last_persist.get("total"):
                        self._last_persist["total"] = total
                        repo.update_task(tid, total=total)
                return
            # progress：done/total 有变化才写，降低写放大
            done = int(event.get("done") or 0)
            total = int(event.get("total") or 0)
            if done == self._last_persist.get("done") and total == self._last_persist.get("total"):
                return
            self._last_persist["done"] = done
            self._last_persist["total"] = total
            stats = event.get("stats") or {}
            repo.update_task(
                tid,
                total=total,
                done=done,
                phase=str(event.get("phase") or ""),
                current_file=event.get("currentFile") or "",
                success=int(stats.get("success") or 0),
                skipped=int(stats.get("skipped") or 0),
                failed=int(stats.get("failed") or 0),
                cancelled=int(stats.get("cancelled") or 0),
            )
        except Exception:  # noqa: BLE001 - 进度回写失败不影响引擎
            pass

    # ==================================================================
    # 状态
    # ==================================================================
    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def current_task_id(self) -> str:
        with self._lock:
            return self._current_task_id

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "taskId": self._current_task_id,
                "lastSummary": self._last_summary.to_dict() if self._last_summary else None,
                "sseClients": self.broker.connection_count,
            }

    # ==================================================================
    # 启动 / 停止
    # ==================================================================
    def start(self, paths: List[str], *, dry_run: Optional[bool] = None,
              recursive: Optional[bool] = None, task_id: Optional[str] = None) -> Dict[str, Any]:
        """创建并启动一个任务。已有任务运行时抛 TaskConflictError。"""
        with self._lock:
            if self._running:
                raise TaskConflictError("已有任务正在运行，请先停止或等待完成")
            self._running = True
            self._current_task_id = task_id or f"t_{uuid.uuid4().hex[:12]}"
            current_id = self._current_task_id

        cfg = self.config
        output_cfg = cfg.output
        effective_dry = bool(output_cfg.get("dry_run")) if dry_run is None else bool(dry_run)
        effective_recursive = (
            bool(cfg.data.get("input", {}).get("recursive", True))
            if recursive is None else bool(recursive)
        )

        # ---------- 落库 ----------
        task = repo.create_task(
            root_path=os.pathsep.join(paths),
            total=0,
            dry_run=effective_dry,
            config_snapshot=cfg.as_dict(),
            task_id=current_id,
        )
        repo.update_task(current_id, status="running", started_at=_now_ms(),
                         phase=Phase.PROBE)

        options = EngineOptions(
            paths=list(paths),
            dry_run=effective_dry,
            recursive=effective_recursive,
            task_id=current_id,
            frame_workers=max(1, int(cfg.frames.get("workers", 8))),
            ai_workers=max(1, int(cfg.runtime.get("ai_workers", 1))),
            whisper_workers=max(1, int(cfg.whisper.get("workers", 1))),
        )

        engine = RenameEngine(
            cfg, self.bus,
            tool_paths=resolve_tools(),
            on_db_log=self._on_db_log,
            on_file_start=self._on_file_start,
            on_file_done=self._on_file_done,
            on_review=self._on_review,
        )
        with self._lock:
            self._engine = engine

        thread = threading.Thread(
            target=self._run_engine, args=(engine, options),
            name=f"engine-{current_id}", daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()

        return {"taskId": current_id, "dryRun": effective_dry,
                "recursive": effective_recursive, "task": task}

    def _run_engine(self, engine: RenameEngine, options: EngineOptions) -> None:
        summary: Optional[TaskSummary] = None
        status = "completed"
        error = ""
        try:
            summary = engine.run(options)
            status = "stopped" if summary.stopped else "completed"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            error = str(exc)
            self.bus.emit_log("ERROR", f"任务异常终止：{exc}", source="scheduler")
            self.bus.emit_task("stopped", options.task_id)
        finally:
            with self._lock:
                self._running = False
                self._last_summary = summary
                self._engine = None
                self._thread = None
            repo.finish_task(options.task_id, status=status, error=error)
            if summary is not None:
                repo.update_task(
                    options.task_id,
                    success=summary.success, skipped=summary.skipped,
                    failed=summary.failed, cancelled=summary.cancelled,
                    avg_ms=summary.avg_seconds * 1000,
                    done=summary.success + summary.skipped + summary.failed + summary.cancelled,
                    phase=Phase.IDLE,
                )

    def stop(self, task_id: Optional[str] = None) -> Dict[str, Any]:
        """请求停止当前任务。"""
        with self._lock:
            engine = self._engine
            current = self._current_task_id
            running = self._running
        if not running or engine is None:
            return {"stopped": False, "reason": "当前没有正在运行的任务"}
        if task_id and task_id not in ("current", current):
            return {"stopped": False, "reason": f"任务 {task_id} 不是当前运行任务"}
        engine.stop()
        self.bus.emit_log("INFO", "已收到停止指令，正在收尾（已完成文件将保留）", source="scheduler")
        return {"stopped": True, "taskId": current}

    # ==================================================================
    # 引擎回调 → DB
    # ==================================================================
    def _on_db_log(self, level: str, message: str, file: str, task_id: str) -> None:
        try:
            repo.add_log(level=level, message=message, file=file,
                         source="engine", task_id=task_id or self.current_task_id)
        except Exception:  # noqa: BLE001 - 日志落库失败不致命
            pass

    def _on_file_start(self, task_id: str, path: str) -> int:
        """文件开始处理：落库返回 file_id（供后续更新）。"""
        try:
            return repo.add_file(
                task_id, old_path=path, old_name=os.path.basename(path),
                status="running",
            )
        except Exception:  # noqa: BLE001
            return 0

    def _on_file_done(self, file_id: int, payload: Dict[str, Any]) -> None:
        if not file_id:
            return
        try:
            repo.update_file(
                file_id,
                status=payload.get("status", ""),
                new_path=payload.get("new_path", ""),
                new_name=payload.get("new_name", ""),
                duration=payload.get("duration", 0.0),
                has_audio=payload.get("has_audio", False),
                resolution=payload.get("resolution", ""),
                frames=payload.get("frames", 0),
                subtitle_chars=payload.get("subtitle_chars", 0),
                model=payload.get("model", ""),
                elapsed_ms=payload.get("elapsed_ms", 0),
                error=payload.get("error", ""),
            )
        except Exception:  # noqa: BLE001
            pass

    def _on_review(self, item: Dict[str, Any]) -> None:
        try:
            repo.upsert_review(
                item_id=item["id"],
                old_name=item.get("oldName", ""),
                new_name=item.get("newName", ""),
                confidence=item.get("confidence", 0.85),
                tags=item.get("tags", []),
                file=item.get("file", ""),
                task_id=item.get("taskId", ""),
            )
        except Exception:  # noqa: BLE001
            pass


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
