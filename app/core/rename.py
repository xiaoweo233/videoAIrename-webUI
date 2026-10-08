"""core/rename.py — 文件名模板渲染 / 清洗 / _N 去重（阶段⑤的一部分）。

规则来自 batch_rename.py，并与前端命名模板 {date}_{title} 对齐。
"""
from __future__ import annotations

import datetime
import os
import platform
import re
import threading
from typing import List, Optional, Tuple

from ..infra.paths import to_long_path

__all__ = [
    "sanitize_filename", "extract_date_str", "build_new_stem",
    "render_template", "rename_file", "move_to_failed",
]

_WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_rename_lock = threading.Lock()

# 支持的日期解析格式（creation_time 优先）
_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


def sanitize_filename(text: str, max_chars: Optional[int] = 20) -> str:
    """清洗成合法文件名片段（Windows 安全）。"""
    text = str(text or "")
    text = _WIN_ILLEGAL.sub("_", text)
    text = re.sub(r"\s+", "_", text.strip())
    text = re.sub(r"_+", "_", text)
    text = text.strip(". _-")
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
    text = text.rstrip(". _-")
    if platform.system() == "Windows" and text.upper() in _WIN_RESERVED:
        text = text + "_"
    return text or "untitled"


def extract_date_str(video_path: str, creation_time: str, date_format: str = "%Y%m%d_%H%M") -> str:
    """提取日期前缀：creation_time → 文件时间 → 当前时间。"""
    if creation_time:
        ct = str(creation_time).replace("Z", "+00:00")
        for fmt in _DT_FORMATS:
            try:
                return datetime.datetime.strptime(ct, fmt).strftime(date_format)
            except ValueError:
                continue
    try:
        st = os.stat(to_long_path(video_path))
        ts = min(st.st_mtime, st.st_ctime)
        if ts > 100_000_000:
            return datetime.datetime.fromtimestamp(ts).strftime(date_format)
    except OSError:
        pass
    return datetime.datetime.now().strftime(date_format)


def render_template(
    template: str,
    *,
    date: str = "",
    title: str = "",
    original: str = "",
    marker: str = "",
) -> str:
    """渲染命名模板，如 {date}_{title}。

    未知占位符原样保留（避免模板写错导致整个名字被吞掉）。
    """
    values = {
        "date": date,
        "title": title,
        "original": original,
        "marker": marker,
    }
    result = template or "{date}_{title}"
    for key, value in values.items():
        result = result.replace("{" + key + "}", value or "")
    # 清理因空缺占位符产生的多余分隔符
    result = re.sub(r"_{2,}", "_", result)
    result = result.strip("._- ")
    return result


def build_new_stem(
    video_path: str,
    *,
    title: str,
    creation_time: str = "",
    naming: Optional[dict] = None,
) -> str:
    """按配置构造新文件名（不含扩展名）。

    默认行为与 batch_rename.py 一致：
      [日期]_标题_[标记]_[原名]
    """
    naming = naming or {}
    template = str(naming.get("template") or "{date}_{title}")
    date_format = str(naming.get("date_format") or "%Y%m%d_%H%M")
    include_date = bool(naming.get("include_date", True))
    include_original = bool(naming.get("include_original", False))
    enable_marker = bool(naming.get("enable_marker", True))
    marker = str(naming.get("marker") or "AI")

    date_str = extract_date_str(video_path, creation_time, date_format) if include_date else ""
    title_clean = sanitize_filename(title, 50)
    original_clean = (
        sanitize_filename(os.path.splitext(os.path.basename(video_path))[0], 30)
        if include_original else ""
    )
    marker_clean = _WIN_ILLEGAL.sub("_", marker) if (enable_marker and marker) else ""

    stem = render_template(
        template,
        date=date_str,
        title=title_clean,
        original=original_clean,
        marker=marker_clean,
    )

    # 模板未覆盖到的部分按开关补足（兼容自定义模板）
    if enable_marker and marker_clean and marker_clean not in stem:
        stem = f"{stem}_{marker_clean}" if stem else marker_clean
    if include_original and original_clean and original_clean not in stem:
        stem = f"{stem}_{original_clean}" if stem else original_clean

    return sanitize_filename(stem, 120) or sanitize_filename(title_clean or "untitled", 50)


def _unique_target(src: str, stem: str, suffix: str) -> Tuple[str, bool]:
    """计算不冲突的目标路径；返回 (目标路径, 是否等于源路径)。

    同名时追加 _1 / _2 ...（上限 100）。
    """
    parent = os.path.dirname(src)
    src_long = to_long_path(src)
    target = os.path.join(parent, f"{stem}{suffix}")
    if to_long_path(target) == src_long:
        return target, True
    counter = 1
    while os.path.exists(to_long_path(target)):
        target = os.path.join(parent, f"{stem}_{counter}{suffix}")
        if to_long_path(target) == src_long:
            return target, True
        counter += 1
        if counter > 100:
            raise OSError(f"目标文件名冲突超过 100 次：{stem}{suffix}")
    return target, False


def rename_file(
    video_path: str,
    new_stem: str,
    *,
    dry_run: bool = False,
    preserve_times: bool = True,
) -> Tuple[str, str]:
    """重命名文件。

    返回 (最终路径, 状态)，状态取值：ok | skipped | error
      - skipped：新名与原名相同（含仅大小写差异导致的同路径）
      - dry_run：不落盘，但仍返回演算后的目标名（状态 ok）
    """
    src = os.path.abspath(video_path)
    suffix = os.path.splitext(src)[1]

    with _rename_lock:
        try:
            target, same = _unique_target(src, new_stem, suffix)
        except OSError as exc:
            return src, "error"

        if same:
            return target, "skipped"
        if dry_run:
            return target, "ok"

        orig_stat = None
        if preserve_times:
            try:
                orig_stat = os.stat(to_long_path(src))
            except OSError:
                orig_stat = None

        try:
            os.replace(to_long_path(src), to_long_path(target))
        except OSError:
            return src, "error"

        if orig_stat is not None:
            try:
                os.utime(to_long_path(target), (orig_stat.st_atime, orig_stat.st_mtime))
            except OSError:
                pass
        return target, "ok"


def move_to_failed(error_files: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """把失败文件移入同级 _failed 目录；返回实际移动清单。"""
    moved: List[Tuple[str, str]] = []
    for full_path, name in error_files:
        try:
            if not os.path.exists(to_long_path(full_path)):
                continue
            dest_dir = os.path.join(os.path.dirname(full_path), "_failed")
            os.makedirs(to_long_path(dest_dir), exist_ok=True)
            stem, suffix = os.path.splitext(name)
            dest = os.path.join(dest_dir, name)
            counter = 1
            src_long = to_long_path(full_path)
            while os.path.exists(to_long_path(dest)) and to_long_path(dest) != src_long:
                dest = os.path.join(dest_dir, f"{stem}_{counter}{suffix}")
                counter += 1
            os.replace(src_long, to_long_path(dest))
            moved.append((full_path, dest))
        except OSError:
            continue
    return moved
