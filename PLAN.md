# 转写后端改造：优先 API ASR，失败自动回退本地 Whisper

## Context（现状与问题）

用户当前任务日志（m00001）：

```
正在加载 Whisper 模型 large-v3-turbo（device=cpu, compute=int8）...
WARN  Whisper 模型加载失败（cpu）：加载模型时连不上模型下载站（peer closed connection
      without sending complete message body (received 1773958 bytes, expected 1617884929)）
DEBUG 跳过转写：加载模型时连不上模型下载站…
→ 最终靠「纯画面分析」命名成功（本批成功 1）
```

根因：`models/whisper/large-v3-turbo/` 只有 `model.bin`(317MB) + `model.bin.incomplete`(190MB)，缺
`vocabulary.json`，`resolve_local_model()` 返回 `''` → 回落到按「模型名」加载 → 联网下载 1.6GB → 弱网失败。

而用户本机已有可用的远程 ASR：Qwen3-ASR Pro @ `http://127.0.0.1:7867`（config.json 里已填好
`whisper.api.base_url`，`protocol=gradio`），`app/core/asr_remote.py` 已实现完整远程后端 +
`probe_asr_backend()` 连通性探测 + `/api/whisper/test` 接口 + 前端「测试转写 API」按钮。

**问题在于它是「后端二选一」**（`whisper.backend = local | api`），且 `WhisperEngine.load()` 在
`backend=api` 失败后置 `_load_failed` 终态，**没有任何回退**；反过来 `backend=local` 时完全不会尝试远程。
用户要的是「优先远程，远程不可用自动回退本地」，并把测试连接做得显眼。

目标：
1. 新增开关「优先使用 API 接口 ASR」；开启时先探测/使用自定义 API（默认 `http://127.0.0.1:7867`）。
2. API 不可用 → 本次任务内自动回退本地 faster-whisper，并在日志/界面明确告知。
3. 设置页提供「测试连接」按钮（现有 `buildAsrTestPanel` 复用/增强）。

## Approach（推荐方案）

### 1. 语义：把「二选一」变成「优先级 + 回退」

保留 `whisper.backend` 字段做兼容，但改为三态（下拉）：

| backend | 行为 |
| --- | --- |
| `auto`（新默认） | 优先 API（若 `prefer_api` 且填了 base_url），失败自动回退本地 |
| `local` | 仅本地（等价旧 local） |
| `api` | 仅 API，不回退（等价旧 api，用于「远程是唯一手段」场景） |

新增开关 `whisper.prefer_api: true`（用户明确要求的那个开关）：
- `prefer_api=true` + `backend=auto` → 先 API 后本地
- `prefer_api=false` → 忽略 API，走本地（即使 backend=auto）
- 配置迁移：读到旧配置（无 `prefer_api`）时默认 `true`；`backend` 缺省从 `local` 改为 `auto`，
  避免老用户升级后仍永远走本地。**（待确认：是否改默认值，见问题 1）**

### 2. 核心：`WhisperEngine` 支持「主备」而非「单选」

`app/core/whisper.py`：

- 新增构造参数 `prefer_api: bool = False`；内部维护
  `self._remote_active: bool`（当前是否在用远程）、`self._remote_failures: int`。
- `_effective_plan()` → 返回 `("api"|"local")`：
  `api` 若 `prefer_api/backend==api` 且 `_api_config.base_url` 非空 且 `_remote_failures < 阈值`。
- `load()`：
  - 计划为 api → `remote.load()` 成功则 `_actual_device="remote"` 返回 True；
  - 失败（且 `backend != "api"`）→ `WARN 远程 ASR 不可用，自动回退本地 Whisper：{remote.load_error}`，
    `self._remote_failures += 1`，**不置 `_load_failed`**，继续走现有本地加载分支。
  - 本地也失败 → 保持现有 `_load_failed` 终态 + `_friendly_error`（不重复联网重试）。
- `transcribe()`：按 `_effective_plan()` 分流到 `_transcribe_remote()` 或本地；
  远程返回 `error`（非 skipped、无 segments）时 → `WARN 远程转写失败，本条改用本地：{error}`，
  `_remote_failures += 1`，**同一条音频回退本地重试一次**；达到阈值（建议 2）后本任务内固定本地。
- 远程看门狗超时同样计入 `_remote_failures`。
- `set_api_config()` 小方法：测试/切换后允许热更新 `ASRConfig`（清 `_remote` 缓存）。

`app/core/engine.py::_get_whisper()`：透传 `prefer_api=bool(w_cfg.get("prefer_api", True))`。

### 3. API / 配置

- `app/infra/config.py`：`whisper` 增 `prefer_api: true`；`backend` 默认改 `auto`；注释更新。
- `app/shell/web/api.py::whisper_status()`：新增返回
  `prefer_api`、`effective_backend`（`api|local`，由 `prefer_api && base_url` 推得）、
  `api_ready`（`base_url` 是否填写）。供环境面板显示「当前生效：API / 本地」。
- `/api/whisper/test` **保持不变**（已够用：`probe_asr_backend` 探 `/config` 并校验 `single_handler`）。
  可选增强：加 `deep: true` 参数 → 用 `demo` 音频真跑一次 `single_handler`（慢，默认关闭）。**（问题 3）**
- `requirements.txt` + `bootstrap._OPTIONAL`：补 `gradio_client>=1.0`（当前 libs 里已有 2.7.2，
  但干净环境装了 faster-whisper 也跑不了 gradio 协议，且现在报错信息不指向安装动作）。

### 4. 前端 `app/shell/web/static/js/views/settings.js`

- `SWITCH_GROUPS` 的「转写」组新增：
  `{ id: 'prefer_api', path: 'whisper.prefer_api', label: '优先使用 API 接口 ASR', desc: 'API 不可用时自动回退本地 Whisper' }`
  （放在 `enable_whisper` 之后、`use_gpu` 之前）
- `TIP` 增 `prefer_api` 文案；`store.js` DEFAULT_CONFIG.whisper 同步 `prefer_api: true`、`backend: 'auto'`。
- `buildAsrPanel()` 改造：
  - `backendSel` 选项改为 3 项（auto/local/api）+ 提示「auto = 先 API 后本地」。
  - `renderAsrBody()`：`backend !== 'local'` 时都渲染 API 字段（不只 `=== 'api'`）；
    `auto` 时在顶部加一句「远程不可用会回退本地；base_url 留空则直接走本地」。
  - 「测试转写 API」按钮从 `api` 分支里提到面板常驻位置（用户说「添加测试连接按钮」——
    实际已存在但被 `backend=local` 隐藏，这就是他觉得没有的原因）。
  - 测试成功后若 `silentAutoFill`（base_url 为空但探测到 7867 可用）提示一键填入。**（问题 4，可选）**
- `renderEnv()`（环境面板）：显示「当前转写后端：API（Qwen3-ASR Pro @ …）/ 本地 faster-whisper」，
  并在 API 优先但探测失败时给 `env-warn`「远程不可用，本次会回退本地」。

### 5. 日志与可见性

- 引擎侧统一日志前缀：`转写后端：API {base_url}（{protocol}）` / `已回退本地 Whisper`。
- `_transcribe_one()` 的 `emit_phase(TRANSCRIBE, ...)` 增加 `backend` 字段（可选），
  live 视图/文件详情能看到本条走的是远程还是本地。

## Files to modify

- `app/core/whisper.py` — 主备逻辑（`_effective_plan`、`load`、`transcribe`、失败计数、`set_api_config`）
- `app/core/engine.py:161-179` — 透传 `prefer_api`
- `app/infra/config.py:50-77` — `prefer_api` 默认值、`backend` 默认 `auto`、注释
- `app/shell/web/api.py:330-357` — `whisper_status` 增加 `prefer_api`/`effective_backend`
- `app/shell/web/static/js/views/settings.js:70-110, 797-943` — 开关 + 面板改造
- `app/shell/web/static/js/store.js:113-135` — DEFAULT_CONFIG 同步
- `requirements.txt`、`app/infra/bootstrap.py:574` — 补 `gradio_client`
- `README.md:139-140, 203-225` — 文档更新（新开关、回退语义）

## Reuse（不要新写）

