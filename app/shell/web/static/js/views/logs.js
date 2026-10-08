/**
 * views/logs.js — 运行日志视图
 * 分级过滤（ALL/DEBUG/INFO/WARN/ERROR）、关键字搜索、自动滚动开关、虚拟滚动；
 * 以及单文件推理时间线（模型名 / 帧数 / 字幕字数 / 耗时）。
 *
 * 性能：虚拟滚动 + 固定行高 + 行节点池 → DOM 节点数恒定（约 26 个），
 * 无论日志量多大都不增删节点；滚动监听 passive。
 */
import { h, formatTime, formatDuration, rafBatch, debounce } from '../utils.js';
import { LOG_LEVEL_ORDER } from '../store.js';

const ROW_HEIGHT = 26;
const OVERSCAN = 4;
const VIEWPORT_HEIGHT = 420;
const POOL_SIZE = Math.ceil(VIEWPORT_HEIGHT / ROW_HEIGHT) + OVERSCAN * 2; // ≈ 25

const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARN', 'ERROR'];

export function createView(ctx) {
  const { store } = ctx;
  let container = null;
  const unsubs = [];
  const pool = [];
  const refs = { viewport: null, rows: null, spacer: null, empty: null, timeline: null };
  let activeLevel = 'ALL';
  let query = '';
  let autoscroll = true;
  let lastSig = '';
  let lastTimelineRef = null;

  // ---------- 过滤 / 取行 ----------
  function computeRows() {
    const all = store.state.logBuffer.toArray();
    const minOrder = activeLevel === 'ALL' ? -1 : LOG_LEVEL_ORDER[activeLevel];
    const q = query.trim().toLowerCase();
    const out = [];
    for (let i = 0; i < all.length; i++) {
      const e = all[i];
      const order = LOG_LEVEL_ORDER[e.level];
      if (minOrder >= 0 && (order === undefined || order < minOrder)) continue;
      if (q) {
        const hay = `${e.message} ${e.file}`.toLowerCase();
        if (hay.indexOf(q) === -1) continue;
      }
      out.push(e);
    }
    return out;
  }

  // ---------- 行节点池 ----------
  function makeRow() {
    return h('div', { class: 'log-row' }, [
      h('span', { class: 'log-time' }),
      h('span', { class: 'log-level' }),
      h('span', { class: 'log-msg' }),
    ]);
  }

  function ensurePool() {
    while (pool.length < POOL_SIZE) {
      const node = makeRow();
      refs.rows.appendChild(node);
      pool.push(node);
    }
  }

  function paint(rows) {
    if (!refs.viewport) return;
    const total = rows.length;
    const start = Math.max(0, Math.floor(refs.viewport.scrollTop / ROW_HEIGHT) - OVERSCAN);
    refs.rows.style.transform = `translateY(${start * ROW_HEIGHT}px)`;

    for (let i = 0; i < pool.length; i++) {
      const node = pool[i];
      const item = rows[start + i];
      if (!item) {
        if (node.style.display !== 'none') node.style.display = 'none';
        continue;
      }
      if (node.style.display === 'none') node.style.display = '';
      node.className = `log-row is-${String(item.level).toLowerCase()}`;
      node.children[0].textContent = formatTime(item.ts);
      node.children[1].textContent = item.level;
      node.children[2].textContent = item.message;
    }

    if (refs.empty) refs.empty.style.display = total ? 'none' : '';
  }

  const scheduleScrollPaint = rafBatch(() => {
    paint(computeRows());
  });

  // ---------- 时间线 ----------
  function rebuildTimeline(items) {
    lastTimelineRef = items;
    if (!refs.timeline) return;
    refs.timeline.textContent = '';
    if (!items.length) {
      refs.timeline.appendChild(h('div', { class: 'tl-empty', text: '暂无单文件推理记录' }));
      return;
    }
    const reversed = items.slice().reverse();
    for (const it of reversed) {
      refs.timeline.appendChild(
        h('div', { class: 'tl-row' }, [
          h('span', { class: 'tl-file', title: it.file, text: it.file }),
          h('span', { class: 'tl-metric', html: `模型 <b>${it.model || '—'}</b>` }),
          h('span', { class: 'tl-metric', html: `帧数 <b>${it.frames}</b>` }),
          h('span', { class: 'tl-metric', html: `字幕 <b>${it.subtitleChars}</b> 字` }),
          h('span', { class: 'tl-metric', html: `耗时 <b>${formatDuration(it.elapsedMs)}</b>` }),
        ])
      );
    }
  }

  // ---------- 工具条 ----------
  function buildToolbar() {
    const seg = h('div', { class: 'seg', role: 'tablist' });
    for (const level of LEVELS) {
      const btn = h('button', {
        class: `seg-item${level === activeLevel ? ' is-active' : ''}`,
        type: 'button',
        role: 'tab',
        'aria-selected': level === activeLevel ? 'true' : 'false',
        text: level,
      });
      btn.addEventListener('click', () => {
        activeLevel = level;
        for (const c of seg.children) {
          const on = c.textContent === level;
          c.classList.toggle('is-active', on);
          c.setAttribute('aria-selected', on ? 'true' : 'false');
        }
        lastSig = '';
        render(store.state);
      });
      seg.appendChild(btn);
    }

    const search = h('input', { class: 'log-search', type: 'search', placeholder: '搜索日志关键字 / 文件名', 'aria-label': '搜索日志' });
    const applySearch = debounce((value) => {
      query = value;
      lastSig = '';
      render(store.state);
    }, 180);
    search.addEventListener('input', () => applySearch(search.value));

    const autoscrollCb = h('input', { type: 'checkbox', checked: autoscroll });
    autoscrollCb.addEventListener('change', () => {
      autoscroll = autoscrollCb.checked;
      if (autoscroll && refs.viewport) refs.viewport.scrollTop = refs.viewport.scrollHeight;
    });
    const autoscrollLabel = h('label', { class: 'toggle-inline' }, [autoscrollCb, '自动滚动']);

    const clearBtn = h('button', { class: 'btn btn-sm', type: 'button' }, '清空');
    clearBtn.addEventListener('click', () => {
      store.clearLogs();
      lastSig = '';
    });

    return h('div', { class: 'logs-toolbar' }, [seg, search, autoscrollLabel, clearBtn]);
  }

  function buildViewport() {
    const rows = h('div', { class: 'log-rows' });
    const spacer = h('div', { class: 'log-spacer' });
    const empty = h('div', { class: 'log-empty', text: '暂无日志' });
    const viewport = h('div', { class: 'log-viewport', tabindex: '0' }, [spacer, rows, empty]);
    refs.viewport = viewport;
    refs.rows = rows;
    refs.spacer = spacer;
    refs.empty = empty;
    viewport.addEventListener('scroll', scheduleScrollPaint, { passive: true });
    return viewport;
  }

  function buildTimeline() {
    const timeline = h('div', { class: 'timeline' });
    refs.timeline = timeline;
    return timeline;
  }

  function render(state) {
    if (!refs.viewport) return;
    const size = state.logBuffer.size;
    const sig = `${size}|${activeLevel}|${query}`;
    if (sig !== lastSig) {
      lastSig = sig;
      const rows = computeRows();
      refs.spacer.style.height = `${rows.length * ROW_HEIGHT}px`;
      if (autoscroll) refs.viewport.scrollTop = refs.viewport.scrollHeight;
      paint(rows);
    }
    if (state.inference !== lastTimelineRef) rebuildTimeline(state.inference);
  }

  function mount(el) {
    container = el;
    container.textContent = '';
    const view = h('div', { class: 'view view-logs' }, [
      h('div', { class: 'view-head' }, [
        h('h1', { class: 'view-title', text: '运行日志' }),
        h('p', { class: 'view-sub', text: '每个功能、AI 模型、处理过程的完整记录；单文件推理耗时见下方时间线' }),
      ]),
      h('div', { class: 'panel' }, [buildToolbar(), h('div', { style: { height: '12px' } }), buildViewport()]),
      h('div', { class: 'panel' }, [
        h('div', { class: 'panel-head' }, [
          h('span', { class: 'panel-title', text: '单文件推理时间线' }),
          h('span', { class: 'spacer' }),
          h('span', { class: 'panel-hint', text: '模型名 · 帧数 · 字幕字数 · 端到端耗时' }),
        ]),
        buildTimeline(),
      ]),
    ]);
    container.appendChild(view);
    ensurePool();
    unsubs.push(store.subscribe(render));
  }

  function destroy() {
    for (const u of unsubs) u();
    unsubs.length = 0;
    container = null;
    refs.viewport = null;
    refs.rows = null;
    refs.spacer = null;
    refs.empty = null;
    refs.timeline = null;
    pool.length = 0;
    lastSig = '';
    lastTimelineRef = null;
  }

  return { mount, render, destroy };
}
