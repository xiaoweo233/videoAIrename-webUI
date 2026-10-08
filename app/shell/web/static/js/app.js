/**
 * app.js — 应用入口 / 路由 / 事件泵 / 主题 / 全局 UI 编排
 *
 * 职责：
 *  - 初始化状态中心与主题
 *  - 订阅事件流（SSE → mock 自动降级），把事件翻译成 store 变更
 *  - 极简 hash 路由 + 视图挂载/销毁（切换时释放监听器，防内存泄漏）
 *  - 顶栏连接状态、导航滑动指示器、移动端抽屉、Toast
 */
import { store, LOG_LEVELS, MAX_INFERENCE, MAX_REVIEW } from './store.js';
import { apiClient, subscribeEvents } from './api-client.js';
import { createView as createDashboardView } from './views/dashboard.js';
import { createView as createLiveView } from './views/live.js';
import { createView as createReviewView } from './views/review.js';
import { createView as createLogsView } from './views/logs.js';
import { createView as createSettingsView } from './views/settings.js';
import { h, $$, debounce } from './utils.js';

const ROUTES = ['dashboard', 'live', 'review', 'logs', 'settings'];
const TITLES = {
  dashboard: '工作台',
  live: '实时进度',
  review: '命名审批',
  logs: '运行日志',
  settings: '设置',
};
const VIEW_CREATORS = {
  dashboard: createDashboardView,
  live: createLiveView,
  review: createReviewView,
  logs: createLogsView,
  settings: createSettingsView,
};

// ---------- DOM 引用 ----------
const viewRoot = document.getElementById('view-root');
const navIndicator = document.getElementById('nav-indicator');
const toastRoot = document.getElementById('toast-root');
const bootBar = document.getElementById('boot-bar');

// ---------- 配置持久化（debounce，改动即存） ----------
// 说明：配置的「真源」在服务端 config.json，前端每次改动都会 PUT 回去，
// 因此下次打开仍在。为了不丢失「刚改完就关页面」的那一次改动，
// 这里额外记录待保存配置，并在页面隐藏/关闭时用 keepalive 冲出。
let pendingConfig = null;

