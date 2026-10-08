/**
 * simulator.js — 内置 mock 流水线事件模拟器
 *
 * 当 SSE 后端不可用时启用，产生逼真的五阶段事件（扫描 → 探测 → 抽帧 ∥ 转写 →
 * 合并 → AI 分析 → 落盘），含随机文件名、耗时、日志分级、审批条目与推理时间线。
 * 单开页面即可演示全部动效。事件契约与后端 SSE 完全一致（见 README）。
 */
import { LOG_LEVELS } from './store.js';

/** 采样素材：真实感的原始文件名 + 可能的标题与标签 */
const SAMPLE_FILES = [
  { old: 'VID_20240112_143210.mp4', title: '无人机航拍城市夜景', tags: ['航拍', '夜景', '城市'] },
  { old: 'DJI_0087.MP4', title: '海边日出延时摄影', tags: ['航拍', '自然', '延时'] },
  { old: '20240315_产品发布会_原片.mov', title: '新品发布会现场记录', tags: ['产品', '直播', '人物'] },
  { old: '录屏 2024-04-02 21.33.10.mp4', title: '软件操作教程演示', tags: ['教程', '科技', '录屏'] },
  { old: 'gx010234.mp4', title: '周末露营篝火晚会', tags: ['旅行', 'Vlog', '人物'] },
  { old: 'IMG_4521.MOV', title: '宠物猫的日常玩耍', tags: ['宠物', '生活', '可爱'] },
  { old: '海滩日落 4K.mp4', title: '日落时分的海滩风光', tags: ['自然', '旅行', '日落'] },
  { old: 'meeting_record_final.mkv', title: '项目周会讨论纪要', tags: ['人物', '会议', '记录'] },
  { old: '学习笔记-线性代数第3讲.mp4', title: '线性代数矩阵运算讲解', tags: ['教程', '学习', '科技'] },
  { old: '厨房做菜 番茄炒蛋.mp4', title: '家常番茄炒蛋做法', tags: ['美食', '教程', '生活'] },
  { old: 'gameplay_rank_match.mp4', title: '排位赛精彩集锦', tags: ['游戏', '集锦', '运动'] },
  { old: '直播回放 带货专场.mp4', title: '商品直播带货回放', tags: ['直播', '产品', '人物'] },
];

const AI_MODELS = ['qwen3.8-27b', 'Qwen2.5-VL-7B-Instruct', 'MiniCPM-V-2_6', 'llava-v1.6-34b'];

function rand(min, max) {
  return min + Math.random() * (max - min);
}

function randInt(min, max) {
  return Math.floor(rand(min, max + 1));
}

function pick(arr) {
  return arr[randInt(0, arr.length - 1)];
}

function pad2(n) {
  return String(n).padStart(2, '0');
}

/** 依据标题与时间生成新文件名（模拟 {date}_{title} 模板） */
function makeNewName(title, ts) {
  const d = new Date(ts);
  const stamp = `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}_${pad2(d.getHours())}${pad2(d.getMinutes())}`;
  return `${stamp}_${title}_AI.mp4`;
}

/**
 * 创建模拟器。
 * @param {(evt:Object)=>void} onEvent
 * @param {(status:Object)=>void} [onStatus]
 * @returns {{ start:()=>void, stop:()=>void }}
 */
