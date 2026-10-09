/**
 * api-client.js — 后端接入契约层
 *
 * 职责：
 *  - fetch 封装（base URL 注入、JSON 解析、超时、错误统一映射）
 *  - 预留端点封装：POST /api/tasks、GET /api/logs、POST /api/review/{id}、
 *    GET /api/config、PUT /api/config
 *  - subscribeEvents：SSE 优先 / 失败自动降级到内置 mock 模拟器
 *
 * base URL 解析顺序：window.__VAIR_API__ → 同源 '/api'
 */

const DEFAULT_TIMEOUT = 15000;

/** 解析后端 base URL */
export function resolveBaseUrl() {
  const injected = window.__VAIR_API__;
  if (typeof injected === 'string' && injected.trim()) {
    return injected.trim().replace(/\/+$/, '');
  }
  return '/api';
}

/** 统一 API 错误 */
export class ApiError extends Error {
  constructor(message, { status = 0, path = '', cause = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.path = path;
    this.cause = cause;
  }
}

/** HTTP 客户端 */
export class ApiClient {
  constructor(baseUrl) {
    this.baseUrl = (baseUrl || resolveBaseUrl());
  }

  _url(path) {
    if (/^https?:\/\//i.test(path)) return path;
    return `${this.baseUrl}${path}`;
  }

  /**
   * 通用请求。
   * @param {string} method
   * @param {string} path
   * @param {*} [body]
   * @param {{timeout?:number}} [options]
   * @returns {Promise<*>}
   */
  async request(method, path, body, options = {}) {
    const timeout = typeof options.timeout === 'number' ? options.timeout : DEFAULT_TIMEOUT;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const init = {
        method,
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      };
      if (body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(body);
      }
      const res = await fetch(this._url(path), init);
      const text = await res.text();
      let data = null;
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = text;
        }
      }
      if (!res.ok) {
        const detail = (data && typeof data === 'object' && (data.detail || data.message)) || `HTTP ${res.status}`;
        throw new ApiError(String(detail), { status: res.status, path });
      }
      return data;
    } catch (err) {
      if (err && err.name === 'AbortError') {
        throw new ApiError('请求超时，请检查后端服务', { status: 0, path });
      }
      if (err instanceof ApiError) throw err;
      throw new ApiError((err && err.message) || '网络错误', { status: 0, path, cause: err });
    } finally {
      clearTimeout(timer);
    }
  }

  get(path, options) {
    return this.request('GET', path, undefined, options);
  }

  post(path, body, options) {
    return this.request('POST', path, body, options);
  }

  put(path, body, options) {
    return this.request('PUT', path, body, options);
  }

  // ---------- 契约端点封装 ----------

  /** 创建任务 @param {{path:string, [key:string]:*}} payload */
  createTask(payload) {
    return this.post('/tasks', payload);
  }

  /** 查询历史日志 @param {Object} params */
  getLogs(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return this.get(`/logs${qs ? `?${qs}` : ''}`);
  }

  /** 提交审批决定 @param {string} id @param {{decision:string, newName?:string}} body */
  submitReview(id, body) {
    return this.post(`/review/${encodeURIComponent(id)}`, body);
  }

  /** 读取配置 */
  getConfig() {
    return this.get('/config');
  }

  /** 保存配置 */
  putConfig(config) {
    return this.put('/config', config);
  }

  // ---------- 系统原生选择器 ----------
  // 浏览器拿不到绝对路径，因此交给服务端弹原生对话框，这里用超长超时等待用户点选。

  /** 选择视频文件（可多选） @param {{initial_dir?:string, multi?:boolean}} [payload] */
  pickFiles(payload = {}) {
    return this.post('/dialog/files', payload, { timeout: 900000 });
  }

  /** 选择文件夹 @param {{initial_dir?:string}} [payload] */
  pickFolder(payload = {}) {
    return this.post('/dialog/folder', payload, { timeout: 900000 });
  }

  /** 取消挂起的原生选择器（解决「看不到窗口却提示已打开」） */
  cancelDialog() {
    return this.post('/dialog/cancel', {}, { timeout: 8000 });
  }

  /** 查询原生选择器当前状态 */
  dialogStatus() {
    return this.get('/dialog/status', { timeout: 8000 });
  }

  // ---------- AI 连接测试 / 转写环境 ----------

  /** 测试 AI 后端连通性并识别模型 @param {Object} [payload] */
  testAI(payload = {}) {
    return this.post('/ai/test', payload, { timeout: 90000 });
  }

  /** 查询转写运行环境（faster-whisper / CUDA 运行库 / GPU 开关） */
  whisperStatus() {
    return this.get('/whisper/status');
  }

  /** 下载 / 修复 CUDA 运行库（较慢，放宽超时） */
  setupWhisper() {
    return this.post('/whisper/setup', {}, { timeout: 1800000 });
  }

  /** 安装 faster-whisper 依赖（走配置的 pip 镜像） */
  installWhisper() {
    return this.post('/whisper/install', {}, { timeout: 1800000 });
  }

  /** 读取下载镜像预设与当前配置 */
  getMirrors() {
    return this.get('/mirrors');
  }

  /** 查询服务监听方式（本机 / 局域网地址） */
  serverInfo() {
    return this.get('/server');
  }
}

