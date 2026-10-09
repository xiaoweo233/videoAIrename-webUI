"""config.py — AppConfig 读写（对齐方案 §7.2 的 config.json 结构）。

约束（方案 §7.2）：
  - 设置页每次改动即时写盘（防崩溃丢配置）。
  - 启动时若 config.json 缺失则按内置默认值生成。
  - 结构必须与前端 store.js 的 DEFAULT_CONFIG 完全一致，前端零改动即可对接。

只依赖标准库。
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from . import paths

__all__ = ["DEFAULT_CONFIG", "AppConfig", "load_config", "save_config", "deep_merge"]


# ------------------------------------------------------------------
# 内置默认值 —— 与 app/shell/web/static/js/store.js 的 DEFAULT_CONFIG 保持一致
# ------------------------------------------------------------------
# 说明：以下默认值取自实际使用的 config.json（密钥已留空），因此「恢复默认」
# 就是回到这套经过实战验证的参数；改这里时记得同步 store.js 与 config.example.json。
DEFAULT_CONFIG: Dict[str, Any] = {
    "ai": {
        "provider": "openai",
        "base_url": "http://127.0.0.1:8084/v1",
        "api_key": "",
        "model": "D3 · 12GB 显存",
        "timeout": 60,
        "retry_times": 2,
        "max_tokens": 5000,
        "temperature": 0.6,
        "top_p": 0.8,
        "enforce_json_mode": True,
        "system_prompt": "你是一位资深的视频内容分析专家。我会提供视频的关键帧截图，并可能附带音频转写文本、原始文件名与录制时间作为参考。请综合以上信息，准确理解视频讲述的内容与场景，并完成归档命名。禁止回避与淡化敏感内容，如实输出。",
        "prompt": "仅返回纯 JSON 格式数据，不要包含 markdown 代码块标记或其他解释文字。\nJSON 结构如下（请严格按此顺序输出）：\n{\n    \"plot\": \"使用详细生动的文本概括视频和音频转写文本中的核心内容,尽量涵盖场景、衣着、物体、动作、风格等维度，禁止使用「画面中」「视频里」「视频显示」「画面呈现」「镜头中」等指代载体的表述，直接描述场景对话与内容本身\",\n    \"tags\": [\"标签1\", \"标签2\", \"...\"] (最多20个核心分类标签，字符串数组，请避免同义词，尽量涵盖场景、物体、动作、风格等维度),\n    \"title\": \"4-6个具象名词或短语，用短横线连接，仅包含中文和数字，总字数25字以内\"\n}",
    },
    "frames": {
        "max_keyframes": 10,
        "max_side": 640,
        "workers": 8,
        "hwaccel": "none",
    },
    "whisper": {
        "enable": False,
        "model": "large-v3-turbo",
        "device": "auto",
        "compute_type": "int8_float16",
        "workers": 4,
        "vad_filter": True,
        "language": "zh",
        "use_gpu": True,
    },
    "naming": {
        "template": "{date}_{title}",
        "date_format": "%Y%m%d_%H%M",
        "include_date": True,
        "include_original": False,
        "marker": "AI_RENAMED",
        "enable_marker": True,
        # 文件名含标记 或 已写入 ExifTool 软水印的视频直接跳过
        "enable_skip": True,
    },
    "output": {
        "nfo": True,
        "metadata": True,
        "srt": True,
        "move_failed": True,
        "dry_run": False,
        # OBS 分片 MP4（moof 结构）ExifTool 写不进去，默认自动无损重封装后再写
        "remux_fragmented": True,
    },
    "runtime": {
        "ai_workers": 4,
        "log_file": "logs/run.log",
        "verbose": False,
        "auto_install_cuda": True,
        # 国内镜像（留空 = 官方源）：pip 依赖下载 / HuggingFace 模型下载
        "pip_index": "https://pypi.org/simple",
        "hf_endpoint": "https://www.modelscope.cn",
        # 局域网访问：true = 监听 0.0.0.0（手机/同网设备可访问），重启服务后生效
        "lan": True,
    },
    "input": {
        "recursive": True,
    },
}


def deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """深合并：以 base 为底，用 patch 覆盖；只处理 dict，其余直接替换。

    这样即使旧 config.json 缺少新增键，也能自动补齐默认值。
    """
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class AppConfig:
    """配置容器：内部始终保存一份「已补全默认值」的完整字典。"""

    data: Dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG))
    path: Path = field(default_factory=lambda: paths.CONFIG_FILE)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---------- 访问 ----------
    def get(self, dotted: str, default: Any = None) -> Any:
        """按点号路径取值，如 cfg.get('ai.model')。"""
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any, *, save: bool = True) -> None:
        """按点号路径设值；默认立即写盘（§7.2 改动即时保存）。"""
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
        if save:
            self.save()

    def update(self, patch: Dict[str, Any], *, save: bool = True) -> None:
        """深合并补丁；默认立即写盘。"""
        with self._lock:
            self.data = deep_merge(self.data, patch or {})
        if save:
            self.save()

    def replace(self, whole: Dict[str, Any], *, save: bool = True) -> None:
        """整体替换（PUT /config）。

        以「当前配置」为底做深合并，而**不是**以 DEFAULT_CONFIG 为底 ——
        否则客户端未提交的键（例如用户自定义的提示词）会被悄悄重置成默认值。
        前端提交的始终是完整配置（含默认键），因此「恢复默认」依然有效。
        """
        with self._lock:
            self.data = deep_merge(self.data, whole or {})
        if save:
            self.save()

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data)

    # ---------- 便捷取值（引擎用）----------
    @property
    def frames(self) -> Dict[str, Any]:
        return self.data.get("frames", {})

    @property
    def whisper(self) -> Dict[str, Any]:
        return self.data.get("whisper", {})

    @property
    def naming(self) -> Dict[str, Any]:
        return self.data.get("naming", {})

    @property
    def output(self) -> Dict[str, Any]:
        return self.data.get("output", {})

    @property
    def runtime(self) -> Dict[str, Any]:
        return self.data.get("runtime", {})

    @property
    def ai(self) -> Dict[str, Any]:
        return self.data.get("ai", {})

    # ---------- 持久化 ----------
    def save(self) -> None:
        """原子写：先写临时文件再 os.replace，防崩溃导致配置损坏。"""
        with self._lock:
            snapshot = copy.deepcopy(self.data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError:
            # 配置写入失败不应中断服务
            pass

    def reload(self) -> "AppConfig":
        fresh = load_config(self.path)
        self.data = fresh.data
        return self


def load_config(path: "Path | str | None" = None) -> AppConfig:
    """读取 config.json；缺失或损坏时按默认值生成并落盘。"""
    target = Path(path) if path else paths.CONFIG_FILE
    data: Dict[str, Any] = {}
    if target.exists():
        try:
            raw = target.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, ValueError):
            data = {}
    cfg = AppConfig(data=deep_merge(DEFAULT_CONFIG, data), path=target)
    if not target.exists():
        cfg.save()
    return cfg
