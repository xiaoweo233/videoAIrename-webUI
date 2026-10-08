# 视频 AI 重命名助手（vair）

拖入一个文件夹，自动看懂每个视频讲了什么 —— 重命名、写元数据、生成 NFO / SRT。

本仓库为**全栈实现**：前端（`app/shell/web/static`）+ 后端（FastAPI）+ 数据库（SQLite）。

---

## 快速开始

```bash
# 双击任一即可（Windows）
启动.bat          # 保留控制台，可见启动与运行日志
启动.vbs          # 无窗口静默启动，自动打开浏览器
启动_静默.bat     # 无窗口 + 自动打开浏览器

# 或命令行
python run.py                 # http://127.0.0.1:8000/
python run.py --lan           # 允许手机在同一 Wi-Fi 访问
python run.py --selfcheck     # 仅做环境自检后退出
```

首次启动会自动把缺失依赖安装到 `libs/`（**不写系统环境**）。  
默认仅监听本机；需要手机访问时用 `--lan` 或把 `启动.bat` 里的 `LAN=1`。

---

## 架构：一核两壳

```
                    ┌──────────────────────────────┐
                    │   Engine Core（纯 Python）    │
                    │  探测/抽帧/转写/AI/落盘/配置  │
                    └──────┬───────────────┬───────┘
                           │               │
              ┌────────────▼────┐   ┌──────▼──────────┐
              │ 桌面外壳 (Tk)    │   │ Web 外壳 (HTTP) │
              │ 预留             │   │ 手机/平板访问   │  ← 本仓库实现
              └─────────────────┘   └─────────────────┘
```

```
app/
├─ infra/                基础设施层（仅标准库）
│   paths.py             全局目录常量（产物全部落在 APP_ROOT 内）
│   bootstrap.py         env 重定向 + 依赖探测/补齐（必须早于第三方 import）
│   config.py            AppConfig 读写（对齐 config.json 结构）
│   tools.py             ffmpeg / ffprobe / exiftool 子进程封装（UTF-8 安全）
├─ core/                 引擎层（禁止 import UI 库）
│   base.py              EventBus / StopToken / JobStats / 阶段常量
│   media.py             探测 / 抽帧 / 时间戳选取        ← 阶段 ① ②a
│   whisper.py           Whisper：懒加载 / GPU→CPU 回退 / 看门狗  ← 阶段 ②b
│   ai.py                AIClient：OpenAI 兼容 + Gemini 双后端  ← 阶段 ④
│   metadata.py          ExifTool / NFO / SRT / 时间戳还原  ← 阶段 ⑤
│   rename.py            文件名模板 / 清洗 / _N 去重      ← 阶段 ⑤
│   engine.py            五阶段并发流水线 + 熔断          ← 总编排
├─ db/                   数据层（SQLite + SQLAlchemy）
│   models.py            Task / TaskFile / LogEntry / ReviewItem
│   session.py           引擎与 Session（WAL / 外键 / 忙等超时）
│   repo.py              仓储函数（CRUD + 改名映射 + 崩溃恢复）
└─ shell/web/            外壳层
    api.py               FastAPI 路由（对齐前端契约）
    scheduler.py         任务调度（后台线程跑引擎 + 回调落库）
    sse.py               EventBus(线程) → SSE(asyncio) 桥
    static/              前端（零框架 ES Module）
```

---

## 处理流水线

```
[① 探测]  ffprobe：时长 / 音轨 / creation_time / 关键帧时间
    ├────────────────► [②a 抽帧] 线程池并发 N 路
    └────────────────► [②b 转写] Whisper 单路串行（GPU 独占）
                                                          ▼
                    [③ 合并] 视觉 + 字幕都到齐才放行
                                                          ▼
                    [④ AI 分析] 多模态模型 → title / plot / tags
                                                          ▼
                    [⑤ 落盘] ExifTool 写元数据 → 重命名 → 时间戳还原 → NFO / SRT
```

并发配额：抽帧 `min(cpu,8)` · 转写 `1`（GPU 独占）· AI `1~4` · 落盘串行。  
②a 与 ②b **互不阻塞**：无声视频跳过转写直接放行，这是吞吐量的来源。

---

## API 契约

| 方法     | 路径                     | 说明                                        |
| ------ | ---------------------- | ----------------------------------------- |
| `POST` | `/api/tasks`           | 创建任务，body `{ path }` 或 `{ paths: [...] }` |
| `POST` | `/api/tasks/{id}/stop` | 停止任务                                      |
| `GET`  | `/api/events`          | SSE 事件流                                   |
| `GET`  | `/api/logs`            | 拉取历史日志（可按级别/任务/关键字过滤）                     |
| `GET`  | `/api/config`          | 读取配置                                      |
| `PUT`  | `/api/config`          | 保存配置（即时写盘）                                |
| `POST` | `/api/review/{id}`     | 提交审批 `{ decision, newName? }`             |

辅助只读端点：`/api/health`、`/api/runtime`、`/api/tasks`、`/api/tasks/{id}`、  
`/api/tasks/{id}/files`、`/api/reviews`。

### SSE 事件类型

与前端 `js/simulator.js` 完全一致：`status` / `task` / `progress` / `phase` /  
`log` / `review` / `inference`。所有事件含 `ts`（毫秒时间戳）与 `type`。

`phase` 取值：`"1"` `"2a"` `"2b"` `"3"` `"4"` `"5"`。

---

## 数据库

SQLite 单文件库 `data/vair.db`（WAL 模式），四张表：

| 表              | 用途                        |
| -------------- | ------------------------- |
| `tasks`        | 任务主表（状态 / 统计 / 配置快照）      |
| `task_files`   | 单文件处理记录，**兼作改名映射表**（支持回滚） |
| `log_entries`  | 分级运行日志                    |
| `review_items` | 命名审批条目                    |

