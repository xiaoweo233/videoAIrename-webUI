# AI 视频重命名整理

<div align="center">

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi)](https://fastapi.tiangolo.com/)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](https://www.microsoft.com/windows)
[![faster-whisper](https://img.shields.io/badge/faster--whisper-large--v3--turbo-orange)](https://github.com/SYSTRAN/faster-whisper)
[![Repo](https://img.shields.io/badge/GitHub-videoAIrename--webUI-181717?logo=github)](https://github.com/xiaoweo233/videoAIrename-webUI)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

🎯 **本地视频 AI 归档工具** — 拖入一个文件夹，自动看懂每个视频讲了什么，
然后重命名、写元数据、生成 NFO / SRT。

</div>

`vair` 是一个**纯本地运行**的视频整理工具：Whisper 把语音转成字幕，多模态大模型看关键帧
理解画面，二者合并后交给 AI 生成「时间 + 具象标题」的文件名，再用 ExifTool 把标题写进视频
内部属性，顺带产出 Jellyfin / Kodi 可识别的 `.nfo` 与同名 `.srt`。全程 Web 界面操作，
手机在同一 Wi-Fi 下也能直接用。

与同类工具的区别：**AI 后端可以是你本机的大模型**（llama.cpp / vLLM / NInfer 等任意
OpenAI 兼容服务），不强制上云；离线也能跑 Whisper。

## 📋 目录

- [✨ 主要特性](#-主要特性)
- [📦 安装](#-安装)
- [🚀 快速开始](#-快速开始)
- [🖥 界面用法](#-界面用法)
- [⚙️ 配置详解](#️-配置详解)
- [🔧 命令行](#-命令行)
- [🧩 处理流水线](#-处理流水线)
- [🔌 API 契约](#-api-契约)
- [🗄 数据库与回滚](#-数据库与回滚)
- [📁 目录规范](#-目录规范)
- [🚨 注意事项](#-注意事项)
- [❓ 常见问题](#-常见问题)
- [技术栈](#技术栈)
- [📄 许可证](#-许可证)
- [🙏 致谢](#-致谢)

## ✨ 主要特性

- 🎯 **AI 语义化命名** — 关键帧 + 语音转写一起送给多模态模型，输出「4-6 个具象名词」的标题
- 🎙 **Whisper 语音转写** — faster-whisper GPU 加速，缺 CUDA 库自动回退 CPU，带挂起看门狗
- 🏷 **ExifTool 写元数据** — 标题 / 描述 / 关键词写进视频内部，并打软水印供二次扫描毫秒级跳过
- 📄 **NFO + SRT** — 自动生成 Jellyfin / Kodi 可读的 `.nfo` 与同名字幕
- 🗂 **批量处理** — 整个文件夹递归扫描，生产者/消费者并发流水线 + 失败熔断
- 👀 **预览模式** — 只演算不落盘，先确认命名效果再动手
- ⏭ **跳过已处理** — 文件名含标记或已写软水印的文件直接跳过，不重复烧 Token
- 🔁 **可回滚** — SQLite 保留 `旧路径 → 新路径` 映射，改错了能查回来
- 📱 **手机可用** — 局域网开关一键开启，移动端适配，SSE 实时推进度与日志
- 🧰 **原生文件选择器** — 服务端调起系统对话框拿真实绝对路径（浏览器沙箱拿不到路径）
- 🎛 **全图形化设置** — 每个开关都带悬停「?」术语解释，改动即时写盘
- 🔧 **容错兜底** — 分片 MP4（OBS 录像）自动无损重封装、CUDA 库自动补装、镜像源可换国内

## 📦 安装

### 方式一：克隆即用（推荐）

```bash
git clone https://github.com/xiaoweo233/videoAIrename-webUI.git
cd videoAIrename-webUI
```

仓库**自带** `ffmpeg/ffmpeg.exe`、`ffprobe`、`exiftool`（放在项目 `ffmpeg/` 目录），
首次启动会把缺失的 Python 依赖自动装到 `libs/`（**不写系统 site-packages**）。

### 方式二：手动准备环境

| 依赖 | 说明 | 是否必需 |
|------|------|----------|
| Python 3.11+ | 运行时 | ✅ 必需 |
| ffmpeg / ffprobe | 抽帧、探测、抽音轨 | ✅ 必需（仓库已自带，也可自行放入 `ffmpeg/`） |
| exiftool | 写视频内部元数据 | ⭕ 可选（关闭「写元数据」仍可改名） |
| faster-whisper | 语音转写 | ⭕ 可选（设置页一键安装） |
| NVIDIA 显卡 + CUDA | GPU 转写 / 抽帧加速 | ⭕ 可选（缺库会自动补装或回退 CPU） |

> 便携版 Python 可直接放到项目根目录的 `python\`，启动脚本会优先使用它。

## 🚀 快速开始

### 1. 启动

Windows 双击任一即可：

| 脚本 | 行为 |
|------|------|
| `启动.bat` | 保留控制台窗口，能看到启动与运行日志 |
| `启动.vbs` | 无窗口静默启动，就绪后自动打开浏览器 |
| `启动_打开浏览器.bat` | 无窗口 + 自动打开浏览器 |

命令行：

```bash
python run.py                 # http://127.0.0.1:8000/
python run.py --port 8080     # 换端口
python run.py --lan           # 强制监听 0.0.0.0（手机可访问）
python run.py --selfcheck     # 只做环境自检后退出
```

### 2. 配置 AI 后端

打开**设置 → AI 配置**，填服务地址与模型名，点「测试连接」确认可达：

| 场景 | provider | base_url 示例 |
|------|----------|---------------|
| 本地大模型（llama.cpp / vLLM / NInfer） | `openai` | `http://127.0.0.1:8084/v1` |
| OpenAI 官方 | `openai` | `https://api.openai.com/v1` |
| Google Gemini | `gemini` | — |

> ⚠️ `base_url` 结尾的 `/v1` 别漏，否则会 404。

### 3. 处理视频

工作台里**拖入**文件夹，或点「选择视频 / 选择文件夹」用系统窗口挑，然后开始任务。
想先看看效果就打开「预览模式」。

## 🖥 界面用法

| 页面 | 能做什么 |
|------|----------|
| 工作台 | 拖入 / 选择路径、开始停止任务、实时进度与日志、命名审批、预览模式开关 |
| 设置 | 全部开关与参数、AI 连接测试、Whisper 环境、CUDA 补装、下载镜像、提示词自定义、访问方式 |
| 日志 | 按级别 / 任务 / 关键字过滤历史日志 |
| 审批 | 对 AI 给出的名字逐条通过或改名 |

设置页每个开关右侧的 **「?」** 悬停即可看到一句人话解释（如 `temperature` 是什么、
`max_side` 为什么 640 合适），页面底部还有**术语速查**表。

## ⚙️ 配置详解

配置即 `config.json`（首次运行自动生成，模板见 `config.example.json`），
**设置页改动即时写盘**。「恢复默认」会回到仓库内置的实战参数。

| 分组 | 关键字段 | 说明 |
|------|----------|------|
| `ai` | `provider` `base_url` `api_key` `model` `timeout` `temperature` `enforce_json_mode` `system_prompt` `prompt` | AI 后端与提示词；`enforce_json_mode` 对本地后端基本必开 |
| `frames` | `max_keyframes` `max_side` `workers` `hwaccel` | 抽几张图、缩放到多大、并发几路 |
| `whisper` | `enable` `backend` `prefer_api` `allow_download` `model` `device` `compute_type` `language` `use_gpu` `workers` | 转写开关与设备；`backend=auto/local/api` 决定优先 API 还是本地；`prefer_api` 控制 auto 下是否先试 API；`allow_download` 控制本地模型缺失时是否自动联网下载（默认关，避免弱网卡住整批任务）；`workers` 在 GPU 场景建议 1 |
| `whisper.api` | `protocol` `base_url` `api_key` `model` `timeout` `prompt` `diarize` `max_chars` `punc` `hotwords` | 远程转写服务；`backend=auto/api` 时生效；`protocol` 见下节 |
| `naming` | `template` `date_format` `marker` `enable_marker` `enable_skip` | 命名模板、时间格式、处理标记、跳过已处理 |
| `output` | `nfo` `metadata` `srt` `move_failed` `dry_run` `remux_fragmented` | 落盘项；`dry_run` = 预览模式，`remux_fragmented` = 分片 MP4 自动重封装 |
| `runtime` | `ai_workers` `log_file` `verbose` `auto_install_cuda` `pip_index` `hf_endpoint` `lan` | 并发、日志、镜像源、局域网开关 |
| `input` | `recursive` | 是否递归子文件夹 |

命名模板支持变量：`{date}` 时间前缀、`{title}` AI 标题、`{original}` 原文件名。

### 日期格式

| 写法 | 结果 |
|------|------|
| `%Y%m%d_%H%M` | `20261009_2313` |
| `%Y-%m-%d` | `2026-10-09` |
| `%Y年%m月%d日` | `2026年10月09日` |

### 下载镜像（国内加速）

| 配置 | 作用 | 推荐值 |
|------|------|--------|
| `runtime.pip_index` | 装依赖（pip `-i`） | `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `runtime.hf_endpoint` | 下 Whisper 模型 | `https://hf-mirror.com` |

> ⚠️ `www.modelscope.cn` **不是** HuggingFace 兼容端点，填进去会让模型加载报
> `Expecting value: line 1 column 1`。（模型已缓存时不受影响；未缓存会自动临时换源。）

## 🔧 命令行

```bash
python run.py [选项]
```

| 选项 | 说明 |
|------|------|
| `--port <n>` | 监听端口，默认 `8000` |
| `--lan` | 监听 `0.0.0.0`，允许手机 / 同网设备访问（优先级最高） |
| `--host <addr>` | 指定监听地址，优先级次之 |
| `--selfcheck` | 输出环境自检报告后退出（Python / ffmpeg / exiftool / 依赖 / CUDA） |
| `--no-install` | 不自动补齐缺失依赖（离线环境用） |
| `--reload` | 开发模式，代码热重载 |

**监听地址优先级**：`--lan` > `--host` > 配置 `runtime.lan` > 仅本机。
在设置页改「局域网访问」后需**重启服务**才生效（监听地址无法热切换）。

## 🧩 处理流水线

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

并发配额：抽帧 `min(cpu, 8)` · 转写 `1`（GPU 独占，多路反而抢显存）· AI `1~4` · 落盘串行。
②a 与 ②b **互不阻塞**，无声视频跳过转写直接放行 —— 这是吞吐量的主要来源。

### ②b 转写：优先 API，失败自动回退本地

`whisper.backend` 决定这一阶段怎么出字幕：

| backend | 说明 | 适用 |
|---------|------|------|
| `auto`（推荐） | 先试 HTTP 转写服务，不可用自动回退本地 faster-whisper | 既有本机 ASR 服务又想有兵底 |
| `local` | 只用本机 faster-whisper 推理 | 有显卡、想完全离线 |
| `api` | 只把音频发给 HTTP 转写服务，失败就跳过（不回退） | 没有显卡 / 想强制走远程 |

`auto` 下的行为细节：

- `whisper.prefer_api`（默认开）控制是否真的先试 API；关闭后等价于 `local`
- 加载阶段就探连通性（`probe_asr_backend`，8 秒超时）：远程服务没起来会**立刻**回退本地，
  而不是等第一个视频转写失败才发现
- 回退粒度是「本批」而不是「本文件」：远程连续失败 `2` 个文件（`_REMOTE_FAIL_CAP`）后，
  本次任务剩余文件全部直走本地，不再每个文件都白等一次连接超时
- 每个新任务开始（`Engine.run` → `WhisperEngine.reset_task_state`）会清掉上一批的失败终态，
  重新试探远程 —— 远程服务在上一批之后才启动/重启，这一批就能正常用上
- 单个文件远程失败时会立即用本地重试一次；本地也失败才把两者错误拼起来报出
- 模型还没下完（缺词表）也能跑：只要 API 可用，`auto` 后端不会因为本地模型不完整而罢工

`api` 下再选 `whisper.api.protocol` 两种协议，二者产出的都是标准 `TranscriptSegment` 列表，
对下游（AI 分析、SRT 落盘）完全等价：

**1. `openai`** — OpenAI 兼容 `POST {base_url}/audio/transcriptions`（multipart）

- 适用：whisper.cpp server、LM Studio、vLLM-whisper、OpenAI 官方、各类中转站
- `base_url` 要填到 `/v1` 结尾；请求带 `response_format=verbose_json`，
  服务端返回 `segments` 就用其时间轴，只返回 `text` 时退化为「整段一条字幕」

**2. `gradio`** — Qwen3-ASR Pro 等 Gradio 服务的 `/single_handler`

- 适用：本机 `127.0.0.1:7867` 那套 Qwen3-ASR（含角色识别 / 热词 / 标点断句 / 逐字时间戳）
- 需要 `gradio_client`；服务端返回一个 zip，里面有 `single_全员.srt`（往往逐词一行）、
  按角色分的 `.srt` 和 `时间.json`（词级时间 + speaker）。**服务端那个 SRT 未必能直接用，
  `max_chars` 也不对它生效**，所以本工具先看它像不像正常字幕行：像（中文场景，服务端
  已按标点断句）就直接用，只把行首 `[角色 SPEAKER_xx]` 归一成 `[SPEAKER_xx]`；不像
  （英文场景逐词一行）才拿它的词配 `时间.json` 的 speaker，自己按 `max_chars` / 句末标点 /
  说话人切换 / >0.8s 停顿聚合成行（`group_words_into_segments`，`app/core/asr_remote.py`）
- 独有选项：`diarize`（角色识别，字幕行首带 `[SPEAKER_xx]`，行内字数按正文算）、
  `hotwords`（热词注入）、`max_chars`（单行字数，Gradio 协议下由本工具聚合时使用）、
  `punc`（标点断句）

> 兼容性说明：Gradio 6.x 的 `api_name` 不带前导斜杠（`single_handler`），
> 5.x 及更早则带（`/single_handler`），本工具两种都认。
> 探测接口见 `POST /api/whisper/test`：默认只探协议层（秒回，不传音频）；
> 传 `{"deep": true}` 会现场用 ffmpeg 合成一段 ~7 秒的测试语音**真跑一次转写**，
> 只有拿回非空文本才算通过 —— 能区分「服务在跑」和「模型真能出字」。

### 架构：一核两壳

```
                    ┌──────────────────────────────┐
                    │   Engine Core（纯 Python）    │
                    │  探测/抽帧/转写/AI/落盘/配置  │
                    └──────┬───────────────┬───────┘
                           │               │
              ┌────────────▼────┐   ┌──────▼──────────┐
              │ 桌面外壳 (预留)  │   │ Web 外壳 (HTTP) │  ← 本仓库实现
              └─────────────────┘   └─────────────────┘
```

```
app/
├─ infra/     基础设施（仅标准库）：paths / bootstrap / config / tools / dialogs
├─ core/      引擎内核（禁止 import UI 库）：media / whisper / ai / metadata / rename / engine
├─ db/        SQLite + SQLAlchemy：models / session / repo
└─ shell/web/ FastAPI 路由 + 任务调度 + SSE 桥 + 零框架前端
```

## 🔌 API 契约

### 任务与数据

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/tasks` | 创建任务，body `{ path }` 或 `{ paths: [...] }` |
| `POST` | `/api/tasks/{id}/stop` | 停止任务 |
| `GET` | `/api/tasks`、`/api/tasks/{id}` | 任务列表 / 详情 |
| `GET` | `/api/tasks/{id}/files` | 任务内单文件记录 |
| `GET` | `/api/events` | SSE 事件流 |
| `GET` | `/api/logs` | 历史日志（可按级别 / 任务 / 关键字过滤） |
| `GET` `PUT` | `/api/config` | 读取 / 保存配置 |
| `POST` | `/api/review/{id}` | 提交审批 `{ decision, newName? }` |
| `GET` | `/api/reviews` | 审批条目 |

### 工具与诊断

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/dialog/files`、`/api/dialog/folder` | 服务端调起系统原生对话框，返回绝对路径 |
| `POST` `GET` | `/api/dialog/cancel`、`/api/dialog/status` | 取消 / 查询对话框 |
| `POST` | `/api/ai/test` | 测试 AI 后端可达性并识别模型 |
| `GET` | `/api/whisper/status` | faster-whisper / CUDA 设备 / 运行库 / 后端 / 模型就绪状态 |
| `POST` | `/api/whisper/install`、`/api/whisper/setup` | 装依赖 / 补 CUDA 库（系统已装过会直接复用） |
| `POST` | `/api/whisper/model` | 下载 / 续传 Whisper 模型（断点续传，弱网可反复重试） |
| `POST` | `/api/whisper/test` | 测试自定义转写 API（`deep=true` 时真跑一次转写） |
| `GET` | `/api/mirrors` | 下载镜像预设与当前值 |
| `GET` | `/api/server` | 当前监听地址、本机与局域网访问 URL |
| `GET` | `/api/health`、`/api/runtime` | 健康检查 / 运行期依赖报告 |

### SSE 事件类型

`status` / `task` / `progress` / `phase` / `log` / `review` / `inference`。
所有事件含 `ts`（毫秒时间戳）与 `type`；`phase` 取值 `"1"` `"2a"` `"2b"` `"3"` `"4"` `"5"`。

## 🗄 数据库与回滚

SQLite 单文件库 `data/vair.db`（WAL 模式）：

| 表 | 用途 |
|------|------|
| `tasks` | 任务主表（状态 / 统计 / 配置快照） |
| `task_files` | 单文件处理记录，**兼作改名映射表**（可回滚） |
| `log_entries` | 分级运行日志 |
| `review_items` | 命名审批条目 |

启动时会自动把上次未正常结束的任务标记为 `interrupted`（断电恢复）。

## 📁 目录规范

```
videoAIrenameAPP/
├─ app/                源码
├─ ffmpeg/             ffmpeg.exe / ffprobe.exe / exiftool（仓库自带）
├─ libs/               第三方包（运行期生成，不写系统 site-packages）
├─ models/             Whisper / HF 模型缓存（运行期生成）
├─ cache/  tmp/  logs/ 缓存 / 临时文件 / 运行日志
├─ data/               SQLite 数据库
├─ config.json         用户配置（运行期生成，含密钥，不入库）
├─ config.example.json 配置模板
└─ docs/               方案文档
```

启动的第一段代码就把 `TMP/TEMP/TMPDIR`、`HF_HOME`、`XDG_CACHE_HOME` 重定向到上述目录 ——
这是「不到处乱放」的唯一可靠做法（很多库在 import 时就读这些变量）。

## 🚨 注意事项

### 隐私与数据安全

- 关键帧与转写文本会发送给你**自己配置的** AI 后端；用本地模型则数据不出本机。
- `config.json` 含 `api_key`，**不入库**（仓库只保留 `config.example.json`）。
- 仓库不含 `libs/`、`ffmpeg/`、`models/`、`data/` 等大体积或含隐私的目录，首次启动自动补齐。

### 性能建议

| 现象 | 建议 |
|------|------|
| AI 报「上下文溢出」 | 降低 `frames.max_keyframes`（如 10 → 6）或 `frames.max_side`（640 → 480） |
| 处理很慢 | 提高 `frames.workers` 与 `runtime.ai_workers`；确认 Whisper 走的是 GPU |
| 转写挂起不动 | 缺 `cublas64_12.dll`；设置页点「下载 / 修复 CUDA 库」（系统 Python 里已装过 `nvidia-cublas-cu12` 会直接复用，不再重复下载），或把 `whisper.device` 设为 `cpu` |
| 模型下载卡住 / 反复失败 | 已内置断点续传：中断后再点「下载 / 续传模型」会从已下载的字节继续，不会从零开始 |
| 转写报「本地模型未就绪」 | 本地模型没下完（缺词表）。两种解法：①保持 `whisper.backend=auto`，走 API 转写，本地模型不参与；②点「下载 / 续传模型」补全（续传从 `model.bin` 与 `model.bin.incomplete` 里较大的那份接着下）。`whisper.allow_download` 默认关闭，所以任务不会自己去拉 1.6GB |
| 手机访问不了 | 打开设置页「局域网访问」并**重启服务**，确认同一 Wi-Fi 且防火墙放行端口 |

### 已知行为

- **元数据写在哪**（这决定了 Windows 属性面板能不能看到）：ExifTool 会分两遍写 ——
  ① UTF-8 写 `ItemList` / `XMP`，给播放器、Jellyfin、Kodi 读；
  ② GBK 写 QuickTime `Keys` 盒，Windows 资源管理器「属性 → 详细信息」读的是这一份。
  为什么第 ② 步必须用 GBK：Windows 按**系统 ANSI 代码页**（中文 = GBK）解码 Keys 盒，
  若存 UTF-8，面板会显示「榛勮壊鑺辫-榛戜笣缇庡コ」这类乱码，看起来就像「没生效」。
  另外 `Keys` 盒一旦存在，Windows 就**只认它**、完全忽略 `ItemList` ——
  所以只写 ItemList 的文件在属性面板里永远是空的。
- **Windows 属性面板能看到**：标题、说明/备注、程序名称（软水印 `AIVideoRenameV1`）。
- **看不到的**：**「标记 / 关键词」**。这不是本工具的 bug —— Windows 的 MP4 属性处理器
  不读第三方写入的关键词（`Keys:Keywords`、`XMP-dc:Subject`、`XMP-pdf:Keywords`
  实测全部读不到）。关键词仍写进了 `XMP`，Jellyfin / Kodi / 其它读 XMP 的工具正常可见。
- **分片 MP4**（OBS 的 fragmented MP4、录制中断产物）ExifTool 写不进去，工具会用
  ffmpeg `-c copy` **无损重封装**成标准 MP4 后再写（只换容器，不重编码，画质无损）。
- **跳过已处理**默认开启：文件名含标记或已写软水印的文件跳过；关闭则全部重跑，
  同名 `.nfo` / `.srt` 会被覆盖。
- ExifTool 覆写会刷新文件时间，工具会在写完后还原改名前的创建 / 修改时间。

## ❓ 常见问题

**Q：为什么要用系统文件选择器，不能直接拖吗？**
A：都能用。拖拽依赖浏览器给的 `text/uri-list`（`file:///D:/...`）；部分浏览器不给路径，
这时用「选择视频 / 选择文件夹」按钮，由服务端调起原生对话框拿真实绝对路径。

**Q：不装 faster-whisper 能用吗？**
A：能。会自动降级为**纯画面分析**，只是没有字幕和 `.srt`。设置页可一键安装。

**Q：没有显卡 / 不想下 1.6GB 模型，能出字幕吗？**
A：能。设置 → 转写后端选「自动（优先 API）」或「仅自定义 HTTP API」，填一个 OpenAI 兼容的语音服务地址
（或本机的 Qwen3-ASR Gradio 服务），音频会发过去转写，本机不占显卡、不下载模型。
两种协议的区别见[②b 转写](#b-转写本地模型-或-自定义-api)。

**Q：AI 用的是云端还是本地？**
A：你说了算。`ai.provider=openai` + 本地 `base_url` 就是本地模型；填云端地址就是云端。
设置页「测试连接」会告诉你后端识别出来是什么模型。

**Q：改错了名字能撤回吗？**
A：`data/vair.db` 的 `task_files` 表保留了 `old_path → new_path` 映射，
`repo.rename_map()` 可导出后自行改回。建议先开「预览模式」试跑。

**Q：`启动.vbs` 双击没反应 / 报脚本错误？**
A：该脚本必须以 **ANSI(GBK)** 保存 —— Windows 脚本宿主只认 GBK 或 UTF-16LE，
存成 UTF-8 会报「无效字符」。另外 `logs/start.log` 里有完整启动日志。

## 技术栈

| 层 | 选型 |
|------|------|
| 后端 | Python 3.11+ / FastAPI / Uvicorn / Pydantic |
| 前端 | 零框架原生 ES Module + SSE，无构建步骤 |
| 数据库 | SQLite（WAL）+ SQLAlchemy 2.0 |
| 视频 | ffmpeg / ffprobe（抽帧、抽音轨、探测） |
| 元数据 | ExifTool（perl 版，UTF-8 ArgFile） |
| 语音 | faster-whisper（CTranslate2，CUDA / CPU） |
| AI | OpenAI 兼容接口 或 Google Gemini（多模态） |

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源。

```
Copyright (c) 2026 xiaoweo233
```

你可以自由地使用、复制、修改、合并、发布、分发、再许可和/或销售本软件的副本，
唯一的要求是保留上述版权声明与许可声明。本软件按「原样」提供，不附带任何明示或
暗示的担保。

> 注意：MIT 许可仅覆盖本仓库自身的源代码。程序运行时会下载或调用的第三方组件
> （faster-whisper、FFmpeg、ExifTool、FastAPI 等）遵循各自的许可证，其中 FFmpeg
> 的授权方式取决于你使用的构建版本（LGPL / GPL），商用分发前请自行确认。

## 🙏 致谢

- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — 高效的 Whisper 推理实现
- [FFmpeg](https://ffmpeg.org/) — 视频处理核心
- [ExifTool](https://exiftool.org/) — 元数据读写
- [FastAPI](https://fastapi.tiangolo.com/) — Web 外壳
- [OpenAI 兼容生态](https://platform.openai.com/docs/api-reference) — 本地大模型也能直接接

---

<div align="center">

**拖进去，剩下的交给 AI** 🚀

[GitHub](https://github.com/xiaoweo233/videoAIrename-webUI) •
[Issues](https://github.com/xiaoweo233/videoAIrename-webUI/issues)

</div>
