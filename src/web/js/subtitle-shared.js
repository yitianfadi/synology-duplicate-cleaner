/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 字幕渲染共享助手（v0057 从 app.js 拆出；结果页/任务面板共用）
   --------------------------------------------------------------------------
   行为与拆出前完全一致；依赖 js/ 下的基础模块，不依赖 app.js。ES2019。
   规范：不使用 innerHTML，不可信文本一律 textContent。
   ========================================================================== */

import {
  api, errorMessage
} from './api.js';
import {
  SUBTITLE_LANGUAGES, SUBTITLE_OUTPUT_KIND_LABEL, SUBTITLE_SOURCE_LABEL,
  SUBTITLE_STAGE_LABEL, SUBTITLE_STATUS_BADGE, SUBTITLE_STATUS_LABEL,
  SUBTITLE_TARGET_LANGUAGES, SUBTITLE_WARNING_LABEL
} from './constants.js';
import {
  el, setBusy
} from './dom.js';
import {
  fmtBytes, fmtInt
} from './format.js';
import {
  closeModal, confirmDialog
} from './modal.js';
import { state } from './state.js';
import { toast } from './toast.js';
import { encodePath } from './util.js';

export function subtitleCapability() {
  return state.subtitle || {
    supported: false, enabled: false, asrKeySet: false, llmKeySet: false,
    asrLanguage: 'auto', translateEnabled: false, targetLang: 'zh',
    writeVtt: false
  };
}

export function subtitleTargetLangLabel(value) {
  for (var i = 0; i < SUBTITLE_TARGET_LANGUAGES.length; i += 1) {
    if (SUBTITLE_TARGET_LANGUAGES[i].value === value) { return SUBTITLE_TARGET_LANGUAGES[i].label; }
  }
  return '中文';
}

/** 补翻译按钮（列表 action / 详情 footer 共用）：done 且缺译文时出现。 */

export function firstFiniteNumber(values) {
  for (var i = 0; i < values.length; i += 1) {
    var num = Number(values[i]);
    if (values[i] !== null && values[i] !== undefined && values[i] !== '' && isFinite(num)) {
      return num;
    }
  }
  return null;
}

export function ensureLanguageOption(select, value) {
  if (!select || !value) { return; }
  for (var i = 0; i < select.options.length; i += 1) {
    if (select.options[i].value === value) { return; }
  }
  select.appendChild(el('option', { attrs: { value: value }, text: value }));
}

export function subtitleJobOutputs(job) {
  var outputs = job && Array.isArray(job.outputs) ? job.outputs : [];
  return outputs.filter(function (item) {
    return item && typeof item.path === 'string' && item.path;
  });
}

/** 已写回视频目录的字幕列表（compact 时最多 3 条）；无写回返回 null。 */

export function subtitleShortJobId(jobId) {
  var id = String(jobId || '');
  if (!id) { return '—'; }
  if (id.length <= 14) { return id; }
  return id.slice(0, 6) + '…' + id.slice(-8);
}

export function buildSubtitleLanguageSelect(options) {
  options = options || {};
  var source = options.includeAuto ? SUBTITLE_LANGUAGES : SUBTITLE_TARGET_LANGUAGES;
  var attrs = { 'aria-label': options.ariaLabel || '语言' };
  if (options.id) { attrs.id = options.id; }
  var select = el('select', { attrs: attrs });
  source.forEach(function (item) {
    select.appendChild(el('option', { attrs: { value: item.value }, text: item.label }));
  });
  if (options.value) {
    ensureLanguageOption(select, options.value);
    select.value = options.value;
  }
  return select;
}

export function subtitleJobStatusLabel(status) {
  return SUBTITLE_STATUS_LABEL[status] || status || '未知';
}

export function subtitleJobStatusClass(status) {
  return SUBTITLE_STATUS_BADGE[status] || 'badge-blue';
}

export function isTerminalSubtitleStatus(status) {
  return status === 'done' || status === 'failed' || status === 'cancelled';
}

export function subtitleWarningText(job) {
  if (!job) { return ''; }
  var text = '';
  if (job.warning) { text = SUBTITLE_WARNING_LABEL[job.warning] || job.warning; }
  if (job.translation_partial === true) {
    // v0030：翻译批次失败会回退原文，必须在任务详情里明确提示，避免误以为已翻译
    var note = '部分翻译批次失败，对应 cue 已回退原文；请检查 LLM 配置（Base URL/Key/模型，本地推理模型可尝试“禁用思考”）后重新生成。';
    text = text ? text + '；' + note : note;
  }
  return text;
}

export function subtitleStageLabel(stage) {
  return SUBTITLE_STAGE_LABEL[stage] || stage || '处理中';
}

export function subtitleSourceLabel(job) {
  if (!job) { return ''; }
  if (job.source_job_id) {
    return '补翻译（源任务 ' + subtitleShortJobId(job.source_job_id) + '）';
  }
  if (!job.source) { return ''; }
  return SUBTITLE_SOURCE_LABEL[job.source] || job.source;
}

// ---------- v0035：补翻译（只做了 ASR、没翻译的已完成任务） ----------
/** 已完成且缺译文（或译文部分回退）的任务可补翻译。 */

export function subtitleJobTranslatable(job) {
  if (!job || job.status !== 'done') { return false; }
  return job.translate !== true || job.translation_partial === true;
}

/** 补翻译能力门控：总开关 + 翻译开关 + LLM Key 都就绪才可提交。 */

export function subtitleOutputModeLabel(mode) {
  if (mode === 'download_only') { return '仅任务目录下载'; }
  if (mode === 'alongside_video') { return '写入视频同目录'; }
  return '—';
}

export function isSafeSubtitleFileName(name) {
  if (typeof name !== 'string' || !name || name.length > 160) { return false; }
  if (name.indexOf('/') !== -1 || name.indexOf('\\') !== -1) { return false; }
  return /\.(srt|vtt)$/i.test(name);
}

export function subtitleOutputKindLabel(kind) {
  return SUBTITLE_OUTPUT_KIND_LABEL[kind] || kind || '字幕';
}

export function renderSubtitleOutputs(job, compact) {
  var outputs = subtitleJobOutputs(job);
  if (!outputs.length) { return null; }
  var limit = compact ? 3 : outputs.length;
  var wrap = el('div', { className: compact ? 'subtitle-outputs is-compact' : 'subtitle-outputs' });
  outputs.slice(0, limit).forEach(function (item) {
    wrap.appendChild(el('div', { className: 'subtitle-output-item' }, [
      el('span', { className: 'badge badge-green', text: subtitleOutputKindLabel(item.kind) }),
      el('span', { className: 'subtitle-output-path', attrs: { title: item.path }, text: item.path }),
      el('span', { className: 'subtitle-output-size', text: fmtBytes(item.size) })
    ]));
  });
  if (outputs.length > limit) {
    wrap.appendChild(el('p', { className: 'hint', text: '…共 ' + fmtInt(outputs.length) + ' 个写回文件（见任务详情）' }));
  }
  return wrap;
}

/** v0035：任务编号简写（前 6 位…后 8 位，沿用项目 head…tail 展示习惯）。 */
