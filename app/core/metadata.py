"""core/metadata.py — ExifTool 写元数据 / 软水印 / 时间戳还原 / NFO / SRT（阶段⑤）。

移植参考仓库的关键特性：
  - 零拷贝元数据写入（ExifTool -overwrite_original）
  - 软水印 AIVideoRenameV1：二次扫描毫秒级跳过，杜绝重复消耗 Token
  - kernel32 级时间戳还原：改名后保留录像创建时间
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from ..infra.paths import to_long_path
from ..infra.tools import ToolPaths
from ..infra import tools as tool_mod
from .whisper import TranscriptResult

__all__ = [
    "SOFTWARE_MARKER", "write_video_metadata", "has_watermark",
    "write_nfo", "write_srt", "restore_timestamps", "preserve_stat",
]

SOFTWARE_MARKER = "AIVideoRenameV1"


# ==================================================================
# ExifTool 元数据写入
# ==================================================================
def _emit(on_log: Optional[Callable[[str, str], None]], level: str, msg: str) -> None:
    if not on_log:
        return
    try:
        on_log(level, msg)
    except Exception:  # noqa: BLE001 - 日志回调失败不影响主流程
        pass


def _is_fragment_error(err: str) -> bool:
    """ExifTool 对分片封装 MP4 的报错：
    `Error: Can't yet handle movie fragments when writing`。"""
    low = (err or "").lower()
    return "movie fragment" in low or ("fragment" in low and "writing" in low)


def write_video_metadata(
    tool_paths: ToolPaths,
    video_path: str,
    *,
    title: str = "",
    plot: str = "",
    tags: Optional[List[str]] = None,
    duration: float = 0.0,
    original_name: str = "",
    allow_remux: bool = True,
    on_log: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, str]:
    """把标题/描述/关键词写进视频内部属性（零拷贝）。

    同时写入软水印，便于二次运行跳过。

    分片封装兜底：OBS 的「分片 MP4」/录制中断产物是 `moov(mvex)+moof+mdat` 结构，
    ExifTool 无法写入（Can't yet handle movie fragments when writing）。此时先用
    ffmpeg `-c copy` 无损重封装成标准 MP4（不改画质、不重编码），再重试写入，
    并还原文件时间戳，避免录像创建时间被抹掉。
    """
    tags = tags or []
    exif_tags: Dict[str, str] = {}

    if title:
        exif_tags["Title"] = title
        exif_tags["XMP-dc:Title"] = title
    if plot:
        exif_tags["Description"] = plot
        exif_tags["Comment"] = plot
        exif_tags["XMP-dc:Description"] = plot
    if tags:
        # ExifTool 的 Keywords 支持多条，用逗号合并更稳
        exif_tags["Keywords"] = ", ".join(str(t) for t in tags if t)
    if original_name:
        exif_tags["OriginalFileName"] = original_name
    exif_tags["Software"] = SOFTWARE_MARKER

    ok, err = tool_mod.write_metadata(tool_paths, video_path, exif_tags)
    if ok or not allow_remux:
        return ok, err
    # 触发条件：ExifTool 明说分片写不了，或文件本身就是分片封装（错误信息表述可能变化）
    if not (_is_fragment_error(err) or tool_mod.is_fragmented_mp4(video_path)):
        return ok, err

    _emit(on_log, "INFO", "检测到分片封装 MP4（ExifTool 无法写入），正在无损重封装 ...")
    stat_before = preserve_stat(video_path)
    fixed, fix_err = tool_mod.remux_mp4(tool_paths, video_path)
    if not fixed:
        return False, f"{err}（重封装失败：{fix_err}）"
    restore_timestamps(video_path, stat_before)

    ok2, err2 = tool_mod.write_metadata(tool_paths, video_path, exif_tags)
    if ok2:
        _emit(on_log, "INFO", "已重封装为标准 MP4 并写入元数据")
        return True, ""
    return False, f"{err}（重封装后仍失败：{err2 or '未知原因'}）"


def has_watermark(tool_paths: ToolPaths, video_path: str) -> bool:
    """检查视频是否已被处理过（软水印）。"""
    meta = tool_mod.read_metadata(tool_paths, video_path)
    for key, value in meta.items():
        if key.lower().endswith("software") and SOFTWARE_MARKER in str(value):
            return True
    return False


# ==================================================================
# NFO 生成
# ==================================================================
def write_nfo(
    video_path: str,
    *,
    title: str,
    plot: str,
    tags: Optional[List[str]] = None,
    duration: float = 0.0,
    original_name: str = "",
) -> bool:
    """生成 Jellyfin / Kodi 可识别的 .nfo 文件。"""
    try:
        target = str(Path(video_path).with_suffix(".nfo"))
        runtime_minutes = int(duration / 60) if duration else 0

        lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            "<movie>",
            f"    <title>{escape(title or '')}</title>",
            f"    <plot>{escape(plot or '')}</plot>",
            f"    <runtime>{runtime_minutes}</runtime>",
            f"    <originaltitle>{escape(original_name or '')}</originaltitle>",
        ]
        for tag in tags or []:
            if tag:
                lines.append(f"    <tag>{escape(str(tag))}</tag>")
        lines.append("</movie>")

        with open(to_long_path(target), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        return True
    except OSError:
        return False


# ==================================================================
# SRT 生成
# ==================================================================
def write_srt(video_path: str, transcript: TranscriptResult) -> bool:
    """把转写结果写成同名 .srt 字幕。"""
    if transcript is None or not transcript.segments:
        return False
    try:
        # 语言后缀（多语言场景便于区分）
        target = str(Path(video_path).with_suffix(".srt"))
        content = transcript.to_srt()
        if not content.strip():
            return False
        with open(to_long_path(target), "w", encoding="utf-8") as fh:
            fh.write(content)
        return True
    except OSError:
        return False


# ==================================================================
# 时间戳还原（kernel32 级）
# ==================================================================
def preserve_stat(src_path: str) -> Optional[os.stat_result]:
    """读取源文件 stat（用于改名后还原时间）。"""
    try:
        return os.stat(to_long_path(src_path))
    except OSError:
        return None


def restore_timestamps(target_path: str, stat: Optional[os.stat_result]) -> bool:
    """把改名前的访问/修改时间还原到目标文件。

    Windows 上进一步用 kernel32 设置「创建时间」，这是资源管理器与
    部分媒体库读取的字段（参考仓库的核心特性）。
    """
    if stat is None:
        return False
    ok = False
    try:
        os.utime(to_long_path(target_path), (stat.st_atime, stat.st_mtime))
        ok = True
    except OSError:
        pass

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            handle = ctypes.windll.kernel32.CreateFileW(
                to_long_path(target_path),
                256,  # GENERIC_WRITE
                0,    # 不共享
                None,
                3,    # OPEN_EXISTING
                0x80,  # FILE_ATTRIBUTE_NORMAL
                None,
            )
            if handle != -1 and handle is not None:
                class FILETIME(ctypes.Structure):
                    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                                ("dwHighDateTime", wintypes.DWORD)]

                def _to_filetime(unix_ts: float) -> FILETIME:
                    # Unix 秒 → Windows FILETIME（100ns 单位，起点 1601-01-01）
                    ticks = int((unix_ts + 11644473600) * 10_000_000)
                    ft = FILETIME()
                    ft.dwLowDateTime = ticks & 0xFFFFFFFF
                    ft.dwHighDateTime = ticks >> 32
                    return ft

                created = _to_filetime(stat.st_ctime)
                accessed = _to_filetime(stat.st_atime)
                written = _to_filetime(stat.st_mtime)
                ctypes.windll.kernel32.SetFileTime(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(accessed),
                    ctypes.byref(written),
                )
                ctypes.windll.kernel32.CloseHandle(handle)
                ok = True
        except Exception:  # noqa: BLE001 - 时间戳还原失败不致命
            pass
    return ok
