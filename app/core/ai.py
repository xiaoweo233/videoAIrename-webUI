"""core/ai.py — AIClient：OpenAI 兼容 + Gemini 双后端（阶段④）。

移植自 batch_rename.py 的成熟逻辑：
  - 重试 / 熔断（连续失败判定上下文溢出 vs 服务无响应）
  - JSON 解析容错（剥离 markdown 代码块、正则兜底）
  - 标签去重、title 清洗
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["AIResult", "AIClient", "is_context_error", "CONTEXT_ERROR_KEYWORDS", "probe_backend"]

CONTEXT_ERROR_KEYWORDS = [
    "context_length_exceeded", "context length", "max_length",
    "maximum context length", "tokens", "message too long",
    "上下文", "too many tokens",
]

# 常见模型家族特征（用于「识别是什么模型」）
MODEL_HINTS: "List[Tuple[str, str]]" = [
    ("qwen", "Qwen（通义千问）"),
    ("deepseek", "DeepSeek"),
    ("llama", "Llama"),
    ("gemma", "Gemma"),
    ("mistral", "Mistral"),
    ("mixtral", "Mixtral"),
    ("internvl", "InternVL"),
    ("internlm", "InternLM"),
    ("minicpm", "MiniCPM"),
    ("llava", "LLaVA"),
    ("glm", "GLM / ChatGLM"),
    ("chatglm", "GLM / ChatGLM"),
    ("yi-", "Yi（零一万物）"),
    ("phi", "Phi"),
    ("claude", "Claude"),
    ("gemini", "Gemini"),
    ("moonshot", "Moonshot / Kimi"),
    ("kimi", "Moonshot / Kimi"),
    ("baichuan", "Baichuan"),
    ("gpt", "GPT"),
    ("o1", "OpenAI o-series"),
    ("smolvlm", "SmolVLM"),
]


def _friendly_net_error(msg: str, base_url: str) -> str:
    """把底层网络异常翻译成人话提示。"""
    low = (msg or "").lower()
    if "connection" in low or "refused" in low or "max retries" in low or "10061" in low:
        return (f"无法连接后端 {base_url or '(未填写)'}：请确认本地推理服务已启动"
                "（如 llama.cpp / LM Studio / Ollama / vLLM），且端口正确")
    if "timed out" in low or "timeout" in low:
        return "连接超时：服务可能未响应，或模型正在加载"
    if "401" in low or "unauthorized" in low or "api key" in low or "invalid_api_key" in low:
        return "鉴权失败：请检查 api_key"
    if "404" in low or "not found" in low:
        return "接口 404：base_url 可能少了 /v1，或该服务不兼容 OpenAI 协议"
    if "model" in low and ("not exist" in low or "not found" in low or "unknown" in low):
        return "模型名不存在：请在下方可用模型列表中选择正确的 model"
    return msg or "未知错误"


def _identify_model(reply: str, models: "List[str]", configured: str) -> str:
    """根据模型自述 + 可用列表，推断「这是什么模型」。"""
    text = (reply or "").lower()
    for hint, label in MODEL_HINTS:
        if hint in text:
            return label
    if configured and configured.lower() in text:
        return configured
    short = (reply or "").strip()
    if 0 < len(short) <= 60:
        return short
    return configured or (models[0] if models else "")


def probe_backend(cfg: "Dict[str, Any]") -> "Dict[str, Any]":
    """探测 AI 后端是否可连接，并识别模型。

    返回字段：
      ok           业务可用（一次最小对话成功）
      reachable    网络可达（/models 或对话任一成功）
      latencyMs    本次探测耗时
      models       服务端报告的可用模型 ID 列表
      modelExists  配置的 model 是否在 models 列表中（None=服务端未报告）
      identified   识别出的模型（自述 + 特征匹配）
      reply        模型对「你是什么模型」的自述
      servedModel  服务端在响应里回填的真实 model 字段
      error        失败原因（已翻译成人话）
    """
    import time as _time

    provider = str(cfg.get("provider") or "openai").lower()
    base_url = str(cfg.get("base_url") or "")
    api_key = str(cfg.get("api_key") or "not-needed")
    model = str(cfg.get("model") or "")
    timeout = max(3, int(cfg.get("timeout") or 60))

    result: "Dict[str, Any]" = {
        "ok": False, "reachable": False, "latencyMs": 0,
        "models": [], "modelExists": None, "identified": "",
        "reply": "", "servedModel": "", "error": "",
        "provider": provider, "baseUrl": base_url, "model": model,
    }

    if provider == "gemini":
        try:
            from google import genai  # type: ignore
        except ImportError:
            result["error"] = "google-genai 未安装（provider=gemini 需要）"
            return result
        try:
            client = genai.Client(api_key=api_key)
            t0 = _time.time()
            names = []
            for m in client.models.list():
                name = getattr(m, "name", "") or ""
                if name:
                    names.append(name.split("/")[-1])
            result["models"] = sorted(set(names))
            result["reachable"] = True
            result["latencyMs"] = int((_time.time() - t0) * 1000)
            if model and result["models"]:
                result["modelExists"] = any(model.lower() == i.lower() for i in result["models"])
            result["ok"] = True
            result["identified"] = _identify_model("", result["models"], model)
            return result
        except Exception as exc:  # noqa: BLE001
            result["error"] = _friendly_net_error(str(exc), base_url)
            return result

    # ---------- OpenAI 兼容 ----------
    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        result["error"] = "openai 库未安装"
        return result

    try:
        client = OpenAI(api_key=api_key, base_url=base_url or None)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"客户端初始化失败：{exc}"
        return result

    # 1) 拉取模型列表（部分服务不实现 /models，失败不致命）
    try:
        t0 = _time.time()
        page = client.models.list(timeout=min(timeout, 15))
        ids = []
        for m in getattr(page, "data", []) or []:
            mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            if mid:
                ids.append(str(mid))
        result["models"] = sorted(set(ids))
        result["reachable"] = True
        result["latencyMs"] = int((_time.time() - t0) * 1000)
        if model and result["models"]:
            result["modelExists"] = any(model.lower() == i.lower() for i in result["models"])
    except Exception as exc:  # noqa: BLE001
        result["modelsError"] = str(exc)[:300]

    # 2) 最小对话探针（决定 ok：这才是流水线真正依赖的能力）
    if not model:
        result["error"] = "未填写 model，无法发起对话探测"
        return result
    try:
        t1 = _time.time()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "只回答你的模型名称，不要解释，不要标点。"}],
            max_tokens=48,
            temperature=0,
            timeout=timeout,
        )
        reply = ""
        if resp.choices and resp.choices[0].message:
            reply = (resp.choices[0].message.content or "").strip()
        result["reply"] = reply
        result["servedModel"] = str(getattr(resp, "model", "") or "")
        result["reachable"] = True
        result["latencyMs"] = int((_time.time() - t1) * 1000)
        result["ok"] = True
        result["identified"] = _identify_model(reply, result["models"], model)
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = _friendly_net_error(str(exc), base_url)
        return result


def is_context_error(err_msg: str) -> bool:
    """判断错误是否属于「上下文窗口溢出」（方案 §13 的识别与提示）。"""
    lower = (err_msg or "").lower()
    return any(k in lower for k in CONTEXT_ERROR_KEYWORDS)


@dataclass
class AIResult:
    """AI 分析结果（阶段④ 产物）。"""

    title: str = ""
    plot: str = ""
    tags: List[str] = field(default_factory=list)
    retries: int = 0
    error: str = ""
    context_error: bool = False
    elapsed_ms: int = 0
    model: str = ""
    raw: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.title) and not self.error


class AIClient:
    """多模态 AI 客户端。

    provider=openai → OpenAI 兼容接口（llama.cpp / LM Studio / 各类中转）
    provider=gemini → Google Gemini（google-genai）
    """

    def __init__(
        self,
        *,
        provider: str = "openai",
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout: int = 60,
        retry_times: int = 2,
        max_tokens: int = 5000,
        temperature: float = 0.6,
        top_p: float = 0.8,
        enforce_json_mode: bool = False,
        system_prompt: str = "",
        prompt: str = "",
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.provider = (provider or "openai").lower()
        self.base_url = base_url
        self.api_key = api_key or "not-needed"
        self.model = model
        self.timeout = max(1, int(timeout))
        self.retry_times = max(0, int(retry_times))
        self.max_tokens = max(1, int(max_tokens))
        self.temperature = temperature
        self.top_p = top_p
        self.enforce_json_mode = bool(enforce_json_mode)
        self.system_prompt = system_prompt
        self.prompt = prompt
        self._on_log = on_log
        self._client = None

    # ---------- 日志 ----------
    def _log(self, level: str, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, msg)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 连接自检 ----------
    def check_connection(self) -> Tuple[bool, str]:
        """启动时自检：确认后端可用（对应 batch_rename.py 的 models.list）。"""
        if self.provider == "gemini":
            if not self.api_key:
                return False, "Gemini 需要 api_key"
            return True, "ok"
        try:
            client = self._get_openai()
            client.models.list(timeout=10)
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # ---------- 后端客户端 ----------
    def _get_openai(self):
        if self._client is not None:
            return self._client
        from openai import OpenAI  # type: ignore

        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)
        return self._client

    # ---------- 主入口 ----------
    def analyze(
        self,
        frames_b64: List[str],
        *,
        video_name: str = "",
        duration: float = 0.0,
        transcript: str = "",
        stop_token=None,
    ) -> AIResult:
        """分析关键帧 + 字幕，产出 title / plot / tags。"""
        import time as _time

        started = _time.time()
        result = AIResult(model=self.model)

        if not frames_b64 and not transcript:
            result.error = "无有效帧且无字幕可分析"
            return result

        if self.provider == "gemini":
            raw, attempts, err = self._call_gemini(
                frames_b64, video_name, duration, transcript, stop_token
            )
        else:
            raw, attempts, err = self._call_openai(
                frames_b64, video_name, duration, transcript, stop_token
            )

        result.retries = attempts
        result.elapsed_ms = int((_time.time() - started) * 1000)
        if err:
            result.error = err
            result.context_error = is_context_error(err)
            return result

        result.raw = raw
        try:
            title, plot, tags = self._parse_payload(raw)
        except ValueError as exc:
            result.error = str(exc)
            result.context_error = is_context_error(str(exc))
            return result

        result.title = title
        result.plot = plot
        result.tags = tags
        return result

    # ---------- 消息构造 ----------
    def _meta_suffix(self, video_name: str, duration: float, transcript: str) -> str:
        parts = ["\n\n[辅助参考信息]"]
        if video_name:
            parts.append(f"- 原始文件名: {video_name}")
        parts.append(f"- 视频总时长: {f'{duration:.1f}秒' if duration > 0 else '未知'}")
        if transcript:
            trimmed = transcript.strip()
            if len(trimmed) > 1200:
                trimmed = trimmed[:1200] + "…"
            parts.append(f"- 语音转写内容: {trimmed}")
        else:
            parts.append("- 语音转写内容: （无音轨或未启用转写）")
        return "\n".join(parts)

    def _build_messages(
        self, frames_b64: List[str], video_name: str, duration: float, transcript: str
    ) -> List[Dict]:
        messages: List[Dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        user_content: List[Dict] = [
            {"type": "text", "text": (self.prompt or "") + self._meta_suffix(video_name, duration, transcript)}
        ]
        for b64 in frames_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        messages.append({"role": "user", "content": user_content})
        return messages

    # ---------- OpenAI 兼容后端 ----------
    def _call_openai(
        self,
        frames_b64: List[str],
        video_name: str,
        duration: float,
        transcript: str,
        stop_token,
    ) -> Tuple[str, int, str]:
        try:
            from openai import (  # type: ignore
                APIConnectionError, APIStatusError, APITimeoutError,
                InternalServerError, RateLimitError,
            )
        except ImportError:
            return "", 0, "openai 库未安装"

        retryable = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)
        messages = self._build_messages(frames_b64, video_name, duration, transcript)
        last_error = "未知错误"

        for attempt in range(self.retry_times + 1):
            if stop_token is not None and stop_token.is_set():
                return "", attempt, "已取消"
            try:
                client = self._get_openai()
                kwargs: Dict = {
                    "model": self.model,
                    "messages": messages,
                    "timeout": self.timeout,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                }
                if self.enforce_json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                resp = client.chat.completions.create(**kwargs)
                if resp.choices and resp.choices[0].message:
                    raw = (resp.choices[0].message.content or "").strip()
                    if not raw:
                        raise ValueError("AI 返回空内容")
                    return raw, attempt, ""
                raise ValueError("AI 返回空内容")

            except ValueError as exc:
                last_error = str(exc)
                self._log("WARN", f"{exc}（{attempt + 1}/{self.retry_times + 1}），重试中...")
                if stop_token is not None and stop_token.wait(min(2 * (attempt + 1), 8)):
                    return "", attempt, "已取消"
            except APIStatusError as exc:
                code = getattr(exc, "status_code", 0) or 0
                last_error = f"服务器错误 ({code})"
                if 500 <= code < 600:
                    self._log("WARN", f"服务器错误 {code}（{attempt + 1}/{self.retry_times + 1}）")
                    if stop_token is not None and stop_token.wait(min(2 * (attempt + 1), 8)):
                        return "", attempt, "已取消"
                else:
                    self._log("ERROR", f"API 错误：{exc}")
                    return "", attempt, f"API错误: {exc}"
            except retryable as exc:
                last_error = str(exc)
                self._log("WARN", f"可重试错误（{attempt + 1}/{self.retry_times + 1}）：{exc}")
                if stop_token is not None and stop_token.wait(min(2 * (attempt + 1), 8)):
                    return "", attempt, "已取消"
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self._log("WARN", f"未知错误（{attempt + 1}/{self.retry_times + 1}）：{exc}")
                if stop_token is not None and stop_token.wait(min(2 * (attempt + 1), 8)):
                    return "", attempt, "已取消"

        return "", self.retry_times, last_error

    # ---------- Gemini 后端 ----------
    def _call_gemini(
        self,
        frames_b64: List[str],
        video_name: str,
        duration: float,
        transcript: str,
        stop_token,
    ) -> Tuple[str, int, str]:
        try:
            from google import genai  # type: ignore
            from google.genai import types as genai_types  # type: ignore
        except ImportError:
            return "", 0, "google-genai 未安装（provider=gemini 需要）"

        try:
            client = genai.Client(api_key=self.api_key)
        except Exception as exc:  # noqa: BLE001
            return "", 0, f"Gemini 客户端初始化失败：{exc}"

        contents: List = [(self.prompt or "") + self._meta_suffix(video_name, duration, transcript)]
        for b64 in frames_b64:
            contents.append(genai_types.Part.from_bytes(
                data=_b64decode(b64), mime_type="image/jpeg",
            ))

        last_error = "未知错误"
        for attempt in range(self.retry_times + 1):
            if stop_token is not None and stop_token.is_set():
                return "", attempt, "已取消"
            try:
                resp = client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        temperature=self.temperature,
                        top_p=self.top_p,
                        max_output_tokens=self.max_tokens,
                        system_instruction=self.system_prompt or None,
                    ),
                )
                raw = (getattr(resp, "text", "") or "").strip()
                if not raw:
                    raise ValueError("AI 返回空内容")
                return raw, attempt, ""
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self._log("WARN", f"Gemini 请求失败（{attempt + 1}/{self.retry_times + 1}）：{exc}")
                if stop_token is not None and stop_token.wait(min(2 * (attempt + 1), 8)):
                    return "", attempt, "已取消"
        return "", self.retry_times, last_error

    # ---------- 结果解析 ----------
    def _parse_payload(self, raw: str) -> Tuple[str, str, List[str]]:
        """从模型输出里抽出 title / plot / tags。"""
        if "{" not in raw or "}" not in raw:
            raise ValueError("AI 输出格式错误（未检测到 JSON 结构）")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        json_str = match.group(0) if match else raw
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise ValueError(f"AI 输出格式错误（JSON 解析失败：{str(exc)[:40]}）") from exc

        title = str(data.get("title", "") or "").strip().strip("\"'").strip()
        plot = str(data.get("plot", "") or "").strip()
        tags_raw = data.get("tags", [])

        tags: List[str] = []
        if isinstance(tags_raw, list):
            seen = set()
            for item in tags_raw:
                if not item:
                    continue
                text = str(item).strip()
                low = text.lower()
                if low and low not in seen:
                    seen.add(low)
                    tags.append(text)

        # title 清洗（与 rename.sanitize_filename 同规则，此处做长度限制）
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title)
        title = re.sub(r"\s+", "_", title).strip("._-")
        if len(title) > 50:
            title = title[:50].rstrip("._-")
        if not title:
            raise ValueError("AI 返回的 title 为空")
        return title, plot, tags

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


def _b64decode(data: str) -> bytes:
    import base64

    try:
        return base64.b64decode(data)
    except Exception:  # noqa: BLE001
        return b""
