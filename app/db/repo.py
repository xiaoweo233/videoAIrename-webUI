"""db/repo.py — 仓储函数（所有 CRUD 集中于此，供 API 与引擎调用）。

约定：
  - 所有函数都在自己的 session_scope 内完成，调用方无需管理 Session。
  - 返回纯 Python 对象/dict，不把 ORM 实例泄漏到上层（避免 detached 问题）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select, update

from .models import LogEntry, ReviewItem, Task, TaskFile, now_ms
from .session import session_scope

__all__ = [
    "new_id", "create_task", "get_task", "update_task", "finish_task",
    "list_tasks", "task_to_dict",
    "add_file", "update_file", "list_files", "file_to_dict",
    "add_log", "list_logs", "clear_logs",
    "upsert_review", "decide_review", "list_reviews", "get_review",
    "recover_orphan_tasks", "rename_map",
]


def new_id(prefix: str = "t") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ==================================================================
# Task
# ==================================================================
def create_task(
    *,
    root_path: str = "",
    total: int = 0,
    dry_run: bool = False,
    config_snapshot: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    tid = task_id or new_id("t")
    with session_scope() as s:
        task = Task(
            id=tid,
            root_path=root_path,
            total=total,
            dry_run=dry_run,
            status="pending",
            config_snapshot=json.dumps(config_snapshot or {}, ensure_ascii=False),
            created_at=now_ms(),
        )
        s.add(task)
        s.flush()
        return task_to_dict(task)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as s:
        task = s.get(Task, task_id)
        return task_to_dict(task) if task else None


def update_task(task_id: str, **fields: Any) -> None:
    """更新任务字段（忽略 None 与未知字段）。"""
    allowed = {
        "status", "phase", "current_file", "total", "done",
        "success", "skipped", "failed", "cancelled",
        "avg_ms", "started_at", "finished_at", "error", "root_path", "dry_run",
    }
    payload = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not payload:
        return
    with session_scope() as s:
        s.execute(update(Task).where(Task.id == task_id).values(**payload))


def finish_task(task_id: str, status: str = "completed", error: str = "") -> None:
    with session_scope() as s:
        s.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status=status, finished_at=now_ms(), error=error or "")
        )


def list_tasks(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(
            select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return [task_to_dict(t) for t in rows]


def task_to_dict(task: Optional[Task]) -> Dict[str, Any]:
    if task is None:
        return {}
    try:
        snapshot = json.loads(task.config_snapshot or "{}")
    except ValueError:
        snapshot = {}
    return {
        "id": task.id,
        "rootPath": task.root_path,
        "status": task.status,
        "phase": task.phase,
        "currentFile": task.current_file,
        "total": task.total,
        "done": task.done,
        "stats": task.stats,
        "avgSeconds": task.avg_seconds,
        "dryRun": bool(task.dry_run),
        "createdAt": task.created_at,
        "startedAt": task.started_at,
        "finishedAt": task.finished_at,
        "error": task.error,
        "configSnapshot": snapshot,
    }


# ==================================================================
# TaskFile
# ==================================================================
def add_file(
    task_id: str,
    *,
    old_path: str = "",
    old_name: str = "",
    status: str = "pending",
    duration: float = 0.0,
    has_audio: bool = False,
    resolution: str = "",
    frames: int = 0,
    subtitle_chars: int = 0,
    model: str = "",
    elapsed_ms: int = 0,
    new_path: str = "",
    new_name: str = "",
    error: str = "",
) -> int:
    with session_scope() as s:
        row = TaskFile(
            task_id=task_id,
            old_path=old_path,
            old_name=old_name,
            status=status,
            duration=duration,
            has_audio=has_audio,
            resolution=resolution,
            frames=frames,
            subtitle_chars=subtitle_chars,
            model=model,
            elapsed_ms=elapsed_ms,
            new_path=new_path,
            new_name=new_name,
            error=error,
            created_at=now_ms(),
        )
        s.add(row)
        s.flush()
        return int(row.id)


def update_file(file_id: int, **fields: Any) -> None:
    allowed = {
        "status", "new_path", "new_name", "duration", "has_audio", "resolution",
        "frames", "subtitle_chars", "model", "elapsed_ms", "error", "rolled_back",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return
    with session_scope() as s:
        s.execute(update(TaskFile).where(TaskFile.id == file_id).values(**payload))


def list_files(task_id: str, limit: int = 1000, offset: int = 0) -> List[Dict[str, Any]]:
    with session_scope() as s:
        rows = s.scalars(
            select(TaskFile)
            .where(TaskFile.task_id == task_id)
            .order_by(TaskFile.id.asc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [file_to_dict(r) for r in rows]


def file_to_dict(row: Optional[TaskFile]) -> Dict[str, Any]:
    if row is None:
        return {}
    return {
        "id": row.id,
        "taskId": row.task_id,
        "oldPath": row.old_path,
        "oldName": row.old_name,
        "newPath": row.new_path,
        "newName": row.new_name,
        "status": row.status,
        "duration": row.duration,
        "hasAudio": bool(row.has_audio),
        "resolution": row.resolution,
        "frames": row.frames,
        "subtitleChars": row.subtitle_chars,
        "model": row.model,
        "elapsedMs": row.elapsed_ms,
        "error": row.error,
        "rolledBack": bool(row.rolled_back),
        "createdAt": row.created_at,
    }


# ==================================================================
# LogEntry
# ==================================================================
def add_log(
    *,
    level: str = "INFO",
    message: str = "",
    file: str = "",
    source: str = "",
    task_id: str = "",
    ts: Optional[int] = None,
) -> None:
    with session_scope() as s:
        s.add(LogEntry(
            task_id=task_id,
            ts=ts or now_ms(),
            level=level.upper(),
            message=message,
            file=file,
            source=source,
        ))


def list_logs(
    *,
    limit: int = 500,
    offset: int = 0,
    level: Optional[str] = None,
    task_id: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    stmt = select(LogEntry)
    count_stmt = select(func.count(LogEntry.id))
    if level:
        stmt = stmt.where(LogEntry.level == level.upper())
        count_stmt = count_stmt.where(LogEntry.level == level.upper())
    if task_id:
        stmt = stmt.where(LogEntry.task_id == task_id)
        count_stmt = count_stmt.where(LogEntry.task_id == task_id)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(LogEntry.message.like(like))
        count_stmt = count_stmt.where(LogEntry.message.like(like))

    with session_scope() as s:
        total = int(s.scalar(count_stmt) or 0)
        rows = s.scalars(
            stmt.order_by(LogEntry.ts.desc()).limit(limit).offset(offset)
        ).all()
        items = [
            {
                "id": r.id,
                "ts": r.ts,
                "level": r.level,
                "message": r.message,
                "file": r.file,
                "source": r.source,
                "taskId": r.task_id,
            }
            for r in rows
        ]
    # 返回时按时间正序，便于前端直接追加
    items.reverse()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def clear_logs(task_id: Optional[str] = None) -> int:
    with session_scope() as s:
        stmt = delete(LogEntry)
        if task_id:
            stmt = stmt.where(LogEntry.task_id == task_id)
        result = s.execute(stmt)
        return int(result.rowcount or 0)


# ==================================================================
# ReviewItem
# ==================================================================
def upsert_review(
    *,
    item_id: str,
    old_name: str = "",
    new_name: str = "",
    confidence: float = 0.85,
    tags: Optional[List[str]] = None,
    file: str = "",
    task_id: str = "",
) -> Dict[str, Any]:
    with session_scope() as s:
        row = s.get(ReviewItem, item_id)
        if row is None:
            row = ReviewItem(
                id=item_id,
                task_id=task_id,
                old_name=old_name,
                new_name=new_name,
                confidence=confidence,
                tags=json.dumps(tags or [], ensure_ascii=False),
                file=file,
                status="pending",
                ts=now_ms(),
            )
            s.add(row)
        else:
            row.old_name = old_name or row.old_name
            row.new_name = new_name or row.new_name
            row.confidence = confidence
            row.tags = json.dumps(tags if tags is not None else json.loads(row.tags or "[]"),
                                  ensure_ascii=False)
            row.file = file or row.file
        s.flush()
        return review_to_dict(row)


def get_review(item_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as s:
        row = s.get(ReviewItem, item_id)
        return review_to_dict(row) if row else None


def decide_review(
    item_id: str,
    decision: str,
    new_name: Optional[str] = None,
    *,
    apply_rename: bool = False,
) -> Optional[Dict[str, Any]]:
    """记录审批决定。

    apply_rename=True 时，若用户编辑了新名字，则真正执行改名（供后端审批闭环）。
    """
    status_map = {"accept": "accepted", "ignore": "ignored", "edit": "edited"}
    with session_scope() as s:
        row = s.get(ReviewItem, item_id)
        if row is None:
            return None
        row.status = status_map.get(decision, row.status)
        if decision == "edit" and new_name:
            row.new_name = new_name
        s.flush()
        result = review_to_dict(row)

    if apply_rename and decision == "edit" and new_name:
        # 实际改名交由引擎层执行；此处仅落库，避免数据层做文件 IO
        pass
    return result


def list_reviews(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    with session_scope() as s:
        stmt = select(ReviewItem).order_by(ReviewItem.ts.asc()).limit(limit)
        if status:
            stmt = select(ReviewItem).where(ReviewItem.status == status)\
                .order_by(ReviewItem.ts.asc()).limit(limit)
        rows = s.scalars(stmt).all()
        return [review_to_dict(r) for r in rows]


def review_to_dict(row: Optional[ReviewItem]) -> Dict[str, Any]:
    if row is None:
        return {}
    try:
        tags = json.loads(row.tags or "[]")
    except ValueError:
        tags = []
    return {
        "id": row.id,
        "taskId": row.task_id,
        "oldName": row.old_name,
        "newName": row.new_name,
        "confidence": row.confidence,
        "tags": tags if isinstance(tags, list) else [],
        "file": row.file,
        "status": row.status,
        "ts": row.ts,
    }


# ==================================================================
# 恢复 / 映射
# ==================================================================
def recover_orphan_tasks() -> int:
    """崩溃恢复：把上次未正常结束的任务标记为 interrupted。"""
    with session_scope() as s:
        result = s.execute(
            update(Task)
            .where(Task.status.in_(["running", "pending"]))
            .values(status="interrupted", finished_at=now_ms())
        )
        return int(result.rowcount or 0)


def rename_map(task_id: Optional[str] = None) -> Dict[str, str]:
    """导出 旧路径 → 新路径 映射（一键回滚用，方案 §13）。"""
    with session_scope() as s:
        stmt = select(TaskFile).where(
            TaskFile.status == "ok", TaskFile.rolled_back.is_(False)
        )
        if task_id:
            stmt = stmt.where(TaskFile.task_id == task_id)
        rows = s.scalars(stmt).all()
        return {r.old_path: r.new_path for r in rows if r.old_path and r.new_path}
