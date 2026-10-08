"""paths.py — 全局目录常量（唯一真源）。

方案红线 G2：所有产物必须落在 APP_ROOT 内，卸载 = 删文件夹。
本模块 **只依赖标准库**，且不得 import 任何第三方库——
因为它会被 bootstrap.py 在重定向 env 之前导入。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "APP_ROOT", "APP_DIR", "BIN_DIR", "LIBS_DIR", "MODELS_DIR", "CACHE_DIR",
    "TMP_DIR", "LOGS_DIR", "DATA_DIR", "DOCS_DIR", "CONFIG_FILE", "DB_FILE",
    "ensure_dirs", "relative_to_root", "to_long_path",
]

# ------------------------------------------------------------------
# APP_ROOT 推导：app/infra/paths.py → 上溯三级得到项目根目录
# ------------------------------------------------------------------
_THIS = Path(__file__).resolve()
APP_DIR = _THIS.parent.parent              # app/
APP_ROOT = APP_DIR.parent                  # videoAIrenameAPP/

BIN_DIR = APP_ROOT / "bin"                 # ffmpeg / ffprobe / exiftool
FFMPEG_DIR = APP_ROOT / "ffmpeg"           # 兼容既有 ffmpeg/ 目录（自带工具链）
LIBS_DIR = APP_ROOT / "libs"               # pip --target 安装的第三方包
MODELS_DIR = APP_ROOT / "models"           # Whisper / HF 模型缓存
CACHE_DIR = APP_ROOT / "cache"             # 设备探测结果等小缓存
TMP_DIR = APP_ROOT / "tmp"                 # 临时帧与中间文件
LOGS_DIR = APP_ROOT / "logs"                # 运行日志
DATA_DIR = APP_ROOT / "data"               # SQLite 数据库 / rename_map
DOCS_DIR = APP_ROOT / "docs"               # 方案与说明

CONFIG_FILE = APP_ROOT / "config.json"     # 用户配置
DB_FILE = DATA_DIR / "vair.db"             # SQLite 数据库
RENAME_MAP_FILE = DATA_DIR / "rename_map.json"

# 需要随应用创建的子目录
_MANAGED_DIRS = (LIBS_DIR, MODELS_DIR, CACHE_DIR, TMP_DIR, LOGS_DIR, DATA_DIR)

LONG_PATH_PREFIX = "\\\\?\\"


def ensure_dirs() -> None:
    """创建所有运行期需要的目录（幂等）。"""
    for d in _MANAGED_DIRS:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 极端情况（只读盘）下不阻断启动，由后续写操作报错
            pass


def relative_to_root(path: "str | os.PathLike[str]") -> str:
    """把绝对路径转成相对 APP_ROOT 的 POSIX 风格字符串（用于展示/配置）。"""
    try:
        return Path(path).resolve().relative_to(APP_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def to_long_path(path: "str | os.PathLike[str]") -> str:
    """Windows 长路径前缀处理（>260 字符路径必需）。"""
    abs_path = os.path.abspath(str(path))
    if sys.platform == "win32":
        if abs_path.startswith(LONG_PATH_PREFIX):
            return abs_path
        if abs_path.startswith("\\\\"):
            return LONG_PATH_PREFIX + "UNC\\" + abs_path[2:]
        return LONG_PATH_PREFIX + abs_path
    return abs_path
