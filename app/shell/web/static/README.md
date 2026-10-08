# VAIR Web 外壳 · 静态前端（app/shell/web/static）

「视频 AI 重命名助手」的 **Web 外壳**，是「一核两壳」架构中的浏览器访问端。  
本目录**只含前端静态页**，不含任何后端代码；后端由 `shell/web/api.py`（FastAPI）提供，  
本页通过同源 `/api` 或 `window.__VAIR_API__` 接入。

## 设计目标

| 目标   | 落地方式                                                         |
| ---- | ------------------------------------------------------------ |
| 现代美观 | 深色玻璃拟态（glassmorphism）+ 渐变光晕 + 圆角卡片                           |
| 动画丝滑 | 仅动 `transform` / `opacity`，统一 `cubic-bezier(.22,1,.36,1)`    |
| 毛玻璃  | `backdrop-filter: blur() saturate()` + 高光内边框 `inset 1px 0 0` |
| 首屏极快 | 零框架、关键 CSS 内联、模块化 JS、系统字体栈、无外部资源                             |
| 小内存  | 环形日志缓冲（上限 500）+ 虚拟滚动 + rAF 合并渲染 + 监听器可清理                     |

## 运行方式

无需构建、无依赖。用任意静态服务器指向本目录即可：

```bash
# 在 static 目录内
python -m http.server 8000
# 浏览器打开 http://127.0.0.1:8000/
```

单独打开也能完整演示：SSE 连接失败时会**自动降级到内置 mock 模拟器**，  
产生逼真的五阶段流水线事件（探测 → 抽帧 ∥ 转写 → 合并 → AI → 落盘）。

> 直接双击 `index.html`（`file://`）也可运行，但 ES Module 可能受浏览器 CORS 限制；  
> 推荐用上面的本地服务器方式。

## 文件结构

```
static/
  index.html            首屏骨架 + 关键 CSS 内联 + 模块入口
  css/app.css           全部样式：主题变量 / 玻璃 / 动画 / 响应式 / 降级
  js/app.js             应用入口：路由、事件泵、主题、Toast、导航编排
  js/store.js           状态中心 + 环形日志缓冲 + 默认配置（对齐 §7.2）
  js/api-client.js      fetch 封装 + SSE 订阅（失败降级 mock）
  js/simulator.js       内置 mock 流水线事件模拟器
  js/utils.js           通用工具（DOM 构造 / rAF 批处理 / 对象路径等）
  js/views/dashboard.js 工作台
  js/views/live.js      实时进度（五阶段流水线）
  js/views/review.js    命名审批
  js/views/logs.js      运行日志（虚拟滚动）
  js/views/settings.js  设置（开关总表 + AI 配置 + 术语）
  README.md             本文件
```

> 说明：相比最初建议结构，额外新增了 `js/utils.js`（通用工具，避免各视图重复造轮子）。

## 界面范围（对应方案 §4.2 / §7 / §9）

1. **工作台**：拖拽文件夹投放区（drag\&drop + 路径输入 + 拖入高亮）、任务控制（开始/停止/预览模式开关）、统计卡片（成功/跳过/失败/取消 + 平均秒/视频）、当前任务进度条 + 进度环。
2. **实时进度**：五阶段流水线可视化（①探测 → ②a抽帧 ∥ ②b转写 → ③合并 → ④AI分析 → ⑤落盘），节点点亮/脉冲、当前文件高亮、各阶段耗时。
3. **命名审批**：改名前后对比卡片（原名 → 新名 + 置信度 + tags），逐条「采纳/编辑/忽略」+ 批量操作 + 空状态。
4. **运行日志**：分级过滤（ALL/DEBUG/INFO/WARN/ERROR）、关键字搜索、自动滚动开关、虚拟滚动；单文件推理时间线（模型名/帧数/字幕字数/耗时）。
5. **设置**：§7.1 开关总表六组（输入/转写/命名/输出/安全/高级）+ 术语 tooltip；AI 配置表单对齐 §7.2；改动即时保存（debounce + 保存提示）。
6. 顶部**深色/浅色主题切换**（默认深色，localStorage 记忆）。
7. 移动端响应式（<768px 侧栏折叠为抽屉 + 底部导航），方便手机审批命名。

## 数据 / 对接契约

### base URL

`window.__VAIR_API__`（字符串，如 `http://192.168.1.10:8000/api`）优先；否则同源 `/api`。

### 预留端点

| 方法     | 路径                 | 说明                                 |
| ------ | ------------------ | ---------------------------------- |
| `POST` | `/api/tasks`       | 创建任务，body `{ path, ... }`          |
| `GET`  | `/api/events`      | SSE 事件流                            |
| `GET`  | `/api/logs`        | 拉取历史日志                             |
| `POST` | `/api/review/{id}` | 提交审批，body `{ decision, newName? }` |
| `GET`  | `/api/config`      | 读取配置                               |
| `PUT`  | `/api/config`      | 保存配置                               |

