"""core/asr_remote.py — 远程语音转写后端（自定义 API 接入）。

支持两种协议，覆盖目前常见的自建 / 第三方语音服务：

  1. `openai`  OpenAI 兼容的 /v1/audio/transcriptions
                 适用于 whisper.cpp server、LM Studio、vLLM-whisper、
                 OpenAI 官方、硅基流动、各类中转站。
  2. `gradio`  本机 Qwen3-ASR Pro（Gradio）的 /single_handler
                 具备角色识别（说话人分离）、热词、标点断句、逐字时间戳。

设计约束（红线 G6）：
  - 可选依赖缺失必须优雅降级：gradio 协议需要 gradio_client，缺失时给出可执行的提示，
    而不是让整个流水线崩掉。
  - 绝不 import UI 库（红线 G3）。
  - 网络异常要翻译成「用户能直接照做」的说明。

对外只暴露一个与 WhisperEngine 同构的接口：
    engine.load() -> bool
    engine.transcribe(audio_path, stop_token=...) -> TranscriptResult
"""
from __future__ import annotations

import json
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .whisper import TranscriptResult, TranscriptSegment, _is_cjk

__all__ = [
    "RemoteASREngine", "ASRConfig", "PROTOCOLS", "GRADIO_LANG_MAP",
    "parse_srt_segments", "group_words_into_segments", "probe_asr_backend",
]

# 界面上的协议标识 → 展示名
PROTOCOLS = {
    "openai": "OpenAI 兼容（/v1/audio/transcriptions）",
    "gradio": "Gradio 服务（Qwen3-ASR Pro /single_handler）",
}

# 本项目的语言码 → Qwen3-ASR 界面上的选项
GRADIO_LANG_MAP = {
    "auto": "自动识别",
    "zh": "Chinese",
    "en": "English",
    "yue": "Cantonese",
    "cantonese": "Cantonese",
    "chinese": "Chinese",
    "english": "English",
}


def _friendly_net_error(exc: Exception, base_url: str) -> str:
    """把底层异常翻译成用户能直接照做的说明。"""
    text = str(exc)
    low = text.lower()
    if "connection" in low or "refused" in low or "10061" in low or "max retries" in low:
        return (f"无法连接转写服务 {base_url or '(未填写地址)'}："
                "请确认该服务已启动、端口正确、地址没有被防火墙拦截")
    if "timeout" in low or "timed out" in low:
        return "转写服务响应超时：音频可能过长，或服务端正在加载模型，可在设置里加大 timeout"
    if "401" in low or "unauthorized" in low or "api key" in low:
        return "转写服务鉴权失败：请检查 api_key"
    if "404" in low or "not found" in low:
        return ("转写接口 404：OpenAI 协议请确认地址以 /v1 结尾"
                "（会请求 {base_url}/audio/transcriptions）")
    return text or "未知错误"


# ------------------------------------------------------------------
# 配置容器
# ------------------------------------------------------------------
@dataclass
class ASRConfig:
    """远程转写配置（对应 config.json 的 whisper.api 段）。"""

    protocol: str = "openai"          # openai | gradio
    base_url: str = ""                # 如 http://127.0.0.1:8080/v1 或 http://127.0.0.1:7867
    api_key: str = ""
    model: str = ""                   # OpenAI 协议的 model 字段（whisper-1 / large-v3 ...）
    timeout: int = 600                # 单次转写最长等待（秒）
    language: str = "auto"            # auto / zh / en ...
    # --- OpenAI 协议专用 ---
    prompt: str = ""                  # 热词 / 上下文提示
    # --- Gradio（Qwen3-ASR）专用 ---
    diarize: bool = False             # 说话人分离
    max_chars: int = 20               # 单行字幕字数
    punc: bool = True                 # 标点断句
    hotwords: str = ""                # 热词

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ASRConfig":
        data = data or {}
        return cls(
            protocol=str(data.get("protocol") or "openai").lower(),
            base_url=str(data.get("base_url") or "").strip(),
            api_key=str(data.get("api_key") or ""),
            model=str(data.get("model") or ""),
            timeout=int(data.get("timeout") or 600),
            language=str(data.get("language") or "auto"),
            prompt=str(data.get("prompt") or ""),
            diarize=bool(data.get("diarize", False)),
            max_chars=int(data.get("max_chars") or 20),
            punc=bool(data.get("punc", True)),
            hotwords=str(data.get("hotwords") or ""),
        )