启动时自动把上次未正常结束的任务标记为 `interrupted`（断电恢复）。

---

## 目录规范（产物一律在应用目录内）

```
videoAIrenameAPP/
├─ app/          源码
├─ ffmpeg/       ffmpeg.exe / ffprobe.exe / exiftool
├─ libs/         第三方包（运行期生成，不写系统 site-packages）
├─ models/       Whisper / HF 模型缓存（运行期生成）
├─ cache/        设备探测结果缓存
├─ tmp/          临时音频与中间文件
├─ logs/         运行日志
├─ data/         SQLite 数据库 / rename_map.json
├─ config.json   用户配置（运行期生成）
└─ docs/         方案与说明
```

启动第一段代码就把 `TMP/TEMP/TMPDIR`、`HF_HOME`、`XDG_CACHE_HOME` 重定向到上述目录 ——  
这是「不到处乱放」的唯一可靠做法（很多库在 import 时就读这些变量）。

---

## 环境自检

```bash
python run.py --selfcheck
```

会输出 Python / ffmpeg / ffprobe / exiftool / 各依赖 / CUDA 可用性报告。

---

## 常见问题

**Q：转写很慢或挂起？**  
A：检查是否为 NVIDIA 显卡且 `nvidia-cublas-cu12` 已装（提供 `cublas64_12.dll`）。  
缺失时引擎会自动回退 CPU 转写（`whisper.device=auto`）。也可在设置中改 `device=cpu`。

**Q：AI 报「上下文溢出」？**  
A：关键帧太多。在设置中降低 `frames.max_keyframes`（如 35 → 20）或  
`frames.max_side`（如 520 → 420）。

**Q：不想真的改文件？**  
A：打开「预览模式」（`output.dry_run`），只演算不落盘。

**Q：如何回滚改名？**  
A：`data/vair.db` 的 `task_files` 表保留了 `old_path → new_path` 映射，  
`repo.rename_map()` 可导出。

---

## 功能更新

### 1. 用系统资源管理器选择视频 / 多个视频 / 文件夹

浏览器出于沙箱安全**拿不到绝对路径**（`<input type="file">` 只给文件名），因此改由
**服务端调起系统原生对话框**，直接拿到可交给引擎的真实绝对路径。

- 工作台提供两个按钮：**选择视频**（可多选）、**选择文件夹**；
- 多选时前端以 `paths: [...]` 一次性提交，全部交给同一次任务处理；
- 仍支持手动粘贴路径（手输会清空已选列表，避免歧义）。

后端端点（`app/infra/dialogs.py` 实现，PowerShell + WinForms）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/dialog/files`  | 原生多选文件对话框，返回绝对路径数组 |
| POST | `/api/dialog/folder` | 原生文件夹对话框，返回绝对路径 |

同一时刻只允许一个对话框（全局锁），重复请求返回 `409`。

### 2. AI 配置：连接测试 + 模型识别

设置页新增 **AI 连接测试** 面板，点一次即可确认：后端是否可达、延迟、
服务端报告的可用模型列表、配置的 `model` 是否在列表中，以及**识别出这是什么模型**
（综合模型自述与家族特征匹配，如 Qwen / Llama / DeepSeek / InternVL …）。

- 端点：`POST /api/ai/test`
- 返回：`ok / reachable / latencyMs / models[] / modelExists / identified / reply / servedModel / error`
- 失败时给出人话提示（服务未启动 / 401 鉴权 / 404 少了 `/v1` / 模型名不存在）。
- 可用模型以**可点击标签**呈现，点一下即切换 `ai.model`。

### 3. 转写：GPU 开关 + 自动补 `cublas64_12.dll`

- 设置页「转写」组新增 **GPU 加速转写** 开关（`whisper.use_gpu`）；关闭即强制 CPU。
- 新增 **转写运行环境** 面板，实时显示 faster-whisper / CUDA 设备 / CUDA 运行库状态。
- **下载 / 修复 CUDA 库** 按钮：`POST /api/whisper/setup` 会把
  `nvidia-cublas-cu12`、`nvidia-cudnn-cu12` 装到 `libs/`，并刷新 DLL 搜索路径，
  补上缺失的 `cublas64_12.dll`（该文件缺失会让 GPU 推理**挂起**而非报错）。
- 启动自检也已修正：原逻辑只在 `device_count == 0` 时才补装，恰好漏掉
  「有 GPU 但缺 dll」这一最典型故障；现改为独立检查 DLL 是否就位。

### 4. 修复：手机点击任何按钮都没反应

根因是移动端遮罩层 `.nav-scrim` 使用了 `position: fixed; inset: 0; z-index: 44`
且仅 `opacity: 0` —— **透明但仍然拦截触摸**，层级又高于内容区与底部导航（`z-index: 30`），
导致整屏点击被吞掉。

修复：遮罩默认 `pointer-events: none`，仅在抽屉展开（`body.nav-open`）时置为 `auto`；
并给按钮/开关/导航项加 `touch-action: manipulation` 消除 300ms 点击延迟。

---

## 推送到远程仓库

```bash
git init
git add .
git commit -m "feat: 原生文件选择器 + AI 连接测试 + GPU 开关/CUDA 自动补装 + 移动端修复"
git remote add origin git@github.com:xiaoweo233/videoAIrename-webUI.git
git push -u origin main
```

> 仓库**不包含** `libs/`、`ffmpeg/`、`models/`、`data/` —— 它们体积大（模型单文件 >1.5GB，
> 超 GitHub 100MB 硬限制）或含隐私，由首次启动自动补齐。配置模板见 `config.example.json`。

