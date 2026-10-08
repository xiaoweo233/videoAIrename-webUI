"""db/session.py — 引擎与 Session 管理（SQLite）。

要点：
  - SQLite 允许跨线程使用同一引擎，但 Session 不线程安全；
    本模块用 session_scope() 上下文管理器保证每次操作独立 Session。
  - check_same_thread=False + WAL 模式，兼顾 FastAPI 多线程与写入并发。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..infra import paths
from .models import Base

__all__ = ["init_db", "session_scope", "get_engine", "dispose_engine", "get_session_factory"]

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None
_lock = threading.Lock()


def _build_url(db_path: "Path | str") -> str:
    return f"sqlite:///{Path(db_path).as_posix()}"


def init_db(db_path: "Path | str | None" = None) -> Engine:
    """初始化数据库引擎并建表（幂等，可重复调用）。"""
    global _engine, _SessionFactory
    with _lock:
        if _engine is not None:
            return _engine

        target = Path(db_path) if db_path else paths.DB_FILE
        target.parent.mkdir(parents=True, exist_ok=True)

        # 用 QueuePool（默认）而非 StaticPool：
        # StaticPool 只维护单一共享连接，当引擎线程长时间持有连接时，
        # API 线程的读写会被串行阻塞（本应用实测会卡死）。
        # check_same_thread=False 允许跨线程复用连接（SQLAlchemy 内部有借用机制）。
        engine = create_engine(
            _build_url(target),
            echo=False,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=1800,
        )

        # SQLite 调优：WAL 提升并发读写；外键约束；忙等超时
        @event.listens_for(engine, "connect")
        def _set_pragma(dbapi_conn, _record):  # noqa: ANN001
            try:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
            except Exception:  # noqa: BLE001 - PRAGMA 失败不应阻断
                pass

        Base.metadata.create_all(engine)
        _engine = engine
        _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        return engine


def get_engine() -> Engine:
    if _engine is None:
        return init_db()
    return _engine


def get_session_factory() -> sessionmaker:
    if _SessionFactory is None:
        init_db()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务作用域：正常提交，异常回滚，最后关闭。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """释放引擎（进程退出 / 测试用）。"""
    global _engine, _SessionFactory
    with _lock:
        if _engine is not None:
            try:
                _engine.dispose()
            except Exception:  # noqa: BLE001
                pass
        _engine = None
        _SessionFactory = None