# ------------------------------------------------------------------
# SRT 解析（Gradio 侧返回的 zip 里就是 SRT）
# ------------------------------------------------------------------
_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


# 断句标点：遇到这些字符优先结束一行（英文句点另作弱判据，见 _ends_sentence）
_SENTENCE_END = set("。！？!?…；;")

# 服务端 SRT 平均每行显示宽度低于这个值就当作「逐词碎块」，重新聚合成行。
# 正常中文字幕行宽 20~40，正常英文字幕行 4~8 个词 → 宽度 25 以上；逐词块则是个位数。
_REFLOW_MIN_WIDTH = 10


# SRT 里的说话人前缀（Qwen3-ASR 开角色识别时为 `[角色 SPEAKER_00] text`）
_SPEAKER_TAG = re.compile(r"^\[([^\]]*SPEAKER_\w+[^\]]*)\]\s*")
_SPEAKER_ID = re.compile(r"(SPEAKER_\w+)")


def _split_speaker(text: str) -> tuple[str, str]:
    """把 `[角色 SPEAKER_00] 你好` 拆成 (说话人 ID, 正文)。

    服务端的标签是 `[角色 SPEAKER_00]`（中文前缀），直接上屏太长；
    统一归一到 `SPEAKER_00`，行首拼成 `[SPEAKER_00]`。
    """
    match = _SPEAKER_TAG.match(text or "")
    if not match:
        return "", (text or "")
    ident = _SPEAKER_ID.search(match.group(1))
    return (ident.group(1) if ident else match.group(1).strip()), text[match.end():].strip()


def _ends_sentence(text: str) -> bool:
    """行尾是否已经构成一个完整句子。"""
    text = text.rstrip()
    if not text:
        return False
    if text[-1] in _SENTENCE_END:
        return True
    # 英文句点单独判断：太短的片段不切（避免 "Mr." / "1." 被切开）
    return text[-1] == "." and len(text) >= 6 and not text[-2:-1].isdigit()


def _join_word(prev: str, word: str) -> str:
    """按语言决定词与词之间要不要空格（CJK 直接相连）。"""
    if not prev:
        return word
    if _is_cjk(prev[-1]) or _is_cjk(word[0]):
        return prev + word
    return prev + " " + word


def group_words_into_segments(
    words: List[Dict[str, Any]],
    max_chars: int = 20,
    *,
    gap_seconds: float = 0.8,
) -> List[TranscriptSegment]:
    """把逐词时间轴聚合成「一行一句」的正常字幕。

    Qwen3-ASR 的 zip 里 `single_全员.srt` 是**逐词**一行（This / is / an / …），
    直接落盘观感很差；`时间.json` 里的 words 才带真正的词级时间。这里自己按
    单行字数（max_chars）、句末标点、说话人切换、静音间隔切行。

    words 元素形如：{"word": "This", "start": 0.16, "end": 0.4, "speaker": None}
    """
    limit = max(5, int(max_chars or 20))
    segments: List[TranscriptSegment] = []
    buf: List[Dict[str, Any]] = []
    text = ""          # 正文（**不含**角色标签，否则标签本身就占满一行字数）
    speaker = ""       # 本行说话人标签

    def flush() -> None:
        nonlocal buf, text, speaker
        body = text.strip()
        if body:
            start = float(buf[0].get("start") or 0.0)
            end = float(buf[-1].get("end") or start)
            if end <= start:
                end = start + max(0.2, 0.06 * len(body))
            segments.append(TranscriptSegment(
                start=round(start, 3), end=round(end, 3),
                text=f"[{speaker}] {body}" if speaker else body,
            ))
        buf = []
        text = ""
        speaker = ""

    for raw in words or []:
        word = str(raw.get("word") or "").strip()
        if not word:
            continue
        start = float(raw.get("start") or 0.0)
        spk = str(raw.get("speaker") or "")

        if buf:
            prev_end = float(buf[-1].get("end") or start)
            extra = len(word) + (0 if _is_cjk(word[0]) else 1)
            too_long = len(text) + extra > limit
            speech_break = start - prev_end > gap_seconds
            speaker_break = bool(spk and speaker and spk != speaker)
            # 句末标点只在行内已经有内容时才切，避免把孤立的 "sofa." 切成空行
            if too_long or speech_break or speaker_break or _ends_sentence(text):
                flush()

        if not buf:
            speaker = spk
        text = _join_word(text, word)
        buf.append(raw)

    flush()
    return segments


