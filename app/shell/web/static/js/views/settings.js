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
  exiftool: 'exiftool：专门改文件属性的工具，负责把标题写进视频内部。',
  whisper: 'Whisper：语音识别模型，把说的话变成字。',
  keyframe: '关键帧：从视频里挑出的代表性画面，挑得多看得准但更慢。',
  transcribe: '转写：把视频里说的话变成文字，帮 AI 判断内容。',
  multimodal: '多模态模型：既能看图又能读字的 AI，本应用必须用这种。',
  nfo: 'NFO：XML 信息文件，Jellyfin / Kodi 靠它认视频。',
  srt: 'SRT：最常见的字幕格式，记事本就能打开。',
  cuda: 'CUDA：让程序用显卡加速的东西，没有也能用，只是慢。',
  recursive: '递归：连子文件夹一起处理。',
  dryrun: '预览模式：只演算不落盘，用来确认命名效果。',
};

/** 开关分组（严格对应 §7.1 开关总表） */
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
      { id: 'use_gpu', path: 'whisper.use_gpu', label: 'GPU 加速转写', desc: '用显卡跑 Whisper；缺 CUDA 库会自动回退 CPU', tip: TIP.cuda },
      { id: 'srt_vad_filter', path: 'whisper.vad_filter', label: 'VAD 过滤', desc: '关闭后静音段也会送进模型', tip: TIP.transcribe },
    ],
  },
  {
    id: 'naming',
    label: '命名',
    items: [
      { id: 'include_date', path: 'naming.include_date', label: '时间前缀', desc: '文件名前缀加 20261008_2313', tip: '' },
      { id: 'include_original', path: 'naming.include_original', label: '原名后缀', desc: '保留原文件名片段', tip: '' },
      { id: 'enable_marker', path: 'naming.enable_marker', label: '处理标记', desc: '加 AI_RENAMED 标记，便于识别', tip: '' },
      { id: 'enable_skip', path: 'naming.enable_skip', label: '跳过已处理', desc: '名字含标记或已写软水印的视频跳过；关闭则全部重跑', tip: '' },
    ],
  },
  {
    id: 'output',
    label: '输出',
    items: [
      { id: 'enable_nfo', path: 'output.nfo', label: '生成 NFO', desc: '供 Jellyfin / Kodi 识别', tip: TIP.nfo },
      { id: 'enable_srt', path: 'output.srt', label: '生成 SRT 字幕', desc: '生成同名 .srt 字幕文件', tip: TIP.srt },
      { id: 'write_metadata', path: 'output.metadata', label: 'ExifTool 写元数据', desc: '把标题写进视频内部属性', tip: TIP.exiftool },
      { id: 'move_failed', path: 'output.move_failed', label: '失败移入 _failed', desc: '失败文件集中存放，便于复查', tip: '' },
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
      { id: 'verbose', path: 'runtime.verbose', label: '详细日志', desc: '输出 DEBUG 级详细日志', tip: '' },
    ],
  },
];

/** AI 配置字段（严格对齐 §7.2 ai 结构） */
const AI_FIELDS = [
  { path: 'ai.provider', label: 'provider 后端', type: 'select', options: ['openai', 'gemini'], hint: 'openai 兼容 或 gemini' },
  { path: 'ai.base_url', label: 'base_url 服务地址', type: 'text', hint: '如 http://127.0.0.1:8094/v1' },
  { path: 'ai.api_key', label: 'api_key 密钥', type: 'password', hint: '本地服务可随意填写' },
  { path: 'ai.model', label: 'model 模型名', type: 'text', hint: '必须支持看图（多模态）', tip: TIP.multimodal },
  { path: 'ai.timeout', label: 'timeout 超时(秒)', type: 'number', step: '1', min: '1' },
  { path: 'ai.retry_times', label: 'retry_times 重试次数', type: 'number', step: '1', min: '0' },
  { path: 'ai.max_tokens', label: 'max_tokens 最大输出', type: 'number', step: '1', min: '1' },
  { path: 'ai.temperature', label: 'temperature 温度', type: 'number', step: '0.1', min: '0', max: '2' },
  { path: 'ai.top_p', label: 'top_p 采样', type: 'number', step: '0.1', min: '0', max: '1' },
  { path: 'ai.enforce_json_mode', label: 'enforce_json_mode 强制 JSON', type: 'bool', hint: '要求模型只返回 JSON' },
];