### SSE 事件契约（与 `js/simulator.js` 产出一致）

所有事件均含 `ts`（毫秒时间戳）与 `type`：

```jsonc
// 连接状态
{ "type":"status", "connected":true, "mode":"sse" }

// 任务生命周期
{ "type":"task", "action":"started|completed|stopped", "taskId":"...", "total":6 }

// 进度（驱动工作台统计与进度条）
{ "type":"progress", "taskId":"...", "phase":"4", "currentFile":"xxx.mp4",
  "done":3, "total":6, "stats":{"success":2,"skipped":1,"failed":0,"cancelled":0}, "avgSeconds":41.3 }

// 流水线阶段（驱动实时进度节点）
{ "type":"phase", "phase":"2a", "state":"start|done|skip", "file":"xxx.mp4", "elapsedMs":820 }

// 日志（分级）
{ "type":"log", "level":"INFO|WARN|ERROR|DEBUG", "message":"...", "file":"xxx.mp4" }

// 单文件推理时间线
{ "type":"inference", "file":"新名.mp4", "model":"qwen3.8-27b",
  "frames":28, "subtitleChars":312, "elapsedMs":41200 }

// 审批条目
{ "type":"review", "action":"add|clear", "item":{"id":"r_xxx","oldName":"...","newName":"...","confidence":0.91,"tags":["航拍"]} }
```

`phase` 取值：`"1"`、`"2a"`、`"2b"`、`"3"`、`"4"`、`"5"`。

### config.json 结构

前端默认配置与状态中心 `js/store.js` 的 `DEFAULT_CONFIG` 一致，严格对齐方案 §7.2。

**说明（最小扩展）**：§7.1 开关总表要求 `recursive`（递归）与 `srt_vad_filter`（VAD 过滤）两个开关，  
但 §7.2 的 config.json 骨架未列出对应键，故补齐为 `input.recursive` 与 `whisper.vad_filter`（默认均为 `true`）。  
`frames.hwaccel` 在 §7.2 中为字符串（`"none"`），界面上以「GPU 硬解抽帧」布尔开关呈现：开 = `"cuda"`，关 = `"none"`。

## 性能 / 内存优化点

- **零框架**：无 React/Vue/MUI/Tailwind，无 npm 包、无 CDN、无网络字体（系统字体栈），首屏 JS 极小。
- **关键 CSS 内联**：`index.html` 内联首屏必需样式（布局 / 骨架 / 主题变量），完整样式表异步友好加载，避免 FOUC 与 CLS。
- **骨架屏**：视图挂载前显示 shimmer 骨架，避免布局跳动。
- **环形日志缓冲**：`RingBuffer` 固定上限 500，满时覆盖最旧，读写 O(1)，无数组搬迁。
- **虚拟滚动**：日志列表固定行高 26px，只渲染可视窗口 + 越界行，DOM 行节点池恒定（≈25 个），日志再多也不增节点。
- **rAF 合并渲染**：`store` 通知与滚动重绘均用 `requestAnimationFrame` 合并，同一帧内多次变更只渲染一次；事件泵不逐条写 DOM。
- **引用变更判定**：`review` 与 `inference` 仅在数组引用变化时重建 DOM，避免日志/进度高频刷新时每帧重排。
- **按需加载 mock**：`simulator.js` 用动态 `import()`，仅在 SSE 失败时才加载。
- **blur 层数量受控**：`backdrop-filter` 只用于顶栏/侧栏/卡片/底栏/Toast；日志滚动容器**不使用** backdrop-filter，并加 `contain: strict`，保证滚动帧率。
- **监听器可清理**：视图 `destroy()` 统一退订 store，切换路由释放监听器；滚动/拖拽监听使用 `passive: true`。
- **离屏优化**：虚拟列表行 `contain: layout style`；关键动效 `will-change: transform`。
- **无障碍降级**：`@media (prefers-reduced-motion: reduce)` 关闭所有动画与过渡。

## 已知限制 / 后端接入待办

- 浏览器安全模型下，网页**无法获得文件夹绝对路径**：拖拽只能读到名称（`webkitGetAsEntry`），落地处理需后端提供目录选择接口或在输入框填绝对路径。`showDirectoryPicker` 仅返回目录名。
- 任务「停止」在 mock 模式下通过停止事件泵模拟；真实语义由后端 `StopToken` 实现（建议新增 `POST /api/tasks/{id}/stop`，前端已按此约定调用）。
- `POST /api/tasks` 与 `POST /api/review/{id}` 的返回体未做严格校验，按「成功即静默、失败静默降级」处理。
- 审批「采纳/忽略」为乐观更新（本地先移除）；如需严格一致，可改为等待后端确认。
- 断线重连：SSE 连接过又中断时依赖浏览器自动重连；从未连上则降级 mock 且不再自动尝试。
- 事件契约、配置字段若与后端实现有差异，以本文档与 `js/store.js`、`js/simulator.js` 为准，需同步更新。
