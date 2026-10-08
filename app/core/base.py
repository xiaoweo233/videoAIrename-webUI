"""core/base.py — 事件契约 / 停止令牌 / 统计（红线 G4 的载体）。

工作线程不直接操作 UI，一律通过 EventBus 抛事件；
外壳（Web 用 SSE、桌面用 after() 轮询）订阅事件并渲染。

事件类型与前端 app/shell/web/static/js/app.js 的 handlers 完全一一对应：
  status / task / progress / phase / log / review / inference
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "EventType", "Event", "EventBus", "StopToken", "JobStats", "Phase",
    "PHASE_LABELS", "PHASE_SEQUENCE",
]


class EventType:
    """事件类型常量（字符串与前端一致）。"""

    STATUS = "status"
    TASK = "task"
    PROGRESS = "progress"
    PHASE = "phase"
    LOG = "log"
    REVIEW = "review"
    INFERENCE = "inference"


class Phase:
    """流水线阶段键（与前端 PHASE_LABEL 一致）。"""

    IDLE = "0"
    PROBE = "1"
    FRAMES = "2a"
    TRANSCRIBE = "2b"
    MERGE = "3"
    AI = "4"
    WRITE = "5"


PHASE_LABELS: Dict[str, str] = {
    "0": "待机",
    "1": "① 探测",
    "2a": "②a 抽帧",
    "2b": "②b 转写",
    "3": "③ 合并",
    "4": "④ AI 分析",
    "5": "⑤ 落盘",
}

# 正常流水线的阶段顺序（用于 UI 顺序展示）
PHASE_SEQUENCE: List[str] = ["1", "2a", "2b", "3", "4", "5"]


@dataclass
class Event:
    """一条事件。to_dict() 产出前端可直接消费的 JSON 对象。"""

    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.payload)
        data["type"] = self.type
        data["ts"] = self.ts
        return data


class EventBus:
    """线程安全的事件总线（发布 / 订阅）。

    - publish() 可被任意工作线程调用
    - subscribe() 返回取消函数
    - 每个订阅者自带队列语义：用 threading.Lock 保护列表快照，回调在调用线程执行
      （Web 壳的 SSE 生成器会把事件塞进自己的 asyncio 队列）
    """

    def __init__(self, max_log: int = 0) -> None:
        self._subs: List[Callable[[Dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._history: List[Dict[str, Any]] = []
        self._max_history = max_log  # >0 时保留历史，便于新连接回放

    def subscribe(self, fn: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs.remove(fn)
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, event: "Event | Dict[str, Any] | str", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发布事件。支持三种调用形式：
        publish(Event(...)) / publish({"type": ...}) / publish("log", {...})
        """
        if isinstance(event, Event):
            data = event.to_dict()
        elif isinstance(event, str):
            evt = Event(type=event, payload=payload or {})
            data = evt.to_dict()
        else:
            data = dict(event)
            data.setdefault("ts", int(time.time() * 1000))

        if self._max_history > 0:
            with self._lock:
                self._history.append(data)
                if len(self._history) > self._max_history:
                    del self._history[: len(self._history) - self._max_history]

        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(data)
            except Exception:  # noqa: BLE001 - 单个订阅者异常不影响其他
                continue
        return data

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._history)

    # ---------- 便捷方法 ----------
    def emit_status(self, connected: bool = True, mode: str = "sse", **extra: Any) -> None:
        self.publish(EventType.STATUS, {"connected": connected, "mode": mode, **extra})

    def emit_task(self, action: str, task_id: str, **extra: Any) -> None:
        self.publish(EventType.TASK, {"action": action, "taskId": task_id, **extra})

    def emit_progress(
        self,
        *,
        task_id: str,
        phase: str,
        current_file: str = "",
        done: int = 0,
        total: int = 0,
        stats: Optional[Dict[str, int]] = None,
        avg_seconds: float = 0.0,
    ) -> None:
        self.publish(EventType.PROGRESS, {
            "taskId": task_id,
            "phase": phase,
            "currentFile": current_file,
            "done": done,
            "total": total,
            "stats": stats or {"success": 0, "skipped": 0, "failed": 0, "cancelled": 0},
            "avgSeconds": avg_seconds,
        })

    def emit_phase(self, phase: str, state: str, file: str = "", elapsed_ms: int = 0) -> None:
        """state: start | done | skip"""
        self.publish(EventType.PHASE, {
            "phase": phase, "state": state, "file": file, "elapsedMs": elapsed_ms,
        })

    def emit_log(
        self,
        level: str,
        message: str,
        *,
        file: str = "",
        source: str = "engine",
    ) -> None:
        self.publish(EventType.LOG, {
            "level": level.upper(), "message": message, "file": file, "source": source,
        })

    def emit_review(
        self,
        action: str,
        item: Optional[Dict[str, Any]] = None,
        items: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        payload: Dict[str, Any] = {"action": action}
        if item is not None:
            payload["item"] = item
        if items is not None:
            payload["items"] = items
        self.publish(EventType.REVIEW, payload)

    def emit_inference(
        self,
        *,
        file: str,
        model: str,
        frames: int,
        subtitle_chars: int,
        elapsed_ms: int,
    ) -> None:
        self.publish(EventType.INFERENCE, {
            "file": file,
            "model": model,
            "frames": frames,
            "subtitleChars": subtitle_chars,
            "elapsedMs": elapsed_ms,
        })


class StopToken:
    """停止令牌。

    方案 §6.3 停止语义：
      置位 → 不再调度新任务 → 在跑的视频跑完当前文件 → 已完成改名保留 → 统计记 cancelled。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """等待直到停止，或在 timeout 后返回 False（用于可中断的退避重试）。"""
        return self._event.wait(timeout)


class JobStats:
    """任务统计（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = {"success": 0, "skipped": 0, "failed": 0, "cancelled": 0}
        self._durations: List[int] = []
        self.total = 0
        self.done = 0

    def inc(self, key: str, delta: int = 1) -> None:
        with self._lock:
            self._data[key] = self._data.get(key, 0) + delta
            if key in ("success", "skipped", "failed", "cancelled"):
                self.done += delta

    def record_duration(self, ms: int) -> None:
        with self._lock:
            self._durations.append(ms)

    @property
    def avg_seconds(self) -> float:
        with self._lock:
            if not self._durations:
                return 0.0
            return round(sum(self._durations) / len(self._durations) / 1000.0, 1)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._data)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stats": self.snapshot(),
            "done": self.done,
            "total": self.total,
            "avgSeconds": self.avg_seconds,
        }
