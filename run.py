"""run.py — 应用入口（编排层）。

关键顺序（红线 G1）：
    1. bootstrap.early_init()   重定向 env + 注入 libs/  ← 必须在任何第三方 import 之前
    2. bootstrap.ensure_runtime()  探测/补齐依赖
    3. import uvicorn + api     ← 此时才允许导入第三方

用法：
    python run.py                     # 127.0.0.1:8000
    python run.py --lan               # 0.0.0.0:8000（手机同一 Wi-Fi 可访问）
    python run.py --host 0.0.0.0 --port 8080
    python run.py --selfcheck         # 自检后退出（不启动服务）
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

# --- 让 app 包可被导入（脚本可能从任意 cwd 启动）---
sys.path.insert(0, str(Path(__file__).resolve().parent))

# pythonw 启动（无控制台）时 sys.stdout / stderr 为 None，任何 print 都会抛
# AttributeError 并让进程静默退出 —— 静默版启动脚本踩的就是这个坑。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = sys.stdout

from app.infra import bootstrap  # noqa: E402  (仅标准库依赖，安全)

bootstrap.early_init()  # ← 红线 G1：必须最先执行


def _local_ip() -> str:
    """获取本机在局域网中的 IP（供手机访问提示）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _lan_from_config() -> bool:
    """读取配置里的局域网开关（runtime.lan）。

    命令行 --lan / --host 优先；都没给时才由配置决定。
    """
    try:
        from app.infra.config import load_config

        return bool(load_config().runtime.get("lan", False))
    except Exception:  # noqa: BLE001 - 配置不可用时不阻断启动
        return False


def _print_banner(host: str, port: int) -> None:
    line = "=" * 62
    print(line)
    print("  视频 AI 重命名助手 · vair")
    print(line)
    print(f"  应用目录 : {Path(__file__).resolve().parent}")
    print(f"  监听地址 : {host}:{port}"
          + ("（局域网可访问）" if host not in ("127.0.0.1", "localhost") else "（仅本机）"))
    print(f"  本机访问 : http://127.0.0.1:{port}/")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  手机访问 : http://{_local_ip()}:{port}/   （需与电脑同一 Wi-Fi）")
    else:
        print("  提示     : 需要手机/局域网访问时，在设置里打开「局域网访问」并重启服务")
    print("  停止服务 : 按 Ctrl+C")
    print(line)


def _apply_mirrors_from_config() -> None:
    """把 config.json 里的国内镜像应用到 pip / HuggingFace 下载。

    必须在 ensure_runtime() 之前调用，否则依赖安装仍会走官方源。
    """
    try:
        from app.infra.config import load_config

        rt = load_config().runtime
        applied = bootstrap.apply_mirrors(rt.get("pip_index", ""), rt.get("hf_endpoint", ""))
        warn = applied.get("hf_endpoint_warning") or ""
        if warn:
            print(f"  [mirror] ⚠ {warn}")
    except Exception:  # noqa: BLE001 - 配置不可用不应阻断启动
        pass


def _selfcheck() -> int:
    """自检：探测依赖与工具，输出报告后退出（CI / 分发前验证用）。"""
    _apply_mirrors_from_config()
    report = bootstrap.ensure_runtime(auto_install_cuda=False)
    data = report.as_dict()
    print("=" * 62)
    print("  自检报告")
    print("=" * 62)
    print(f"  Python        : {data['python']}  ({data['executable']})")
    print(f"  ffmpeg        : {data['ffmpeg'] or '❌ 未找到'}")
    print(f"  ffprobe       : {data['ffprobe'] or '❌ 未找到'}")
    print(f"  exiftool      : {data['exiftool'] or '⚠️ 未找到（可选，关闭「写元数据」仍可改名）'}")
    print(f"  openai        : {'✅' if data['openai'] else '❌'}")
    print(f"  fastapi       : {'✅' if data['fastapi'] else '❌'}")
    print(f"  uvicorn       : {'✅' if data['uvicorn'] else '❌'}")
    print(f"  sqlalchemy    : {'✅' if data['sqlalchemy'] else '❌'}")
    print(f"  faster-whisper: {'✅' if data['faster_whisper'] else '❌（将仅用画面分析）'}")
    print(f"  CUDA 可用     : {'✅ ' + data['cuda_reason'] if data['cuda_available'] else '⚠️ ' + data['cuda_reason']}")
    if data["missing"]:
        print(f"  缺失项        : {', '.join(data['missing'])}")
    print("=" * 62)
    ok = bool(data["ffmpeg"] and data["ffprobe"] and data["fastapi"] and data["uvicorn"])
    print("  结论：" + ("✅ 环境就绪" if ok else "❌ 缺少必需项，无法启动"))
    print("=" * 62)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="视频 AI 重命名助手（Web 外壳）")
    parser.add_argument("--host", default="", help="监听地址（默认由配置 runtime.lan 决定）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--lan", action="store_true", help="绑定 0.0.0.0，允许手机/局域网访问")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码热重载")
    parser.add_argument("--selfcheck", action="store_true", help="仅执行环境自检后退出")
    parser.add_argument("--no-install", action="store_true",
                        help="不自动补齐缺失依赖（离线环境用）")
    args = parser.parse_args()

    if args.selfcheck:
        return _selfcheck()

    # 第二步：探测/补齐依赖（必需项自动安装到 libs/）
    # 先应用国内镜像，保证下面的自动安装走用户指定的源
    _apply_mirrors_from_config()

    auto_cuda = True
    try:
        from app.infra.config import load_config

        auto_cuda = bool(load_config().runtime.get("auto_install_cuda", True))
    except Exception:  # noqa: BLE001
        pass

    if args.no_install:
        report = bootstrap.probe_runtime()
    else:
        report = bootstrap.ensure_runtime(auto_install_cuda=auto_cuda,
                                          on_log=lambda msg: print(f"  [deps] {msg}"))

    if not (report.ffmpeg and report.ffprobe):
        print("❌ 未找到 ffmpeg / ffprobe。请将它们放入项目 ffmpeg/ 目录后重试。", file=sys.stderr)
        return 1
    if not (report.fastapi and report.uvicorn):
        print("❌ fastapi / uvicorn 未就绪。请联网后重试，或手动执行：\n"
              f"   {sys.executable} -m pip install --target libs fastapi uvicorn", file=sys.stderr)
        return 1

    # 第三步：此时才允许导入第三方
    import uvicorn  # noqa: E402

    # 优先级：--lan > --host > 配置 runtime.lan > 仅本机
    if args.lan:
        host = "0.0.0.0"
    elif args.host:
        host = args.host
    else:
        host = "0.0.0.0" if _lan_from_config() else "127.0.0.1"

    _print_banner(host, args.port)

    try:
        from app.shell.web import api as api_mod  # noqa: E402

        api_mod.set_server_info(host, args.port)
        # reload 模式只接受 import string，否则传 app 对象
        application = "app.shell.web.api:app" if args.reload else api_mod.app
    except Exception:  # noqa: BLE001 - 极端情况下退回字符串导入
        application = "app.shell.web.api:app"

    uvicorn.run(
        application,
        host=host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