function sendConfig(config, keepalive) {
  if (keepalive && typeof fetch === 'function') {
    try {
      fetch(`${apiClient.baseUrl}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(config),
        keepalive: true,
      }).catch(() => {
        /* 页面正在卸载，失败也无能为力 */
      });
      return;
    } catch (e) {
      /* 回退到常规请求 */
    }
  }
  apiClient.putConfig(config).catch(() => {
    /* 后端不可用时静默：配置仍保存在本地状态 */
  });
}

const persistConfig = debounce((config) => {
  pendingConfig = null;
  store.set({ configDirty: false });
  sendConfig(config, false);
}, 700);

/** 排队保存（视图统一入口） */
function queueConfigSave(config) {
  pendingConfig = config;
  persistConfig(config);
}

/** 页面隐藏/关闭时把待保存配置冲出去 */
function flushPendingConfig() {
  if (!pendingConfig) return;
  const cfg = pendingConfig;
  pendingConfig = null;
  persistConfig.cancel();
  sendConfig(cfg, true);
}

// ---------- Toast ----------
/**
 * 弹出一条提示。
 * @param {string} message
 * @param {'info'|'ok'|'warn'|'err'} [type]
 * @param {number} [timeout]
 */
function toast(message, type = 'info', timeout = 2600) {
  const node = h('div', { class: `toast toast-${type}`, role: 'status' }, [
    h('span', { class: 'toast-dot' }),
    h('span', { class: 'toast-text', text: message }),
  ]);
  toastRoot.appendChild(node);
  requestAnimationFrame(() => node.classList.add('is-in'));
  setTimeout(() => {
    node.classList.remove('is-in');
    setTimeout(() => node.remove(), 320);
  }, timeout);
}

// ---------- 路由 ----------
let currentView = null;
let currentRoute = '';

function parseHash() {
  const m = location.hash.match(/^#\/?([a-z]+)/i);
  const r = m ? m[1].toLowerCase() : '';
  return ROUTES.includes(r) ? r : 'dashboard';
}

const router = {
  navigate(route) {
    const target = ROUTES.includes(route) ? route : 'dashboard';
    if (parseHash() !== target || !location.hash) {
      location.hash = `#/${target}`;
    } else {
      renderRoute(target);
    }
  },
  current() {
    return currentRoute;
  },
};

// ---------- 事件流控制（供视图调用） ----------
let stream = null;

function ensureStream() {
  if (!stream) {
    stream = subscribeEvents(handleEvent, handleStatus);
  }
  return stream;
}

const control = {
  /**
   * 创建任务：优先调用后端；后端不可用时确保模拟器在跑。
   * @param {{path:string, [key:string]:*}} payload
   */
  async start(payload) {
    try {
      const result = await apiClient.createTask(payload);
      toast('任务已创建', 'ok');
      return result;
    } catch (err) {
      toast('后端未连接，已用本地模拟数据演示', 'warn', 3200);
      if (!stream || !stream.isMock()) {
        if (stream) stream.stop();
        stream = null;
        ensureStream();
      }
      return null;
    }
  },
  /** 停止任务 */
  stop() {
    const id = store.state.task.id || 'current';
    apiClient.request('POST', `/tasks/${encodeURIComponent(id)}/stop`).catch(() => {});
    if (stream && stream.isMock()) {
      // mock 模式下停止事件泵以模拟“停止”
      stream.stop();
      stream = null;
    }
    store.set({
      task: Object.assign({}, store.state.task, { running: false, currentFile: '' }),
    });
    toast('已发送停止指令（已完成文件将保留）', 'info', 3000);
  },
  /** 重新接通事件流 */
  reconnect() {
    if (stream) stream.stop();
    stream = null;
    ensureStream();
    toast('正在重新连接数据源…', 'info');
  },
};

const ctx = { store, apiClient, toast, router, persistConfig: queueConfigSave, control };

// ---------- 视图挂载 ----------
function renderRoute(route, { force = false } = {}) {
  if (!force && route === currentRoute && currentView) return;
  if (currentView && typeof currentView.destroy === 'function') currentView.destroy();
  currentView = null;
  currentRoute = route;
  store.set({ route });

  const creator = VIEW_CREATORS[route] || VIEW_CREATORS.dashboard;
  currentView = creator(ctx);

  viewRoot.classList.remove('is-entering');
  viewRoot.textContent = '';
  currentView.mount(viewRoot);
  void viewRoot.offsetWidth; // 强制重排，重启动入动画
  viewRoot.classList.add('is-entering');

  updateNav(route);
}

// ---------- 导航联动 ----------
function updateNav(route) {
  const items = $$('.nav-item');
  const bottomItems = $$('.bottomnav-item');
  for (const el of items) el.classList.toggle('is-active', el.dataset.route === route);
  for (const el of bottomItems) el.classList.toggle('is-active', el.dataset.route === route);

  const active = items.find((el) => el.dataset.route === route);
  if (active && navIndicator && window.innerWidth > 760) {
    navIndicator.style.transform = `translateY(${active.offsetTop}px)`;
  }
  document.title = `${TITLES[route] || 'VAIR'} · 视频 AI 重命名助手`;
}

// ---------- 主题 ----------
function applyTheme(theme) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  store.set({ theme: next });
  try {
    localStorage.setItem('vair.theme', next);
  } catch (e) {
    /* 忽略隐私模式下的存储异常 */
  }
}

