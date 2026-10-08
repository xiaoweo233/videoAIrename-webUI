/**
 * views/live.js — 实时进度视图
 * 五阶段流水线可视化（①探测 → ②a抽帧 ∥ ②b转写 → ③合并 → ④AI分析 → ⑤落盘），
 * 节点点亮/脉冲动画、当前文件高亮、各阶段耗时。
 */
import { h, formatDuration } from '../utils.js';

/** 流水线节点定义（②a/②b 并行） */
const NODES = [
  { key: '1', num: '1', label: '探测', sub: 'ffprobe 读取时长 / 音轨 / creation_time' },
  { key: '2a', num: '2a', label: '抽帧', sub: 'ffmpeg 并发抽关键帧' },
  { key: '2b', num: '2b', label: '转写', sub: 'Whisper 单路串行（占 GPU）' },
  { key: '3', num: '3', label: '合并', sub: '视觉 + 字幕都到齐才放行' },
  { key: '4', num: '4', label: 'AI 分析', sub: '多模态模型 → 标题 / 简介 / 标签' },
  { key: '5', num: '5', label: '落盘', sub: '写元数据 → 重命名 → NFO / SRT' },
];

const PHASE_TEXT = {
  '0': '待机',
  '1': '① 探测',
  '2a': '②a 抽帧',
  '2b': '②b 转写',
  '3': '③ 合并',
  '4': '④ AI 分析',
  '5': '⑤ 落盘',
};

export function createView(ctx) {
  const { store } = ctx;
  let container = null;
  const unsubs = [];
  const nodeRefs = {};
  const refs = {};

  function buildNode(node) {
    const dot = h('span', { class: 'pipe-dot', text: node.num });
    const elapsed = h('div', { class: 'pipe-elapsed', text: '—' });
    const el = h('div', { class: 'pipe-node is-idle' }, [
      h('div', { class: 'pipe-head' }, [dot, h('span', { class: 'pipe-label', text: node.label })]),
      h('div', { class: 'pipe-sub', text: node.sub }),
      elapsed,
    ]);
    nodeRefs[node.key] = { el, elapsed };
    return el;
  }

  function arrow() {
    return h('div', { class: 'pipe-arrow' });
  }

  function buildPipeline() {
    const stage01 = createStage([buildNode(NODES[0])]);
    const parallel = h('div', { class: 'pipe-parallel' }, [buildNode(NODES[1]), buildNode(NODES[2])]);
    const stage2 = createStage([parallel]);
    const stage3 = createStage([buildNode(NODES[3])]);
    const stage4 = createStage([buildNode(NODES[4])]);
    const stage5 = createStage([buildNode(NODES[5])]);

    return h('div', { class: 'panel' }, [
      h('div', { class: 'panel-head' }, [
        h('span', { class: 'panel-title', text: '五阶段流水线' }),
        h('span', { class: 'spacer' }),
        h('span', { class: 'panel-hint', text: '②a 抽帧与 ②b 转写互不阻塞，字幕与视觉齐备后进入 ③ 合并' }),
      ]),
      h('div', { class: 'pipeline' }, [
        stage01,
        arrow(),
        stage2,
        arrow(),
        stage3,
        arrow(),
        stage4,
        arrow(),
        stage5,
      ]),
    ]);
  }

  function createStage(children) {
    return h('div', { class: 'pipe-stage' }, children);
  }

  function buildBanner() {
    const fileEl = h('div', { class: 'cb-file', text: '空闲中' });
    const subEl = h('div', { class: 'cb-sub', text: '等待任务开始' });
    refs.fileEl = fileEl;
    refs.subEl = subEl;
    return h('div', { class: 'current-banner' }, [
      h('div', { class: 'cb-icon' }, [
        h('svg', {
          attrs: {
            viewBox: '0 0 24 24',
            width: '22',
            height: '22',
            fill: 'none',
            stroke: 'currentColor',
            'stroke-width': '2',
            'stroke-linecap': 'round',
            'stroke-linejoin': 'round',
          },
          html: '<path d="M4 5h11a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4z"/><path d="M17 9l3-2v10l-3-2z"/>',
        }),
      ]),
      h('div', { style: { minWidth: '0' } }, [fileEl, subEl]),
      h('div', { class: 'cb-live' }, [h('span', { class: 'live-dot' }), h('span', { text: 'LIVE' })]),
    ]);
  }

  function applyNodeState(key, info) {
    const ref = nodeRefs[key];
    if (!ref) return;
    const el = ref.el;
    el.classList.remove('is-idle', 'is-active', 'is-done', 'is-skip');
    const state = info ? info.state : 'idle';
    if (state === 'start') el.classList.add('is-active');
    else if (state === 'done') el.classList.add('is-done');
    else if (state === 'skip') el.classList.add('is-skip');
    else el.classList.add('is-idle');

    if (info && (state === 'done' || state === 'skip') && info.elapsedMs) {
      ref.elapsed.textContent = `耗时 ${formatDuration(info.elapsedMs)}`;
    } else if (state === 'start') {
      ref.elapsed.textContent = '处理中…';
    }
  }

  function render(state) {
    if (refs.fileEl) refs.fileEl.textContent = state.task.currentFile || (state.task.running ? '准备中…' : '空闲中');
    if (refs.subEl) {
      refs.subEl.textContent = state.task.running
        ? `当前阶段：${PHASE_TEXT[state.task.phase] || '运行中'}`
        : '等待任务开始';
    }
    for (const node of NODES) {
      applyNodeState(node.key, state.phases ? state.phases[node.key] : null);
    }
  }

  function mount(el) {
    container = el;
    container.textContent = '';
    const view = h('div', { class: 'view view-live' }, [
      h('div', { class: 'view-head' }, [
        h('h1', { class: 'view-title', text: '实时进度' }),
        h('p', { class: 'view-sub', text: '当前正在处理哪个文件，一眼看清；长时间无输出会在此高亮提示' }),
      ]),
      buildBanner(),
      buildPipeline(),
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
