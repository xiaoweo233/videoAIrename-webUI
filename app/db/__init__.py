"""db —— 数据层（SQLite + SQLAlchemy ORM + 仓储）。

表结构：
  Task       一次批量任务（对齐前端 SSE 的 taskId / total / stats）
  TaskFile   任务下单个视频的处理记录（状态 / 改名前后 / 耗时 / 推理指标）
  LogEntry   运行日志（分级，供 GET /api/logs 与日志页回看）
  ReviewItem 命名审批条目（供 GET 审批列表与 POST /api/review/{id}）
"""
from .models import Base, Task, TaskFile, LogEntry, ReviewItem
from .session import init_db, session_scope, get_engine, dispose_engine
from . import repo

__all__ = [
    "Base", "Task", "TaskFile", "LogEntry", "ReviewItem",
    "init_db", "session_scope", "get_engine", "dispose_engine", "repo",
]
