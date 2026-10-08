"""视频 AI 重命名助手（vair）— 应用包。

目录
----
infra/   基础设施层（paths / config / tools / bootstrap），仅标准库
core/    引擎层（五阶段流水线），禁止 import UI 库
db/      数据层（SQLite + 仓储）
shell/   外壳层（web = FastAPI 后端；desktop = 预留）
"""
