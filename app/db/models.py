"""db/models.py — SQLAlchemy ORM 模型。

用 SQLite 单文件库（APP_ROOT/data/vair.db），无需外部数据库即可离线运行。
所有时间戳统一用 Unix 毫秒（与前端事件契约的 ts 字段一致）。
"""
from __future__ import annotations

import time

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, String, Text, BigInteger
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = ["Base", "Task", "TaskFile", "LogEntry", "ReviewItem", "now_ms"]


def now_ms() -> int:
    return int(time.time() * 1000)


class Base(DeclarativeBase):
    """ORM 基类。"""


class Task(Base):
    """一次批量处理任务。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    root_path: Mapped[str] = mapped_column(Text, default="")          # 用户指定的入口路径
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|completed|stopped|failed
    phase: Mapped[str] = mapped_column(String(8), default="0")
    current_file: Mapped[str] = mapped_column(Text, default="")
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)

    success: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)

    avg_ms: Mapped[float] = mapped_column(Float, default=0.0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    started_at: Mapped[int] = mapped_column(BigInteger, default=0)
    finished_at: Mapped[int] = mapped_column(BigInteger, default=0)

    # 任务启动时的配置快照（JSON 字符串），便于复现
    config_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")

    files: Mapped[list["TaskFile"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("ix_tasks_status", "status"),)

    @property
    def stats(self) -> dict:
        return {
            "success": self.success,
            "skipped": self.skipped,
            "failed": self.failed,
            "cancelled": self.cancelled,
        }

    @property
    def avg_seconds(self) -> float:
        return round(self.avg_ms / 1000.0, 1) if self.avg_ms else 0.0


class TaskFile(Base):
    """任务下单个视频的处理记录。

    同时充当「改名映射表」：old_path → new_path，支持一键回滚（方案 §13）。
    """

    __tablename__ = "task_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)

    old_path: Mapped[str] = mapped_column(Text, default="")
    old_name: Mapped[str] = mapped_column(Text, default="")
    new_path: Mapped[str] = mapped_column(Text, default="")
    new_name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # ok|skipped|error|cancelled|skipped_processed

    duration: Mapped[float] = mapped_column(Float, default=0.0)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution: Mapped[str] = mapped_column(String(16), default="")
    frames: Mapped[int] = mapped_column(Integer, default=0)
    subtitle_chars: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(128), default="")
    elapsed_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    rolled_back: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)

    task: Mapped[Task] = relationship(back_populates="files")

    __table_args__ = (
        Index("ix_task_files_task_status", "task_id", "status"),
    )


class LogEntry(Base):
    """运行日志条目（分级），供 GET /api/logs 与日志页回看。"""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(48), default="", index=True)
    ts: Mapped[int] = mapped_column(BigInteger, default=now_ms)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    message: Mapped[str] = mapped_column(Text, default="")
    file: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(32), default="")

    __table_args__ = (
        Index("ix_log_entries_ts", "ts"),
        Index("ix_log_entries_level", "level"),
    )


class ReviewItem(Base):
    """命名审批条目（逐条采纳 / 编辑 / 忽略）。"""

    __tablename__ = "review_items"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(48), default="", index=True)
    old_name: Mapped[str] = mapped_column(Text, default="")
    new_name: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.85)
    tags: Mapped[str] = mapped_column(Text, default="[]")   # JSON 数组
    file: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|accepted|ignored|edited
    ts: Mapped[int] = mapped_column(BigInteger, default=now_ms)

    __table_args__ = (Index("ix_review_status", "status"),)
