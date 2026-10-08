"""core/media.py — 探测 / 抽帧 / 时间戳选取（阶段 ①②a）。

对齐参考仓库的三阶段流水线：抽帧与转写互不阻塞。
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from typing import List, Optional

from ..infra import tools as tool_mod
from ..infra.tools import ProbeResult, ToolPaths

__all__ = ["MediaInfo", "KeyFrames", "probe", "select_timestamps",
           "extract_keyframes", "probe_and_extract", "VIDEO_EXTS", "collect_videos"]

VIDEO_EXTS = tool_mod.VIDEO_EXTS


@dataclass
class MediaInfo:
    """探测结果（阶段① 产物）。"""

    path: str = ""
    duration: float = 0.0
    creation_time: str = ""
    has_audio: bool = False
    has_video: bool = False
    width: int = 0
    height: int = 0
    resolution: str = ""
    size: int = 0
    keyframe_ts: List[float] = field(default_factory=list)
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return self.has_video and not self.error

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "duration": self.duration,
            "creation_time": self.creation_time,
            "has_audio": self.has_audio,
            "has_video": self.has_video,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "size": self.size,
            "frames_found": len(self.keyframe_ts),
            "error": self.error,
        }


@dataclass
class KeyFrames:
    """抽帧结果（阶段②a 产物）。"""

    path: str = ""
    frames_b64: List[str] = field(default_factory=list)
    count: int = 0
    elapsed_ms: int = 0
    error: str = ""


def probe(tool_paths: ToolPaths, video_path: str, *, timeout: float = 20) -> MediaInfo:
    """ffprobe 探测单个视频。"""
    info = MediaInfo(path=video_path)
    result: ProbeResult = tool_mod.probe_video(tool_paths, video_path, timeout=timeout)
    if result.error:
        info.error = result.error
        return info
    info.duration = result.duration
    info.creation_time = result.creation_time
    info.has_audio = result.has_audio
    info.has_video = result.has_video
    info.width = result.width
    info.height = result.height
    info.resolution = result.resolution_label
    info.size = result.size
    info.keyframe_ts = list(result.keyframe_ts)
    return info


def select_timestamps(duration: float, key_ts: List[float], n: int) -> List[float]:
    """选取 n 个时间点（与 batch_rename.py 的策略保持一致）。

    1. 极短视频（<10s）：取首 / 中 / 尾三点
    2. 有关键帧：按需等距采样
    3. 兜底：按时长均分
    """
    n = max(1, int(n))
    if 0 < duration < 10.0:
        return [0.0, duration / 2.0, max(0.0, duration - 0.1)]
    if key_ts:
        if len(key_ts) >= n:
            if n == 1:
                return [key_ts[len(key_ts) // 2]]
            step = (len(key_ts) - 1) / (n - 1)
            return [key_ts[int(round(i * step))] for i in range(n)]
        return list(key_ts)
    if duration > 0:
        actual_n = min(n, max(1, int(duration)))
        return [duration * (i + 1) / (actual_n + 1) for i in range(actual_n)]
    return [0.0]


def extract_keyframes(
    tool_paths: ToolPaths,
    video_path: str,
    timestamps: List[float],
    *,
    max_side: int = 520,
    hwaccel: str = "none",
    stop_token=None,
) -> KeyFrames:
    """按时间点抽帧并 base64 编码（阶段②a 实际工作）。"""
    import time as _time

    started = _time.time()
    out = KeyFrames(path=video_path)
    frames: List[str] = []

    for ts in timestamps:
        if stop_token is not None and stop_token.is_set():
            break
        raw = tool_mod.extract_frame_bytes(
            tool_paths, video_path, ts, max_side, hwaccel=hwaccel
        )
        if raw:
            frames.append(base64.b64encode(raw).decode("ascii"))

    # 兜底：一帧都没抽到时，取中点重试
    if not frames and (stop_token is None or not stop_token.is_set()):
        fallback_ts = 0.0
        raw = tool_mod.extract_frame_bytes(
            tool_paths, video_path, fallback_ts, max_side, hwaccel=hwaccel
        )
        if raw:
            frames.append(base64.b64encode(raw).decode("ascii"))

    out.frames_b64 = frames
    out.count = len(frames)
    out.elapsed_ms = int((_time.time() - started) * 1000)
    if not frames:
        out.error = "抽帧失败：未获得有效帧"
    return out


def probe_and_extract(
    tool_paths: ToolPaths,
    video_path: str,
    *,
    max_keyframes: int = 35,
    max_side: int = 520,
    hwaccel: str = "none",
    stop_token=None,
) -> "tuple[MediaInfo, KeyFrames]":
    """阶段①②a 合并入口（供线程池调用）：先探测再抽帧。"""
    import time as _time

    info = probe(tool_paths, video_path)
    if info.error or not info.has_video:
        return info, KeyFrames(path=video_path, error=info.error or "无视频流")

    started = _time.time()
    timestamps = select_timestamps(info.duration, info.keyframe_ts, max_keyframes)
    frames = extract_keyframes(
        tool_paths, video_path, timestamps,
        max_side=max_side, hwaccel=hwaccel, stop_token=stop_token,
    )
    frames.elapsed_ms = int((_time.time() - started) * 1000)
    return info, frames


def collect_videos(paths: List[str], *, recursive: bool = True) -> List[str]:
    """扫描路径列表，返回视频绝对路径（跳过 _failed 目录）。"""
    found: List[str] = []
    for raw in paths:
        p = os.path.abspath(str(raw).strip().strip('"'))
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                found.append(p)
            continue
        if not os.path.isdir(p):
            continue
        if recursive:
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d != "_failed"]
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                        found.append(os.path.join(root, f))
        else:
            try:
                for f in sorted(os.listdir(p)):
                    full = os.path.join(p, f)
                    if os.path.isfile(full) and os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                        found.append(full)
            except OSError:
                continue
    # 去重并保持顺序
    seen = set()
    unique: List[str] = []
    for v in found:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique
