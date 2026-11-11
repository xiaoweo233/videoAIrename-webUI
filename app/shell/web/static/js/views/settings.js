/**
 * views/settings.js — 设置视图
 * 完整覆盖方案 §7.1 开关总表（输入/转写/命名/输出/安全/高级六组），
 * 每个开关带「这是什么」术语 tooltip；AI 配置表单严格对齐 §7.2 的 config.json 结构；
 * 改动即时保存（debounce + 保存提示）。
 */
import { h, getPath, setPath, deepClone } from '../utils.js';
import { DEFAULT_CONFIG } from '../store.js';

/** 术语表（一句话人话解释，来源：方案 §附） */
const TIP = {
  ffmpeg: 'ffmpeg：处理音视频的万能工具，负责截图和读时长。',
  ffprobe: 'ffprobe：ffmpeg 的兄弟，专门读视频信息（分辨率、时长、有没有声音）。',
  exiftool: 'exiftool：把标题/说明写进视频内部属性。写的是 MP4 的 Apple Keys 盒（Windows 资源管理器「属性→详细信息」读的就是它）+ ItemList（播放器）+ XMP（Jellyfin/Kodi）。注意：Windows 对 MP4 的「标记/关键词」列不读第三方写入的值，属系统限制；标题、说明、程序名称可正常显示。',
  whisper: 'Whisper：语音识别模型，把说的话变成字。',
  keyframe: '关键帧：从视频里挑出的代表性画面，挑得多看得准但更慢。',
  transcribe: '转写：把视频里说的话变成文字，帮 AI 判断内容。',
  multimodal: '多模态模型：既能看图又能读字的 AI，本应用必须用这种。',
  nfo: 'NFO：XML 信息文件，Jellyfin / Kodi 靠它认视频。',
  srt: 'SRT：最常见的字幕格式，记事本就能打开。',
  cuda: 'CUDA：让程序用显卡加速的东西，没有也能用，只是慢。',
  recursive: '递归：连子文件夹一起处理。',
  dryrun: '预览模式：只演算不落盘，用来确认命名效果。',
  fmp4: '分片 MP4：OBS 常见格式，ExifTool 写不进去；重封装只换容器不重编码，画质无损。',
  lan: '局域网访问：服务监听 0.0.0.0，手机/同网设备用 http://<本机IP>:8000/ 打开；改动需重启服务生效。',
  provider: 'provider：后端类型。openai = OpenAI 兼容接口（llama.cpp / vLLM / NInfer 都算），gemini = 谷歌云端。',
  base_url: 'base_url：服务地址，形如 http://127.0.0.1:8084/v1；结尾的 /v1 别漏，否则 404。',
  api_key: 'api_key：密钥。本地服务随便填即可；云端服务是真凭证，不要外传。',
  timeout: 'timeout：单次请求最长等多少秒。模型越慢越要调大，否则频繁超时。',
  retry: 'retry_times：失败重试次数。本地 0-1 次够用，网络不稳可 3。',
  max_tokens: 'max_tokens：AI 单次最多输出多少 token。太小会把 JSON 截断，导致解析失败。',
  temperature: 'temperature：温度，越低越稳定（0-0.3 适合命名），越高越发散有创意。',
  top_p: 'top_p：采样范围，越小越集中在高概率词；与 temperature 调一个即可。',
  json_mode: 'enforce_json_mode：强制只输出 JSON。本地后端必须开，否则会夹带解释文字导致解析失败。',
  template: '命名模板：{date} → 时间前缀，{title} → AI 标题，{original} → 原文件名。',
  date_format: '日期格式：strftime 写法，%Y 年 %m 月 %d 日 %H 时 %M 分；%Y%m%d_%H%M → 20261009_2313。',
  marker: '处理标记：加进文件名的字符串（如 AI_RENAMED），用来识别哪些已处理过。',
  date_prefix: '时间前缀：文件名开头加录制时间，便于按时间排序。',
  original_suffix: '原名后缀：把原文件名片段追加到新名后面，保留一点原始线索。',
  marker_switch: '处理标记开关：在文件名里追加标记，配合「跳过已处理」使用。',
  skip: '跳过已处理：文件名已含标记或视频内已写入软水印的文件直接跳过，省时间省 token。',
  move_failed: '失败移入 _failed：处理失败的文件集中放到 _failed 目录，便于复查。',
  verbose: '详细日志：输出 DEBUG 级日志，排错时开，平时关掉更清爽。',
  max_side: '帧最长边：截图缩放到这个像素再送 AI。越大越准但更慢更费 token，640 是实测平衡点。',
  device: 'Whisper 设备：auto 自动选，cuda 用显卡，cpu 纯 CPU（慢但不会因缺库挂起）。',
  language: '转写语言：auto 自动检测；指定 zh 更快更准，同时决定 .srt 字幕语言。',
  compute_type: '计算精度：int8_float16 兼顾速度与显存；float16 更准更占显存；显存小选 int8。',
  whisper_workers: '转写并发：Whisper 建议 1 —— GPU 独占，多路反而互相抢显存。',
  frames_workers: '抽帧并发：同时跑几个 ffmpeg 抽帧，CPU 核心多可加到 8-16。',
  ai_workers: 'AI 并发：同时向 AI 后端发几个请求。本地大模型 2-4 合适，云端可更高。',
  log_file: '日志文件：运行日志落盘位置，排错时看它。',
  pip_mirror: 'pip 源：装依赖（faster-whisper / CUDA 库）走哪个镜像，国内用清华/阿里更快。',
  hf_mirror: 'HuggingFace 端点：下载 Whisper 模型走哪个源；必须是 HF 兼容站点（hf-mirror.com 可以，modelscope.cn 不行）。',
  asr_backend: '转写后端：auto = 优先用下面的 API、不可用时自动回退本地 faster-whisper（推荐）；local = 只用本机跑 faster-whisper（要下载模型，约 1.6GB）；api = 只用 HTTP 转写服务，不可用就报错、不回退。',
  asr_prefer_api: '优先使用 API 接口 ASR：开 = 先试下方「转写后端」里的 API 地址，连不上或连续失败 2 次自动回退本地 Whisper；关 = 只用本地 Whisper。backend 选 local/api 时本开关不参与（由后端下拉直接决定）。',
  asr_protocol: 'API 协议：openai = 任意 OpenAI 兼容的 /v1/audio/transcriptions（whisper.cpp server、LM Studio、硅基流动等）；gradio = Qwen3-ASR Pro 这类带界面和角色识别的 Gradio 服务。',
  asr_base_url: '服务地址：OpenAI 协议填到 /v1 结尾（如 http://127.0.0.1:8080/v1）；Gradio 协议填到根路径（如 http://127.0.0.1:7867）。',
  asr_model: 'OpenAI 协议的 model 字段：一般 whisper-1 或服务端实际注册的模型名；Gradio 协议不需要。',
  asr_timeout: '单次转写最长等待秒数。长音频（超过 30 分钟）建议 900 以上，否则会被判超时。',
  asr_diarize: '角色识别（说话人分离）：区分说话人，供 AI 分析对话主体；SRT 字幕不会带 [SPEAKER] 标签。只有 Gradio 协议支持。',
  asr_hotwords: '热词：把常出现的专业术语、人名、品牌名填进来，能显著提升识别率。只有 Gradio 协议支持。',
  asr_prompt: '提示词：给转写服务的上下文hint，例如「以下是关于深度学习的课程」。',
  asr_maxchars: '单行字数：字幕每行最多几个字，超过就断行（遇到句末标点、说话人切换、超过 0.8 秒停顿也会断行）。英文按字符数算，一个单词不会硬拆开。20 是观看舒适的常规值。',
  asr_punc: '标点断句：开启后会自动加逗号句号并据此断句；关闭则是一整段连续文本。',
  model_download: '模型下载：large-v3-turbo 约 1.6GB，弱网会反复续传，可以随时关掉页面稍后再点（会接着下）。',
  prompt_sys: '系统提示词：给 AI 定身份与尺度，决定它用什么视角理解视频。',
  prompt_user: '用户提示词：规定输出格式，必须要求返回含 title / plot / tags 的 JSON。',
};

