"""tools.py — ffmpeg / ffprobe / exiftool 子进程封装（仅标准库）。

要点：
  - 所有子进程显式 UTF-8，规避中文 Windows 的 GBK 乱码（方案 §13）。
  - exiftool 用 -@ ArgFile 传参，避免命令行长度与中文编码问题。
  - ffprobe 输出 JSON，统一返回 (duration, creation_time, keyframe_ts, has_audio)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "ToolPaths", "ProbeResult", "resolve_tools", "probe_video", "extract_frame",
    "extract_frame_bytes", "extract_audio", "write_metadata", "read_metadata",
    "build_exiftool_command", "run", "ToolError",
    "is_fragmented_mp4", "remux_mp4",
]

# 子进程统一环境：强制 UTF-8、禁用交互
_SUBPROC_ENV = dict(os.environ)
_SUBPROC_ENV.setdefault("PYTHONIOENCODING", "utf-8")
# perl 版 exiftool 会因 locale 未装而刷 warning；置 LC_ALL 消除噪音
_SUBPROC_ENV.pop("LC_ALL", None)
_SUBPROC_ENV.pop("LANG", None)
_SUBPROC_ENV["LC_ALL"] = "C"

# Windows：隐藏子进程控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_SUBPROC_FLAGS = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}

VIDEO_EXTS = frozenset(
    ".mp4 .mov .avi .mkv .m4v .wmv .flv .webm .ts .mts .m2ts".split()
)


class ToolError(RuntimeError):
    """子进程调用失败。"""


@dataclass
class ToolPaths:
    """三件套可执行文件路径（由 bootstrap 探测后注入）。"""

    ffmpeg: str = ""
    ffprobe: str = ""
    exiftool: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)


@dataclass
class ProbeResult:
    """ffprobe 探测结果。"""

    duration: float = 0.0
    creation_time: str = ""
    keyframe_ts: List[float] = field(default_factory=list)
    has_audio: bool = False
    has_video: bool = False
    width: int = 0
    height: int = 0
    codec: str = ""
    size: int = 0
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return self.has_video and not self.error

    @property
    def resolution_label(self) -> str:
        if self.height >= 2160:
            return "4K"
        if self.height >= 1440:
            return "2K"
        if self.height >= 1080:
            return "1080p"
        if self.height >= 720:
            return "720p"
        if self.height:
            return f"{self.height}p"
        return "未知"


def resolve_tools(ffmpeg: str = "", ffprobe: str = "", exiftool: str = "") -> ToolPaths:
    """构造 ToolPaths；未显式传入时自动探测。"""
    if ffmpeg and ffprobe:
        return ToolPaths(ffmpeg=ffmpeg, ffprobe=ffprobe, exiftool=exiftool)
    try:
        from . import bootstrap

        return ToolPaths(
            ffmpeg=ffmpeg or bootstrap._find_tool("ffmpeg"),
            ffprobe=ffprobe or bootstrap._find_tool("ffprobe"),
            exiftool=exiftool or bootstrap._find_tool("exiftool"),
        )
    except Exception:  # noqa: BLE001
        return ToolPaths(ffmpeg=ffmpeg, ffprobe=ffprobe, exiftool=exiftool)


def run(
    args: Sequence[str],
    *,
    timeout: float = 60,
    input_bytes: Optional[bytes] = None,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """统一子进程调用：UTF-8、无窗口、超时保护。"""
    try:
        return subprocess.run(
            list(args),
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env=_SUBPROC_ENV,
            **_SUBPROC_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"子进程超时（{timeout}s）：{os.path.basename(str(args[0]))}") from exc
    except OSError as exc:
        raise ToolError(f"子进程启动失败：{exc}") from exc


# ------------------------------------------------------------------
# ffprobe 探测
# ------------------------------------------------------------------
def probe_video(tools: ToolPaths, video_path: str, timeout: float = 20) -> ProbeResult:
    """探测时长 / creation_time / 关键帧时间戳 / 是否含音轨 / 分辨率。

    注意：-select_streams v:0 会过滤掉音频流，因此音轨存在性单独查询。
    """
    result = ProbeResult()
    if not tools.ffprobe:
        result.error = "ffprobe 不可用"
        return result

    # 第一路：视频流信息 + 关键帧时间戳
    cmd = [
        tools.ffprobe, "-v", "quiet", "-print_format", "json",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries",
        "format=duration:format_tags=creation_time:"
        "stream=width,height,codec_name,codec_type:frame=pts_time",
        str(video_path),
    ]
    data: Dict = {}
    try:
        proc = run(cmd, timeout=timeout)
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except (ToolError, ValueError):
        data = {}

    # 第二路：完整流列表（判定是否有音轨 / 视频流）
    streams: List[Dict] = []
    try:
        proc2 = run([
            tools.ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", str(video_path),
        ], timeout=timeout)
        if proc2.returncode == 0 and proc2.stdout:
            full = json.loads(proc2.stdout.decode("utf-8", errors="replace"))
            streams = full.get("streams", []) or []
            if not data:
                data = full
            elif "format" not in data and "format" in full:
                data["format"] = full["format"]
    except (ToolError, ValueError):
        streams = []

    if not data and not streams:
        result.error = "ffprobe 未返回有效数据"
        return result

    fmt = data.get("format", {}) or {}
    try:
        result.duration = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        result.duration = 0.0
    try:
        result.size = int(fmt.get("size", 0) or 0)
    except (TypeError, ValueError):
        result.size = 0

    ct = (fmt.get("tags", {}) or {}).get("creation_time", "")
    if ct and "0000" not in str(ct):
        result.creation_time = str(ct)
    else:
        result.creation_time = ""

    # 从完整流列表判定音轨 / 视频流（权威来源）
    for stream in streams:
        ctype = stream.get("codec_type")
        if ctype == "video" and not result.has_video:
            result.has_video = True
            result.codec = str(stream.get("codec_name", ""))
            try:
                result.width = int(stream.get("width", 0) or 0)
                result.height = int(stream.get("height", 0) or 0)
            except (TypeError, ValueError):
                pass
        elif ctype == "audio":
            result.has_audio = True

    # 关键帧时间戳
    for frame in data.get("frames", []) or []:
        pts = frame.get("pts_time")
        if pts and pts != "N/A":
            try:
                result.keyframe_ts.append(float(pts))
            except (TypeError, ValueError):
                continue

    return result


# ------------------------------------------------------------------
# ffmpeg 抽帧
# ------------------------------------------------------------------
def _frame_scale_filter(max_side: int) -> str:
    return (
        f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease"
    )


def extract_frame_bytes(
    tools: ToolPaths,
    video_path: str,
    ts: float,
    max_side: int = 520,
    *,
    hwaccel: str = "none",
    timeout: float = 30,
) -> Optional[bytes]:
    """在 ts 秒处抽一帧，返回 JPEG 原始字节。失败返回 None。"""
    if not tools.ffmpeg:
        return None

    cmd: List[str] = [tools.ffmpeg, "-y", "-nostdin"]
    if hwaccel and hwaccel != "none":
        cmd += ["-hwaccel", hwaccel]
    cmd += [
        "-ss", f"{ts:.3f}", "-i", str(video_path),
        "-frames:v", "1",
        "-vf", _frame_scale_filter(max_side),
        "-q:v", "5", "-f", "image2", "pipe:1",
    ]
    try:
        proc = run(cmd, timeout=timeout)
    except ToolError:
        return None
    if proc.stdout and len(proc.stdout) > 1024:
        return proc.stdout
    return None


def extract_frame(
    tools: ToolPaths,
    video_path: str,
    ts: float,
    out_path: str,
    max_side: int = 520,
    *,
    hwaccel: str = "none",
    timeout: float = 30,
) -> bool:
    """在 ts 秒处抽一帧落盘为 JPEG。"""
    if not tools.ffmpeg:
        return False
    cmd: List[str] = [tools.ffmpeg, "-y", "-nostdin"]
    if hwaccel and hwaccel != "none":
        cmd += ["-hwaccel", hwaccel]
    cmd += [
        "-ss", f"{ts:.3f}", "-i", str(video_path),
        "-frames:v", "1", "-vf", _frame_scale_filter(max_side),
        "-q:v", "5", str(out_path),
    ]
    try:
        proc = run(cmd, timeout=timeout)
    except ToolError:
        return False
    return proc.returncode == 0 and os.path.exists(out_path)


def extract_audio(
    tools: ToolPaths,
    video_path: str,
    out_path: str,
    *,
    sample_rate: int = 16000,
    timeout: float = 600,
) -> bool:
    """抽取 16kHz 单声道 WAV（Whisper 输入）。"""
    if not tools.ffmpeg:
        return False
    cmd = [
        tools.ffmpeg, "-y", "-nostdin", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(out_path),
    ]
    try:
        proc = run(cmd, timeout=timeout)
    except ToolError:
        return False
    return proc.returncode == 0 and os.path.exists(out_path)


# ------------------------------------------------------------------
# 分片封装（fMP4）处理
# ------------------------------------------------------------------
_FRAGMENTED_EXTS = frozenset((".mp4", ".mov", ".m4v"))


def _safe_unlink(path: Path) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass


def is_fragmented_mp4(video_path: str, *, scan_bytes: int = 8 * 1024 * 1024) -> bool:
    """判断是否为分片封装（fMP4）的 MP4 / MOV。

    OBS 的「分片 MP4」与录制中断产物的结构为
    `ftyp + moov(含 mvex) + moof + mdat ...`，而普通 MP4 只有 `ftyp + moov + mdat`。
    ExifTool 目前无法写入分片文件（Can't yet handle movie fragments when writing），
    必须先无损重封装，因此这里只需要「头部是否出现 moof」这一个判据。
    """
    if os.path.splitext(str(video_path))[1].lower() not in _FRAGMENTED_EXTS:
        return False
    try:
        from .paths import to_long_path

        with open(to_long_path(str(video_path)), "rb") as fh:
            head = fh.read(scan_bytes)
    except OSError:
        return False
    return b"moov" in head and b"moof" in head


def remux_mp4(
    tools: ToolPaths,
    video_path: str,
    *,
    timeout: float = 900,
) -> Tuple[bool, str]:
    """把分片封装（fMP4）无损重封装成标准 MP4，并就地替换原文件。

    只换容器不重编码（`-c copy`），速度接近磁盘拷贝速度且画质无损；
    重封装后 ExifTool 即可正常写入元数据。

    安全约束：产物落同目录临时文件，校验体积合理后再 `os.replace` 原子替换，
    任何一步失败都保留原文件不动。
    返回 (是否成功, 错误信息)。注意：不会还原时间戳，调用方需自行 preserve/restore。
    """
    if not tools.ffmpeg:
        return False, "ffmpeg 不可用"
    src = Path(str(video_path))
    try:
        src_size = src.stat().st_size
    except OSError as exc:
        return False, f"无法读取源文件：{exc}"

    tmp = src.with_name(src.stem + ".__remux__.mp4")
    _safe_unlink(tmp)
    cmd = [
        tools.ffmpeg, "-y", "-nostdin",
        "-i", str(src),
        "-map", "0", "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4", str(tmp),
    ]
    try:
        proc = run(cmd, timeout=timeout)
    except ToolError as exc:
        _safe_unlink(tmp)
        return False, str(exc)

    if proc.returncode != 0 or not tmp.is_file():
        _safe_unlink(tmp)
        detail = _clean_stderr(proc.stderr or b"")[:300]
        return False, detail or f"ffmpeg 重封装失败（退出码 {proc.returncode}）"
    try:
        out_size = tmp.stat().st_size
    except OSError:
        _safe_unlink(tmp)
        return False, "重封装产物不可读"
    # 体积校验：重封装只会让体积接近原文件（±少量 moof 开销），
    # 大幅缩水说明流没拷全，绝不能替换原文件。
    if out_size < max(1, int(src_size * 0.5)):
        _safe_unlink(tmp)
        return False, f"重封装产物异常（{out_size} 字节 < 源文件 {src_size} 的一半），已放弃替换"

    try:
        os.replace(str(tmp), str(src))
    except OSError as exc:
        _safe_unlink(tmp)
        return False, f"替换原文件失败：{exc}"
    return True, ""


# ------------------------------------------------------------------
# exiftool 元数据
# ------------------------------------------------------------------
def write_metadata(
    tools: ToolPaths,
    video_path: str,
    tags: Dict[str, str],
    *,
    timeout: float = 120,
) -> Tuple[bool, str]:
    """用 exiftool 就地写入元数据（零拷贝，毫秒级）。

    关键点（Windows 中文环境实测）：
      1. 优先用 `perl.exe + exiftool.pl` 调用，**不要**直接用 exiftool(-k).exe ——
         后者名字里的 `-k` 会让 exiftool 结束后「-- press ENTER --」等待输入，
         在 subprocess 里表现为永久挂起。
      2. 通过 `-@ ArgFile`（UTF-8）传参，规避中文路径 / 中文值 / 命令行长度 / GBK 乱码。
      3. 必须显式 `-charset exiftool=UTF8`，否则中文值会被写成 `???`。
    返回 (是否成功, 错误信息)。
    """
    if not tools.exiftool:
        return False, "exiftool 不可用"
    if not tags:
        return True, ""

    # ArgFile 采用 UTF-8，每条参数一行
    lines: List[str] = [
        "-overwrite_original",
        "-charset", "exiftool=UTF8",
        "-charset", "filename=UTF8",
    ]
    for key, value in tags.items():
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"-{key}={text}")
    lines.append(str(video_path))

    argfile = None
    try:
        fd, argfile = tempfile.mkstemp(suffix=".args", dir=str(_tmp_dir()))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")

        # 注意：必须是两个独立参数 ["-@", argfile]，写成 "-@"+argfile 会被
        # 解析成无效 tag（Windows 下 perl 版 exiftool 实测）。
        cmd = build_exiftool_command(tools.exiftool, ["-@", argfile])
        if cmd is None:
            return False, "exiftool 调用方式不可用"
        proc = run(cmd, timeout=timeout)
        if proc.returncode == 0:
            return True, ""
        err = _clean_stderr(proc.stderr or proc.stdout or b"")
        return False, err[:300] or f"exiftool 退出码 {proc.returncode}"
    except (ToolError, OSError) as exc:
        return False, str(exc)
    finally:
        if argfile:
            try:
                os.unlink(argfile)
            except OSError:
                pass


def _clean_stderr(raw: bytes) -> str:
    """过滤 perl 的 locale 警告噪音，只保留真正的错误信息。"""
    text = raw.decode("utf-8", errors="replace").strip()
    keep: List[str] = []
    skip_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("perl: warning: Setting locale failed"):
            skip_block = True
            continue
        if skip_block:
            # locale 警告后续的缩进说明行都跳过
            if stripped.startswith(("LC_ALL", "LANG", "are supported", "perl: warning: Please check",
                                    "perl: warning: Falling back", "perl: warning:")):
                continue
            skip_block = False
            continue
        if stripped.startswith("perl: warning: Falling back"):
            continue
        if stripped:
            keep.append(stripped)
    return "\n".join(keep).strip()


def build_exiftool_command(exiftool_path: str, extra_args: Sequence[str]) -> Optional[List[str]]:
    """构造 exiftool 调用命令。

    优先使用同目录 `exiftool_files/perl.exe + exiftool.pl`，
    彻底规避 `exiftool(-k).exe` 的「press ENTER」挂起问题。
    """
    if not exiftool_path:
        return None

    # 若传入的就是 exiftool.pl，直接用 perl 跑
    if exiftool_path.lower().endswith(".pl"):
        perl = Path(exiftool_path).parent / "perl.exe"
        if perl.exists():
            return [str(perl), exiftool_path, *extra_args]
        return ["perl", exiftool_path, *extra_args]

    # 在 exiftool 同目录 / exiftool_files 子目录下找 exiftool.pl + perl.exe
    base_dir = Path(exiftool_path).parent
    for candidate_dir in (base_dir, base_dir / "exiftool_files"):
        perl = candidate_dir / "perl.exe"
        script = candidate_dir / "exiftool.pl"
        if perl.exists() and script.exists():
            return [str(perl), str(script), *extra_args]

    # 兜底：直接用 exe（若其名字含 (-k)，仍可能挂起，故不推荐）
    return [exiftool_path, *extra_args]


def read_metadata(
    tools: ToolPaths,
    video_path: str,
    *,
    timeout: float = 30,
) -> Dict[str, str]:
    """读取视频元数据（JSON）。失败返回空 dict。"""
    if not tools.exiftool:
        return {}
    cmd = build_exiftool_command(
        tools.exiftool,
        ["-j", "-G1", "-s", "-charset", "filename=UTF8", str(video_path)],
    )
    if cmd is None:
        return {}
    try:
        proc = run(cmd, timeout=timeout)
    except ToolError:
        return {}
    if proc.returncode != 0 or not proc.stdout:
        return {}
    try:
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    if isinstance(data, list) and data:
        return data[0] or {}
    return {}


def _tmp_dir():
    from . import paths

    try:
        paths.TMP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return tempfile.gettempdir()
    return paths.TMP_DIR