/** 高级参数（frames / whisper / naming / runtime） */
const ADVANCED_FIELDS = [
  { path: 'naming.template', label: '命名模板 naming.template', type: 'text', hint: '如 {date}_{title}' },
  { path: 'naming.date_format', label: '日期格式 naming.date_format', type: 'text', hint: '如 %Y%m%d_%H%M' },
  { path: 'naming.marker', label: '处理标记 naming.marker', type: 'text', hint: '如 AI' },
  { path: 'frames.max_keyframes', label: '关键帧上限 frames.max_keyframes', type: 'number', step: '1', min: '1', tip: TIP.keyframe },
  { path: 'frames.max_side', label: '帧最长边 frames.max_side', type: 'number', step: '1', min: '64' },
  { path: 'frames.workers', label: '抽帧并发 frames.workers', type: 'number', step: '1', min: '1', tip: TIP.ffmpeg },
  { path: 'whisper.model', label: 'Whisper 模型', type: 'text', tip: TIP.whisper },
  { path: 'whisper.device', label: 'Whisper 设备 whisper.device', type: 'select', options: ['auto', 'cuda', 'cpu'] },
  {
    path: 'whisper.language',
    label: '转写 / 字幕语言',
    type: 'select',
    options: ['auto', 'zh', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'ru', 'pt', 'it', 'ar'],
    hint: 'auto = 自动检测；同时决定 .srt 字幕的语言',
  },
  { path: 'whisper.compute_type', label: '计算精度 whisper.compute_type', type: 'text', hint: '如 int8_float16 / float16' },
  { path: 'whisper.workers', label: '转写并发 whisper.workers', type: 'number', step: '1', min: '1' },
  { path: 'runtime.ai_workers', label: 'AI 并发 runtime.ai_workers', type: 'number', step: '1', min: '1' },
  { path: 'runtime.log_file', label: '日志文件 runtime.log_file', type: 'text', hint: '如 logs/run.log' },
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
];

export function createView(ctx) {
  const { store, apiClient, toast, persistConfig } = ctx;
  let container = null;
  const unsubs = [];
  const switchRefs = {}; // id -> switch 元素
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
    });
    switchRefs[item.id] = sw;

    const labelRow = h('div', { class: 'switch-label' }, [item.label]);
    if (item.tip) {
      labelRow.appendChild(h('span', { class: 'tip', tabindex: '0', 'data-tip': item.tip, text: '?' }));
    }

    return h('div', { class: 'switch-row' }, [
      h('div', { class: 'switch-meta' }, [labelRow, h('div', { class: 'switch-desc', text: item.desc })]),
      sw,
    ]);
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
      if (field.tip) labelRow.appendChild(h('span', { class: 'tip', tabindex: '0', 'data-tip': field.tip, text: '?' }));
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
    if (field.tip) labelRow.appendChild(h('span', { class: 'tip', tabindex: '0', 'data-tip': field.tip, text: '?' }));

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
    grid.appendChild(kv('faster-whisper', s.faster_whisper ? '已安装' : '未安装'));
    grid.appendChild(kv('GPU 开关', s.use_gpu ? '开启' : '关闭'));
    grid.appendChild(kv('CUDA 设备', s.cuda_available ? '可用' : '不可用'));
    grid.appendChild(kv('CUDA 运行库', libs.present ? '已就位' : '缺失'));
    out.appendChild(grid);
    if (libs.cublas) out.appendChild(h('div', { class: 'env-log', text: `cublas: ${libs.cublas}` }));
    if (s.cuda_reason) out.appendChild(h('div', { class: 'env-log', text: `说明: ${s.cuda_reason}` }));
    if (!libs.present) {
      out.appendChild(h('div', {
        class: 'env-warn',
        text: '缺少 cublas64_12.dll 会让 GPU 转写挂起；点下方按钮自动补装到 libs/。',
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
      fixBtn.textContent = '正在下载…（可能数分钟）';
      out.textContent = '正在向 libs/ 安装 nvidia-cublas-cu12 / nvidia-cudnn-cu12 …';
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

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '转写运行环境' }),
        h('span', { class: 'spacer' }),
        refreshBtn,
      ]),
      out,
      h('div', { class: 'control-row' }, [installBtn, fixBtn]),
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
      modelscope: 'https://www.modelscope.cn',
    },
  };

  function buildMirrorRow(label, cfgPath, presets, hint) {
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
      h('div', { class: 'field-label', text: label }),
      input,
      h('div', { class: 'field-hint', text: hint }),
      chips,
    ]);
  }

  function buildMirrorPanel() {
    const grid = h('div', { class: 'form-grid' });
    grid.appendChild(buildMirrorRow(
      'pip 源 runtime.pip_index', 'runtime.pip_index', MIRROR_PRESETS.pip,
      '依赖下载（pip install）使用的镜像源，可自定义填任意地址',
    ));
    grid.appendChild(buildMirrorRow(
      'HuggingFace 端点 runtime.hf_endpoint', 'runtime.hf_endpoint', MIRROR_PRESETS.hf,
      '模型下载使用的端点（如 hf-mirror.com），可自定义',
    ));
    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '下载镜像（国内加速）' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '依赖与模型下载走这里设置的源' }),
      ]),
      grid,
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
      buildFormPanel('AI 配置', '严格对齐 config.json 的 ai 结构', AI_FIELDS),
      buildAITestPanel(),
      buildFormPanel('高级参数', 'frames / whisper / naming / runtime', ADVANCED_FIELDS),
      buildMirrorPanel(),
      buildEnvPanel(),
      buildGlossary(),
    ]);
    container.appendChild(view);
    unsubs.push(store.subscribe(render));
    refreshEnv(true);
  }

  function destroy() {
    for (const u of unsubs) u();
    unsubs.length = 0;
    container = null;
    for (const key of Object.keys(switchRefs)) delete switchRefs[key];
    fieldRefs.length = 0;
    refs.saveHint = null;
    refs.aiTestOut = null;
    refs.envOut = null;
  }

  return { mount, render, destroy };
}