/** 开关分组（严格对应 §7.1 开关总表） */
/** buildAsrPanel 的 renderAsrBody：顶部「优先使用 API」开关切换后联动刷新 API 参数区 */
let asrRerender = null;

const SWITCH_GROUPS = [
  {
    id: 'input',
    label: '输入',
    items: [
      { id: 'recursive', path: 'input.recursive', label: '递归子文件夹', desc: '连子文件夹一起处理', tip: TIP.recursive },
    ],
  },
  {
    id: 'whisper',
    label: '转写',
    items: [
      { id: 'enable_whisper', path: 'whisper.enable', label: '启用 Whisper 转写', desc: '关闭后跳过语音，纯画面分析', tip: TIP.whisper },
      { id: 'prefer_api', path: 'whisper.prefer_api', label: '优先使用 API 接口 ASR', desc: '开：先走下方 API，不可用自动回退本地 Whisper；关：只用本地 Whisper', tip: TIP.asr_prefer_api, onChange: () => { if (asrRerender) asrRerender(); } },
      { id: 'use_gpu', path: 'whisper.use_gpu', label: 'GPU 加速转写', desc: '用显卡跑 Whisper；缺 CUDA 库会自动回退 CPU', tip: TIP.cuda },
      { id: 'srt_vad_filter', path: 'whisper.vad_filter', label: 'VAD 过滤', desc: '关闭后静音段也会送进模型', tip: TIP.transcribe },
    ],
  },
  {
    id: 'naming',
    label: '命名',
    items: [
      { id: 'include_date', path: 'naming.include_date', label: '时间前缀', desc: '文件名前缀加 20261008_2313', tip: TIP.date_prefix },
      { id: 'include_original', path: 'naming.include_original', label: '原名后缀', desc: '保留原文件名片段', tip: TIP.original_suffix },
      { id: 'enable_marker', path: 'naming.enable_marker', label: '处理标记', desc: '加 AI_RENAMED 标记，便于识别', tip: TIP.marker_switch },
      { id: 'enable_skip', path: 'naming.enable_skip', label: '跳过已处理', desc: '名字含标记或已写软水印的视频跳过；关闭则全部重跑', tip: TIP.skip },
    ],
  },
  {
    id: 'output',
    label: '输出',
    items: [
      { id: 'enable_nfo', path: 'output.nfo', label: '生成 NFO', desc: '供 Jellyfin / Kodi 识别', tip: TIP.nfo },
      { id: 'enable_srt', path: 'output.srt', label: '生成 SRT 字幕', desc: '生成同名 .srt 字幕文件', tip: TIP.srt },
      { id: 'write_metadata', path: 'output.metadata', label: 'ExifTool 写元数据', desc: '把标题写进视频内部属性', tip: TIP.exiftool },
      { id: 'remux_fragmented', path: 'output.remux_fragmented', label: '分片 MP4 自动重封装', desc: 'OBS 分片录像写不进元数据时，无损重封装后重写', tip: TIP.fmp4 },
      { id: 'move_failed', path: 'output.move_failed', label: '失败移入 _failed', desc: '失败文件集中存放，便于复查', tip: TIP.move_failed },
    ],
  },
  {
    id: 'service',
    label: '服务',
    items: [
      {
        id: 'lan',
        path: 'runtime.lan',
        label: '局域网访问',
        desc: '允许手机 / 同网设备访问（重启服务后生效）',
        tip: TIP.lan,
        // 注意：第二参 toast 由 createView 传入，模块作用域里取不到它
        onChange: (next, toastFn) => toastFn(
          next ? '已开启：重启服务后手机可用局域网 IP 访问' : '已关闭：重启服务后仅本机可访问',
          'info',
          3600,
        ),
      },
    ],
  },
  {
    id: 'safety',
    label: '安全',
    items: [
      { id: 'dry_run', path: 'output.dry_run', label: '预览模式', desc: '只演算不落盘', tip: TIP.dryrun },
    ],
  },
  {
    id: 'advanced',
    label: '高级',
    items: [
      {
        id: 'frame_hwaccel',
        label: 'GPU 硬解抽帧',
        desc: '1080p 建议关闭（纯 CPU 更稳）',
        tip: TIP.cuda,
        get: (cfg) => getPath(cfg, 'frames.hwaccel', 'none') !== 'none',
        set: (cfg, v) => setPath(cfg, 'frames.hwaccel', v ? 'cuda' : 'none'),
      },
      { id: 'auto_install_cuda', path: 'runtime.auto_install_cuda', label: '自动补 CUDA 库', desc: '缺库时只补装缺失的包到 libs/', tip: TIP.cuda },
      { id: 'verbose', path: 'runtime.verbose', label: '详细日志', desc: '输出 DEBUG 级详细日志', tip: TIP.verbose },
    ],
  },
];

/** AI 配置字段（严格对齐 §7.2 ai 结构） */
const AI_FIELDS = [
  { path: 'ai.provider', label: 'provider 后端', type: 'select', options: ['openai', 'gemini'], hint: 'openai 兼容 或 gemini', tip: TIP.provider },
  { path: 'ai.base_url', label: 'base_url 服务地址', type: 'text', hint: '如 http://127.0.0.1:8084/v1', tip: TIP.base_url },
  { path: 'ai.api_key', label: 'api_key 密钥', type: 'password', hint: '本地服务可随意填写', tip: TIP.api_key },
  { path: 'ai.model', label: 'model 模型名', type: 'text', hint: '必须支持看图（多模态）', tip: TIP.multimodal },
  { path: 'ai.timeout', label: 'timeout 超时(秒)', type: 'number', step: '1', min: '1', hint: '单次请求等待上限', tip: TIP.timeout },
  { path: 'ai.retry_times', label: 'retry_times 重试次数', type: 'number', step: '1', min: '0', hint: '失败后重试几次', tip: TIP.retry },
  { path: 'ai.max_tokens', label: 'max_tokens 最大输出', type: 'number', step: '1', min: '1', hint: '太小会截断 JSON', tip: TIP.max_tokens },
  { path: 'ai.temperature', label: 'temperature 温度', type: 'number', step: '0.1', min: '0', max: '2', hint: '越低越稳定', tip: TIP.temperature },
  { path: 'ai.top_p', label: 'top_p 采样', type: 'number', step: '0.1', min: '0', max: '1', hint: '越小越集中', tip: TIP.top_p },
  { path: 'ai.enforce_json_mode', label: 'enforce_json_mode 强制 JSON', type: 'bool', hint: '要求模型只返回 JSON', tip: TIP.json_mode },
];

