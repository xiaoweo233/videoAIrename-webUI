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


class AITestBody(BaseModel):
    provider: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    timeout: Optional[int] = None


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
        )
    except dialogs.DialogBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not result.get("ok") and not result.get("cancelled"):
        raise HTTPException(status_code=400, detail=result.get("error") or "选择失败")
    return result


@app.post("/api/dialog/folder")
def dialog_folder(body: DialogBody = Body(default_factory=DialogBody)) -> Dict[str, Any]:
    try:
        result = dialogs.pick_folder(initial_dir=body.initial_dir or "")
    except dialogs.DialogBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not result.get("ok") and not result.get("cancelled"):
        raise HTTPException(status_code=400, detail=result.get("error") or "选择失败")
    return result


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
    return {
        "faster_whisper": report.faster_whisper,
        "cuda_available": report.cuda_available,
        "cuda_reason": report.cuda_reason,
        "cuda_libs": bootstrap.cuda_libs_status(),
        "use_gpu": bool(config.whisper.get("use_gpu", True)),
        "device": config.whisper.get("device", "auto"),
    }


@app.post("/api/whisper/setup")
def whisper_setup() -> Dict[str, Any]:
    """下载 / 修复 CUDA 运行库（cublas64_12.dll 等）到 libs/。"""
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