- `app/core/asr_remote.py`：`ASRConfig.from_dict`、`RemoteASREngine.load/transcribe`、`probe_asr_backend`、
  `_friendly_net_error`、`PROTOCOLS`、`GRADIO_LANG_MAP`
- `app/core/whisper.py`：`_transcribe_remote()`（已带看门狗）、`_get_remote()`（已继承 `whisper.language`）、
  `resolve_local_model`、`_friendly_error`、`_is_source_error`、`_hf_endpoint_guard`
- `app/shell/web/api.py`：`/api/whisper/test` 整套（`ASRTestBody` 合并 saved 配置）
- `app/shell/web/static/js/views/settings.js`：`buildAsrField`、`buildAsrTestPanel`、`kv`、`commit`、`getPath/setPath`、`toast`
- `api-client.js::testASR / whisperStatus`

## Steps

- [ ] 1. `config.py`：加 `whisper.prefer_api`（默认 true）与 `backend` 默认 `auto`；写迁移（旧值 `local` 保留不动，只补缺失键）
- [ ] 2. `whisper.py`：新增 `prefer_api` 参数 + `_effective_plan()` + 远程失败回退（load 级 + 单条转写级 + N 次后固定本地）+ `set_api_config()`
- [ ] 3. `engine.py`：透传 `prefer_api`；本地/远程后端在 `_log` 里标明来源
- [ ] 4. `api.py::whisper_status`：暴露 `prefer_api` / `effective_backend` / `api_ready`
- [ ] 5. `settings.js` + `store.js`：三态下拉、常驻测试连接按钮、prefer_api 开关、环境面板显示生效后端
- [ ] 6. `requirements.txt` / `bootstrap._OPTIONAL` 补 `gradio_client`；`RemoteASREngine.load()` 的导入失败文案指向安装动作
- [ ] 7. `README.md` 更新「②b 转写」章节与配置表
- [ ] 8. 自检脚本（一次性，不入库）：构造 `WhisperEngine(prefer_api=True, base_url=7867)` → `load()` 应为 True；
      再把 base_url 改成 `:7999` → 应打印回退日志并回落到本地（本地不可用时返回 skipped 而非崩溃）

## Verification

1. **API 优先生效**：config `whisper.enable=true, prefer_api=true, backend=auto, api.base_url=http://127.0.0.1:7867, api.protocol=gradio`，
   跑 1 个视频 → 日志出现「转写后端：API」，`转写完成：字幕 N 字`，`device=remote`。
2. **回退**：把 base_url 改成 `http://127.0.0.1:7999` → 日志「远程 ASR 不可用，自动回退本地 Whisper：<友好错误>」，
   然后走本地加载（本地模型完整时 tiny 可用；large-v3-turbo 未下完 → 提示去下载，不崩）。
3. **开关关闭**：`prefer_api=false` → 完全不访问 7867（用 `netstat`/日志确认），直接本地。
4. **测试连接**：设置页点「测试转写 API」→ 「转写服务可用 / Gradio 服务已就绪（version=…，接口：single_handler）」；
   地址写错 → 中文错误提示 + 日志 WARN。
5. **状态显示**：`GET /api/whisper/status` 返回 `effective_backend=api`；环境面板显示当前生效后端。
6. **回归**：`backend=api` 且远程挂掉 → 不静默回退（符合「仅 API」语义），文件按「无字幕」继续命名。

## Questions（待用户确认）

1. `whisper.backend` 默认值是否从 `local` 改为 `auto`（老配置不动）？现有 config.json 里是 `local` —— 是否需要我把你机器上这份直接改成 `auto` + `prefer_api=true`？
2. 回退粒度：远程单条转写失败是否要「同一条音频立刻本地重试」，还是只在「失败 2 次后整批固定本地」（前者更稳但慢）？
3. 「测试连接」要不要加「真实短音频试转写」深度测试（会花 3~10s）？还是保持现在的协议层探测（`/config` + `single_handler` 校验，秒回）？
4. `large-v3-turbo` 现在只下了 500MB/1.6GB：本次要不要顺带把「下载/续传模型」按钮的续传逻辑再压一遍（弱网实测），还是本轮只管 API 优先与回退？