// ---------- 移动端抽屉 ----------
function setSidebar(open) {
  store.set({ sidebarOpen: open });
  document.body.classList.toggle('nav-open', open);
  const btn = document.getElementById('menu-toggle');
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

// ---------- 事件处理 ----------
function normalizeLog(evt) {
  return {
    ts: evt.ts || Date.now(),
    level: evt.level || LOG_LEVELS.INFO,
    message: String(evt.message || ''),
    file: evt.file || '',
    source: evt.source || '',
  };
}

function handleReview(evt) {
  if (evt.action === 'clear') {
    store.set({ review: { items: [] } });
    return;
  }
  const items = store.state.review.items.slice();
  if (evt.item) {
    const idx = items.findIndex((x) => x.id === evt.item.id);
    if (idx >= 0) items[idx] = Object.assign({}, items[idx], evt.item);
    else items.push(Object.assign({ status: 'pending', confidence: 0.85, tags: [] }, evt.item));
  } else if (Array.isArray(evt.items)) {
    items.length = 0;
    for (const it of evt.items) items.push(Object.assign({ status: 'pending' }, it));
  }
  while (items.length > MAX_REVIEW) items.shift();
  store.set({ review: { items } });
}

const handlers = {
  status(evt) {
    store.set({ connected: !!evt.connected, mode: evt.mode || store.state.mode });
  },
  task(evt) {
    const t = store.state.task;
    if (evt.action === 'started') {
      store.state.phases = {};
      store.set({
        task: Object.assign({}, t, {
          id: evt.taskId || t.id,
          running: true,
          total: evt.total || 0,
          done: 0,
          phase: '1',
          startedAt: Date.now(),
          finishedAt: 0,
          stats: { success: 0, skipped: 0, failed: 0, cancelled: 0 },
        }),
      });
    } else if (evt.action === 'completed') {
      store.set({
        task: Object.assign({}, store.state.task, {
          running: false,
          currentFile: '',
          finishedAt: Date.now(),
        }),
      });
    } else if (evt.action === 'stopped') {
      store.set({
        task: Object.assign({}, store.state.task, { running: false, currentFile: '' }),
      });
    }
  },
  progress(evt) {
    const t = store.state.task;
    store.set({
      task: Object.assign({}, t, {
        running: true,
        phase: evt.phase || t.phase,
        currentFile: evt.currentFile || '',
        total: typeof evt.total === 'number' ? evt.total : t.total,
        done: typeof evt.done === 'number' ? evt.done : t.done,
        stats: evt.stats ? Object.assign({}, t.stats, evt.stats) : t.stats,
        avgSeconds: typeof evt.avgSeconds === 'number' ? evt.avgSeconds : t.avgSeconds,
      }),
    });
  },
  phase(evt) {
    const key = String(evt.phase);
    store.state.phases[key] = {
      state: evt.state,
      startedAt: evt.state === 'start' ? Date.now() : 0,
      elapsedMs: evt.elapsedMs || 0,
    };
    store.touch();
  },
  log(evt) {
    store.pushLog(normalizeLog(evt));
  },
  review(evt) {
    handleReview(evt);
  },
  inference(evt) {
    const list = store.state.inference.slice();
    list.push({
      ts: evt.ts || Date.now(),
      file: evt.file || '',
      model: evt.model || '',
      frames: evt.frames || 0,
      subtitleChars: evt.subtitleChars || 0,
      elapsedMs: evt.elapsedMs || 0,
    });
    while (list.length > MAX_INFERENCE) list.shift();
    store.set({ inference: list });
  },
};

function handleEvent(evt) {
  if (!evt || !evt.type) return;
  const fn = handlers[evt.type];
  if (!fn) return;
  try {
    fn(evt);
  } catch (err) {
    console.error('[event]', evt.type, err);
  }
}

let shownMockToast = false;
function handleStatus(status) {
  store.set({
    connected: !!status.connected,
    mode: status.mode || store.state.mode,
    modeMessage: status.error || '',
  });
  renderStatusPill();
  if (status.mode === 'mock' && !shownMockToast) {
    shownMockToast = true;
    toast('未连接后端，已启用本地模拟数据', 'warn', 3400);
  }
}

function renderStatusPill() {
  const pill = document.getElementById('conn-status');
  if (!pill) return;
  const textEl = pill.querySelector('.conn-text');
  const map = {
    sse: { text: '已连接后端', cls: 'is-ok' },
    mock: { text: '模拟数据', cls: 'is-mock' },
    connecting: { text: '连接中…', cls: 'is-warn' },
    offline: { text: '离线', cls: 'is-err' },
  };
  const info = map[store.state.mode] || map.offline;
  pill.className = `conn-status ${info.cls}`;
  if (textEl) textEl.textContent = info.text;
}

// ---------- 初始化 ----------
function init() {
  // 主题
  let theme = 'dark';
  try {
    theme = localStorage.getItem('vair.theme') || 'dark';
  } catch (e) {
    theme = 'dark';
  }
  applyTheme(theme);

  document.getElementById('theme-toggle').addEventListener('click', () => {
    applyTheme(store.state.theme === 'dark' ? 'light' : 'dark');
  });

  document.getElementById('menu-toggle').addEventListener('click', () => {
    setSidebar(!store.state.sidebarOpen);
  });
  const scrim = document.getElementById('nav-scrim');
  if (scrim) {
    scrim.hidden = false;
    scrim.addEventListener('click', () => setSidebar(false));
  }
  // 点击导航项后关闭移动端抽屉
  for (const el of $$('.nav-item')) {
    el.addEventListener('click', () => {
      if (window.innerWidth <= 760) setSidebar(false);
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && store.state.sidebarOpen) setSidebar(false);
  });

  window.addEventListener('resize', debounce(() => updateNav(currentRoute), 150), {
    passive: true,
  });

  // 关页面/切后台时，把还没发出去的配置改动补发（keepalive）
  window.addEventListener('pagehide', flushPendingConfig);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flushPendingConfig();
  });

  // 配置引导：尝试拉取后端配置，失败则使用内置默认
  apiClient
    .getConfig()
    .then((cfg) => {
      if (cfg && typeof cfg === 'object' && !Array.isArray(cfg)) store.set({ config: cfg });
    })
    .catch(() => {
      /* 无后端时使用 DEFAULT_CONFIG */
    });

  // 事件流
  ensureStream();

  // 首屏路由
  if (!location.hash) location.hash = '#/dashboard';
  window.addEventListener('hashchange', () => renderRoute(parseHash()), { passive: true });
  renderRoute(parseHash(), { force: true });
  renderStatusPill();

  // 移除启动进度条
  if (bootBar) {
    bootBar.style.transition = 'opacity .3s ease';
    bootBar.style.opacity = '0';
    setTimeout(() => bootBar.remove(), 400);
  }
}

init();