export function createSimulator(onEvent, onStatus = () => {}) {
  let stopped = false;
  let timer = 0;
  let batch = null;

  function emit(evt) {
    onEvent(Object.assign({ ts: Date.now() }, evt));
  }

  function log(level, message, extra) {
    emit(Object.assign({ type: 'log', level, message }, extra || {}));
  }

  function after(ms, fn) {
    timer = setTimeout(() => {
      if (!stopped) fn();
    }, ms);
  }

  function emitProgress(phase, currentFile) {
    const stats = Object.assign({}, batch.stats);
    const done = stats.success + stats.skipped + stats.failed + stats.cancelled;
    const avg = batch.durations.length
      ? batch.durations.reduce((a, b) => a + b, 0) / batch.durations.length / 1000
      : 0;
    emit({
      type: 'progress',
      taskId: batch.id,
      phase,
      currentFile: currentFile || '',
      done,
      total: batch.total,
      stats,
      avgSeconds: Number(avg.toFixed(1)),
    });
  }

  function startBatch() {
    if (stopped) return;
    const total = randInt(4, 8);
    batch = {
      id: `mock-${Date.now().toString(36)}`,
      total,
      index: 0,
      stats: { success: 0, skipped: 0, failed: 0, cancelled: 0 },
      durations: [],
    };
    log(LOG_LEVELS.INFO, `扫描到 ${total} 个视频，并发配置：抽帧 8 / 转写 1 / AI 1`);
    emit({ type: 'task', action: 'started', taskId: batch.id, total });
    emitProgress('1', '');
    after(500, nextFile);
  }

  function nextFile() {
    if (stopped) return;
    if (batch.index >= batch.total) {
      emit({ type: 'task', action: 'completed', taskId: batch.id });
      log(
        LOG_LEVELS.INFO,
        `本批完成：成功 ${batch.stats.success} · 跳过 ${batch.stats.skipped} · 失败 ${batch.stats.failed}`
      );
      after(randInt(2600, 4200), startBatch);
      return;
    }

    const sample = pick(SAMPLE_FILES);
    const startedAt = Date.now();
    const file = {
      oldName: sample.old,
      title: sample.title,
      tags: sample.tags.slice(),
      startedAt,
      frames: randInt(12, 35),
      chars: Math.random() < 0.15 ? 0 : randInt(40, 520),
    };
    batch.index++;

    // 约 12% 概率：跳过已处理
    if (Math.random() < 0.12) {
      log(LOG_LEVELS.INFO, `跳过已处理：${file.oldName}`, { file: file.oldName });
      batch.stats.skipped++;
      emitProgress('1', file.oldName);
      after(randInt(250, 500), nextFile);
      return;
    }

    log(LOG_LEVELS.DEBUG, `探测视频：${file.oldName}`, { file: file.oldName });
    runPipeline(file);
  }

  function runPipeline(file) {
    const silent = file.chars === 0; // 无声视频：跳过转写
    const failAt = Math.random() < 0.08 ? '4' : null; // 约 8% 在 AI 阶段失败
    const seq = [
      { key: '1', label: '探测', ms: randInt(250, 520) },
      { key: '2a', label: '抽帧', ms: randInt(420, 900) },
      { key: '2b', label: '转写', ms: silent ? 0 : randInt(520, 1100), skip: silent },
      { key: '3', label: '合并', ms: randInt(120, 260) },
      { key: '4', label: 'AI 分析', ms: randInt(620, 1400) },
      { key: '5', label: '落盘', ms: randInt(220, 460) },
    ];

    let i = 0;

    function finish() {
      const elapsedMs = Date.now() - file.startedAt;
      const newName = makeNewName(file.title, file.startedAt);
      emit({
        type: 'inference',
        file: newName,
        model: pick(AI_MODELS),
        frames: file.frames,
        subtitleChars: file.chars,
        elapsedMs,
      });
      emit({
        type: 'review',
        action: 'add',
        item: {
          id: `r_${Math.random().toString(36).slice(2, 9)}`,
          oldName: file.oldName,
          newName,
          confidence: Number(rand(0.72, 0.98).toFixed(2)),
          tags: file.tags,
          file: file.oldName,
        },
      });
      log(LOG_LEVELS.INFO, `重命名：${file.oldName} → ${newName}`, { file: file.oldName });
      batch.stats.success++;
      batch.durations.push(elapsedMs);
      emitProgress('5', file.oldName);
      after(randInt(320, 760), nextFile);
    }

    function fail() {
      log(LOG_LEVELS.ERROR, `AI 分析失败：${file.oldName} —— 响应无内容，已计入失败`, {
        file: file.oldName,
      });
      batch.stats.failed++;
      emitProgress('4', file.oldName);
      after(randInt(320, 700), nextFile);
    }

    function step() {
      if (stopped) return;
      if (i >= seq.length) {
        finish();
        return;
      }
      const p = seq[i++];

      if (p.skip) {
        emit({ type: 'phase', phase: p.key, state: 'skip', file: file.oldName, elapsedMs: 0 });
        log(LOG_LEVELS.DEBUG, `无声视频，跳过转写：${file.oldName}`, { file: file.oldName });
        step();
        return;
      }

      emit({ type: 'phase', phase: p.key, state: 'start' });
      emitProgress(p.key, file.oldName);
      if (p.key === '4') {
        log(LOG_LEVELS.DEBUG, `AI 推理开始：模型 ${pick(AI_MODELS)} · ${file.frames} 帧`, {
          file: file.oldName,
        });
      }
      const phaseStart = Date.now();

      after(p.ms, () => {
        const elapsed = Date.now() - phaseStart;
        emit({ type: 'phase', phase: p.key, state: 'done', file: file.oldName, elapsedMs: elapsed });

        // 阶段特定日志
        if (p.key === '1') {
          log(LOG_LEVELS.DEBUG, `探测结果：时长 ${randInt(40, 320)}s · ${silent ? '无音轨' : '含音轨'} · 1080p`, {
            file: file.oldName,
          });
        } else if (p.key === '2a') {
          log(LOG_LEVELS.DEBUG, `抽取关键帧 ${file.frames} 张`, { file: file.oldName });
        } else if (p.key === '2b') {
          log(LOG_LEVELS.DEBUG, `转写完成：字幕 ${file.chars} 字`, { file: file.oldName });
        } else if (p.key === '4' && Math.random() < 0.3) {
          log(LOG_LEVELS.WARN, `提示：关键帧较多可能触发上下文溢出，可降低 max_keyframes`, {
            file: file.oldName,
          });
        }

        if (failAt && p.key === failAt) {
          fail();
          return;
        }
        step();
      });
    }

    step();
  }

  function start() {
    if (stopped) return;
    onStatus({ mode: 'mock', connected: true, error: '后端未连接，已启用本地模拟数据' });
    log(LOG_LEVELS.INFO, 'VAIR Web 外壳已就绪（模拟模式）');
    log(LOG_LEVELS.DEBUG, 'ffmpeg: bin/ffmpeg.exe · ffprobe: bin/ffprobe.exe · exiftool: bin/exiftool.exe');
    log(LOG_LEVELS.INFO, 'Whisper 设备：cuda · large-v3-turbo · compute_type=int8_float16');
    log(LOG_LEVELS.INFO, 'AI 后端：openai 兼容 · http://127.0.0.1:8094/v1 · model=qwen3.8-27b');
    after(450, startBatch);
  }

  function stop() {
    stopped = true;
    if (timer) {
      clearTimeout(timer);
      timer = 0;
    }
  }

  return { start, stop };
}
