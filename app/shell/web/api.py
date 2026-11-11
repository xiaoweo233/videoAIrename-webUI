"""shell/web/api.py — FastAPI 后端（对齐前端契约，前端零改动即可对接）。

端点（严格对应 static/README.md「预留端点」表）：
  POST /api/tasks                 创建任务   body { path, ... } 或 { paths: [...] }
  POST /api/tasks/{id}/stop       停止任务
  GET  /api/events                SSE 事件流（事件格式与 simulator.js 完全一致）
  GET  /api/logs                  拉取历史日志
  GET  /api/config                读取配置
  PUT  /api/config                保存配置
  POST /api/review/{id}           提交审批

附加（只读辅助，不改变既有契约）：
  GET  /api/health                健康检查 + 运行期探测
  GET  /api/tasks                 任务列表
  GET  /api/tasks/{id}            任务详情
  GET  /api/tasks/{id}/files      任务下文件明细
  GET  /api/reviews               审批列表
  GET  /api/runtime               运行期依赖报告
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ...core.ai import probe_backend
from ...core.base import EventBus
from ...db import init_db, repo
from ...infra import bootstrap, dialogs
from ...infra.config import load_config
from ...infra.paths import APP_ROOT
from .scheduler import TaskConflictError, TaskScheduler
from .sse import SSEBroker

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ------------------------------------------------------------------
# 全局单例（进程级）
# ------------------------------------------------------------------
event_bus = EventBus(max_log=500)
sse_broker = SSEBroker()
config = load_config()
scheduler = TaskScheduler(config, event_bus, sse_broker)


# ------------------------------------------------------------------
# 生命周期
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) 初始化数据库 + 崩溃恢复
    init_db()
    recovered = repo.recover_orphan_tasks()

    # 1.5) 应用配置里的下载镜像（pip 源 / HuggingFace 端点）
    bootstrap.apply_mirrors(config.get("runtime.pip_index"),
                            config.get("runtime.hf_endpoint"))

    # 2) 绑定事件循环，供 SSE 桥使用
    sse_broker.bind_loop(asyncio.get_running_loop())

    # 3) 把 EventBus 的事件同时喂给 SSE 桥
    event_bus.subscribe(sse_broker.on_event)

    # 4) 启动日志
    report = bootstrap.probe_runtime()
    event_bus.emit_status(connected=True, mode="sse")
    event_bus.emit_log("INFO", "VAIR Web 外壳已就绪")
    event_bus.emit_log("DEBUG", f"ffmpeg: {report.ffmpeg or '未找到'} · ffprobe: {report.ffprobe or '未找到'}"
                                f" · exiftool: {report.exiftool or '未找到'}", source="startup")
    if report.faster_whisper:
        event_bus.emit_log("INFO", f"Whisper 就绪：{config.whisper.get('model')} · "
                                  f"{config.whisper.get('device')} · {config.whisper.get('compute_type')}",
                           source="startup")
    else:
        event_bus.emit_log("WARN", "Whisper 未就绪：将仅用画面分析（可在设置中开启依赖安装）",
                           source="startup")
    event_bus.emit_log("INFO", f"AI 后端：{config.ai.get('provider')} · {config.ai.get('base_url')} · "
                              f"model={config.ai.get('model')}", source="startup")
    if recovered:
        event_bus.emit_log("WARN", f"检测到 {recovered} 个上次未正常结束的任务，已标记为 interrupted",
                           source="startup")

    try:
        yield
    finally:
        scheduler.stop()
        event_bus.emit_status(connected=False, mode="offline")


app = FastAPI(title="视频 AI 重命名助手", version="1.0.0", lifespan=lifespan)

# 允许局域网 / 本地调试跨域（前端也可同源访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """前端 JS/CSS 一律「可缓存但每次校验」（协商缓存）。

    不加这个的话，改完 settings.js 刷新页面仍可能拿到浏览器里的旧副本 ——
    表现就是「代码明明加了开关/按钮，界面上却看不到」。
    文件没变时浏览器会收到 304，代价只是一次条件请求。
    """
    response = await call_next(request)
    path = (request.url.path or "")
    if (path.startswith("/js/") or path.startswith("/css/")
            or path in ("/", "/index.html")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ------------------------------------------------------------------
# 请求模型
# ------------------------------------------------------------------
class CreateTaskBody(BaseModel):
    path: Optional[str] = None
    paths: Optional[List[str]] = None
    dry_run: Optional[bool] = None
    recursive: Optional[bool] = None


class ReviewBody(BaseModel):
    decision: str
    newName: Optional[str] = None


class DialogBody(BaseModel):
    initial_dir: Optional[str] = None
    multi: Optional[bool] = None
    timeout: Optional[float] = None


class AITestBody(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    timeout: Optional[int] = None


class ASRTestBody(BaseModel):
    """自定义转写 API 的测试参数。"""

    protocol: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    timeout: Optional[int] = None
    # deep=True 时：生成一段 3~10s 的测试语音并真跑一次转写（验证模型真可用）
    deep: bool = False


class ModelDownloadBody(BaseModel):
    """Whisper 模型下载参数。"""

    model: Optional[str] = None
    force: Optional[bool] = False


# ------------------------------------------------------------------
# /api/health —— 健康检查
# ------------------------------------------------------------------
@app.get("/api/health")
async def health() -> Dict[str, Any]:
    report = bootstrap.probe_runtime()
    return {
        "ok": True,
        "app": "vair",
        "version": app.version,
        "appRoot": str(APP_ROOT),
        "scheduler": scheduler.status(),
        "runtime": report.as_dict(),
    }


def _env_port(default: int = 8000) -> int:
    try:
        return int(os.environ.get("VAIR_PORT", "") or default)
    except ValueError:
        return default


# 初值同时读环境变量：uvicorn --reload 会另起子进程，模块级赋值传不过去
SERVER_INFO: Dict[str, Any] = {
    "host": os.environ.get("VAIR_HOST", "127.0.0.1"),
    "port": _env_port(),
}


def set_server_info(host: str, port: int) -> None:
    """由 run.py 注入实际监听地址（供前端展示访问方式）。"""
    SERVER_INFO["host"] = host or "127.0.0.1"
    SERVER_INFO["port"] = int(port or 0)
    os.environ["VAIR_HOST"] = SERVER_INFO["host"]
    os.environ["VAIR_PORT"] = str(SERVER_INFO["port"])


def _is_fake_ip(ip: str) -> bool:
    """判断是不是「不能用」的地址。

    本机实测（用户机器上装了对代理软件）：
      - gethostname() 解析出 198.18.0.1 和 192.168.1.108；
      - socket.connect(("8.8.8.8", 80)) 拿到 198.18.0.1（被 TUN 劫持）。
    198.18.0.0/15 是 RFC 2544 保留的**基准测试网段**，代理软件（Clash/Surge 的
    TUN 模式等）会把它当假地址用。手机根本连不上，因此必须排除。
    """
    try:
        parts = [int(x) for x in ip.split(".")]
    except (ValueError, AttributeError):
        return True
    if len(parts) != 4 or any(x < 0 or x > 255 for x in parts):
        return True
    a, b = parts[0], parts[1]
    if a == 127:                              # 回环
        return True
    if a == 169 and b == 254:                 # 链路本地（没连上 DHCP）
        return True
    if a == 198 and b in (18, 19):            # RFC 2544 基准测试网段（代理 TUN）
        return True
    if a == 0:
        return True
    if a >= 224:                              # 组播 / 保留
        return True
    return False


def _lan_ips(limit: int = 6) -> list:
    """列出本机真实可用的局域网 IPv4（排除回环 / 代理 TUN 假地址）。

    顺序按「手机最可能连上的」排：先 192.168 / 10. / 172.16-31 这类私有网段。
    """
    import socket

    candidates: list = []

    def _add(ip: str) -> None:
        if not ip or _is_fake_ip(ip) or ip in candidates:
            return
        candidates.append(ip)

    # 1) 主动连一个「内网」地址：走默认路由，拿到的是真实出口网卡
    #    连公网（8.8.8.8）会被代理劫持成 198.18.x，必须连内网网关或私有地址。
    for probe in ("192.168.1.1", "10.0.0.1", "172.16.0.1"):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect((probe, 80))
            _add(s.getsockname()[0])
        except OSError:
            pass
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    # 2) 主机名解析（能覆盖多网卡场景）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass

    # 3) 兜底：Windows 上枚举网卡（前两步都失败时）
    if not candidates:
        try:
            import subprocess

            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-NetIPAddress -AddressFamily IPv4 | "
                 "Where-Object { $_.PrefixOrigin -ne 'WellKnown' } | "
                 "Select-Object -ExpandProperty IPAddress"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in (proc.stdout or "").splitlines():
                _add(line.strip())
        except Exception:  # noqa: BLE001 - 兜底失败不影响主流程
            pass

    # 私有网段优先（手机一定在同一个私有网段里）
    def _rank(ip: str) -> int:
        a, b = int(ip.split(".")[0]), int(ip.split(".")[1])
        if a == 192 and b == 168:
            return 0
        if a == 10:
            return 1
        if a == 172 and 16 <= b <= 31:
            return 2
        return 3

    candidates.sort(key=_rank)
    return candidates[:limit]


@app.get("/api/server")
async def server_info() -> Dict[str, Any]:
    """当前服务监听方式与可访问地址（用于设置页的「局域网访问」开关）。"""
    host = SERVER_INFO["host"]
    port = SERVER_INFO["port"]
    lan = host not in ("127.0.0.1", "localhost")
    cfg_lan = bool(config.runtime.get("lan", False))
    return {
        "host": host,
        "port": port,
        "lan": lan,
        "config_lan": cfg_lan,          # 配置值（重启后生效）
        "restart_required": lan != cfg_lan,
        "local_url": f"http://127.0.0.1:{port}/",
        "lan_urls": [f"http://{ip}:{port}/" for ip in _lan_ips()] if lan else [],
    }


@app.get("/api/runtime")
async def runtime_report() -> Dict[str, Any]:
    return bootstrap.probe_runtime().as_dict()


# ------------------------------------------------------------------
# /api/dialog —— 系统原生文件 / 文件夹选择器
#
# 说明：这两个端点用「同步 def」声明（非 async），FastAPI 会把它们丢进线程池，
#       于是阻塞等待用户点选不会卡住事件循环。浏览器拿不到绝对路径，
#       因此必须由服务端（本机）弹原生对话框。
# ------------------------------------------------------------------
@app.post("/api/dialog/files")
def dialog_files(body: DialogBody = Body(default_factory=DialogBody)) -> Dict[str, Any]:
    try:
        result = dialogs.pick_files(
            multi=True if body.multi is None else bool(body.multi),
            initial_dir=body.initial_dir or "",
            timeout=float(body.timeout) if body.timeout else dialogs.DEFAULT_TIMEOUT,
        )
    except dialogs.DialogBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not result.get("ok") and not result.get("cancelled"):
        raise HTTPException(status_code=400, detail=result.get("error") or "选择失败")
    return result


@app.post("/api/dialog/folder")
def dialog_folder(body: DialogBody = Body(default_factory=DialogBody)) -> Dict[str, Any]:
    try:
        result = dialogs.pick_folder(
            initial_dir=body.initial_dir or "",
            timeout=float(body.timeout) if body.timeout else dialogs.DEFAULT_TIMEOUT,
        )
    except dialogs.DialogBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not result.get("ok") and not result.get("cancelled"):
        raise HTTPException(status_code=400, detail=result.get("error") or "选择失败")
    return result


@app.post("/api/dialog/cancel")
async def dialog_cancel() -> Dict[str, Any]:
    """取消当前挂起的系统选择器（解决「看不到窗口但一直提示已打开」）。"""
    return dialogs.cancel_dialog()


@app.get("/api/dialog/status")
async def dialog_status() -> Dict[str, Any]:
    return dialogs.dialog_state()


# ------------------------------------------------------------------
# /api/mirrors —— 下载镜像（pip 源 / HuggingFace 端点）
# ------------------------------------------------------------------
@app.get("/api/mirrors")
async def get_mirrors() -> Dict[str, Any]:
    cur = bootstrap.mirror_config()
    rt = config.runtime
    return {
        **cur,
        "config": {
            "pip_index": rt.get("pip_index", ""),
            "hf_endpoint": rt.get("hf_endpoint", ""),
        },
    }


# ------------------------------------------------------------------
# /api/ai/test —— 测试 AI 后端连通性 + 识别模型
# ------------------------------------------------------------------
@app.post("/api/ai/test")
def ai_test(body: AITestBody = Body(default_factory=AITestBody)) -> Dict[str, Any]:
    saved = config.ai
    merged = {
        "provider": body.provider or saved.get("provider", "openai"),
        "base_url": body.base_url if body.base_url is not None else saved.get("base_url", ""),
        "api_key": body.api_key if body.api_key is not None else saved.get("api_key", ""),
        "model": body.model if body.model is not None else saved.get("model", ""),
        "timeout": body.timeout if body.timeout is not None else saved.get("timeout", 60),
    }
    try:
        result = probe_backend(merged)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"探测失败：{exc}") from exc
    if result.get("ok"):
        event_bus.emit_log(
            "INFO",
            f"AI 连接测试通过 · 模型={result.get('identified') or merged['model']} · "
            f"{result.get('latencyMs')}ms",
            source="ai-test",
        )
    else:
        event_bus.emit_log("WARN", f"AI 连接测试失败：{result.get('error')}", source="ai-test")
    return result


# ------------------------------------------------------------------
# /api/whisper —— 转写运行环境（GPU / CUDA 运行库）
# ------------------------------------------------------------------
@app.get("/api/whisper/status")
async def whisper_status() -> Dict[str, Any]:
    report = bootstrap.probe_runtime()
    w = config.whisper
    backend = str(w.get("backend") or "auto").lower()
    prefer_api = bool(w.get("prefer_api", True))
    api_cfg = w.get("api") or {}
    api_base = str(api_cfg.get("base_url") or "").strip()

    # 推得「本该生效」的后端（尚未运行时无法知道实际回退结果）
    if backend == "api":
        effective = "api"
    elif backend == "local":
        effective = "local"
    else:
        effective = "api" if (prefer_api and api_base) else "local"

    # 本地模型是否已就绪（目录存在且关键文件齐全）
    local_ready = False
    local_path = ""
    model_dir = ""
    download_hint = None
    try:
        from ...core.whisper import resolve_local_model
        from ...infra.paths import MODELS_DIR

        model_name = str(w.get("model") or "large-v3-turbo")
        root = MODELS_DIR / "whisper"
        local_path = resolve_local_model(model_name, str(root))
        local_ready = bool(local_path)
        model_dir = str(root / model_name)
        if not local_ready:
            download_hint = _model_download_progress(root / model_name)
    except Exception:  # noqa: BLE001 - 探测失败不阻断状态查询
        pass

    return {
        "faster_whisper": report.faster_whisper,
        "cuda_available": report.cuda_available,
        "cuda_reason": report.cuda_reason,
        "cuda_libs": bootstrap.cuda_libs_status(),
        "use_gpu": bool(w.get("use_gpu", True)),
        "device": w.get("device", "auto"),
        # 后端与模型状态
        "backend": backend,
        "prefer_api": prefer_api,
        "effective_backend": effective,
        "api_ready": bool(api_base),
        "model": w.get("model", "large-v3-turbo"),
        "model_ready": local_ready,
        "model_path": local_path,
        "model_dir": model_dir,
        "download": download_hint,
        "api": {
            "protocol": api_cfg.get("protocol", "openai"),
            "base_url": api_base,
            "model": api_cfg.get("model", ""),
        },
    }


def _model_download_progress(model_dir) -> Optional[Dict[str, Any]]:
    """汇总模型目录里已下载的字节，给界面显示「已下载 / 总计」。

    只看 model.bin（它占了 99% 的体积），并把 .incomplete 与半成品 model.bin
    中较大的那份算作已下载 —— 这与 _download_one_file 的续传起点一致。
    """
    try:
        from pathlib import Path

        d = Path(model_dir)
        if not d.is_dir():
            return None
        main = d / "model.bin"
        part = d / "model.bin.incomplete"
        have = 0
        for p in (main, part):
            try:
                if p.is_file():
                    have = max(have, p.stat().st_size)
            except OSError:
                pass
        files = []
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            files.append({"name": p.name, "size": p.stat().st_size})
        return {"dir": str(d), "modelBinBytes": have, "files": files}
    except Exception:  # noqa: BLE001
        return None


@app.post("/api/whisper/model")
def whisper_download_model(body: ModelDownloadBody = Body(default_factory=ModelDownloadBody)) -> Dict[str, Any]:
    """下载 / 续传 Whisper 模型到 models/whisper/<model>/（断点续传，弱网可反复重试）。"""
    w = config.whisper
    model_name = (body.model or w.get("model") or "large-v3-turbo").strip()
    logs: List[str] = []

    def _log(msg: str) -> None:
        logs.append(msg)
        event_bus.emit_log("INFO", msg, source="model-download")

    from ...infra.paths import MODELS_DIR

    try:
        result = bootstrap.download_whisper_model(
            model_name,
            on_log=_log,
            download_root=str(MODELS_DIR / "whisper"),
            force=bool(body.force),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"模型下载失败：{exc}") from exc

    return {"ok": bool(result.get("ok")), "path": result.get("path", ""),
            "error": result.get("error", ""), "logs": logs, "model": model_name}


@app.post("/api/whisper/test")
def whisper_test_api(body: ASRTestBody = Body(default_factory=ASRTestBody)) -> Dict[str, Any]:
    """测试自定义转写 API 是否可用。

    默认只做协议层探测（不发音频，秒回）。
    body.deep=True 时额外用 ffmpeg 合成一段 ~7s 的测试语音，真跑一次转写 ——
    只有能拿回文字才算「模型真可用」（避免 /config 能通但推理挂了的情况）。
    """
    from ...core.asr_remote import ASRConfig, probe_asr_backend

    saved = config.whisper.get("api") or {}
    merged = {
        "protocol": body.protocol if body.protocol is not None else saved.get("protocol", "openai"),
        "base_url": body.base_url if body.base_url is not None else saved.get("base_url", ""),
        "api_key": body.api_key if body.api_key is not None else saved.get("api_key", ""),
        "model": body.model if body.model is not None else saved.get("model", ""),
        "timeout": body.timeout if body.timeout is not None else saved.get("timeout", 600),
        # 深度测试也要用真实的字幕参数，否则测出来的东西和实际用的不一样
        "language": saved.get("language", "auto"),
        "max_chars": saved.get("max_chars", 20),
        "punc": saved.get("punc", True),
        "hotwords": saved.get("hotwords", ""),
        "diarize": saved.get("diarize", False),
        "prompt": saved.get("prompt", ""),
    }
    cfg = ASRConfig.from_dict(merged)
    try:
        result = probe_asr_backend(cfg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"探测失败：{exc}") from exc

    if body.deep:
        try:
            deep = _run_deep_asr_test(cfg)
        except Exception as exc:  # noqa: BLE001 - 深度测试失败不掩盖探测结果
            deep = {"ok": False, "error": f"深度测试异常：{exc}"}
        result["deep"] = deep
        # 深度测试才能证明「真能转写」：它失败时整体判失败
        if result.get("ok") and not deep.get("ok"):
            result["ok"] = False
            result["error"] = deep.get("error", "真实转写测试失败")

    ok = bool(result.get("ok"))
    if ok:
        event_bus.emit_log("INFO", f"转写 API 测试通过 · {result.get('detail')}", source="asr-test")
    else:
        event_bus.emit_log("WARN", f"转写 API 测试失败：{result.get('error')}", source="asr-test")
    return result


def _run_deep_asr_test(cfg) -> Dict[str, Any]:
    """合成一段测试语音并真跑一次转写，返回 {ok, text, segments, elapsedMs, error}。"""
    import tempfile
    import time as _time

    from ...core.asr_remote import RemoteASREngine
    from ...infra.tools import ToolError, resolve_tools, run

    out: Dict[str, Any] = {"ok": False, "text": "", "segments": 0,
                          "elapsedMs": 0, "error": "", "audio": ""}
    tools = resolve_tools()
    if not tools.ffmpeg:
        out["error"] = "未找到 ffmpeg，无法生成测试语音"
        return out

    # ~7 秒英文测试句（flite 免网络、发音清晰，Qwen3-ASR 与 Whisper 都能认）
    sentence = ("hello world this is a test of the speech recognition service "
                "running on this machine")
    tmpdir = tempfile.mkdtemp(prefix="vair-asr-test-")
    wav = os.path.join(tmpdir, "probe.wav")
    try:
        cmd = [tools.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", f"flite=text='{sentence}':voice=slt",
               "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav]
        proc = run(cmd, timeout=60)
        if proc.returncode != 0 or not os.path.isfile(wav):
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[:200]
            out["error"] = f"ffmpeg 合成测试语音失败：{err or '未知错误'}"
            return out
        out["audio"] = wav

        engine = RemoteASREngine(cfg)
        started = _time.time()
        sub = engine.transcribe(wav)
        out["elapsedMs"] = int((_time.time() - started) * 1000)
        out["text"] = (sub.text or "")[:200]
        out["segments"] = len(sub.segments or [])
        if sub.error and not (sub.text or "").strip():
            out["error"] = sub.error
            return out
        if not (sub.text or "").strip():
            out["error"] = "转写服务返回了空文本（服务在跑，但模型可能没加载好）"
            return out
        out["ok"] = True
        return out
    except ToolError as exc:
        out["error"] = str(exc)
        return out
    finally:
        try:
            if os.path.isfile(wav):
                os.unlink(wav)
            os.rmdir(tmpdir)
        except OSError:
            pass


@app.post("/api/whisper/setup")
def whisper_setup() -> Dict[str, Any]:
    """下载 / 修复 CUDA 运行库（cublas64_12.dll 等）到 libs/。"""
    rt = config.runtime
    bootstrap.apply_mirrors(rt.get("pip_index", ""), rt.get("hf_endpoint", ""))
    logs: List[str] = []
    try:
        result = bootstrap.ensure_cuda_libs(on_log=lambda m: logs.append(m))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"CUDA 运行库补装失败：{exc}") from exc
    for line in logs:
        event_bus.emit_log("INFO", line, source="cuda-setup")
    report = bootstrap.probe_runtime()
    return {
        "ok": bool(result.get("ok")),
        "installed": bool(result.get("installed")),
        "error": result.get("error", ""),
        "logs": logs,
        "cuda_libs": result.get("status"),
        "cuda_available": report.cuda_available,
    }


@app.post("/api/whisper/install")
def whisper_install() -> Dict[str, Any]:
    """安装 faster-whisper 依赖到 libs/（走配置的 pip 镜像）。"""
    rt = config.runtime
    bootstrap.apply_mirrors(rt.get("pip_index", ""), rt.get("hf_endpoint", ""))
    logs: List[str] = []
    try:
        result = bootstrap.install_whisper(on_log=lambda m: logs.append(m))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Whisper 依赖安装失败：{exc}") from exc
    for line in logs:
        event_bus.emit_log("INFO", line, source="whisper-setup")
    report = bootstrap.probe_runtime()
    return {
        "ok": bool(result.get("ok")),
        "installed": bool(result.get("installed")),
        "already": bool(result.get("already")),
        "error": result.get("error", ""),
        "logs": logs,
        "faster_whisper": report.faster_whisper,
    }


# ------------------------------------------------------------------
# /api/tasks —— 创建 / 查询任务
# ------------------------------------------------------------------
@app.post("/api/tasks")
async def create_task(body: CreateTaskBody = Body(default_factory=CreateTaskBody)) -> Dict[str, Any]:
    raw_paths: List[str] = []
    if body.paths:
        raw_paths.extend(p for p in body.paths if p and str(p).strip())
    if body.path:
        raw_paths.append(body.path)
    if not raw_paths:
        raise HTTPException(status_code=400, detail="缺少 path（文件夹绝对路径）")

    # 路径校验：不存在直接报错，避免静默失败
    cleaned: List[str] = []
    missing: List[str] = []
    for p in raw_paths:
        candidate = os.path.abspath(str(p).strip().strip('"'))
        if os.path.exists(candidate):
            cleaned.append(candidate)
        else:
            missing.append(p)
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"路径不存在：{', '.join(missing)}")

    try:
        result = scheduler.start(
            cleaned,
            dry_run=body.dry_run,
            recursive=body.recursive,
        )
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"任务启动失败：{exc}") from exc

    return {
        "ok": True,
        "taskId": result["taskId"],
        "dryRun": result["dryRun"],
        "recursive": result["recursive"],
        "ignoredPaths": missing,
        "task": result["task"],
    }


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str) -> Dict[str, Any]:
    return {"ok": True, **scheduler.stop(task_id)}


@app.get("/api/tasks")
async def list_tasks(limit: int = Query(50, ge=1, le=500),
                     offset: int = Query(0, ge=0)) -> Dict[str, Any]:
    return {"items": repo.list_tasks(limit=limit, offset=offset)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> Dict[str, Any]:
    task = repo.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.get("/api/tasks/{task_id}/files")
async def get_task_files(task_id: str, limit: int = Query(1000, ge=1, le=5000),
                         offset: int = Query(0, ge=0)) -> Dict[str, Any]:
    return {"items": repo.list_files(task_id, limit=limit, offset=offset)}


# ------------------------------------------------------------------
# /api/events —— SSE 事件流
# ------------------------------------------------------------------
@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    conn = sse_broker.connect()

    async def generator():
        try:
            # 首帧：连接状态（前端 handleStatus / status handler 消费）
            yield sse_broker.format_sse({"type": "status", "connected": True, "mode": "sse",
                                         "ts": _now_ms()})
            # 回放当前任务状态，避免刷新后空白
            last = scheduler._last_summary
            if scheduler.running and scheduler.current_task_id:
                yield sse_broker.format_sse({
                    "type": "task", "action": "started",
                    "taskId": scheduler.current_task_id, "ts": _now_ms(),
                })
            if last is not None:
                yield sse_broker.format_sse({
                    "type": "task",
                    "action": "stopped" if last.stopped else "completed",
                    "taskId": last.task_id, "ts": _now_ms(),
                })

            while True:
                if await request.is_disconnected():
                    break
                try:
                    items = await asyncio.wait_for(conn.drain(), timeout=20.0)
                except asyncio.TimeoutError:
                    # 心跳，保持连接与代理存活
                    yield ": keep-alive\n\n"
                    continue
                for data in items:
                    yield sse_broker.format_sse(data)
                if conn.closed:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            sse_broker.disconnect(conn)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------
# /api/logs —— 历史日志
# ------------------------------------------------------------------
@app.get("/api/logs")
async def get_logs(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    level: Optional[str] = None,
    taskId: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    return repo.list_logs(limit=limit, offset=offset, level=level,
                          task_id=taskId, keyword=keyword)


# ------------------------------------------------------------------
# /api/review —— 审批
# ------------------------------------------------------------------
@app.post("/api/review/{item_id}")
async def submit_review(item_id: str, body: ReviewBody) -> Dict[str, Any]:
    decision = (body.decision or "").lower()
    if decision not in ("accept", "ignore", "edit"):
        raise HTTPException(status_code=400, detail="decision 必须是 accept / ignore / edit")
    result = repo.decide_review(item_id, decision, body.newName)
    if result is None:
        raise HTTPException(status_code=404, detail="审批条目不存在")
    event_bus.emit_log("INFO", f"审批[{decision}]：{result['oldName']} → {result['newName']}",
                       source="review")
    return {"ok": True, "item": result}


@app.get("/api/reviews")
async def list_reviews(status: Optional[str] = None,
                      limit: int = Query(200, ge=1, le=1000)) -> Dict[str, Any]:
    return {"items": repo.list_reviews(status=status, limit=limit)}


# ------------------------------------------------------------------
# /api/config —— 读写配置
# ------------------------------------------------------------------
@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    return config.as_dict()


@app.put("/api/config")
async def put_config(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    try:
        config.replace(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"配置保存失败：{exc}") from exc
    # 镜像改动立即生效（下次依赖/模型下载即走新源）
    bootstrap.apply_mirrors(config.get("runtime.pip_index"),
                            config.get("runtime.hf_endpoint"))
    event_bus.emit_log("DEBUG", "配置已更新并写盘 config.json", source="config")
    return {"ok": True, "config": config.as_dict()}


# ------------------------------------------------------------------
# 静态前端（挂在最后，避免吞掉 /api 路由）
#
# 前端 index.html 使用相对路径（./css/、./js/），因此必须让静态目录
# 直接挂在根路径下，否则相对资源会 404。
# ------------------------------------------------------------------
if os.path.isdir(STATIC_DIR):

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/index.html")
    async def index_html() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    # 兜底：未命中 /api/* 的请求，先找静态文件，找不到则回落到 index.html
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not Found")
        # 防目录穿越
        normalized = os.path.normpath(full_path).replace("\\", "/")
        if normalized.startswith("..") or normalized.startswith("/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = os.path.join(STATIC_DIR, normalized)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    # 静态资源（显式挂载，便于快速路径匹配）
    app.mount("/css", StaticFiles(directory=os.path.join(STATIC_DIR, "css")), name="css")
    app.mount("/js", StaticFiles(directory=os.path.join(STATIC_DIR, "js")), name="js")


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
