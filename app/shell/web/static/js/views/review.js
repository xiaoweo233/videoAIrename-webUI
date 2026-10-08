/**
 * views/review.js — 命名审批视图
 * 改名前后对比卡片列表（原名 → 新名 + 置信度 / tags），逐条「采纳 / 编辑 / 忽略」
 * 与批量操作，含空状态。手机浏览器可访问，用于随时点头确认。
 *
 * 性能：仅在 review.items 数组引用变化时重建列表 DOM，
 * 避免日志/进度高频刷新时每帧重排卡片。
 */
import { h } from '../utils.js';

export function createView(ctx) {
  const { store, apiClient, toast } = ctx;
  let container = null;
  const unsubs = [];
  const refs = { list: null, count: null, acceptAll: null, ignoreAll: null };
  let editingId = null;
  let lastItemsRef = null;

  /** 提交决定并从列表移除（乐观更新，离线静默） */
  function decide(item, decision, newName) {
    const items = store.state.review.items.filter((x) => x.id !== item.id);
    store.set({ review: { items } });
    apiClient.submitReview(item.id, { decision, newName }).catch(() => {});
    const label = { accept: '已采纳', ignore: '已忽略', edit: '已保存修改' }[decision] || '已处理';
    toast(`${label}：${item.oldName}`, decision === 'ignore' ? 'info' : 'ok');
  }

  function buildCard(item) {
    const editing = editingId === item.id;
    const confidence = typeof item.confidence === 'number' ? item.confidence : 0.85;

    let nameNode;
    if (editing) {
      const input = h('input', { class: 'rc-new-input', type: 'text', value: item.newName });
      nameNode = input;
      queueMicrotask(() => {
        if (document.activeElement !== input) {
          input.focus();
          input.select();
        }
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          const value = input.value.trim();
          if (value) {
            editingId = null;
            decide(Object.assign({}, item, { newName: value }), 'edit', value);
          }
        } else if (e.key === 'Escape') {
          editingId = null;
          rebuild();
        }
      });
    } else {
      nameNode = h('span', { class: 'rc-new', title: item.newName, text: item.newName });
    }

    const actions = h('div', { class: 'rc-actions' });
    if (editing) {
      actions.appendChild(
        h('button', {
          class: 'btn btn-sm btn-primary',
          type: 'button',
          onClick: () => {
            const value = nameNode.value.trim();
            if (value) {
              editingId = null;
              decide(Object.assign({}, item, { newName: value }), 'edit', value);
            }
          },
        }, '保存')
      );
      actions.appendChild(
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          onClick: () => {
            editingId = null;
            rebuild();
          },
        }, '取消')
      );
    } else {
      actions.appendChild(
        h('button', { class: 'btn btn-sm btn-primary', type: 'button', onClick: () => decide(item, 'accept') }, '采纳')
      );
      actions.appendChild(
        h('button', {
          class: 'btn btn-sm',
          type: 'button',
          onClick: () => {
            editingId = item.id;
            rebuild();
          },
        }, '编辑')
      );
      actions.appendChild(
        h('button', { class: 'btn btn-sm btn-danger', type: 'button', onClick: () => decide(item, 'ignore') }, '忽略')
      );
    }

    const confFill = h('span', { class: 'conf-fill', style: { transform: `scaleX(${confidence})` } });

    const tagsRow = h('div', { class: 'rc-meta' });
    for (const tag of item.tags || []) tagsRow.appendChild(h('span', { class: 'tag', text: tag }));
    tagsRow.appendChild(
      h('span', { class: 'rc-confidence' }, ['置信度', h('span', { class: 'conf-bar' }, [confFill]), h('span', { text: confidence.toFixed(2) })])
    );

    return h('div', { class: 'review-card' }, [
      h('div', { class: 'rc-main' }, [
        h('div', { class: 'rc-names' }, [
          h('span', { class: 'rc-old', title: item.oldName, text: item.oldName }),
          h('span', { class: 'rc-arrow', text: '→' }),
          nameNode,
        ]),
        tagsRow,
      ]),
      actions,
    ]);
  }

  function buildEmpty() {
    return h('div', { class: 'empty-state' }, [
      h('div', { class: 'empty-icon' }, [
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
          html: '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
        }),
      ]),
      h('div', { class: 'empty-title', text: '暂无待审批的命名' }),
      h('div', { class: 'empty-sub', text: '任务开始后，每个视频的新旧文件名会实时出现在这里，供你逐条确认。' }),
    ]);
  }

  function rebuild() {
    const items = store.state.review.items;
    lastItemsRef = items;
    if (refs.count) refs.count.textContent = `待审批 ${items.length} 条`;
    if (refs.acceptAll) refs.acceptAll.disabled = items.length === 0;
    if (refs.ignoreAll) refs.ignoreAll.disabled = items.length === 0;
    if (!refs.list) return;
    refs.list.textContent = '';
    if (items.length === 0) {
      refs.list.appendChild(buildEmpty());
      return;
    }
    for (const item of items) refs.list.appendChild(buildCard(item));
  }

  function buildList() {
    const list = h('div', { class: 'review-list' });
    refs.list = list;
    return list;
  }

  function buildToolbar() {
    const count = h('span', { class: 'review-count', text: '待审批 0 条' });
    refs.count = count;

    const acceptAll = h('button', { class: 'btn btn-sm btn-primary', type: 'button' }, '全部采纳');
    acceptAll.addEventListener('click', () => {
      const items = store.state.review.items.slice();
      if (!items.length) return;
      for (const item of items) apiClient.submitReview(item.id, { decision: 'accept' }).catch(() => {});
      store.set({ review: { items: [] } });
      toast(`已批量采纳 ${items.length} 条`, 'ok');
    });
    refs.acceptAll = acceptAll;

    const ignoreAll = h('button', { class: 'btn btn-sm btn-danger', type: 'button' }, '全部忽略');
    ignoreAll.addEventListener('click', () => {
      const items = store.state.review.items.slice();
      if (!items.length) return;
      for (const item of items) apiClient.submitReview(item.id, { decision: 'ignore' }).catch(() => {});
      store.set({ review: { items: [] } });
      toast(`已批量忽略 ${items.length} 条`, 'info');
    });
    refs.ignoreAll = ignoreAll;

    return h('div', { class: 'review-toolbar' }, [count, acceptAll, ignoreAll]);
  }

  function render(state) {
    if (state.review.items !== lastItemsRef) rebuild();
  }

  function mount(el) {
    container = el;
    container.textContent = '';
    const view = h('div', { class: 'view view-review' }, [
      h('div', { class: 'view-head' }, [
        h('h1', { class: 'view-title', text: '命名审批' }),
        h('p', { class: 'view-sub', text: '对比改名前后，逐条确认或修改；不满意可一键忽略保留原名' }),
      ]),
      closePanel(buildToolbar(), buildList()),
    ]);
    container.appendChild(view);
    unsubs.push(store.subscribe(render));
  }

  function closePanel(toolbar, list) {
    return h('div', { class: 'panel' }, [toolbar, h('div', { style: { height: '12px' } }), list]);
  }

  function destroy() {
    for (const u of unsubs) u();
    unsubs.length = 0;
    container = null;
    lastItemsRef = null;
    editingId = null;
  }

  return { mount, render, destroy };
}
