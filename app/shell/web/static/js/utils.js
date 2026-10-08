/**
 * utils.js — 通用工具函数
 * 纯函数集合：DOM 构造、批处理、格式化、对象路径读写。
 * 不依赖任何其它模块，供全部视图复用。
 */

/**
 * 创建 DOM 元素。
 * @param {string} tag 标签名
 * @param {Object} [attrs] 属性集合：
 *   - class / className：字符串
 *   - text：textContent
 *   - html：innerHTML
 *   - dataset：对象，写入 data-*
 *   - style：对象，写入内联样式
 *   - attrs：对象，setAttribute
 *   - onEvent：函数，addEventListener（scroll/wheel/touchmove 自动 passive）
 * @param {Array|Node|string} [children] 子节点
 * @returns {HTMLElement}
 */
export function h(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const key of Object.keys(attrs)) {
    const val = attrs[key];
    if (val === null || val === undefined || val === false) continue;
    if (key === 'class' || key === 'className') {
      node.className = val;
    } else if (key === 'text') {
      node.textContent = String(val);
    } else if (key === 'html') {
      node.innerHTML = val;
    } else if (key === 'dataset') {
      Object.assign(node.dataset, val);
    } else if (key === 'style' && typeof val === 'object') {
      Object.assign(node.style, val);
    } else if (key === 'attrs') {
      for (const a of Object.keys(val)) node.setAttribute(a, val[a]);
    } else if (key.startsWith('on') && typeof val === 'function') {
      const evt = key.slice(2).toLowerCase();
      const opts = (evt === 'scroll' || evt === 'wheel' || evt === 'touchmove')
        ? { passive: true }
        : undefined;
      node.addEventListener(evt, val, opts);
    } else {
      node.setAttribute(key, val);
    }
  }
  append(node, children);
  return node;
}

/**
 * 追加子节点到父节点。
 * @param {Node} parent
 * @param {Array|Node|string} children
 * @returns {Node}
 */
export function append(parent, children) {
  if (children === null || children === undefined || children === false) return parent;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }
  return parent;
}

/** 清空节点内容 */
export function clear(node) {
  if (node) node.textContent = '';
}

/** querySelector 简写 */
export function $(sel, root = document) {
  return root.querySelector(sel);
}

/** querySelectorAll → 数组 */
export function $$(sel, root = document) {
  return Array.prototype.slice.call(root.querySelectorAll(sel));
}

/**
 * 防抖：等待 wait 毫秒无新调用后执行。
 * 返回的函数带 .cancel()。
 */
export function debounce(fn, wait = 300) {
  let timer = 0;
  function wrapped(...args) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = 0;
      fn.apply(this, args);
    }, wait);
  }
  wrapped.cancel = () => {
    clearTimeout(timer);
    timer = 0;
  };
  return wrapped;
}

/**
 * requestAnimationFrame 合并：同一帧内多次调用只执行一次（末次参数）。
 */
export function rafBatch(fn) {
  let queued = false;
  let lastArgs = null;
  function run() {
    queued = false;
    const args = lastArgs;
    lastArgs = null;
    fn(args);
  }
  return function schedule(...args) {
    lastArgs = args;
    if (queued) return;
    queued = true;
    requestAnimationFrame(run);
  };
}

/** 数字补零 */
export function pad(n, len = 2) {
  return String(n).padStart(len, '0');
}

/** 毫秒 → mm:ss 或 h:mm:ss */
export function formatDuration(ms) {
  let value = (typeof ms === 'number' && isFinite(ms) && ms > 0) ? ms : 0;
  const total = Math.floor(value / 1000);
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  return hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`;
}

/** 时间戳 → HH:MM:SS.mmm */
export function formatTime(ts) {
  const d = new Date(ts || Date.now());
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/** 毫秒 → 秒（保留 1 位小数） */
export function formatSeconds(ms) {
  return (Math.max(0, ms || 0) / 1000).toFixed(1);
}

/** 数值钳制 */
export function clamp(v, min, max) {
  return Math.min(max, Math.max(min, v));
}

/** 读取嵌套路径；不存在时返回 fallback */
export function getPath(obj, path, fallback) {
  if (obj === null || obj === undefined) return fallback;
  const parts = String(path).split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur === null || cur === undefined || typeof cur !== 'object') return fallback;
    cur = cur[p];
  }
  return cur === undefined ? fallback : cur;
}

/** 写入嵌套路径（沿途浅拷贝，返回新对象，不修改原对象） */
export function setPath(obj, path, value) {
  const parts = String(path).split('.');
  const root = (obj && typeof obj === 'object' && !Array.isArray(obj)) ? Object.assign({}, obj) : {};
  let cur = root;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = parts[i];
    const next = cur[p];
    cur[p] = (next && typeof next === 'object') ? Object.assign({}, next) : {};
    cur = cur[p];
  }
  cur[parts[parts.length - 1]] = value;
  return root;
}

/** 深拷贝（优先 structuredClone，回退 JSON） */
export function deepClone(obj) {
  if (typeof structuredClone === 'function') {
    try {
      return structuredClone(obj);
    } catch (e) {
      /* 回退到 JSON 方式 */
    }
  }
  return JSON.parse(JSON.stringify(obj));
}

/** 转义 HTML 特殊字符 */
export function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]));
}