/** 高级参数（frames / whisper / naming / runtime） */
const ADVANCED_FIELDS = [
  { path: 'naming.template', label: '命名模板 naming.template', type: 'text', hint: '如 {date}_{title}', tip: TIP.template },
  { path: 'naming.date_format', label: '日期格式 naming.date_format', type: 'text', hint: '如 %Y%m%d_%H%M', tip: TIP.date_format },
  { path: 'naming.marker', label: '处理标记 naming.marker', type: 'text', hint: '如 AI_RENAMED', tip: TIP.marker },
  { path: 'frames.max_keyframes', label: '关键帧上限 frames.max_keyframes', type: 'number', step: '1', min: '1', hint: '挑几张图给 AI 看', tip: TIP.keyframe },
  { path: 'frames.max_side', label: '帧最长边 frames.max_side', type: 'number', step: '1', min: '64', hint: '截图缩放后的最长边像素', tip: TIP.max_side },
  { path: 'frames.workers', label: '抽帧并发 frames.workers', type: 'number', step: '1', min: '1', hint: '同时跑几个 ffmpeg', tip: TIP.frames_workers },
  { path: 'whisper.model', label: 'Whisper 模型', type: 'text', hint: '如 large-v3-turbo', tip: TIP.whisper },
  { path: 'whisper.device', label: 'Whisper 设备 whisper.device', type: 'select', options: ['auto', 'cuda', 'cpu'], hint: 'auto = 自动检测', tip: TIP.device },
  {
    path: 'whisper.language',
    label: '转写 / 字幕语言',
    type: 'select',
    options: ['auto', 'zh', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'ru', 'pt', 'it', 'ar'],
    hint: 'auto = 自动检测；同时决定 .srt 字幕的语言',
    tip: TIP.language,
  },
  { path: 'whisper.compute_type', label: '计算精度 whisper.compute_type', type: 'text', hint: '如 int8_float16 / float16', tip: TIP.compute_type },
  { path: 'whisper.workers', label: '转写并发 whisper.workers', type: 'number', step: '1', min: '1', hint: 'GPU 场景建议 1', tip: TIP.whisper_workers },
  { path: 'runtime.ai_workers', label: 'AI 并发 runtime.ai_workers', type: 'number', step: '1', min: '1', hint: '同时问 AI 几路', tip: TIP.ai_workers },
  { path: 'runtime.log_file', label: '日志文件 runtime.log_file', type: 'text', hint: '如 logs/run.log', tip: TIP.log_file },
];

/** 术语速查（页面级「这是什么」） */
const GLOSSARY = [
  ['ffmpeg', TIP.ffmpeg],
  ['ffprobe', TIP.ffprobe],
  ['exiftool', TIP.exiftool],
  ['Whisper', TIP.whisper],
  ['关键帧', TIP.keyframe],
  ['转写', TIP.transcribe],
  ['多模态模型', TIP.multimodal],
  ['NFO', TIP.nfo],
  ['SRT', TIP.srt],
  ['CUDA', TIP.cuda],
  ['递归', TIP.recursive],
  ['预览模式', TIP.dryrun],
  ['分片 MP4', TIP.fmp4],
  ['局域网访问', TIP.lan],
  ['base_url', TIP.base_url],
  ['max_tokens', TIP.max_tokens],
  ['temperature', TIP.temperature],
  ['top_p', TIP.top_p],
  ['enforce_json_mode', TIP.json_mode],
  ['命名模板', TIP.template],
  ['日期格式', TIP.date_format],
  ['处理标记', TIP.marker],
  ['跳过已处理', TIP.skip],
  ['计算精度', TIP.compute_type],
  ['并发', TIP.ai_workers],
];