/** 单例客户端 */
export const apiClient = new ApiClient();

/**
 * 订阅事件流：优先 SSE，失败自动降级到内置 mock 模拟器。
 * mock 模拟器用动态 import 加载，保证首屏 JS 体积更小。
 *
 * @param {(evt:Object)=>void} onEvent 事件回调
 * @param {(status:{mode:string, connected:boolean, error?:string})=>void} [onStatus]
 * @returns {{ stop:()=>void, forceMock:(reason?:string)=>void, isMock:()=>boolean }}
 */
export function subscribeEvents(onEvent, onStatus = () => {}) {
  let es = null;
  let stopped = false;
  let opened = false;
  let mock = null;

  function startMock(reason) {
    if (mock || stopped) {
      if (!mock && !stopped) onStatus({ mode: 'offline', connected: false, error: reason || '' });
      return;
    }
    onStatus({ mode: 'mock', connected: true, error: reason || '' });
    import('./simulator.js')
      .then(({ createSimulator }) => {
        if (stopped) return;
        mock = createSimulator(onEvent, onStatus);
        mock.start();
      })
      .catch((err) => {
        onStatus({ mode: 'offline', connected: false, error: String(err) });
      });
  }

  if (typeof EventSource === 'undefined') {
    onStatus({ mode: 'mock', connected: true, error: '当前环境不支持 SSE' });
    startMock('当前环境不支持 SSE');
  } else {
    onStatus({ mode: 'connecting', connected: false });
    try {
      es = new EventSource(`${apiClient.baseUrl}/events`);
      es.addEventListener('open', () => {
        opened = true;
        onStatus({ mode: 'sse', connected: true });
      });
      es.addEventListener('message', (e) => {
        let data = null;
        try {
          data = JSON.parse(e.data);
        } catch (err) {
          return; // 忽略非 JSON 心跳
        }
        onEvent(data);
      });
      es.addEventListener('error', () => {
        if (stopped) return;
        if (!opened) {
          // 从未成功连接 → 判定后端不可用，降级到 mock
          try {
            es.close();
          } catch (e) {
            /* noop */
          }
          es = null;
          startMock('后端未连接，已启用本地模拟数据');
        } else {
          // 连接过但中断 → 交由 EventSource 自动重连
          onStatus({ mode: 'sse', connected: false, error: 'SSE 中断，自动重连中' });
        }
      });
    } catch (err) {
      startMock(String(err));
    }
  }

  return {
    stop() {
      stopped = true;
      if (es) {
        try {
          es.close();
        } catch (e) {
          /* noop */
        }
        es = null;
      }
      if (mock) {
        mock.stop();
        mock = null;
      }
    },
    forceMock(reason = '已手动切换到模拟数据') {
      stopped = false;
      if (es) {
        try {
          es.close();
        } catch (e) {
          /* noop */
        }
        es = null;
      }
      if (mock) {
        mock.stop();
        mock = null;
      }
      startMock(reason);
    },
    isMock() {
      return !!mock;
    },
  };
}
