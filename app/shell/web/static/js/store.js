/**
 * store.js — 轻量发布/订阅状态中心 + 环形日志缓冲 + 默认配置
 *
 * 性能要点：
 *  - 状态对象原地更新（Object.assign），避免每帧产生大对象；
 *  - 通知用 requestAnimationFrame 合并，同一帧内多次变更只渲染一次；
 *  - 日志用环形缓冲（上限 500），DOM 侧配合虚拟滚动，节点数恒定。
 */
import { deepClone } from './utils.js';

/** 日志分级 */
export const LOG_LEVELS = Object.freeze({
  DEBUG: 'DEBUG',
  INFO: 'INFO',
  WARN: 'WARN',
  ERROR: 'ERROR',
});

/** 日志级别顺序（用于过滤） */
export const LOG_LEVEL_ORDER = Object.freeze({
  DEBUG: 0,
  INFO: 1,
  WARN: 2,
  ERROR: 3,
});

/** 日志环形缓冲上限 */
export const MAX_LOGS = 500;

/** 单文件推理时间线保留条数 */
export const MAX_INFERENCE = 50;

/** 审批列表保留条数 */
export const MAX_REVIEW = 200;

/**
 * 环形缓冲：固定容量，满时覆盖最旧元素，读写 O(1)，无数组搬迁。
 */
export class RingBuffer {
  constructor(capacity = MAX_LOGS) {
    this.capacity = Math.max(1, capacity | 0);
    this._buf = new Array(this.capacity);
    this._start = 0; // 最旧元素下标
    this._size = 0;
  }

  get size() {
    return this._size;
  }

  push(item) {
    const idx = (this._start + this._size) % this.capacity;
    this._buf[idx] = item;
    if (this._size < this.capacity) {
      this._size++;
    } else {
      this._start = (this._start + 1) % this.capacity;
    }
  }

  at(i) {
    if (i < 0 || i >= this._size) return undefined;
    return this._buf[(this._start + i) % this.capacity];
  }

  toArray() {
    const out = new Array(this._size);
    for (let i = 0; i < this._size; i++) out[i] = this.at(i);
    return out;
  }

  clear() {
    this._buf = new Array(this.capacity);
    this._start = 0;
    this._size = 0;
  }
}

/**
 * 默认配置，严格对齐方案 §7.2。
 * 说明：§7.1 开关表还要求 `input.recursive`（递归）与 `whisper.vad_filter`（VAD 过滤），
 * 但 §7.2 骨架未列出这两个键，此处按开关表补齐（最小扩展，已在 README 中标注）。
 */
export const DEFAULT_CONFIG = Object.freeze({
  ai: {
    provider: 'openai',
    base_url: 'http://127.0.0.1:8094/v1',
    api_key: '',
    model: 'qwen3.8-27b',
    timeout: 60,
    retry_times: 2,
    max_tokens: 5000,
    temperature: 0.6,
    top_p: 0.8,
    enforce_json_mode: false,
  },
  frames: { max_keyframes: 35, max_side: 520, workers: 8, hwaccel: 'none' },
  whisper: {
    enable: true,
    model: 'large-v3-turbo',
    device: 'auto',
    compute_type: 'int8_float16',
    workers: 1,
    vad_filter: true,
    language: 'auto',
    use_gpu: true,
  },
  naming: {
    template: '{date}_{title}',
    date_format: '%Y%m%d_%H%M',
    include_date: true,
    include_original: false,
    marker: 'AI',
    enable_marker: true,
    enable_skip: false,
  },
  output: { nfo: true, metadata: true, srt: true, move_failed: true, dry_run: false },
  runtime: {
    ai_workers: 1,
    log_file: 'logs/run.log',
    verbose: false,
    auto_install_cuda: true,
  },
  input: { recursive: true },
});

/** 初始任务状态工厂 */
function initialTask() {
  return {
    id: null,
    running: false,
    phase: '0',
    currentFile: '',
    total: 0,
    done: 0,
    stats: { success: 0, skipped: 0, failed: 0, cancelled: 0 },
    avgSeconds: 0,
    startedAt: 0,
    finishedAt: 0,
  };
}

/**
 * 全局状态中心。
 * - set(patch)：合并补丁并合并渲染（rAF）
 * - touch()：无补丁，仅触发一次合并渲染
 * - subscribe(fn)：订阅整状态变更（rAF 合并）
 * - on(topic)/emit(topic)：面向高频话题的专用通道
 */
export class Store {
  constructor(initial = {}) {
    this._state = Object.assign(
      {
        connected: false,
        mode: 'connecting', // connecting | sse | mock | offline
        modeMessage: '',
        theme: 'dark',
        route: 'dashboard',
        sidebarOpen: false,
        task: initialTask(),
        phases: {}, // phaseKey -> { state, startedAt, elapsedMs }
        review: { items: [] },
        inference: [],
        config: deepClone(DEFAULT_CONFIG),
        configDirty: false,
        logBuffer: new RingBuffer(MAX_LOGS),
      },
      initial
    );
    this._listeners = new Set();
    this._topics = new Map();
    this._raf = 0;
  }

  /** 只读状态引用（约定：调用方不得直接改结构，需通过 set/touch） */
  get state() {
    return this._state;
  }

  get() {
    return this._state;
  }

  /** 合并补丁并调度一次渲染 */
  set(patch) {
    if (patch && typeof patch === 'object') Object.assign(this._state, patch);
    this._schedule();
  }

  /** 无补丁触发渲染（用于直接改嵌套对象的场景） */
  touch() {
    this._schedule();
  }

  _schedule() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      for (const fn of this._listeners) {
        try {
          fn(this._state);
        } catch (err) {
          console.error('[store] listener error', err);
        }
      }
    });
  }

  /** 订阅整状态变更；立即回调一次；返回取消函数 */
  subscribe(fn) {
    this._listeners.add(fn);
    fn(this._state);
    return () => {
      this._listeners.delete(fn);
    };
  }

  /** 订阅专用话题 */
  on(topic, fn) {
    let set = this._topics.get(topic);
    if (!set) {
      set = new Set();
      this._topics.set(topic, set);
    }
    set.add(fn);
    return () => {
      set.delete(fn);
    };
  }

  emit(topic, payload) {
    const set = this._topics.get(topic);
    if (!set) return;
    for (const fn of set) {
      try {
        fn(payload);
      } catch (err) {
        console.error('[store] topic error', topic, err);
      }
    }
  }

  /** 写入一条日志（环形缓冲 + 合并渲染） */
  pushLog(entry) {
    this._state.logBuffer.push(entry);
    this._schedule();
  }

  clearLogs() {
    this._state.logBuffer.clear();
    this._schedule();
  }

  /** 释放资源 */
  dispose() {
    if (this._raf) {
      cancelAnimationFrame(this._raf);
      this._raf = 0;
    }
    this._listeners.clear();
    this._topics.clear();
  }
}

/** 单例状态中心 */
export const store = new Store();