def _join_with_speakers(segments: List["TranscriptSegment"]) -> str:
    """把（可能带 `[SPEAKER_xx]` 前缀的）片段拼成整段文本。

    同一个说话人连续的几句话合成一个块（不重复标签），说话人切换时换行 ——
    转写文本是写给 AI 看的，每句都顶一个 `[SPEAKER_00]` 会把正文稀释掉。
    """
    parts: List[str] = []
    prev_speaker = ""
    prev_tail = ""
    for seg in segments:
        speaker, body = _split_speaker(seg.text)
        if not body:
            continue
        if speaker and speaker != prev_speaker:
            if parts:
                parts.append("\n")
            parts.append(f"[{speaker}] ")
            prev_tail = " "
        elif parts and prev_tail:
            if not (_is_cjk(prev_tail[-1]) or _is_cjk(body[0])):
                parts.append(" ")
        parts.append(body)
        prev_speaker = speaker or prev_speaker
        prev_tail = body
    return "".join(parts).strip()


def parse_srt_segments(srt_text: str) -> List[TranscriptSegment]:
    """把 SRT 文本解析成 TranscriptSegment 列表。

    Qwen3-ASR 返回的 zip 里 `single_全员.srt` 不一定可以直接用：中文场景服务端已经断好句，
    英文场景却是**逐词一行**。所以这里只负责老老实实解析成片段，要不要重新聚合成行
    由 `_needs_reflow()` 判断。
    """
    segments: List[TranscriptSegment] = []
    if not srt_text:
        return segments

    blocks = re.split(r"\r?\n\r?\n", srt_text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        # 找到时间轴那一行（可能第 1 行是序号）
        idx = 0
        while idx < len(lines) and "-->" not in lines[idx]:
            idx += 1
        if idx >= len(lines):
            continue
        match = _SRT_TIME.search(lines[idx])
        if not match:
            continue
        start = _ts_to_seconds(*match.group(1, 2, 3, 4))
        end = _ts_to_seconds(*match.group(5, 6, 7, 8))
        text = "\n".join(lines[idx + 1:]).strip()
        if text:
            segments.append(TranscriptSegment(start=start, end=end, text=text))
    return segments


def _normalize_speakers(segments: List[TranscriptSegment]) -> List[TranscriptSegment]:
    """把行首的 `[角色 SPEAKER_00]` 归一成 `[SPEAKER_00]`（服务端原文中文前缀太长）。"""
    for seg in segments:
        head, body = _split_speaker(seg.text)
        if head:
            seg.text = f"[{head}] {body}" if body else f"[{head}]"
    return segments


def _needs_reflow(segments: List[TranscriptSegment]) -> bool:
    """服务端 SRT 是否需要客户端重新聚合成行。

    实测：中文场景服务端已按标点断好句（「大家好，」/「今天我们来测试…」），
    直接使用效果最好；英文场景却是逐词一行（This / is / an / …），直接用会变成
    一词一字幕。判据用「平均显示宽度」（CJK 计 2，西文计 1）：
      · 正常中文字幕行 → 20~45
      · 正常英文字幕行 → 25~40
      · 逐词碎块（中/英）→ 个位数
    平均值低于 `_REFLOW_MIN_WIDTH` 就当作「碎块」，交给 `group_words_into_segments()`
    重新聚合。（早期版本还拿「平均词数 < 1.8」做判据，但中文整句没有空格，
    `split()` 永远返回 1 个词，会把好的中文字幕误判成碎块。）
    """
    if len(segments) < 3:
        return False
    width_total = 0
    counted = 0
    for seg in segments:
        body = _split_speaker(seg.text)[1]
        if not body:
            continue
        width_total += sum(2 if _is_cjk(ch) else 1 for ch in body)
        counted += 1
    if not counted:
        return False
    return (width_total / counted) < _REFLOW_MIN_WIDTH


# ------------------------------------------------------------------
# 引擎
# ------------------------------------------------------------------
class RemoteASREngine:
    """把「远程 HTTP 转写服务」包装成与 WhisperEngine 同构的接口。"""

    def __init__(self, cfg: ASRConfig, *, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        self.cfg = cfg
        self._on_log = on_log
        self._load_error = ""
        self._ready = False
        self._actual_device = "remote"
        self._gradio_client = None

    # ---------- 日志 ----------
    def _log(self, level: str, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(level, msg)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 属性（与 WhisperEngine 对齐） ----------
    @property
    def device(self) -> str:
        return self._actual_device

    @property
    def local_model_path(self) -> str:
        return ""

    @property
    def load_error(self) -> str:
        return self._load_error

    @property
    def model_name(self) -> str:
        return self.cfg.model or self.cfg.protocol

    # ---------- 加载 ----------
    def load(self, *, probe: bool = True, probe_timeout: float = 8.0) -> bool:
        """远程后端没有本地模型要加载，这里做配置、依赖与**连通性**自检。

        连通性检查很关键：以前只校验配置就返回 True，导致服务根本没起来时
        WhisperEngine 也以为「远程就绪」，直到每个视频转写失败才暴露，
        「API 不可用自动回退本地」在加载阶段完全不生效。

        probe=False 时跳过网络探测（供已经确定要直接试转写的内部路径使用）。
        """
        if self._ready:
            return True
        cfg = self.cfg
        if not cfg.base_url:
            self._load_error = "未填写转写服务地址（设置 → 转写 → 自定义 API → base_url）"
            self._log("ERROR", self._load_error)
            return False

        if cfg.protocol == "gradio":
            try:
                from gradio_client import Client  # type: ignore  # noqa: F401
            except ImportError:
                self._load_error = ("gradio 协议需要 gradio_client 库："
                                    "会在服务端安装，或手动 pip install gradio_client")
                self._log("WARN", self._load_error)
                return False

        if probe:
            # 只做协议层探测（秒级），不发送音频；失败即视为远程不可用，
            # 让上层（WhisperEngine）决定是否回退本地。
            try:
                res = probe_asr_backend(cfg, timeout=probe_timeout)
            except Exception as exc:  # noqa: BLE001 - 探测异常同样算不可用
                self._load_error = _friendly_net_error(exc, cfg.base_url)
                self._log("WARN", f"转写服务探测异常：{self._load_error}")
                return False
            if not res.get("ok"):
                self._load_error = res.get("error") or "转写服务不可用"
                self._log("WARN", f"转写服务不可用：{self._load_error}")
                return False
            if res.get("detail"):
                self._log("DEBUG", f"服务探测：{res['detail']}")

        self._ready = True
        self._log("INFO", f"远程转写后端就绪：{cfg.protocol} · {cfg.base_url}")
        return True

    # ---------- 转写 ----------
    def transcribe(
        self,
        audio_path: str,
        *,
        stop_token=None,
        beam_size: int = 5,   # 与 WhisperEngine 签名保持兼容，远程后端不使用
    ) -> TranscriptResult:
        result = TranscriptResult(model=self.model_name)
        if stop_token is not None and stop_token.is_set():
            result.skipped = True
            result.error = "已取消"
            return result
        if not self.load():
            result.skipped = True
            result.error = self._load_error or "远程转写后端不可用"
            return result
        if not os.path.isfile(audio_path):
            result.skipped = True
            result.error = f"音频文件不存在：{audio_path}"
            return result

        started = time.time()
        try:
            if self.cfg.protocol == "gradio":
                segments, language = self._transcribe_gradio(audio_path, stop_token)
            else:
                segments, language = self._transcribe_openai(audio_path, stop_token)
        except Exception as exc:  # noqa: BLE001 - 远程失败不致命，降级为纯画面分析
            result.error = _friendly_net_error(exc, self.cfg.base_url)
            result.elapsed_ms = int((time.time() - started) * 1000)
            self._log("WARN", f"远程转写失败：{result.error}")
            return result

        result.segments = segments
        result.text = _join_with_speakers(segments)
        result.language = language
        result.elapsed_ms = int((time.time() - started) * 1000)
        self._log("DEBUG", f"远程转写完成：字幕 {result.chars} 字 · 用时 {result.elapsed_ms} ms")
        return result

    # ---------- 协议一：OpenAI 兼容 ----------
    def _transcribe_openai(self, audio_path: str, stop_token=None):
        """POST {base_url}/audio/transcriptions（multipart）。"""
        import urllib.error
        import urllib.request
        import uuid

        cfg = self.cfg
        url = cfg.base_url.rstrip("/") + "/audio/transcriptions"
        filename = os.path.basename(audio_path)
        with open(audio_path, "rb") as fh:
            file_bytes = fh.read()

        boundary = "----vair" + uuid.uuid4().hex
        parts: List[bytes] = []

        def _field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )

        _field("model", cfg.model or "whisper-1")
        _field("response_format", "verbose_json")
        _field("temperature", "0")
        if cfg.language and cfg.language.lower() not in ("auto", "", "none"):
            _field("language", cfg.language)
        if cfg.prompt:
            _field("prompt", cfg.prompt)

        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n".encode("utf-8")
        )
        parts.append(file_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        if cfg.api_key:
            req.add_header("Authorization", f"Bearer {cfg.api_key}")

        timeout = max(10, int(cfg.timeout))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"HTTP {exc.code}：{detail or exc.reason}") from exc

        payload: Dict[str, Any] = {}
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}

        if isinstance(payload, dict) and payload.get("segments"):
            segments = [
                TranscriptSegment(
                    start=float(seg.get("start") or 0.0),
                    end=float(seg.get("end") or 0.0),
                    text=str(seg.get("text") or "").strip(),
                )
                for seg in payload["segments"]
                if str(seg.get("text") or "").strip()
            ]
            language = str(payload.get("language") or "")
            if segments:
                return segments, language

        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("text") or "").strip()
        else:
            text = (raw or "").strip()

        if not text:
            raise RuntimeError("转写服务返回了空文本（音频可能没有语音内容）")
        # 服务端没给分段时间轴时，退化为「整段一条字幕」，保证 SRT 仍能产出
        return [TranscriptSegment(start=0.0, end=0.0, text=text)], ""

    # ---------- 协议二：Gradio（Qwen3-ASR Pro） ----------
    def _transcribe_gradio(self, audio_path: str, stop_token=None):
        """调用 Qwen3-ASR Pro 的 /single_handler，取回 zip 并解析 SRT。"""
        import urllib.parse
        import urllib.request

        from gradio_client import Client, handle_file  # type: ignore

        cfg = self.cfg
        base = cfg.base_url.rstrip("/")
        lang = GRADIO_LANG_MAP.get(
            (cfg.language or "auto").lower(),
            "自动识别" if (cfg.language or "auto").lower() in ("auto", "", "none")
            else cfg.language,
        )

        if self._gradio_client is None:
            self._gradio_client = Client(base)

        # 参数顺序严格按服务端签名：file, lang, diarize, max_c, punc, hotwords
        out = self._gradio_client.predict(
            handle_file(audio_path),
            lang,
            bool(cfg.diarize),
            max(5, int(cfg.max_chars)),
            bool(cfg.punc),
            cfg.hotwords or "",
            api_name="/single_handler",
        )

        status, full_text, srt_preview, zip_path = (list(out) + ["", "", "", ""])[:4]
        if stop_token is not None and stop_token.is_set():
            return [], ""

        # 服务端返回 zip 的绝对路径：同机读盘优先，跨机走 /gradio_api/file=
        words, srt_text = self._read_result_from_zip(zip_path)
        if not srt_text:
            srt_text = str(srt_preview or "")

        # 服务端那个 SRT 有时可以直接用（中文场景已按标点断好句），有时是逐词一行
        # （英文场景）。先解析出来，再看它是不是「碎块」：是才用自己的聚合器重排。
        segments = parse_srt_segments(srt_text)
        if words and (not segments or _needs_reflow(segments)):
            segments = group_words_into_segments(words, cfg.max_chars)
        else:
            segments = _normalize_speakers(segments)
        if not segments:
            text = str(full_text or "").strip()
            if not text:
                raise RuntimeError(f"转写服务未返回内容（状态：{status}）")
            segments = [TranscriptSegment(start=0.0, end=0.0, text=text)]

        language = ""
        if lang == "Chinese":
            language = "zh"
        elif lang == "English":
            language = "en"
        return segments, language

    # ---------- 结果 zip 读取（本地盘 / HTTP 两种来源） ----------
    def _read_result_from_zip(self, zip_path: Any) -> tuple[List[Dict[str, Any]], str]:
        """返回 (词级时间轴, SRT 文本)。任一项可能为空。"""
        if not zip_path:
            return [], ""
        path = str(zip_path)
        try:
            if os.path.isfile(path):
                with zipfile.ZipFile(path) as zf:
                    return self._extract_zip(zf)
            if path.startswith("http://") or path.startswith("https://"):
                return self._read_result_from_url(path)

            base = self.cfg.base_url.rstrip("/")
            url = f"{base}/gradio_api/file={path}"
            return self._read_result_from_url(url)
        except Exception as exc:  # noqa: BLE001 - 读不到 zip 时退回用 SRT 预览
            self._log("DEBUG", f"读取结果 zip 失败（改用 SRT 预览）：{exc}")
            return [], ""

    def _read_result_from_url(self, url: str) -> tuple[List[Dict[str, Any]], str]:
        """下载 zip 后在内存里解析（zipfile 需要可 seek 的句柄）。"""
        import io
        import urllib.request

        with urllib.request.urlopen(url, timeout=max(10, int(self.cfg.timeout))) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return self._extract_zip(zf)

    @classmethod
    def _extract_zip(cls, zf: "zipfile.ZipFile") -> tuple[List[Dict[str, Any]], str]:
        """从结果 zip 里同时取出词级时间轴与 SRT。

        词表**以 SRT 为准**：`时间.json` 的词是归一化过的（`end-to-end` →
        `endtoend`，句尾标点也丢了），直接拿来成行会得到没有标点的字幕。
        JSON 只用来补 speaker（SRT 里没有说话人），数量对不上就不补。
        """
        names = zf.namelist()
        srt_text = cls._pick_srt(zf)
        words: List[Dict[str, Any]] = []
        for seg in parse_srt_segments(srt_text):
            speaker, body = _split_speaker(seg.text)
            if body:
                words.append({"word": body, "start": seg.start, "end": seg.end,
                              "speaker": speaker})

        json_name = next(
            (n for n in names if n.lower().endswith(".json") and "时间" in n),
            next((n for n in names if n.lower().endswith(".json")), ""),
        )
        if json_name:
            try:
                payload = json.loads(zf.read(json_name).decode("utf-8", errors="replace"))
                raw = [w for w in ((payload or {}).get("words") or [])
                       if isinstance(w, dict) and str(w.get("word") or "").strip()]
            except Exception:  # noqa: BLE001 - JSON 坏了就只用 SRT
                raw = []
            if len(raw) == len(words):
                for word, extra in zip(words, raw):
                    # SRT 的词带说话人，JSON 的不带；只在 SRT 没给时才补
                    word["speaker"] = word["speaker"] or str(extra.get("speaker") or "")
            elif raw:
                # 词数对不上（服务端断词策略不同）→ 用 JSON 自己的词，至少时间轴更准
                words = [{"word": str(w.get("word") or "").strip(),
                          "start": float(w.get("start") or 0.0),
                          "end": float(w.get("end") or 0.0),
                          "speaker": str(w.get("speaker") or "")} for w in raw]
        return words, srt_text

    @staticmethod
    def _pick_srt(zf: "zipfile.ZipFile") -> str:
        """优先取「全员」字幕，否则取第一个 .srt。"""
        names = [n for n in zf.namelist() if n.lower().endswith(".srt")]
        if not names:
            return ""
        target = next((n for n in names if "全员" in n), names[0])
        with zf.open(target) as fh:
            return fh.read().decode("utf-8", errors="replace")

    def reset(self) -> None:
        """丢弃「就绪」判定，让下一次 load 重新探测连通性。

        新任务开始时由 WhisperEngine.reset_task_state 调用：远程服务可能
        在上一批之后才启动（或重启过），必须重新探一次才不误判。
        """
        self._ready = False
        self._load_error = ""
        self._gradio_client = None

    def close(self) -> None:
        self._gradio_client = None