export function createView(ctx) {
  const { store, apiClient, toast, persistConfig } = ctx;
  let container = null;
  const unsubs = [];
  const switchRefs = {}; // id -> switch 元素
  const rowRefs = {}; // id -> switch-row 容器（整行显隐用）
  const fieldRefs = []; // { field, input }
  const refs = { saveHint: null };

  function getVal(item, cfg) {
    return item.get ? !!item.get(cfg) : !!getPath(cfg, item.path, false);
  }

  function setVal(item, cfg, value) {
    return item.set ? item.set(cfg, value) : setPath(cfg, item.path, value);
  }

  function showSaveHint() {
    if (!refs.saveHint) return;
    refs.saveHint.classList.add('is-on');
    clearTimeout(refs.saveHint._t);
    refs.saveHint._t = setTimeout(() => refs.saveHint.classList.remove('is-on'), 1400);
  }

  function commit(newCfg, silent) {
    store.set({ config: newCfg, configDirty: true });
    persistConfig(newCfg);
    showSaveHint();
    if (!silent) {
      /* 保存提示已足够，避免频繁 toast */
    }
  }

  // ---------- 开关行 ----------
  function buildSwitchRow(item) {
    const sw = h('button', {
      class: 'switch',
      type: 'button',
      role: 'switch',
      'aria-checked': 'false',
      'aria-label': item.label,
    }, [h('span', { class: 'switch-knob' })]);
    sw.addEventListener('click', () => {
      const cfg = store.state.config;
      const next = !getVal(item, cfg);
      commit(setVal(item, cfg, next));
      if (typeof item.onChange === 'function') item.onChange(next, toast);
    });
    switchRefs[item.id] = sw;

    const labelRow = h('div', { class: 'switch-label' }, [item.label]);
    labelRow.appendChild(h('span', {
      class: 'tip',
      tabindex: '0',
      'data-tip': item.tip || item.desc || item.label,
      text: '?',
    }));

    const row = h('div', { class: 'switch-row' }, [
      h('div', { class: 'switch-meta' }, [labelRow, h('div', { class: 'switch-desc', text: item.desc })]),
      sw,
    ]);
    rowRefs[item.id] = row;
    return row;
  }

  function buildSwitchGroups() {
    const wrap = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '14px' } });
    for (const group of SWITCH_GROUPS) {
      const grid = h('div', { class: 'switch-grid' });
      for (const item of group.items) grid.appendChild(buildSwitchRow(item));
      wrap.appendChild(
        h('div', { class: 'settings-group' }, [
          h('div', { class: 'settings-group-title' }, [h('span', { class: 'dot' }), group.label]),
          grid,
        ])
      );
    }
    return wrap;
  }

  // ---------- 表单字段 ----------
  function buildField(field) {
    const value = getPath(store.state.config, field.path, '');

    if (field.type === 'bool') {
      const sw = h('button', {
        class: 'switch',
        type: 'button',
        role: 'switch',
        'aria-checked': 'false',
        'aria-label': field.label,
      }, [h('span', { class: 'switch-knob' })]);
      const labelRow = h('div', { class: 'switch-label' }, [field.label]);
      labelRow.appendChild(h('span', {
        class: 'tip',
        tabindex: '0',
        'data-tip': field.tip || field.hint || field.label,
        text: '?',
      }));
      const desc = field.hint || '';
      sw.addEventListener('click', () => {
        const cfg = store.state.config;
        const next = !getPath(cfg, field.path, false);
        commit(setPath(cfg, field.path, next));
      });
      fieldRefs.push({ field, input: sw, kind: 'bool' });
      return h('div', { class: 'switch-row' }, [
        h('div', { class: 'switch-meta' }, [labelRow, desc ? h('div', { class: 'switch-desc', text: desc }) : null]),
        sw,
      ]);
    }

    let input;
    if (field.type === 'select') {
      input = h('select', { class: 'field-select' });
      for (const opt of field.options || []) {
        input.appendChild(h('option', { value: opt, text: opt }));
      }
      input.value = value;
    } else {
      input = h('input', {
        class: 'field-input',
        type: field.type,
        value: value === undefined || value === null ? '' : value,
        step: field.step,
        min: field.min,
        max: field.max,
      });
    }

    const onChange = () => {
      const raw = input.value;
      const next = field.type === 'number' ? (raw === '' ? 0 : Number(raw)) : raw;
      commit(setPath(store.state.config, field.path, next));
    };
    input.addEventListener('change', onChange);

    const labelRow = h('div', { class: 'field-label' }, [field.label]);
    labelRow.appendChild(h('span', {
      class: 'tip',
      tabindex: '0',
      'data-tip': field.tip || field.hint || field.label,
      text: '?',
    }));

    fieldRefs.push({ field, input, kind: 'input' });
    return h('div', { class: 'field' }, [
      labelRow,
      input,
      field.hint ? h('div', { class: 'field-hint', text: field.hint }) : null,
    ]);
  }

  function buildFormPanel(title, hint, fields) {
    const grid = h('div', { class: 'form-grid' });
    for (const f of fields) grid.appendChild(buildField(f));
    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: title }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: hint }),
      ]),
      grid,
    ]);
  }

  // ---------- AI 连接测试 ----------
  function kv(label, value) {
    return h('div', { class: 'kv' }, [
      h('span', { class: 'kv-k', text: label }),
      h('span', { class: 'kv-v', text: value == null || value === '' ? '—' : String(value) }),
    ]);
  }

  function renderAITest(r) {
    const out = refs.aiTestOut;
    if (!out) return;
    const ok = !!(r && r.ok);
    out.className = `ai-test-result ${ok ? 'is-ok' : 'is-err'}`;
    out.textContent = '';

    out.appendChild(h('div', { class: 'ai-test-head', text: ok ? '连接正常' : '连接失败' }));
    const grid = h('div', { class: 'kv-grid' });
    grid.appendChild(kv('可达', r.reachable ? '是' : '否'));
    if (r.latencyMs) grid.appendChild(kv('延迟', `${r.latencyMs} ms`));
    grid.appendChild(kv('识别模型', r.identified || '—'));
    if (r.servedModel) grid.appendChild(kv('响应 model', r.servedModel));
    grid.appendChild(kv('可用模型数', (r.models && r.models.length) || 0));
    if (r.modelExists === true) grid.appendChild(kv('配置模型', '在可用列表中'));
    else if (r.modelExists === false) grid.appendChild(kv('配置模型', '不在可用列表'));
    out.appendChild(grid);

    if (r.reply) out.appendChild(h('div', { class: 'ai-test-reply', text: `模型自述：${r.reply}` }));
    if (!ok && r.error) out.appendChild(h('div', { class: 'ai-test-err', text: r.error }));

    if (r.models && r.models.length) {
      const wrap = h('div', { class: 'model-chips' });
      const cur = getPath(store.state.config, 'ai.model', '');
      for (const m of r.models.slice(0, 40)) {
        const chip = h('button', {
          class: 'model-chip' + (String(m).toLowerCase() === String(cur).toLowerCase() ? ' is-active' : ''),
          type: 'button',
          text: m,
        });
        chip.addEventListener('click', () => {
          commit(setPath(store.state.config, 'ai.model', m));
          toast(`已切换模型：${m}`, 'ok');
        });
        wrap.appendChild(chip);
      }
      out.appendChild(h('div', { class: 'ai-test-hint', text: '点击模型可直接切换 ai.model：' }));
      out.appendChild(wrap);
    }
  }

  function buildAITestPanel() {
    const btn = h('button', { class: 'btn btn-primary', type: 'button', text: '测试连接' });
    const out = h('div', { class: 'ai-test-result', hidden: true });
    refs.aiTestOut = out;

    btn.addEventListener('click', async () => {
      const ai = (store.state.config && store.state.config.ai) || {};
      const old = btn.textContent;
      btn.disabled = true;
      btn.textContent = '测试中…';
      out.hidden = false;
      out.className = 'ai-test-result is-pending';
      out.textContent = '正在连接后端并识别模型…';
      try {
        const r = await apiClient.testAI({
          provider: ai.provider,
          base_url: ai.base_url,
          api_key: ai.api_key,
          model: ai.model,
          timeout: ai.timeout,
        });
        renderAITest(r);
        toast(r.ok ? 'AI 连接正常' : 'AI 连接失败', r.ok ? 'ok' : 'err', 3200);
      } catch (err) {
        out.className = 'ai-test-result is-err';
        out.textContent = `测试失败：${(err && (err.message || err.detail)) || '未知错误'}`;
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    });

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: 'AI 连接测试' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '验证后端可达并识别模型' }),
      ]),
      h('div', { class: 'control-row' }, [btn]),
      out,
    ]);
  }

  // ---------- 转写运行环境（GPU / CUDA 库） ----------
  function renderEnv(s) {
    const out = refs.envOut;
    if (!out) return;
    out.textContent = '';
    const libs = s.cuda_libs || {};
    const grid = h('div', { class: 'kv-grid' });
    const backendLabel = s.backend === 'api' ? '仅自定义 API'
      : s.backend === 'local' ? '仅本地 Whisper'
      : `自动（优先 ${s.prefer_api ? 'API' : '本地'}）`;
    grid.appendChild(kv('转写后端', backendLabel));
    grid.appendChild(kv('实际生效', s.effective_backend === 'api' ? 'API' : '本地 Whisper'));
    grid.appendChild(kv('faster-whisper', s.faster_whisper ? '已安装' : '未安装'));

    if (s.backend === 'api' || (s.backend === 'auto' && s.prefer_api && s.api_ready)) {
      const api = s.api || {};
      grid.appendChild(kv('API 协议', api.protocol === 'gradio' ? 'Gradio（Qwen3-ASR）' : 'OpenAI 兼容'));
      grid.appendChild(kv('服务地址', api.base_url || '未填写'));
      out.appendChild(grid);
      if (s.backend === 'api' && !api.base_url) {
        out.appendChild(h('div', {
          class: 'env-warn',
          text: '已选择「仅自定义 API」但还没填服务地址，转写会被跳过。填好地址后点「测试连接」。',
        }));
      }
      if (s.backend === 'auto') {
        out.appendChild(h('div', {
          class: 'field-hint',
          text: 'API 不可用时会自动回退本地 Whisper（连续失败 2 个文件后本批固定本地）。',
        }));
      }
      return;
    }

    grid.appendChild(kv('模型', s.model || '—'));
    grid.appendChild(kv('模型状态', s.model_ready ? '已就绪（离线加载）' : '未下载 / 不完整'));
    grid.appendChild(kv('GPU 开关', s.use_gpu ? '开启' : '关闭'));
    grid.appendChild(kv('CUDA 设备', s.cuda_available ? '可用' : '不可用'));
    grid.appendChild(kv('CUDA 运行库', libs.present ? (libs.reused ? '复用系统已装' : '已就位') : '缺失'));
    out.appendChild(grid);

    if (s.model_path) out.appendChild(h('div', { class: 'env-log', text: `模型目录: ${s.model_path}` }));
    // 续传进度：把已下载字节摆出来，弱网下用户能看到「确实在前进」
    const dl = s.download;
    if (!s.model_ready && dl && dl.modelBinBytes > 0) {
      const mib = (dl.modelBinBytes / 1048576).toFixed(0);
      const total = 1542;  // large-v3-turbo model.bin ≈ 1.5GiB
      const pct = Math.min(100, (dl.modelBinBytes / (total * 1048576)) * 100).toFixed(1);
      out.appendChild(h('div', {
        class: 'env-log',
        text: `已下载 model.bin：${mib}MB / 约 ${total}MB（${pct}%）——续传会从这里接着下`,
      }));
    }
    if (libs.cublas) out.appendChild(h('div', { class: 'env-log', text: `cublas: ${libs.cublas}` }));
    if (s.cuda_reason) out.appendChild(h('div', { class: 'env-log', text: `说明: ${s.cuda_reason}` }));
    if (!s.model_ready) {
      const usingApi = s.backend === 'api' || (s.backend === 'auto' && s.prefer_api && s.api_ready);
      out.appendChild(h('div', {
        class: 'env-warn',
        text: usingApi
          ? '本地模型未就绪：不影响转写（当前走 API）。需要离线转写时再点下方「下载 / 续传模型」把 large-v3-turbo（约 1.6GB）下完，弱网会反复续传。'
          : '模型未就绪：点下方「下载 / 续传模型」把 large-v3-turbo（约 1.6GB）下完；'
            + '弱网会反复续传，进度会实时显示。也可以改用「自定义 API」后端完全跳过下载。',
      }));
    }
    if (!libs.present) {
      out.appendChild(h('div', {
        class: 'env-warn',
        text: '缺少 cublas64_12.dll 会让 GPU 转写挂起；点下方按钮自动补装到 libs/。'
          + '（系统 Python 里已装过 nvidia-cublas-cu12 时会自动复用，不会重复下载）',
      }));
    }
  }

  async function refreshEnv(silent) {
    const out = refs.envOut;
    if (!out) return;
    if (!silent) out.textContent = '读取中…';
    try {
      const s = await apiClient.whisperStatus();
      renderEnv(s);
    } catch (err) {
      out.textContent = '无法获取环境状态（后端未连接？）';
    }
  }

  function buildEnvPanel() {
    const out = h('div', { class: 'env-status' });
    refs.envOut = out;

    const refreshBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '刷新状态' });
    refreshBtn.addEventListener('click', () => refreshEnv(false));

    const installBtn = h('button', { class: 'btn', type: 'button', text: '安装 Whisper 依赖' });
    installBtn.addEventListener('click', async () => {
      const old = installBtn.textContent;
      installBtn.disabled = true;
      installBtn.textContent = '安装中…';
      out.textContent = '正在安装 faster-whisper 到 libs/（使用上方 pip 镜像源）…';
      try {
        const r = await apiClient.installWhisper();
        for (const line of (r.logs || [])) out.appendChild(h('div', { class: 'env-log', text: line }));
        toast(r.ok ? 'Whisper 依赖已就绪' : '安装未完成，请查看状态', r.ok ? 'ok' : 'warn', 4000);
        await refreshEnv(true);
      } catch (err) {
        out.textContent = `安装失败：${(err && (err.message || err.detail)) || '未知错误'}`;
        toast('Whisper 依赖安装失败', 'err');
      } finally {
        installBtn.disabled = false;
        installBtn.textContent = old;
      }
    });

    const fixBtn = h('button', { class: 'btn', type: 'button', text: '下载 / 修复 CUDA 库' });
    fixBtn.addEventListener('click', async () => {
      const old = fixBtn.textContent;
      fixBtn.disabled = true;
      fixBtn.textContent = '正在检查…';
      out.textContent = '正在检查 CUDA 运行库（系统已装过的会直接复用，不重复下载）…';
      try {
        const r = await apiClient.setupWhisper();
        for (const line of (r.logs || [])) out.appendChild(h('div', { class: 'env-log', text: line }));
        toast(r.ok ? 'CUDA 库已就位' : '补装未完成，请查看状态', r.ok ? 'ok' : 'warn', 4000);
        await refreshEnv(true);
      } catch (err) {
        out.textContent = `补装失败：${(err && (err.message || err.detail)) || '未知错误'}`;
        toast('CUDA 库补装失败', 'err');
      } finally {
        fixBtn.disabled = false;
        fixBtn.textContent = old;
      }
    });

    // 下载 / 续传 Whisper 模型：弱网会反复续传，进度条与日志实时更新
    const modelBtn = h('button', { class: 'btn btn-primary', type: 'button', text: '下载 / 续传模型' });
    modelBtn.addEventListener('click', async () => {
      const old = modelBtn.textContent;
      modelBtn.disabled = true;
      modelBtn.textContent = '下载中…（可随时关闭，稍后接着下）';
      const modelOut = h('div', { class: 'env-status' });
      modelOut.appendChild(h('div', { class: 'env-log', text: '开始下载模型…' }));
      out.appendChild(modelOut);
      try {
        const r = await apiClient.downloadModel({ model: getPath(store.state.config, 'whisper.model', '') });
        modelOut.textContent = '';
        for (const line of (r.logs || [])) modelOut.appendChild(h('div', { class: 'env-log', text: line }));
        if (!r.ok && r.error) modelOut.appendChild(h('div', { class: 'env-warn', text: r.error }));
        toast(r.ok ? '模型已就绪，可离线加载' : '模型未下载完成（再点一次会续传）', r.ok ? 'ok' : 'warn', 5000);
        await refreshEnv(true);
      } catch (err) {
        modelOut.textContent = `模型下载失败：${(err && (err.message || err.detail)) || '未知错误'}`;
        toast('模型下载失败', 'err');
      } finally {
        modelBtn.disabled = false;
        modelBtn.textContent = old;
      }
    });

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '转写运行环境' }),
        h('span', { class: 'spacer' }),
        refreshBtn,
      ]),
      out,
      h('div', { class: 'control-row' }, [modelBtn, installBtn, fixBtn]),
    ]);
  }

  // ---------- 访问方式（本机 / 局域网） ----------
  function buildAccessPanel() {
    const out = h('div', { class: 'env-status' });

    async function load() {
      out.textContent = '读取中…';
      try {
        const s = await apiClient.serverInfo();
        out.textContent = '';
        const grid = h('div', { class: 'kv-grid' });
        grid.appendChild(kv('监听地址', `${s.host}:${s.port}`));
        grid.appendChild(kv('当前模式', s.lan ? '局域网可访问' : '仅本机'));
        grid.appendChild(kv('配置开关', s.config_lan ? '开启' : '关闭'));
        out.appendChild(grid);

        out.appendChild(h('div', { class: 'env-log', text: `本机：${s.local_url}` }));
        for (const url of s.lan_urls || []) {
          const row = h('div', { class: 'env-log' }, ['局域网：']);
          row.appendChild(h('a', { href: url, target: '_blank', rel: 'noreferrer', text: url }));
          out.appendChild(row);
        }
        if (s.restart_required) {
          out.appendChild(h('div', {
            class: 'env-warn',
            text: '⚠️ 配置开关与当前监听不一致：重启服务后才会生效。',
          }));
        }
      } catch (err) {
        out.textContent = '无法获取服务信息（后端未连接？）';
      }
    }

    const refreshBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '刷新' });
    refreshBtn.addEventListener('click', () => load());
    refs.accessReload = load;

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '访问方式' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '开关改动需重启服务' }),
        refreshBtn,
      ]),
      out,
    ]);
  }

  // ---------- 下载镜像（国内加速） ----------
  const MIRROR_PRESETS = {
    pip: {
      official: 'https://pypi.org/simple',
      tsinghua: 'https://pypi.tuna.tsinghua.edu.cn/simple',
      aliyun: 'https://mirrors.aliyun.com/pypi/simple',
      ustc: 'https://pypi.mirrors.ustc.edu.cn/simple',
      tencent: 'https://mirrors.cloud.tencent.com/pypi/simple',
    },
    hf: {
      official: 'https://huggingface.co',
      'hf-mirror': 'https://hf-mirror.com',
    },
  };

  function buildMirrorRow(label, cfgPath, presets, hint, tip) {
    const input = h('input', {
      class: 'field-input',
      type: 'text',
      value: getPath(store.state.config, cfgPath, '') || '',
      placeholder: '留空 = 官方源',
    });
    input.addEventListener('change', () => {
      commit(setPath(store.state.config, cfgPath, input.value.trim()));
    });
    fieldRefs.push({ field: { path: cfgPath }, input, kind: 'input' });

    const chips = h('div', { class: 'model-chips' });
    for (const name of Object.keys(presets)) {
      const url = presets[name];
      const chip = h('button', {
        class: 'model-chip' + (input.value === url ? ' is-active' : ''),
        type: 'button',
        text: name,
        title: url,
      });
      chip.addEventListener('click', () => {
        input.value = url;
        commit(setPath(store.state.config, cfgPath, url));
        toast(`已设为 ${name}`, 'ok');
      });
      chips.appendChild(chip);
    }
    const reset = h('button', { class: 'model-chip', type: 'button', text: '清空', title: '清空并使用官方源' });
    reset.addEventListener('click', () => {
      input.value = '';
      commit(setPath(store.state.config, cfgPath, ''));
      toast('已恢复官方源', 'info');
    });
    chips.appendChild(reset);

    return h('div', { class: 'field' }, [
      h('div', { class: 'field-label' }, [label, h('span', {
        class: 'tip', tabindex: '0', 'data-tip': tip || hint, text: '?',
      })]),
      input,
      h('div', { class: 'field-hint', text: hint }),
      chips,
    ]);
  }

  // ---------- 自定义转写 API ----------
  const ASR_PROTOCOLS = [
    { value: 'openai', label: 'OpenAI 兼容（/v1/audio/transcriptions）' },
    { value: 'gradio', label: 'Gradio 服务（Qwen3-ASR Pro）' },
  ];

  function buildAsrField(field) {
    const value = getPath(store.state.config, field.path, '');
    let input;
    if (field.type === 'select') {
      input = h('select', { class: 'field-select' });
      for (const opt of field.options || []) {
        const val = typeof opt === 'string' ? opt : opt.value;
        const label = typeof opt === 'string' ? opt : opt.label;
        input.appendChild(h('option', { value: val, text: label }));
      }
      input.value = value;
    } else if (field.type === 'bool') {
      input = h('button', {
        class: 'switch', type: 'button', role: 'switch',
        'aria-checked': 'false', 'aria-label': field.label,
      }, [h('span', { class: 'switch-knob' })]);
      const on = !!value;
      input.classList.toggle('is-on', on);
      input.setAttribute('aria-checked', on ? 'true' : 'false');
      input.addEventListener('click', () => {
        const next = !getPath(store.state.config, field.path, false);
        input.classList.toggle('is-on', next);
        input.setAttribute('aria-checked', next ? 'true' : 'false');
        commit(setPath(store.state.config, field.path, next));
      });
      fieldRefs.push({ field, input, kind: 'bool' });
      return h('div', { class: 'switch-row' }, [
        h('div', { class: 'switch-meta' }, [
          h('div', { class: 'switch-label' }, [field.label, h('span', {
            class: 'tip', tabindex: '0', 'data-tip': field.tip || field.hint, text: '?',
          })]),
          field.hint ? h('div', { class: 'switch-desc', text: field.hint }) : null,
        ]),
        input,
      ]);
    } else {
      input = h('input', {
        class: 'field-input',
        type: field.type || 'text',
        value: value === undefined || value === null ? '' : value,
        step: field.step,
        min: field.min,
        placeholder: field.placeholder || '',
      });
      input.addEventListener('change', () => {
        const raw = input.value;
        const next = field.type === 'number' ? (raw === '' ? 0 : Number(raw)) : raw.trim();
        commit(setPath(store.state.config, field.path, next));
      });
    }

    if (input.tagName !== 'BUTTON') {
      fieldRefs.push({ field, input, kind: 'input' });
    }
    return h('div', { class: 'field' }, [
      h('div', { class: 'field-label' }, [field.label, h('span', {
        class: 'tip', tabindex: '0', 'data-tip': field.tip || field.hint, text: '?',
      })]),
      input,
      field.hint ? h('div', { class: 'field-hint', text: field.hint }) : null,
    ]);
  }

  function buildAsrPanel() {
    const backendSel = h('select', { class: 'field-select' });
    for (const opt of [
      { value: 'auto', label: '自动（优先 API，失败回退本地）' },
      { value: 'local', label: '仅本地 faster-whisper（需下载模型）' },
      { value: 'api', label: '仅自定义 HTTP API（不回退）' },
    ]) {
      backendSel.appendChild(h('option', { value: opt.value, text: opt.label }));
    }
    backendSel.value = getPath(store.state.config, 'whisper.backend', 'auto');
    backendSel.addEventListener('change', () => {
      commit(setPath(store.state.config, 'whisper.backend', backendSel.value));
      renderAsrBody();
    });
    fieldRefs.push({ field: { path: 'whisper.backend' }, input: backendSel, kind: 'input' });

    // 「优先使用 API」开关已提升到上方「转写」开关分组（SWITCH_GROUPS.prefer_api），
    // 这里只保留 backend 下拉与 API 参数区，避免同一配置两个开关。

    // 「本地模型缺失时允许联网下载」开关（默认关：弱网会把整批任务卡死）
    const dlBtn = h('button', {
      class: 'switch', type: 'button', role: 'switch',
      'aria-checked': 'false', 'aria-label': '本地模型缺失时自动联网下载',
    }, [h('span', { class: 'switch-knob' })]);
    const dlWrap = h('div', { class: 'switch-row' }, [
      h('div', { class: 'switch-meta' }, [
        h('div', { class: 'switch-label' }, ['本地模型缺失时自动联网下载']),
        h('div', { class: 'switch-desc', text: '关闭（推荐）：本地模型没下完就直接跳过转写，不再花几分钟去拉 1.6GB；模型请在下方「转写运行环境 → 下载 / 续传模型」里显式下载' }),
      ]),
      dlBtn,
    ]);
    dlBtn.addEventListener('click', () => {
      const next = getPath(store.state.config, 'whisper.allow_download', false) !== true;
      dlBtn.classList.toggle('is-on', next);
      dlBtn.setAttribute('aria-checked', next ? 'true' : 'false');
      commit(setPath(store.state.config, 'whisper.allow_download', next));
    });
    fieldRefs.push({ field: { path: 'whisper.allow_download' }, input: dlBtn, kind: 'bool' });

    const body = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '14px' } });

    function renderAsrBody() {
      asrRerender = renderAsrBody;
      body.textContent = '';
      const backend = getPath(store.state.config, 'whisper.backend', 'auto');
      const prefer = getPath(store.state.config, 'whisper.prefer_api', true) !== false;
      const allowDl = getPath(store.state.config, 'whisper.allow_download', false) === true;
      dlWrap.style.display = backend === 'api' ? 'none' : '';
      dlBtn.classList.toggle('is-on', allowDl);
      dlBtn.setAttribute('aria-checked', allowDl ? 'true' : 'false');

      // API 参数区与「测试连接」按钮常驻显示：
      // 用户随时可以填地址、先测通再打开「优先使用 API 接口 ASR」开关，
      // 不能因为当前开关关着就把入口藏起来（这正是之前「找不到测试连接」的原因）。
      let tipText;
      let tipWarn = false;
      if (backend === 'api') {
        tipText = '只用下面的 API：不可用时转写会被跳过（不回退本地）。';
      } else if (backend === 'local') {
        tipText = '当前「转写后端」= 仅本地 Whisper，下面的 API 地址不会用于转写；可先点「测试连接」验证地址，再把后端改成 auto 或 api。';
        tipWarn = true;
      } else if (prefer) {
        tipText = '先试下面的 API；连不上或连续失败 2 个文件后，本批自动固定用本地 Whisper。';
      } else {
        tipText = '「优先使用 API 接口 ASR」开关已关闭：当前直接用本地 Whisper。填好地址点「测试连接」通过后，打开上方开关即改走 API。';
        tipWarn = true;
      }
      body.appendChild(h('div', {
        class: 'field-hint' + (tipWarn ? ' warn-text' : ''),
        text: tipText,
      }));

      const grid = h('div', { class: 'form-grid' });
      const fields = [
        {
          path: 'whisper.api.protocol', label: 'API 协议 whisper.api.protocol',
          type: 'select', options: ASR_PROTOCOLS, tip: TIP.asr_protocol,
          hint: 'openai = 通用兼容接口；gradio = Qwen3-ASR Pro',
        },
        {
          path: 'whisper.api.base_url', label: '服务地址 whisper.api.base_url', type: 'text',
          placeholder: 'http://127.0.0.1:7867', tip: TIP.asr_base_url,
          hint: 'OpenAI 协议填到 /v1 结尾；Gradio 协议填根路径',
        },
        {
          path: 'whisper.api.api_key', label: 'api_key whisper.api.api_key', type: 'password',
          hint: '本地服务可留空；云端服务填真实密钥', tip: TIP.api_key,
        },
        {
          path: 'whisper.api.model', label: 'model whisper.api.model', type: 'text',
          placeholder: 'whisper-1', hint: 'OpenAI 协议的模型名；Gradio 协议可留空', tip: TIP.asr_model,
        },
        {
          path: 'whisper.api.timeout', label: '超时(秒) whisper.api.timeout', type: 'number',
          step: '30', min: '30', hint: '长音频建议 900 以上', tip: TIP.asr_timeout,
        },
        {
          path: 'whisper.api.prompt', label: '提示词 whisper.api.prompt', type: 'text',
          hint: '给服务的上下文提示（OpenAI 协议）', tip: TIP.asr_prompt,
        },
        {
          path: 'whisper.api.max_chars', label: '单行字数 whisper.api.max_chars', type: 'number',
          step: '5', min: '5', hint: '客户端聚合字幕行时每行最多几个字（Gradio）', tip: TIP.asr_maxchars,
        },
        {
          path: 'whisper.api.hotwords', label: '热词 whisper.api.hotwords', type: 'text',
          hint: '专业术语 / 人名，提升识别率（Gradio）', tip: TIP.asr_hotwords,
        },
        {
          path: 'whisper.api.diarize', label: '角色识别', type: 'bool',
          hint: '区分说话人（供 AI 分析），字幕不带角色标签（Gradio）', tip: TIP.asr_diarize,
        },
        {
          path: 'whisper.api.punc', label: '标点断句', type: 'bool',
          hint: '自动加标点并据此断行（Gradio）', tip: TIP.asr_punc,
        },
      ];
      for (const f of fields) grid.appendChild(buildAsrField(f));
      body.appendChild(grid);
      body.appendChild(buildAsrTestPanel());
    }

    renderAsrBody();

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '转写后端' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '优先 API，不可用时自动回退本地' }),
      ]),
      h('div', { class: 'form-grid' }, [
        h('div', { class: 'field' }, [
          h('div', { class: 'field-label' }, ['转写后端 whisper.backend', h('span', {
            class: 'tip', tabindex: '0', 'data-tip': TIP.asr_backend, text: '?',
          })]),
          backendSel,
          h('div', { class: 'field-hint', text: 'auto = 优先 API；local = 仅本机；api = 仅远程' }),
        ]),
        h('div', { class: 'field' }, [
          h('div', { class: 'field-label' }, ['优先开关位置', h('span', {
            class: 'tip', tabindex: '0', 'data-tip': TIP.asr_prefer_api, text: '?',
          })]),
          h('div', { class: 'field-hint', text: '「优先使用 API 接口 ASR」开关在上方「转写」分组里：backend=auto 时它决定先试 API 还是直接本地；backend=local/api 时由后端下拉直接决定。' }),
        ]),
      ]),
      dlWrap,
      body,
    ]);
  }

  function buildAsrTestPanel() {
    const btn = h('button', { class: 'btn btn-primary', type: 'button', text: '测试连接' });
    const deepBtn = h('button', {
      class: 'btn', type: 'button', text: '真实转写测试（3~10s 测试音频）',
      title: '用 ffmpeg 合成一段测试语音，真跑一次转写，验证模型真的能出字',
    });
    const out = h('div', { class: 'ai-test-result', hidden: true });

    async function runTest(deep) {
      const api = getPath(store.state.config, 'whisper.api', {}) || {};
      btn.disabled = true;
      deepBtn.disabled = true;
      out.hidden = false;
      out.className = 'ai-test-result is-pending';
      out.textContent = deep
        ? '正在合成测试语音并真实转写（约 5~20 秒）…'
        : '正在探测转写服务…';
      try {
        const r = await apiClient.testASR({
          protocol: api.protocol,
          base_url: api.base_url,
          api_key: api.api_key,
          model: api.model,
          timeout: api.timeout,
          deep: !!deep,
        });
        out.textContent = '';
        out.className = `ai-test-result ${r.ok ? 'is-ok' : 'is-err'}`;
        out.appendChild(h('div', {
          class: 'ai-test-head',
          text: r.ok
            ? (deep ? '真实转写成功' : '转写服务可用')
            : (deep ? '真实转写失败' : '转写服务不可用'),
        }));
        const grid = h('div', { class: 'kv-grid' });
        grid.appendChild(kv('可达', r.reachable ? '是' : '否'));
        if (r.latencyMs) grid.appendChild(kv('探测延迟', `${r.latencyMs} ms`));
        grid.appendChild(kv('协议', r.protocol));
        const d = r.deep;
        if (d) {
          grid.appendChild(kv('真实转写', d.ok ? '成功' : '失败'));
          if (d.elapsedMs) grid.appendChild(kv('转写耗时', `${(d.elapsedMs / 1000).toFixed(1)} s`));
          if (d.segments) grid.appendChild(kv('字幕条数', String(d.segments)));
        }
        out.appendChild(grid);
        if (r.detail) out.appendChild(h('div', { class: 'env-log', text: r.detail }));
        if (d && d.text) out.appendChild(h('div', { class: 'env-log', text: `识别结果：${d.text}` }));
        if (!r.ok && r.error) out.appendChild(h('div', { class: 'ai-test-err', text: r.error }));
        toast(r.ok ? (deep ? '真实转写成功' : '转写 API 正常') : '转写 API 不可用',
              r.ok ? 'ok' : 'err', 3600);
      } catch (err) {
        out.className = 'ai-test-result is-err';
        out.textContent = `测试失败：${(err && (err.message || err.detail)) || '未知错误'}`;
      } finally {
        btn.disabled = false;
        deepBtn.disabled = false;
      }
    }

    btn.addEventListener('click', () => runTest(false));
    deepBtn.addEventListener('click', () => runTest(true));

    return h('div', { style: { marginTop: '4px' } }, [
      h('div', { class: 'control-row' }, [btn, deepBtn]),
      h('div', {
        class: 'field-hint',
        text: '「测试连接」只探协议层（秒回）；「真实转写测试」会合成一段语音真跑一次，能证明模型真能出字。',
      }),
      out,
    ]);
  }

  function buildMirrorPanel() {
    const grid = h('div', { class: 'form-grid' });
    grid.appendChild(buildMirrorRow(
      'pip 源 runtime.pip_index', 'runtime.pip_index', MIRROR_PRESETS.pip,
      '依赖下载（pip install）使用的镜像源，可自定义填任意地址',
      TIP.pip_mirror,
    ));
    grid.appendChild(buildMirrorRow(
      'HuggingFace 端点 runtime.hf_endpoint', 'runtime.hf_endpoint', MIRROR_PRESETS.hf,
      '模型下载使用的端点（如 hf-mirror.com），可自定义',
      TIP.hf_mirror,
    ));
    // modelscope 不是 HF 兼容端点（只有网页接口），填进去会让模型加载报 JSON 解析错
    const hfVal = String(getPath(store.state.config, 'runtime.hf_endpoint', '') || '');
    if (/modelscope\.(cn|com)/i.test(hfVal)) {
      grid.appendChild(h('div', {
        class: 'field-hint warn-text',
        text: '⚠️ modelscope.cn 不是 HuggingFace 兼容端点，会导致 Whisper 模型加载失败（JSON 解析错误）。请改用 hf-mirror.com 或留空。',
      }));
    }
    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '下载镜像（国内加速）' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '依赖与模型下载走这里设置的源' }),
      ]),
      grid,
    ]);
  }

  // ---------- 提示词（可自定义） ----------
  function buildPromptArea(label, cfgPath, hint, tip) {
    const ta = h('textarea', { class: 'field-input prompt-area', rows: '7' });
    ta.value = getPath(store.state.config, cfgPath, '') || '';

    const save = () => commit(setPath(store.state.config, cfgPath, ta.value));
    ta.addEventListener('change', save);
    ta.addEventListener('blur', save);

    const counter = h('span', { class: 'prompt-count' });
    const updateCount = () => {
      counter.textContent = `${ta.value.length} 字`;
    };
    ta.addEventListener('input', updateCount);
    updateCount();

    const reset = h('button', { class: 'btn btn-sm', type: 'button', text: '恢复默认' });
    reset.addEventListener('click', () => {
      const def = getPath(DEFAULT_CONFIG, cfgPath, '') || '';
      ta.value = def;
      updateCount();
      commit(setPath(store.state.config, cfgPath, def));
      toast('已恢复默认提示词', 'info');
    });

    fieldRefs.push({ field: { path: cfgPath }, input: ta, kind: 'input' });

    return h('div', { class: 'field field-wide' }, [
      h('div', { class: 'field-label' }, [label, h('span', {
        class: 'tip', tabindex: '0', 'data-tip': tip || hint, text: '?',
      }), h('span', { class: 'spacer' }), reset]),
      ta,
      h('div', { class: 'field-hint' }, [hint, ' · ', counter]),
    ]);
  }

  function buildPromptPanel() {
    const wrap = h('div', { style: { display: 'flex', flexDirection: 'column', gap: '14px' } });
    wrap.appendChild(buildPromptArea(
      '系统提示词 ai.system_prompt', 'ai.system_prompt',
      '角色与总要求：决定 AI 以什么身份、什么尺度理解视频',
      TIP.prompt_sys,
    ));
    wrap.appendChild(buildPromptArea(
      '用户提示词 ai.prompt', 'ai.prompt',
      '输出格式要求：必须要求模型返回 JSON（含 title / plot / tags）',
      TIP.prompt_user,
    ));

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '提示词（可自定义）' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '改动即时保存到 config.json' }),
      ]),
      wrap,
    ]);
  }

  // ---------- 术语速查 ----------
  function buildGlossary() {
    const grid = h('div', { class: 'form-grid' });
    for (const [term, text] of GLOSSARY) {
      grid.appendChild(
        h('div', { class: 'field' }, [
          h('div', { class: 'field-label', text: term }),
          h('div', { class: 'field-hint', text: text.replace(/^[^：]+：/, '') }),
        ])
      );
    }
    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '术语速查' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '专业名 + 一句人话' }),
      ]),
      grid,
    ]);
  }

  // ---------- 渲染 ----------
  function render(state) {
    const cfg = state.config || {};

    // 开关状态
    for (const group of SWITCH_GROUPS) {
      for (const item of group.items) {
        const sw = switchRefs[item.id];
        if (!sw) continue;
        const on = getVal(item, cfg);
        sw.classList.toggle('is-on', on);
        sw.setAttribute('aria-checked', on ? 'true' : 'false');
        const row = rowRefs[item.id];
        if (row) {
          const shown = typeof item.visible === 'function' ? item.visible(cfg) : true;
          row.style.display = shown ? '' : 'none';
        }
      }
    }

    // 表单字段值（不覆盖正在编辑的输入）
    for (const entry of fieldRefs) {
      const { field, input, kind } = entry;
      const value = getPath(cfg, field.path, '');
      if (kind === 'bool') {
        const on = !!value;
        input.classList.toggle('is-on', on);
        input.setAttribute('aria-checked', on ? 'true' : 'false');
      } else if (document.activeElement !== input) {
        const str = value === undefined || value === null ? '' : String(value);
        if (input.value !== str) input.value = str;
      }
    }
  }

  function buildHeader() {
    const hint = h('span', { class: 'save-hint' }, [h('span', { class: 'dot' }), '已保存']);
    refs.saveHint = hint;

    const resetBtn = h('button', { class: 'btn btn-sm', type: 'button' }, '恢复默认');
    resetBtn.addEventListener('click', () => {
      commit(deepClone(DEFAULT_CONFIG));
      toast('已恢复默认配置', 'info');
    });

    return h('div', { class: 'view-head' }, [
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '12px' } }, [
        h('h1', { class: 'view-title', text: '设置' }),
        h('span', { class: 'spacer', style: { marginLeft: 'auto' } }),
        hint,
        resetBtn,
      ]),
      h('p', { class: 'view-sub', text: '每个能力一个开关，改动即时保存到 config.json；悬停「?」查看术语解释' }),
    ]);
  }

  function mount(el) {
    container = el;
    container.textContent = '';
    const view = h('div', { class: 'view view-settings' }, [
      buildHeader(),
      buildSwitchGroups(),
      buildAccessPanel(),
      buildFormPanel('AI 配置', '严格对齐 config.json 的 ai 结构', AI_FIELDS),
      buildPromptPanel(),
      buildAITestPanel(),
      buildFormPanel('高级参数', 'frames / whisper / naming / runtime', ADVANCED_FIELDS),
      buildAsrPanel(),
      buildMirrorPanel(),
      buildEnvPanel(),
      buildGlossary(),
    ]);
    container.appendChild(view);
    unsubs.push(store.subscribe(render));
    refreshEnv(true);
    if (refs.accessReload) refs.accessReload();
  }

  function destroy() {
    for (const u of unsubs) u();
    unsubs.length = 0;
    container = null;
    for (const key of Object.keys(switchRefs)) delete switchRefs[key];
    for (const key of Object.keys(rowRefs)) delete rowRefs[key];
    asrRerender = null;
    fieldRefs.length = 0;
    refs.saveHint = null;
    refs.aiTestOut = null;
    refs.envOut = null;
    refs.accessReload = null;
  }

  return { mount, render, destroy };
}
