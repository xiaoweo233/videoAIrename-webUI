/**
 * views/dashboard.js — 工作台视图
 * 拖拽投放区（drag&drop + 点击选择 + 拖入高亮）、任务控制（开始/停止/预览模式）、
 * 统计卡片（成功/跳过/失败/取消 + 平均秒/视频）、当前任务进度条 + 进度环。
 */
import { h, setPath, formatSeconds, clamp } from '../utils.js';

const RING_RADIUS = 54;
const RING_CIRC = 2 * Math.PI * RING_RADIUS;

const PHASE_LABEL = {
  '0': '待机',
  '1': '① 探测',
  '2a': '②a 抽帧',
  '2b': '②b 转写',
  '3': '③ 合并',
  '4': '④ AI 分析',
  '5': '⑤ 落盘',
};

export function createView(ctx) {
  const { store, apiClient, toast, control, persistConfig } = ctx;
  let container = null;
  const unsubs = [];
  const refs = { picked: [] };

  // ---------- 小工具 ----------
  function baseName(p) {
    const s = String(p || '').replace(/[\\/]+$/, '');
    const idx = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
    return idx >= 0 ? s.slice(idx + 1) : s;
  }

  function pickErrMsg(err) {
    const status = err && err.status;
    if (status === 409) return '已有选择窗口在等待操作；若看不到窗口，请点「取消等待」后重试';
    if (status === 0) return '选择超时或服务未响应，请重试';
    return (err && (err.message || err.detail)) || '打开系统选择器失败';
  }

  // file:///D:/a/b -> D:\a\b ；file://server/share -> \\server\share
  function fileUrlToPath(url) {
    let rest = String(url).replace(/^file:\/\//i, '');
    if (/^\/[a-zA-Z][:|]/.test(rest)) {
      rest = rest.slice(1); // 去掉盘符前的斜杠
    } else if (!rest.startsWith('/')) {
      rest = '//' + rest; // UNC
    }
    try {
      rest = decodeURIComponent(rest);
    } catch (e) {
      /* 保留原样 */
    }
    return rest.replace(/\//g, '\\');
  }

  /**
   * 从 drop 事件里尽力取出「绝对路径」。
   * Chromium/Edge 从资源管理器拖入时会带上 text/uri-list（file:/// 形式），
   * 这是浏览器环境下唯一能拿到真实路径的途径。
   */
  function pathsFromDrop(dt) {
    const out = [];
    const take = (s) => {
      const v = String(s).trim();
      if (v && !out.includes(v)) out.push(v);
    };
    const read = (type) => {
      try {
        return dt.getData(type) || '';
      } catch (e) {
        return '';
      }
    };
    const uri = read('text/uri-list') || read('text/x-moz-url');
    for (const line of uri.split(/\r?\n/)) {
      const t = line.trim();
      if (!t || t.startsWith('#')) continue;
      if (/^file:\/\//i.test(t)) take(fileUrlToPath(t));
    }
    if (!out.length) {
      const plain = read('text/plain');
      for (const line of plain.split(/\r?\n/)) {
        const t = line.trim();
        if (!t) continue;
        if (/^file:\/\//i.test(t)) take(fileUrlToPath(t));
        else if (/^[a-zA-Z]:[\\/]/.test(t) || t.startsWith('\\\\')) take(t);
      }
    }
    return out;
  }

  // ---------- 投放区 ----------
  function buildDropzone() {
    const input = h('input', {
      class: 'field-input',
      type: 'text',
      placeholder: '也可直接粘贴路径，例如 D:\\素材\\产品视频',
      'aria-label': '视频或文件夹路径',
    });
    refs.pathInput = input;
    // 手动编辑 → 放弃已选列表
    input.addEventListener('input', () => {
      if (refs.picked.length) {
        refs.picked = [];
        renderChips();
      }
    });

    const pickFilesBtn = h('button', { class: 'btn', type: 'button', text: '选择视频' });
    const pickFolderBtn = h('button', { class: 'btn', type: 'button', text: '选择文件夹' });

    // 等待期间显示「取消等待」，用于回收卡住的对话框
    const cancelBtn = h('button', { class: 'btn btn-sm btn-danger', type: 'button', text: '取消等待' });
    cancelBtn.hidden = true;
    cancelBtn.addEventListener('click', async () => {
      try {
        const r = await apiClient.cancelDialog();
        toast(r && r.cancelled ? '已取消等待中的选择窗口' : '当前没有等待中的窗口', 'info');
      } catch (e) {
        toast('取消失败：' + ((e && e.message) || '未知错误'), 'warn');
      }
    });

    function setPending(on, which) {
      refs.pending = on;
      pickFilesBtn.disabled = on;
      pickFolderBtn.disabled = on;
      cancelBtn.hidden = !on;
      pickFilesBtn.textContent = on && which === 'files' ? '等待选择…' : '选择视频';
      pickFolderBtn.textContent = on && which === 'folders' ? '等待选择…' : '选择文件夹';
    }

    async function doPick(which) {
      setPending(true, which);
      try {
        const payload = { initial_dir: input.value.trim() };
        const res = which === 'files'
          ? await apiClient.pickFiles(payload)
          : await apiClient.pickFolder(payload);
        if (res && res.cancelled) {
          toast('已取消选择', 'info');
          return;
        }
        const paths = (res && res.paths) || [];
        if (!paths.length) {
          toast('未选择任何内容', 'info');
          return;
        }
        setPicked(paths);
        toast(which === 'files' ? `已选择 ${paths.length} 个视频` : '已选择文件夹', 'ok');
      } catch (err) {
        toast(pickErrMsg(err), 'warn', 4600);
      } finally {
        setPending(false, null);
      }
    }

    pickFilesBtn.addEventListener('click', () => doPick('files'));
    pickFolderBtn.addEventListener('click', () => doPick('folders'));

    const clearBtn = h('button', { class: 'btn btn-sm', type: 'button', text: '清空' });
    clearBtn.addEventListener('click', () => {
      refs.picked = [];
      input.value = '';
      renderChips();
      input.focus();
    });

    const list = h('div', { class: 'dz-list' });
    refs.dzList = list;

    const dz = h('div', { class: 'dropzone', role: 'button', tabindex: '0' }, [
      h('div', { class: 'dz-icon' }, [
        h('svg', {
          attrs: {
            viewBox: '0 0 24 24',
            width: '26',
            height: '26',
            fill: 'none',
            stroke: 'currentColor',
            'stroke-width': '2',
            'stroke-linecap': 'round',
            'stroke-linejoin': 'round',
          },
          html: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
        }),
      ]),
      h('div', { class: 'dz-title', text: '把视频或文件夹拖到这里' }),
      h('div', { class: 'dz-sub', text: '也可用系统窗口选择；支持一次拖入多个视频或整个文件夹' }),
      h('div', { class: 'dz-path-row' }, [input, pickFilesBtn, pickFolderBtn]),
      h('div', { class: 'dz-actions' }, [clearBtn, cancelBtn]),
      list,
    ]);

    let dragDepth = 0;
    dz.addEventListener('dragenter', (e) => {
      e.preventDefault();
      dragDepth++;
      dz.classList.add('is-drag');
    });
    dz.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });
    dz.addEventListener('dragleave', (e) => {
      e.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) dz.classList.remove('is-drag');
    });
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dragDepth = 0;
      dz.classList.remove('is-drag');
      const paths = pathsFromDrop(e.dataTransfer);
      if (paths.length) {
        setPicked(paths);
        toast(`已从拖入内容识别出 ${paths.length} 个路径`, 'ok');
      } else {
        toast('浏览器未提供路径信息，请点「选择视频 / 选择文件夹」用系统窗口选择', 'warn', 4600);
      }
    });
    dz.addEventListener('click', (e) => {
      if (e.target === input || pickFilesBtn.contains(e.target) || pickFolderBtn.contains(e.target)
        || clearBtn.contains(e.target) || cancelBtn.contains(e.target)) return;
      input.focus();
    });
    dz.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        input.focus();
      }
    });

    return dz;
  }

  /** 记录已选路径并渲染 chips */
  function setPicked(paths) {
    refs.picked = paths.slice();
    if (paths.length === 1) {
      refs.pathInput.value = paths[0];
    } else {
      // 多选时输入框只显示数量，真正提交用 refs.picked
      refs.pathInput.value = `${baseName(paths[0])} 等 ${paths.length} 项`;
    }
    renderChips();
  }

  function renderChips() {
    if (!refs.dzList) return;
    refs.dzList.textContent = '';
    const picked = refs.picked;
    if (!picked.length) return;
    const shown = picked.slice(0, 12);
    for (const p of shown) {
      refs.dzList.appendChild(h('span', { class: 'dz-chip', title: p, text: baseName(p) }));
    }
    if (picked.length > shown.length) {
      refs.dzList.appendChild(h('span', { class: 'dz-chip', text: `…等 ${picked.length} 项` }));
    }
  }

  /** 组织提交参数：优先已选，其次手动输入 */
  function collectPayload() {
    const picked = refs.picked;
    if (picked.length > 1) return { paths: picked.slice() };
    if (picked.length === 1) return { path: picked[0] };
    const manual = (refs.pathInput && refs.pathInput.value.trim()) || '';
    return manual ? { path: manual } : null;
  }

  // ---------- 任务控制 ----------
  function buildControls() {
    const startBtn = h('button', { class: 'btn btn-primary', type: 'button' }, '开始处理');
    startBtn.addEventListener('click', () => {
      const payload = collectPayload();
      if (!payload) {
        toast('请先用系统窗口选择视频/文件夹，或粘贴路径', 'warn', 3600);
        if (refs.pathInput) refs.pathInput.focus();
        return;
      }
      control.start(payload);
    });
    refs.startBtn = startBtn;

    const stopBtn = h('button', { class: 'btn btn-danger', type: 'button' }, '停止');
    stopBtn.addEventListener('click', () => control.stop());
    refs.stopBtn = stopBtn;

    const previewSwitch = h('button', {
      class: 'switch',
      type: 'button',
      role: 'switch',
      'aria-checked': 'false',
      'aria-label': '预览模式',
    }, [h('span', { class: 'switch-knob' })]);
    previewSwitch.addEventListener('click', () => {
      const next = !store.state.config.output.dry_run;
      const cfg = setPath(store.state.config, 'output.dry_run', next);
      store.set({ config: cfg, configDirty: true });
      persistConfig(cfg);
      toast(next ? '已开启预览模式：只演算不落盘' : '已关闭预览模式：将真实落盘', 'info');
    });
    refs.previewSwitch = previewSwitch;

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '任务控制' }),
      ]),
      h('div', { class: 'control-row' }, [
        startBtn,
        stopBtn,
        h('div', { class: 'switch-row', style: { padding: '6px 12px', width: 'auto' } }, [
          h('div', { class: 'switch-meta' }, [
            h('div', { class: 'switch-label' }, [
              '预览模式',
              h('span', {
                class: 'tip',
                tabindex: '0',
                'data-tip': '预览模式：只演算不落盘，用来确认命名效果。开启后文件不会被真正修改。',
                text: '?',
              }),
            ]),
            h('div', { class: 'switch-desc', text: '只演算不落盘，确认命名效果' }),
          ]),
          previewSwitch,
        ]),
      ]),
    ]);
  }

  // ---------- 统计卡片 ----------
  function statCard(key, label, tone) {
    const value = h('div', { class: 'stat-value', text: '0' });
    refs[key] = value;
    return h('div', { class: `stat-card is-${tone}` }, [value, h('div', { class: 'stat-label', text: label })]);
  }

  function buildStats() {
    const avgValue = h('div', { class: 'stat-value' }, ['—', h('small', { text: 's/视频' })]);
    refs.avg = avgValue;
    const avgCard = h('div', { class: 'stat-card is-mute' }, [
      avgValue,
      h('div', { class: 'stat-label', text: '平均耗时' }),
    ]);

    return h('div', { class: 'stat-grid' }, [
      statCard('success', '成功', 'ok'),
      statCard('skipped', '跳过', 'warn'),
      statCard('failed', '失败', 'err'),
      statCard('cancelled', '取消', 'mute'),
      avgCard,
    ]);
  }

  // ---------- 进度 ----------
  function buildProgress() {
    const fill = h('div', { class: 'progress-fill' });
    refs.fill = fill;

    const pctLeft = h('span', { text: '0%' });
    refs.pctLeft = pctLeft;
    const countRight = h('span', { text: '0 / 0' });
    refs.countRight = countRight;

    const phaseChip = h('span', { class: 'phase-chip', text: '待机' });
    refs.phaseChip = phaseChip;
    const fileName = h('span', { class: 'file-name', text: '空闲' });
    refs.fileName = fileName;

    const ringValue = h('circle', {
      class: 'ring-value',
      attrs: {
        cx: '66',
        cy: '66',
        r: String(RING_RADIUS),
        'stroke-dasharray': String(RING_CIRC),
        'stroke-dashoffset': String(RING_CIRC),
      },
    });
    refs.ringValue = ringValue;

    const ringPct = h('strong', { text: '0%' });
    refs.ringPct = ringPct;

    const ringSvg = h('svg', {
      class: 'ring-svg',
      attrs: { viewBox: '0 0 132 132', 'aria-hidden': 'true' },
      html:
        '<defs><linearGradient id="ring-grad" x1="0" y1="0" x2="1" y2="1">' +
        '<stop offset="0" stop-color="#6d8bff"/><stop offset="1" stop-color="#43d6c8"/>' +
        '</linearGradient></defs>',
    });
    ringSvg.appendChild(h('circle', { class: 'ring-track', attrs: { cx: '66', cy: '66', r: String(RING_RADIUS) } }));
    ringSvg.appendChild(ringValue);

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '当前任务进度' }),
      ]),
      h('div', { class: 'progress-wrap' }, [
        h('div', { class: 'progress-main' }, [
          h('div', { class: 'current-file' }, [phaseChip, fileName]),
          h('div', { class: 'progress-bar' }, [fill]),
          h('div', { class: 'progress-meta' }, [pctLeft, countRight]),
        ]),
        h('div', { class: 'ring' }, [ringSvg, h('div', { class: 'ring-center' }, [ringPct, h('span', { text: '完成度' })])]),
      ]),
    ]);
  }

  // ---------- 渲染 ----------
  function render(state) {
    const t = state.task;
    const stats = t.stats || { success: 0, skipped: 0, failed: 0, cancelled: 0 };
    if (refs.success) refs.success.textContent = String(stats.success || 0);
    if (refs.skipped) refs.skipped.textContent = String(stats.skipped || 0);
    if (refs.failed) refs.failed.textContent = String(stats.failed || 0);
    if (refs.cancelled) refs.cancelled.textContent = String(stats.cancelled || 0);
    if (refs.avg) refs.avg.firstChild.textContent = t.avgSeconds ? formatSeconds(t.avgSeconds * 1000) : '—';

    const total = t.total || 0;
    const done = t.done || 0;
    const pct = total > 0 ? clamp(done / total, 0, 1) : 0;

    if (refs.fill) refs.fill.style.transform = `scaleX(${pct})`;
    const pctText = `${Math.round(pct * 100)}%`;
    if (refs.pctLeft) refs.pctLeft.textContent = pctText;
    if (refs.countRight) refs.countRight.textContent = `${done} / ${total}`;
    if (refs.ringPct) refs.ringPct.textContent = pctText;
    if (refs.ringValue) {
      refs.ringValue.setAttribute('stroke-dashoffset', String(RING_CIRC * (1 - pct)));
    }

    if (refs.phaseChip) refs.phaseChip.textContent = PHASE_LABEL[t.phase] || '待机';
    if (refs.fileName) refs.fileName.textContent = t.currentFile || (t.running ? '准备中…' : '空闲');

    if (refs.startBtn) refs.startBtn.disabled = !!t.running;
    if (refs.stopBtn) refs.stopBtn.disabled = !t.running;
    if (refs.previewSwitch) {
      const on = !!(state.config && state.config.output && state.config.output.dry_run);
      refs.previewSwitch.classList.toggle('is-on', on);
      refs.previewSwitch.setAttribute('aria-checked', on ? 'true' : 'false');
    }
  }

  function mount(el) {
    container = el;
    container.textContent = '';
    const view = h('div', { class: 'view view-dashboard' }, [
      h('div', { class: 'view-head' }, [
        h('h1', { class: 'view-title', text: '工作台' }),
        h('p', { class: 'view-sub', text: '拖入一个文件夹，剩下的交给引擎' }),
      ]),
      buildDropzone(),
      h('div', { class: 'grid-2' }, [buildControls(), buildStats()]),
      buildProgress(),
    ]);
    container.appendChild(view);
    unsubs.push(store.subscribe(render));
  }

  function destroy() {
    for (const u of unsubs) u();
    unsubs.length = 0;
    container = null;
  }

  return { mount, render, destroy };
}