# ------------------------------------------------------------------
# 连通性探测（供 /api/asr/test 使用）
# ------------------------------------------------------------------
def probe_asr_backend(cfg: ASRConfig, *, timeout: float = 15.0) -> Dict[str, Any]:
    """探测远程转写服务是否可用，返回给人看的结果。

    不发送真实音频（那太慢），只做协议层面的最小请求：
      - openai：GET {base_url}/models（很多服务不实现，失败也不算致命）
      - gradio ：GET {base_url}/config（Gradio 必有）

    timeout：单次探测的网络超时。引擎加载时用较短值（默认 8s），
    用户在设置页点「测试连接」用较长值，避免远程挂掉时整批任务被拖住。
    """
    import urllib.error
    import urllib.request

    result: Dict[str, Any] = {
        "ok": False, "reachable": False, "latencyMs": 0,
        "protocol": cfg.protocol, "baseUrl": cfg.base_url,
        "detail": "", "models": [], "error": "",
    }
    if not cfg.base_url:
        result["error"] = "未填写服务地址"
        return result

    base = cfg.base_url.rstrip("/")
    if cfg.protocol == "gradio":
        url = f"{base}/config"
    else:
        url = f"{base}/models"

    started = time.time()
    try:
        req = urllib.request.Request(url, method="GET")
        if cfg.api_key and cfg.protocol != "gradio":
            req.add_header("Authorization", f"Bearer {cfg.api_key}")
        with urllib.request.urlopen(req, timeout=max(2.0, float(timeout))) as resp:
            raw = resp.read(200000).decode("utf-8", errors="replace")
        result["reachable"] = True
        result["ok"] = True
        result["latencyMs"] = int((time.time() - started) * 1000)
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if cfg.protocol == "gradio":
            # Gradio 6.x 的 api_name 不带前导斜杠（"single_handler"），
            # 5.x 及更早则带（"/single_handler"），两种都要认。
            apis = {
                str(d.get("api_name") or "").lstrip("/")
                for d in (data.get("dependencies") or [])
            }
            found = "single_handler" in apis
            result["detail"] = (
                f"Gradio 服务已就绪（version={data.get('version', '?')}，"
                f"接口：{', '.join(sorted(a for a in apis if a)) or '无'}）"
            )
            if not found:
                result["ok"] = False
                result["error"] = ("服务可达，但未找到 single_handler 接口"
                                   "——确认这是 Qwen3-ASR Pro 而不是别的 Gradio 应用")
        else:
            models = [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict)]
            result["models"] = models
            result["detail"] = f"OpenAI 兼容服务可达（{len(models)} 个模型）" if models else "服务可达"
            if cfg.model and models and cfg.model not in models:
                result["detail"] += f"；注意：配置的 model「{cfg.model}」不在返回列表中"
    except urllib.error.HTTPError as exc:
        result["reachable"] = True
        result["latencyMs"] = int((time.time() - started) * 1000)
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        # 服务在跑但该端点不存在：对 gradio 是致命（说明不是目标服务），对 openai 可接受
        if cfg.protocol == "gradio":
            result["error"] = f"HTTP {exc.code}：这不是可用的 Gradio 服务（{body or exc.reason}）"
        else:
            result["ok"] = True
            result["detail"] = (f"服务可达（/models 返回 HTTP {exc.code}，"
                                "常见于不实现该端点的本地推理服务，不影响转写）")
    except Exception as exc:  # noqa: BLE001
        result["latencyMs"] = int((time.time() - started) * 1000)
        result["error"] = _friendly_net_error(exc, cfg.base_url)
    return result
