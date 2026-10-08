"""shell/web/sse.py — 事件桥：线程侧 EventBus → asyncio 侧 SSE 队列。

问题：引擎在 worker 线程里 publish 事件，而 SSE 生成器跑在 asyncio 事件循环里。
方案：每个 SSE 连接注册一个线程安全 deque + asyncio.Event 唤醒，
     publish 时把事件塞入所有连接队列并 set 事件；生成器 await 事件后批量取走。

同时维护一个「最近事件环形缓冲」，新连接可立即回放最后 N 条（含当前任务状态），
避免刷新页面后进度/日志空白。
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

__all__ = ["SSEBroker", "SSEConnection"]

# 回放缓冲上限
_REPLAY_LIMIT = 200


class SSEConnection:
    """单个 SSE 订阅者。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, replay: List[Dict[str, Any]]) -> None:
        self._loop = loop
        self._queue: Deque[Dict[str, Any]] = deque(replay)
        self._event = asyncio.Event()
        self._closed = False
        self._lock = threading.Lock()

    def push(self, data: Dict[str, Any]) -> None:
        """线程侧调用：入队并唤醒 asyncio 侧。"""
        if self._closed:
            return
        with self._lock:
            self._queue.append(data)
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # 事件循环已关闭
            self._closed = True

    async def drain(self) -> List[Dict[str, Any]]:
        """等待并取走所有待发事件。"""
        await self._event.wait()
        self._event.clear()
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    def close(self) -> None:
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


class SSEBroker:
    """事件总线与 SSE 连接之间的桥。"""

    def __init__(self) -> None:
        self._connections: List[SSEConnection] = []
        self._lock = threading.Lock()
        self._replay: Deque[Dict[str, Any]] = deque(maxlen=_REPLAY_LIMIT)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在 FastAPI startup 时绑定主事件循环。"""
        self._loop = loop

    # ---------- 由 EventBus 订阅调用（线程侧）----------
    def on_event(self, data: Dict[str, Any]) -> None:
        # 只回放「有状态意义」的事件，避免新连接收到大量历史日志造成闪烁
        etype = data.get("type")
        if etype in ("task", "progress", "phase", "status", "inference", "review"):
            self._replay.append(data)
        elif etype == "log":
            self._replay.append(data)

        with self._lock:
            conns = list(self._connections)
        for conn in conns:
            conn.push(data)

    # ---------- 连接管理（asyncio 侧）----------
    def connect(self) -> SSEConnection:
        loop = self._loop or asyncio.get_event_loop()
        with self._lock:
            replay = list(self._replay)
        conn = SSEConnection(loop, replay)
        with self._lock:
            self._connections.append(conn)
        return conn

    def disconnect(self, conn: SSEConnection) -> None:
        conn.close()
        with self._lock:
            try:
                self._connections.remove(conn)
            except ValueError:
                pass

    @property
    def connection_count(self) -> int:
        with self._lock:
            return len(self._connections)

    def clear_replay(self) -> None:
        self._replay.clear()

    @staticmethod
    def format_sse(data: Dict[str, Any], event: str = "") -> str:
        """格式化为 SSE 帧（前端用 addEventListener('message')，故 event 留空）。"""
        payload = json.dumps(data, ensure_ascii=False)
        if event:
            return f"event: {event}\ndata: {payload}\n\n"
        return f"data: {payload}\n\n"
