/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 目录批量字幕页（ES module，v0032）
   --------------------------------------------------------------------------
   与查重解耦的独立入口：选择目录 → 枚举视频 → 勾选 → 批量生成/翻译字幕。
   字幕写回视频同目录（只写 .srt）；写回失败仍可在任务中心下载。
   v0032：一次提交最多 20000 个视频，后端自动按每批 500 个拆成「批次组」顺序执行；
          前端从单批次页改为批次组页（提交 / 进度 / 暂停 / 继续 / 取消 / 重试 / 分页任务行）。
   规范：ES2019、无第三方依赖、不使用 innerHTML、只做安全文本渲染。
   契约：docs/SUBTITLE_BATCH_CONTRACT.md
   ========================================================================== */

import { api, errorMessage } from './api.js';
import { state } from './state.js';
import {
  $$, byId, clear, el, setBusy, setHidden, setText, show
} from './dom.js';
import { fmtBytes, fmtInt } from './format.js';
import { toast } from './toast.js';
import { confirmDialog } from './modal.js';
import {
  SUBTITLE_STAGE_LABEL, SUBTITLE_STATUS_BADGE, SUBTITLE_STATUS_LABEL,
  SUBTITLE_TARGET_LANGUAGES, SUBTITLE_WARNING_LABEL
} from './constants.js';

var PAGE_LIMIT = 200;          // 视频枚举列表每页
var JOB_PAGE_LIMIT = 100;      // 批次组任务行每页（后端 /api/subtitles?limit=）
var JOB_REFRESH_MAX = 500;     // 轮询自动刷新时最多重载的任务行数
var BATCHES_SHOWN = 50;        // 批次列表默认最多展示条数
var CONFIRM_THRESHOLD = 20;    // 超过该数量提交前二次确认
var POLL_MS = 1500;
// 后端 /api/meta.subtitle 缺失时的兜底（与 backend config 默认值一致）
var FALLBACK_BATCH_MAX = 500;
var FALLBACK_GROUP_MAX = 20000;
var FALLBACK_ENUM_MAX = 20000;
var LAST_CONFIG_KEY = 'dupcleaner.lastSubtitleDir.v1';
var LAST_GROUP_KEY = 'dupcleaner.lastSubtitleGroup.v1';

// 批次组状态（与单任务状态的文案区分：running 是“执行中”而不是“识别中”）
var GROUP_STATUS_LABEL = {
  queued: '排队中', running: '执行中', paused: '已暂停',
  done: '已完成', failed: '失败', cancelled: '已取消'
};
var GROUP_STATUS_BADGE = {
  queued: 'badge-gray', running: 'badge-blue', paused: 'badge-amber',
  done: 'badge-green', failed: 'badge-red', cancelled: 'badge-gray'
};

// v0034：任务明细状态筛选（'active' 由后端映射为 running+translating）
var JOB_FILTERS = ['all', 'active', 'queued', 'done', 'failed'];
var JOB_FILTER_LABEL = {
  all: '全部', active: '执行中', queued: '排队', done: '已完成', failed: '失败'
};

var page = {
  initialized: false,
  // 目录 / 枚举 / 筛选
  dir: '',
  recursive: true,
  items: [],
  truncated: false,
  totalBytes: 0,
  selected: new Set(),
  filter: 'all',
  shown: PAGE_LIMIT,
  enumerated: false,
  skipTouched: false,
  busy: false,
  // 批次组
  group: null,
  jobs: {
    items: [], total: 0, offset: 0, limit: JOB_PAGE_LIMIT,
    loading: false, error: '', seq: 0, refreshKey: '', totalKnown: false
  },
  // v0034：任务明细状态筛选（all/active/queued/done/failed）
  jobsFilter: 'all',
  timer: null,
  polling: false,
  restoring: false,
  notified: false,
  batchesExpanded: false
};

function q(id) { return byId(id); }

function metaLimits() {
  var sub = state.meta && state.meta.subtitle ? state.meta.subtitle : null;
  var batchMax = sub && Number(sub.batch_max_items);
  var groupMax = sub && Number(sub.batch_group_max_items);
  var enumMax = sub && Number(sub.enumerate_max_items);
  return {
    batchMax: isFinite(batchMax) && batchMax > 0 ? batchMax : FALLBACK_BATCH_MAX,
    groupMax: isFinite(groupMax) && groupMax > 0 ? groupMax : FALLBACK_GROUP_MAX,
    enumMax: isFinite(enumMax) && enumMax > 0 ? enumMax : FALLBACK_ENUM_MAX
  };
}

/** 记住上次目录/递归/模式（与扫描页记住上次扫描范围一致）。 */
function loadPageConfig() {
  try {
    var raw = window.localStorage ? window.localStorage.getItem(LAST_CONFIG_KEY) : null;
    if (!raw) { return null; }
    var data = JSON.parse(raw);
    if (!data || !data.dir || typeof data.dir !== 'string') { return null; }
    return data;
  } catch (e) {
    return null;
  }
}

function savePageConfig() {
  if (!page.dir) { return; }
  try {
    if (!window.localStorage) { return; }
    window.localStorage.setItem(LAST_CONFIG_KEY, JSON.stringify({
      dir: page.dir,
      recursive: !!page.recursive,
      mode: modeValue(),
      saved_at: Date.now()
    }));
  } catch (e) { /* 隐私模式/配额错误忽略 */ }
}

// ---------------------------------------------------------------------------
// 最近一次批次组（localStorage 会话恢复）
// 键值兼容两种历史形态：{"group_id":"grp-…"} 或裸 group_id 字符串
// ---------------------------------------------------------------------------
function loadLastGroupId() {
  try {
    if (!window.localStorage) { return ''; }
    var raw = window.localStorage.getItem(LAST_GROUP_KEY);
    if (!raw) { return ''; }
    var text = String(raw);
    try {
      var data = JSON.parse(text);
      if (data && typeof data === 'object' && data.group_id) {
        return String(data.group_id);
      }
    } catch (e) { /* 不是 JSON，按裸字符串处理 */ }
    if (text.charAt(0) !== '{') { return text; }
    return '';
  } catch (e) {
    return '';
  }
}

function saveLastGroupId(groupId) {
  if (!groupId) { return; }
  try {
    if (!window.localStorage) { return; }
    window.localStorage.setItem(LAST_GROUP_KEY, JSON.stringify({
      group_id: String(groupId),
      saved_at: Date.now()
    }));
  } catch (e) { /* 忽略配额/隐私模式错误 */ }
}

function clearLastGroupId() {
  try {
    if (window.localStorage) { window.localStorage.removeItem(LAST_GROUP_KEY); }
  } catch (e) { /* 忽略 */ }
}

function capability() {
  var s = state.settings || {};
  var supported = Object.prototype.hasOwnProperty.call(s, 'subtitle_enabled');
  return {
    supported: supported,
    enabled: supported && s.subtitle_enabled === true,
    asrKeySet: supported && s.subtitle_asr_api_key_set === true,
    llmKeySet: supported && s.subtitle_llm_api_key_set === true,
    translateEnabled: supported && s.subtitle_translate_enabled === true,
    outputMode: s.subtitle_output_mode || 'alongside_video',
    skipExisting: s.subtitle_skip_existing !== false,
    asrLanguage: s.subtitle_asr_language || 'auto',
    targetLang: s.subtitle_translate_target || 'zh'
  };
}

// ---------------------------------------------------------------------------
// v0035：补翻译（只做了 ASR、没翻译的历史任务）——仅上传字幕文本，不重新识别音频
// ---------------------------------------------------------------------------
/** 补翻译能力门控：总开关 + 翻译开关 + LLM Key 都就绪时才允许提交。 */
function translateCapability() {
  var cap = capability();
  var reason = '';
  if (!cap.supported) { reason = '当前后端未提供云端字幕能力'; }
  else if (!cap.enabled) { reason = '请先在设置中启用云端字幕'; }
  else if (!cap.translateEnabled) { reason = '字幕翻译未启用（请在设置中开启）'; }
  else if (!cap.llmKeySet) { reason = '请先在设置中配置 LLM API Key'; }
  cap.ok = !reason;
  cap.reason = reason;
  return cap;
}

function targetLanguageLabel(value) {
  for (var i = 0; i < SUBTITLE_TARGET_LANGUAGES.length; i += 1) {
    if (SUBTITLE_TARGET_LANGUAGES[i].value === value) { return SUBTITLE_TARGET_LANGUAGES[i].label; }
  }
  return '中文';
}

/** 已完成且需要补翻译：没译文，或译文部分回退原文（可在确认后重新翻译）。 */
function canSupplementTranslate(job) {
  if (!job || job.status !== 'done') { return false; }
  return job.translate !== true || job.translation_partial === true;
}

function translateButtonText(job) {
  return job && job.translation_partial === true ? '重新翻译' : '补翻译';
}

function modeValue() {
  var asr = q('subtitleModeAsr');
  return asr && asr.checked ? 'asr' : 'existing_srt';
}

// ---------------------------------------------------------------------------
// 能力与表单状态
// ---------------------------------------------------------------------------
function updateCapabilityUi() {
  var cap = capability();
  var badge = q('subtitlePageStatusBadge');
  if (badge) {
    var text = '后端未提供云端字幕';
    var cls = 'is-muted';
    if (cap.supported) {
      if (!cap.enabled) { text = '未启用（默认关闭）'; }
      else if (!cap.asrKeySet) { text = '已启用 · 未配置 ASR Key'; cls = 'is-warn'; }
      else { text = '已启用 · ASR 已配置'; cls = 'is-ok'; }
    }
    setText(badge, text);
    badge.className = 'badge subtitle-status-badge ' + cls;
  }
  var hint = q('subtitlePageHint');
  if (hint) {
    var text2 = '字幕将以 .srt 写入视频同目录；已有同名 srt 默认跳过。音频/字幕文本会上传到所配置的第三方服务。';
    if (!cap.supported) {
      text2 = '当前后端未提供云端字幕能力；升级后端后可用。';
    } else if (!cap.enabled) {
      text2 = '云端字幕未启用：请先在「设置 → 云端字幕」开启并配置 ASR API Key。';
    } else if (!cap.asrKeySet) {
      text2 = '已启用但未配置 ASR API Key：生成字幕不可用；只翻译已有字幕需要 LLM API Key。';
    } else if (!cap.llmKeySet) {
      text2 = 'ASR 已配置；未配置 LLM API Key 时不能生成译文或只翻译已有字幕。';
    }
    setText(hint, text2);
  }
  var translateCheck = q('subtitleTranslate');
  if (translateCheck) {
    translateCheck.disabled = !(cap.llmKeySet && cap.translateEnabled);
    if (!cap.llmKeySet || !cap.translateEnabled) { translateCheck.checked = false; }
  }
  var target = q('subtitleTargetLang');
  if (target && !target.dataset.userTouched) {
    target.value = cap.targetLang;
  }
  var mode = modeValue();
  var translateOn = !!(translateCheck && translateCheck.checked);
  if (target) {
    target.disabled = !cap.llmKeySet
      || (mode === 'asr' && !translateOn)
      || !cap.translateEnabled;
  }
  var skip = q('subtitleSkipExisting');
  if (skip && !page.skipTouched) { skip.checked = cap.skipExisting; }
  var language = q('subtitleLanguage');
  if (language && !language.dataset.userTouched) {
    language.value = cap.asrLanguage;
  }
  updateSubmitState();
}

function modeRequirement() {
  // 返回 {ok, message}：当前任务类型所需的凭据是否就绪
  var cap = capability();
  var mode = modeValue();
  if (!cap.supported) {
    return { ok: false, message: '当前后端未提供云端字幕能力' };
  }
  if (!cap.enabled) {
    return { ok: false, message: '请先在设置中启用云端字幕' };
  }
  if (mode === 'existing_srt') {
    if (!cap.translateEnabled) {
      return { ok: false, message: '字幕翻译未启用（请在设置中开启）' };
    }
    if (!cap.llmKeySet) {
      return { ok: false, message: '请先在设置中配置 LLM API Key' };
    }
    return { ok: true, message: '' };
  }
  if (!cap.asrKeySet) {
    return { ok: false, message: '请先在设置中配置 ASR API Key' };
  }
  var translateCheck = q('subtitleTranslate');
  if (translateCheck && translateCheck.checked && !cap.llmKeySet) {
    return { ok: false, message: '已勾选生成译文，请先配置 LLM API Key' };
  }
  return { ok: true, message: '' };
}

function selectedBatchCount(count, limits) {
  if (!count) { return 0; }
  return Math.ceil(count / limits.batchMax);
}

function updateSubmitState() {
  var confirm = q('subtitleBatchConfirm');
  var submit = q('subtitleSubmitBtn');
  var hint = q('subtitleSelectionHint');
  var reasonEl = q('subtitleSubmitReason');
  var limits = metaLimits();
  var count = page.selected.size;
  var batches = selectedBatchCount(count, limits);
  if (hint) {
    // v0032：不再受单批上限限制，按批次组提交（后端自动拆批，每批最多 batchMax 个）
    setText(hint, count
      ? '已选择 ' + fmtInt(count) + ' 个视频（将分为 ' + fmtInt(batches)
        + ' 批，每批最多 ' + fmtInt(limits.batchMax) + ' 个）'
      : '尚未选择视频');
  }
  if (!submit) { return; }
  // 按钮禁用原因常显（v0031）：只靠 title 提示用户看不到，容易误以为按钮坏了
  var requirement = modeRequirement();
  var reason = '';
  if (!requirement.ok) {
    reason = requirement.message || '当前配置不可提交';
  } else if (!count) {
    reason = '请先在上方视频列表中勾选要处理的视频';
  } else if (count > limits.groupMax) {
    reason = '已选 ' + fmtInt(count) + ' 个，超过单次提交上限 ' + fmtInt(limits.groupMax)
      + ' 个：请点「清空」后分多次提交，或用筛选条件缩小范围';
  } else if (!confirm || !confirm.checked) {
    reason = '请勾选下方「我了解音频/字幕文本将上传到所配置的第三方服务」确认项';
  } else if (page.busy) {
    reason = '正在处理中，请稍候';
  }
  submit.disabled = !!reason;
  submit.title = reason;
  if (reasonEl) {
    setText(reasonEl, reason);
    reasonEl.hidden = !reason;
  }
}

// ---------------------------------------------------------------------------
// 目录选择器：复用扫描范围页的共享文件夹选择器（app.js 通过 state 注入）
// ---------------------------------------------------------------------------
function openDirPicker() {
  var opener = state.openSubtitleDirPicker;
  if (typeof opener !== 'function') {
    toast('目录选择器不可用，请刷新页面后重试。', 'error');
    return;
  }
  var input = q('subtitleDirPath');
  opener({
    initial: input ? input.value.trim() : '',
    onConfirm: function (path) {
      if (!path) { return; }
      if (input) { input.value = path; }
      enumerateNow();
    }
  });
}

// ---------------------------------------------------------------------------
// 枚举与列表
// ---------------------------------------------------------------------------
function filteredItems() {
  var mode = modeValue();
  return page.items.filter(function (item) {
    if (mode === 'existing_srt' && !item.has_source_srt) { return false; }
    if (page.filter === 'missing' && item.has_subtitle) { return false; }
    if (page.filter === 'existing' && !item.has_subtitle) { return false; }
    return true;
  });
}

function itemByPath(path) {
  for (var i = 0; i < page.items.length; i += 1) {
    if (page.items[i].path === path) { return page.items[i]; }
  }
  return null;
}

function renderVideos() {
  var card = q('subtitleVideosCard');
  var list = q('subtitleVideosList');
  var empty = q('subtitleVideosEmpty');
  var countEl = q('subtitleVideosCount');
  var moreBtn = q('subtitleVideosMoreBtn');
  if (!card || !list) { return; }
  setHidden(card, !page.enumerated);
  clear(list);
  var filtered = filteredItems();
  if (!filtered.length) {
    setHidden(empty, false);
    setText(empty, page.items.length
      ? '当前筛选条件下没有视频。'
      : '该目录下没有视频文件。');
  } else {
    setHidden(empty, true);
  }
  var slice = filtered.slice(0, page.shown);
  slice.forEach(function (item) {
    list.appendChild(renderVideoRow(item));
  });
  if (countEl) {
    setText(countEl, '共 ' + fmtInt(filtered.length) + ' 个视频' +
      (page.truncated ? '（枚举已截断，仅显示前 ' + fmtInt(page.items.length) + ' 个）' : '') +
      (page.items.length > slice.length ? '，当前显示 ' + fmtInt(slice.length) + ' 个' : ''));
  }
  if (moreBtn) {
    setHidden(moreBtn, filtered.length <= page.shown);
  }
  updateSelectionSummary();
}

function renderVideoRow(item) {
  var checkbox = el('input', { attrs: { type: 'checkbox', 'aria-label': '选择 ' + item.name } });
  checkbox.checked = page.selected.has(item.path);
  checkbox.addEventListener('change', function () {
    if (checkbox.checked) { page.selected.add(item.path); }
    else { page.selected.delete(item.path); }
    updateSelectionSummary();
  });
  var meta = [
    el('span', { className: 'subtitle-video-size', text: fmtBytes(item.size) }),
    el('span', { className: 'muted', text: item.modified_at || '' }),
    item.has_source_srt
      ? el('span', { className: 'badge badge-green', text: '已有字幕' })
      : el('span', { className: 'badge badge-gray', text: '无字幕' })
  ];
  if (item.has_subtitle && !item.has_source_srt) {
    meta.push(el('span', { className: 'badge badge-amber', text: '已有译文' }));
  }
  var main = el('div', { className: 'subtitle-video-main' }, [
    el('span', { className: 'subtitle-video-path', attrs: { title: item.path }, text: item.path }),
    el('span', { className: 'subtitle-video-meta' }, meta)
  ]);
  return el('label', { className: 'subtitle-video-row' }, [checkbox, main]);
}

function updateSelectionSummary() {
  var mode = modeValue();
  if (mode === 'existing_srt') {
    var eligible = Array.from(page.selected).filter(function (path) {
      var item = itemByPath(path);
      return item && item.has_source_srt;
    });
    if (eligible.length !== page.selected.size) {
      page.selected = new Set(eligible);
    }
  }
  renderSelectionHintOnly();
}

function renderSelectionHintOnly() {
  updateSubmitState();
}

// ---------------------------------------------------------------------------
// 枚举请求
// ---------------------------------------------------------------------------
async function enumerateNow() {
  if (page.busy) { return; }
  var path = q('subtitleDirPath').value.trim();
  var errorEl = q('subtitleEnumerateError');
  if (!path) {
    setHidden(errorEl, false);
    setText(errorEl, '请先选择或输入目录路径。');
    return;
  }
  page.busy = true;
  setBusy(q('subtitleEnumerateBtn'), true, '正在枚举…');
  setHidden(errorEl, true);
  try {
    var result = await api('/api/subtitles/enumerate', {
      method: 'POST',
      body: {
        path: path,
        recursive: !!(q('subtitleRecursive') && q('subtitleRecursive').checked),
        max_items: metaLimits().enumMax
      },
      timeout: 60000
    });
    page.dir = result.path || path;
    page.items = Array.isArray(result.items) ? result.items : [];
    page.truncated = !!result.truncated;
    page.totalBytes = Number(result.total_bytes) || 0;
    page.recursive = !!(q('subtitleRecursive') && q('subtitleRecursive').checked);
    page.selected = new Set();
    page.shown = PAGE_LIMIT;
    page.enumerated = true;
    page.filter = 'all';
    var filter = q('subtitleVideoFilter');
    if (filter) { filter.value = 'all'; }
    renderVideos();
    var withSubs = page.items.filter(function (item) { return item.has_subtitle; }).length;
    var summary = q('subtitleEnumerateSummary');
    if (summary) {
      setText(summary, '枚举到 ' + fmtInt(page.items.length) + ' 个视频 · '
        + fmtBytes(page.totalBytes) + ' · 已有字幕 ' + fmtInt(withSubs) + ' 个'
        + (result.truncated ? ' · 已截断' : ''));
    }
    show(q('subtitleOptionsCard'));
    savePageConfig();
    if (!page.items.length) {
      toast('该目录下没有视频文件。', 'info');
    }
  } catch (err) {
    setHidden(errorEl, false);
    setText(errorEl, errorMessage(err, '枚举失败，请检查目录与权限。'));
  } finally {
    page.busy = false;
    setBusy(q('subtitleEnumerateBtn'), false);
  }
}

// ---------------------------------------------------------------------------
// 提交批次组（v0032：一次提交由后端拆成多个批次，前端不再有单批路径）
// ---------------------------------------------------------------------------
async function submitGroup() {
  if (page.busy) { return; }
  var cap = capability();
  var confirm = q('subtitleBatchConfirm');
  if (!confirm || !confirm.checked) { return; }
  var mode = modeValue();
  var limits = metaLimits();
  var selected = Array.from(page.selected);
  var eligible = selected.filter(function (path) {
    var item = itemByPath(path);
    if (!item) { return false; }
    return mode !== 'existing_srt' || item.has_source_srt;
  });
  if (!eligible.length) {
    toast('没有可提交的视频。', 'info');
    return;
  }
  if (eligible.length > limits.groupMax) {
    toast('已选 ' + fmtInt(eligible.length) + ' 个视频，超过单次提交上限 '
      + fmtInt(limits.groupMax) + ' 个，请分多次提交。', 'error', 7000);
    return;
  }
  var batchCount = selectedBatchCount(eligible.length, limits);
  if (eligible.length > CONFIRM_THRESHOLD) {
    var ok = await confirmDialog({
      title: '确认批量生成字幕',
      message: '将提交 ' + fmtInt(eligible.length) + ' 个视频的字幕任务（分为 '
        + fmtInt(batchCount) + ' 批，每批最多 ' + fmtInt(limits.batchMax)
        + ' 个）。音频/字幕文本会上传到第三方服务，可能产生费用。确定继续？',
      confirmText: '开始处理',
      danger: false
    });
    if (!ok) { return; }
  }
  var translateCheck = q('subtitleTranslate');
  var translate = mode === 'existing_srt'
    ? true
    : !!(translateCheck && translateCheck.checked);
  if (translate && !cap.llmKeySet) {
    toast('未配置 LLM API Key，无法生成译文。', 'error');
    return;
  }
  page.busy = true;
  setBusy(q('subtitleSubmitBtn'), true, '正在提交…');
  try {
    var result = await api('/api/subtitles/batch-groups', {
      method: 'POST',
      body: {
        paths: eligible,
        source: mode,
        language: (q('subtitleLanguage') || {}).value || 'auto',
        translate: translate,
        target_lang: (q('subtitleTargetLang') || {}).value || 'zh',
        skip_existing: !!(q('subtitleSkipExisting') && q('subtitleSkipExisting').checked),
        output_mode: cap.outputMode || 'alongside_video',
        confirm_cloud: true
      },
      timeout: 120000
    });
    stopPolling();
    page.group = result.group || null;
    page.notified = false;
    page.batchesExpanded = false;
    resetJobsView('');
    page.selected = new Set();
    renderVideos();
    if (!page.group || !page.group.group_id) {
      toast('提交已受理，但后端未返回批次组信息，请到任务中心查看。', 'info', 7000);
      return;
    }
    saveLastGroupId(page.group.group_id);
    page.jobs.refreshKey = groupSignature(page.group);
    renderGroup();
    loadGroupJobs('reset');
    startPolling();
    var rejected = Array.isArray(page.group.rejected) ? page.group.rejected.length : 0;
    var duplicates = Array.isArray(page.group.duplicates) ? page.group.duplicates.length : 0;
    var total = Number(page.group.total) || eligible.length;
    var groups = Number(page.group.batch_count) || batchCount;
    toast('已提交 ' + fmtInt(total) + ' 个视频（' + fmtInt(groups) + ' 批）'
      + (rejected ? '，' + fmtInt(rejected) + ' 个被拒绝' : '')
      + (duplicates ? '，' + fmtInt(duplicates) + ' 个已在队列中跳过' : '') + '。',
      rejected ? 'error' : 'success', 6000);
  } catch (err) {
    toast(errorMessage(err, '提交字幕任务失败。'), 'error', 7000);
  } finally {
    page.busy = false;
    setBusy(q('subtitleSubmitBtn'), false);
    updateSubmitState();
  }
}

// ---------------------------------------------------------------------------
// 批次组：进度 / 批次 / 任务明细渲染
// ---------------------------------------------------------------------------
function isGroupTerminal(status) {
  return status === 'done' || status === 'failed' || status === 'cancelled';
}

function groupFinished(group) {
  if (!group) { return 0; }
  var finished = Number(group.finished);
  if (isFinite(finished) && finished >= 0 && group.finished !== undefined && group.finished !== null) {
    return finished;
  }
  var counts = group.counts || {};
  return (Number(counts.done) || 0) + (Number(counts.failed) || 0) + (Number(counts.cancelled) || 0);
}

function groupBatchCount(group) {
  if (!group) { return 0; }
  var count = Number(group.batch_count);
  if (isFinite(count) && count > 0) { return count; }
  return Array.isArray(group.batches) ? group.batches.length : 0;
}

function groupSignature(group) {
  if (!group) { return ''; }
  return String(group.status || '') + '|' + groupFinished(group) + '|'
    + (Number(group.total) || 0) + '|' + groupBatchCount(group);
}

function groupPercent(group) {
  if (!group) { return 0; }
  var progress = Number(group.progress);
  if (isFinite(progress) && progress >= 0) {
    return Math.max(0, Math.min(100, Math.round(progress)));
  }
  var total = Number(group.total) || 0;
  return total ? Math.round(100 * groupFinished(group) / total) : 0;
}

/** 任务显示名：优先 name 字段，缺失时从 path 取文件名。 */
function jobDisplayName(job) {
  if (!job) { return '—'; }
  var name = String(job.name || '').trim();
  if (!name && job.path) {
    name = String(job.path).split(/[\\/]/).pop() || '';
  }
  return name || '—';
}

/** 预估剩余时间文案：<60s 不到 1 分钟 / <3600 约 N 分钟 / <86400 约 N 小时 / 约 N 天 M 小时。 */
function fmtEtaSeconds(seconds) {
  var sec = Number(seconds);
  if (!isFinite(sec) || sec < 0) { return ''; }
  if (sec < 60) { return '不到 1 分钟'; }
  if (sec < 3600) { return '约 ' + Math.max(1, Math.round(sec / 60)) + ' 分钟'; }
  if (sec < 86400) { return '约 ' + Math.max(1, Math.round(sec / 3600)) + ' 小时'; }
  var days = Math.floor(sec / 86400);
  var hours = Math.round((sec % 86400) / 3600);
  if (hours >= 24) { days += 1; hours = 0; }
  return '约 ' + days + ' 天' + (hours > 0 ? ' ' + hours + ' 小时' : '');
}

/** 组内任务计数（与筛选按钮一一对应；全部取组总数，失败含已取消）。 */
function groupJobFilterCounts(group) {
  var counts = group && group.counts ? group.counts : {};
  var running = (Number(counts.running) || 0) + (Number(counts.translating) || 0);
  return {
    all: Number(group && group.total) || 0,
    active: running,
    queued: Number(counts.queued) || 0,
    done: Number(counts.done) || 0,
    failed: (Number(counts.failed) || 0) + (Number(counts.cancelled) || 0)
  };
}

/** 组内正在执行的任务（后端快照 active_jobs，≤10 条）。 */
function groupActiveJobs(group) {
  return group && Array.isArray(group.active_jobs) ? group.active_jobs : [];
}

/** v0034：提交新组/恢复组时重置任务明细与状态筛选（回到「全部」第一页）。 */
function resetJobsView(refreshKey) {
  page.jobsFilter = 'all';
  page.jobs = {
    items: [], total: 0, offset: 0, limit: JOB_PAGE_LIMIT,
    loading: false, error: '', seq: page.jobs.seq + 1, refreshKey: refreshKey || '',
    totalKnown: false
  };
}

function renderGroup() {
  var card = q('subtitleGroupCard');
  if (!card) { return; }
  var group = page.group;
  if (!group) { setHidden(card, true); return; }
  setHidden(card, false);
  renderGroupHead();
  renderBatches();
  renderJobsFilter();
  renderJobsActive();
  renderGroupJobs();
  renderGroupActions();
}

function renderGroupHead() {
  var group = page.group;
  if (!group) { return; }
  var counts = group.counts || {};
  var total = Number(group.total) || 0;
  var finished = groupFinished(group);
  var pct = groupPercent(group);
  var batchCount = groupBatchCount(group);
  var statusEl = q('subtitleGroupStatus');
  if (statusEl) {
    setText(statusEl, (GROUP_STATUS_LABEL[group.status] || group.status || '处理中')
      + ' · ' + fmtInt(finished) + '/' + fmtInt(total));
    statusEl.className = 'badge ' + (GROUP_STATUS_BADGE[group.status] || 'badge-blue');
  }
  var fill = q('subtitleGroupFill');
  if (fill) { fill.style.width = pct + '%'; }
  var bar = q('subtitleGroupBar');
  if (bar) { bar.setAttribute('aria-valuenow', String(pct)); }
  var running = (Number(counts.running) || 0) + (Number(counts.translating) || 0);
  var remaining = Math.max(0, total - finished);
  var missing = Number(group.missing) || 0;
  var countsText = '完成 ' + fmtInt(counts.done) + ' · 失败 ' + fmtInt(counts.failed)
    + ' · 取消 ' + fmtInt(counts.cancelled) + ' · 暂停 ' + fmtInt(counts.paused)
    + ' · 排队 ' + fmtInt(counts.queued) + ' · 运行 ' + fmtInt(running)
    + ' · 剩余 ' + fmtInt(remaining) + ' · 批次 ' + fmtInt(batchCount)
    + (missing > 0 ? ' · 缺失 ' + fmtInt(missing) : '');
  // v0034：非终态组展示预计剩余（后端 eta_seconds，样本不足为 null）
  if (!isGroupTerminal(group.status) && typeof group.eta_seconds === 'number') {
    var etaText = fmtEtaSeconds(group.eta_seconds);
    if (etaText) { countsText += ' · 预计剩余 ' + etaText; }
  }
  setText(q('subtitleGroupCounts'), countsText);
  renderGroupCurrent();
}

/** v0034：组卡片“当前执行”：取 active_jobs 的第一条；无执行中任务时提示等待调度。 */
function renderGroupCurrent() {
  var currentEl = q('subtitleGroupCurrent');
  if (!currentEl) { return; }
  var group = page.group;
  if (!group) { setHidden(currentEl, true); return; }
  var activeJobs = groupActiveJobs(group);
  if (activeJobs.length) {
    var job = activeJobs[0];
    var stageLabel = SUBTITLE_STAGE_LABEL[job.stage]
      || SUBTITLE_STATUS_LABEL[job.status] || job.status || '处理中';
    var jobPct = Math.max(0, Math.min(100, Math.round(Number(job.progress) || 0)));
    setText(currentEl, '当前执行：' + jobDisplayName(job) + ' · ' + stageLabel
      + ' · ' + jobPct + '%'
      + (activeJobs.length > 1 ? '（另有 ' + fmtInt(activeJobs.length - 1) + ' 个并行执行）' : ''));
    setHidden(currentEl, false);
    return;
  }
  if (!isGroupTerminal(group.status) && group.status !== 'paused') {
    setText(currentEl, '等待调度：队列中的任务会自动开始执行。');
    setHidden(currentEl, false);
    return;
  }
  setHidden(currentEl, true);
}

function renderBatches() {
  var list = q('subtitleGroupBatches');
  var hint = q('subtitleGroupBatchesHint');
  var toggle = q('subtitleGroupBatchesToggle');
  if (!list) { return; }
  clear(list);
  var group = page.group;
  if (!group) {
    setText(hint, '');
    setHidden(toggle, true);
    return;
  }
  var batches = Array.isArray(group.batches) ? group.batches : [];
  var batchCount = groupBatchCount(group);
  if (hint) {
    if (batches.length) {
      var shownCount = page.batchesExpanded ? batches.length : Math.min(batches.length, BATCHES_SHOWN);
      setText(hint, '共 ' + fmtInt(batchCount) + ' 批，当前显示 ' + fmtInt(shownCount) + ' 批'
        + (batches.length > shownCount ? '（更多批次可展开）' : ''));
    } else if (batchCount) {
      setText(hint, '共 ' + fmtInt(batchCount) + ' 批，批次明细加载中…');
    } else {
      setText(hint, '暂无批次信息。');
    }
  }
  var shown = page.batchesExpanded ? batches : batches.slice(0, BATCHES_SHOWN);
  shown.forEach(function (batch) {
    list.appendChild(renderBatchRow(batch));
  });
  if (toggle) {
    setHidden(toggle, batches.length <= BATCHES_SHOWN);
    setText(toggle, page.batchesExpanded
      ? '折叠批次列表'
      : '展开全部 ' + fmtInt(batches.length) + ' 批');
  }
  var rejected = Array.isArray(group.rejected) ? group.rejected : [];
  var duplicates = Array.isArray(group.duplicates) ? group.duplicates : [];
  if (rejected.length || duplicates.length) {
    var block = el('div', { className: 'subtitle-batch-rejected' });
    if (rejected.length) {
      block.appendChild(el('p', {
        className: 'hint',
        text: '以下路径被拒绝（共 ' + fmtInt(rejected.length) + ' 个'
          + (rejected.length > 10 ? '，仅显示前 10 个' : '') + '）：'
      }));
      var rejectedList = el('div', { className: 'subtitle-batch-rejected-list' });
      rejected.slice(0, 10).forEach(function (item) {
        rejectedList.appendChild(el('div', {
          className: 'mono',
          text: String(item.path || '') + ' — ' + String(item.message || item.code || '')
        }));
      });
      block.appendChild(rejectedList);
    }
    if (duplicates.length) {
      block.appendChild(el('p', {
        className: 'hint',
        text: '另有 ' + fmtInt(duplicates.length) + ' 个视频已在任务队列中，本次自动跳过。'
      }));
    }
    list.appendChild(block);
  }
}

function renderBatchRow(batch) {
  var counts = batch.counts || {};
  var index = Number(batch.index);
  var label = isFinite(index) && index >= 0 ? '第 ' + fmtInt(index + 1) + ' 批' : '批次';
  var total = Number(batch.total) || 0;
  var finished = (Number(counts.done) || 0) + (Number(counts.failed) || 0)
    + (Number(counts.cancelled) || 0);
  var badges = [
    el('span', {
      className: 'badge ' + (GROUP_STATUS_BADGE[batch.status] || 'badge-gray'),
      text: GROUP_STATUS_LABEL[batch.status] || batch.status || '未知'
    }),
    el('span', {
      className: 'muted',
      text: '完成 ' + fmtInt(finished) + (total ? ' / ' + fmtInt(total) : '')
    })
  ];
  if (Number(counts.failed)) {
    badges.push(el('span', { className: 'badge badge-red', text: '失败 ' + fmtInt(counts.failed) }));
  }
  if (Number(counts.paused)) {
    badges.push(el('span', { className: 'badge badge-amber', text: '暂停 ' + fmtInt(counts.paused) }));
  }
  return el('div', { className: 'subtitle-batch-row' }, [
    el('div', { className: 'subtitle-batch-main' }, [
      el('span', { className: 'subtitle-batch-path', text: label }),
      el('span', { className: 'subtitle-batch-meta' }, badges)
    ])
  ]);
}

function jobsTotal() {
  var jobs = page.jobs;
  var total = Number(jobs.total);
  if (!isFinite(total) || total < 0) { total = 0; }
  // 旧后端不带 total：只有「全部」筛选才回落到组总数；筛选后的数量无法可靠推断
  if (!total && !jobs.totalKnown && page.jobsFilter === 'all' && page.group) {
    total = Number(page.group.total) || 0;
  }
  return total;
}

/** v0034：渲染状态筛选页签（计数来自组快照 counts，切换时重置分页并重新加载）。 */
function renderJobsFilter() {
  var box = q('subtitleJobsFilter');
  if (!box) { return; }
  var counts = groupJobFilterCounts(page.group);
  var buttons = $$('button[data-jobs-filter]', box);
  buttons.forEach(function (btn) {
    var key = btn.dataset.jobsFilter;
    if (JOB_FILTERS.indexOf(key) === -1) { return; }
    var active = page.jobsFilter === key;
    clear(btn);
    btn.appendChild(document.createTextNode(JOB_FILTER_LABEL[key]));
    if (page.group) {
      btn.appendChild(el('span', {
        className: 'subtitle-jobs-filter-count', text: fmtInt(counts[key])
      }));
    }
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

/** v0034：固定置顶「执行中」区块（来自 group.active_jobs，不受筛选/分页影响）。 */
function renderJobsActive() {
  var box = q('subtitleJobsActive');
  if (!box) { return; }
  clear(box);
  var activeJobs = groupActiveJobs(page.group);
  if (!activeJobs.length) { setHidden(box, true); return; }
  setHidden(box, false);
  box.appendChild(el('p', {
    className: 'subtitle-jobs-active-title',
    text: '执行中（' + fmtInt(activeJobs.length) + '） · 不受筛选影响'
  }));
  activeJobs.forEach(function (job) {
    box.appendChild(renderJobRow(job));
  });
}

/** 切换状态筛选：作废进行中的旧请求，清空当前行并从第一页重新加载。 */
function selectJobsFilter(key) {
  if (JOB_FILTERS.indexOf(key) === -1 || page.jobsFilter === key) { return; }
  page.jobsFilter = key;
  var jobs = page.jobs;
  jobs.items = [];
  jobs.total = 0;
  jobs.offset = 0;
  jobs.error = '';
  jobs.totalKnown = false;
  jobs.loading = false;
  jobs.seq += 1;
  renderJobsFilter();
  if (page.group && page.group.group_id) {
    loadGroupJobs('reset');
  } else {
    renderGroupJobs();
  }
}

function renderGroupJobs() {
  var list = q('subtitleGroupJobs');
  if (!list) { return; }
  var jobs = page.jobs;
  var hint = q('subtitleGroupJobsHint');
  var moreBtn = q('subtitleGroupJobsMoreBtn');
  var moreStatus = q('subtitleGroupJobsMoreStatus');
  var filterKey = page.jobsFilter;
  var filterLabel = JOB_FILTER_LABEL[filterKey] || JOB_FILTER_LABEL.all;
  clear(list);
  var total = jobsTotal();
  if (hint) {
    setText(hint, page.group
      ? '任务明细：' + filterLabel + ' · 共 ' + fmtInt(total) + ' 个任务，已显示 '
        + fmtInt(jobs.items.length) + ' 个'
      : '');
  }
  if (jobs.loading && !jobs.items.length) {
    list.appendChild(el('div', { className: 'state-box', text: '正在加载任务明细…' }));
  } else if (jobs.error && !jobs.items.length) {
    list.appendChild(el('p', {
      className: 'field-error', attrs: { role: 'alert' }, text: jobs.error
    }));
  } else if (!jobs.items.length) {
    list.appendChild(el('div', {
      className: 'state-box',
      text: filterKey === 'all'
        ? '暂无任务明细。'
        : '当前筛选（' + filterLabel + '）下没有任务。'
    }));
  }
  jobs.items.forEach(function (job) {
    list.appendChild(renderJobRow(job));
  });
  if (jobs.error && jobs.items.length) {
    list.appendChild(el('p', { className: 'field-error', attrs: { role: 'alert' }, text: jobs.error }));
  }
  if (moreBtn) {
    setHidden(moreBtn, jobs.items.length >= total);
  }
  if (moreStatus) {
    setText(moreStatus, jobs.loading
      ? '正在加载任务明细…'
      : (jobs.items.length
        ? '已显示 ' + fmtInt(jobs.items.length) + ' / ' + fmtInt(total) + ' 个任务'
        : ''));
  }
}

function renderJobRow(job) {
  var name = jobDisplayName(job);
  var pct = Math.max(0, Math.min(100, Math.round(Number(job.progress) || 0)));
  var badges = [
    el('span', {
      className: 'badge ' + (SUBTITLE_STATUS_BADGE[job.status] || 'badge-gray'),
      text: SUBTITLE_STATUS_LABEL[job.status] || job.status
    })
  ];
  if (job.skipped) {
    badges.push(el('span', { className: 'badge badge-gray', text: '已跳过' }));
  }
  if (job.warning === 'OUTPUT_NOT_WRITABLE') {
    badges.push(el('span', {
      className: 'badge badge-amber', text: '字幕未写回（可下载）',
      attrs: { title: SUBTITLE_WARNING_LABEL.OUTPUT_NOT_WRITABLE }
    }));
  } else if (job.warning === 'OUTPUT_WRITE_PARTIAL') {
    badges.push(el('span', {
      className: 'badge badge-amber', text: '部分写回失败',
      attrs: { title: SUBTITLE_WARNING_LABEL.OUTPUT_WRITE_PARTIAL }
    }));
  }
  if (job.translation_partial === true) {
    // v0030：部分翻译批次失败时对应 cue 回退原文，避免误以为译文已完整
    badges.push(el('span', {
      className: 'badge badge-amber', text: '部分译文回退原文',
      attrs: { title: '部分翻译批次失败，对应 cue 保留原文；可检查 LLM 配置后重新生成。' }
    }));
  }
  var outputs = Array.isArray(job.outputs) ? job.outputs : [];
  if (outputs.length) {
    badges.push(el('span', {
      className: 'badge badge-green',
      text: '已写回 ' + outputs.length + ' 个 srt'
    }));
  }
  if (!isGroupTerminal(job.status) && !job.skipped) {
    badges.push(el('span', { className: 'subtitle-pct', text: pct + '%' }));
  }
  var row = el('div', { className: 'subtitle-batch-row' }, [
    el('div', { className: 'subtitle-batch-main' }, [
      el('span', { className: 'subtitle-batch-path', attrs: { title: job.path }, text: name }),
      el('span', { className: 'subtitle-batch-meta' }, badges)
    ])
  ]);
  // v0035：已完成但缺译文（或译文部分回退）的任务提供补翻译入口
  if (canSupplementTranslate(job)) {
    var translateCap = translateCapability();
    var translateBtn = el('button', {
      className: 'btn btn-ghost btn-sm subtitle-batch-translate-btn',
      attrs: { type: 'button' }, text: translateButtonText(job)
    });
    if (!translateCap.ok) {
      translateBtn.disabled = true;
      translateBtn.title = translateCap.reason;
    } else {
      translateBtn.title = '将字幕文本上传到 LLM 翻译，新建翻译任务（不上传音频/视频）';
      translateBtn.addEventListener('click', function (event) {
        if (event && event.stopPropagation) { event.stopPropagation(); }
        translateJob(job, translateBtn);
      });
    }
    row.appendChild(el('div', { className: 'subtitle-batch-row-actions' }, [translateBtn]));
  }
  return row;
}

function renderGroupActions() {
  var box = q('subtitleGroupActions');
  if (!box) { return; }
  clear(box);
  var group = page.group;
  if (!group) { return; }
  var counts = group.counts || {};
  var terminal = isGroupTerminal(group.status);
  var active = (Number(counts.queued) || 0) + (Number(counts.running) || 0)
    + (Number(counts.translating) || 0);
  function actionBtn(label, action, primary) {
    var btn = el('button', {
      className: 'btn ' + (primary ? 'btn-primary' : 'btn-ghost') + ' btn-sm',
      attrs: { type: 'button' }, text: label
    });
    btn.addEventListener('click', function () { groupAction(action, btn); });
    box.appendChild(btn);
  }
  if (!terminal && active > 0) {
    actionBtn('暂停', 'pause', false);
  }
  if (!terminal && ((Number(counts.paused) || 0) > 0 || group.status === 'paused')) {
    actionBtn('继续', 'resume', true);
  }
  if (!terminal) {
    actionBtn('取消剩余', 'cancel', false);
  }
  if ((Number(counts.failed) || 0) + (Number(counts.cancelled) || 0) > 0) {
    actionBtn('重试失败项', 'retry', false);
  }
  // v0035：组内存在「只做了 ASR、没翻译」的已完成任务时，提供组级补翻译入口
  var translatable = group.translatable || {};
  var translateCount = Number(translatable.count) || 0;
  var translatePartial = Number(translatable.partial) || 0;
  if (translateCount > 0 || translatePartial > 0) {
    var translateCap = translateCapability();
    var translateLabel = translateCount > 0
      ? '补翻译（' + fmtInt(translateCount) + '）'
      : '重新翻译（' + fmtInt(translatePartial) + '）';
    var translateBtn = el('button', {
      className: 'btn btn-ghost btn-sm subtitle-batch-translate-btn',
      attrs: { type: 'button' }, text: translateLabel
    });
    if (!translateCap.ok) {
      translateBtn.disabled = true;
      translateBtn.title = translateCap.reason;
    } else {
      translateBtn.title = '将字幕文本上传到 LLM 补翻译，新建翻译批次组（不上传音频/视频）';
      translateBtn.addEventListener('click', function () { translateGroup(translateBtn); });
    }
    box.appendChild(translateBtn);
  }
  var link = el('button', {
    className: 'btn btn-link btn-sm', attrs: { type: 'button' }, text: '在任务中心查看'
  });
  link.addEventListener('click', function () {
    // 任务中心默认标签是“扫描历史”，这里显式切到“字幕任务”
    state.taskTab = 'subtitles';
    window.location.hash = '#/history';
  });
  box.appendChild(link);
}

// ---------------------------------------------------------------------------
// 批次组：任务明细分页加载（GET /api/subtitles?group_id=&offset=&limit=&status=）
// mode: 'reset' 从头加载 / 'more' 追加下一页 / 'refresh' 刷新已加载区间
// v0034：非「全部」筛选时附带 status 参数，total 为过滤后数量
// ---------------------------------------------------------------------------
async function loadGroupJobs(mode) {
  var group = page.group;
  if (!group || !group.group_id) { return; }
  var jobs = page.jobs;
  if (jobs.loading) {
    // 筛选切换/提交重置需要立即重载：作废进行中的旧请求（seq 变化后其结果会被丢弃）
    if (mode !== 'reset') { return; }
    jobs.loading = false;
    jobs.seq += 1;
  }
  var offset = jobs.items.length;
  var limit = JOB_PAGE_LIMIT;
  var replace = false;
  if (mode === 'reset') {
    offset = 0;
    replace = true;
  } else if (mode === 'more') {
    if (jobs.items.length >= jobsTotal()) { return; }
    offset = jobs.items.length;
  } else if (mode === 'refresh') {
    if (!jobs.items.length) {
      offset = 0;
      replace = true;
    } else if (jobs.items.length <= JOB_REFRESH_MAX) {
      offset = 0;
      limit = jobs.items.length;
      replace = true;
    } else {
      return;
    }
  }
  jobs.loading = true;
  jobs.error = '';
  var seq = jobs.seq + 1;
  jobs.seq = seq;
  var filterKey = page.jobsFilter || 'all';
  // v0034：失败页签包含已取消任务（后端支持逗号组合 status）；其余页签一一对应。
  var statusParam = filterKey === 'failed' ? 'failed,cancelled' : filterKey;
  renderGroupJobs();
  try {
    var url = '/api/subtitles?group_id=' + encodeURIComponent(group.group_id)
      + '&offset=' + offset + '&limit=' + limit;
    if (filterKey !== 'all') { url += '&status=' + encodeURIComponent(statusParam); }
    var result = await api(url, { timeout: 30000 });
    if (page.jobs !== jobs || jobs.seq !== seq) { return; }
    var list = result && Array.isArray(result.jobs) ? result.jobs : [];
    var total = result && Number(result.total);
    jobs.totalKnown = isFinite(total) && total >= 0;
    jobs.total = jobs.totalKnown ? total : 0;
    jobs.offset = offset + list.length;
    var pageLimit = result && Number(result.limit);
    if (isFinite(pageLimit) && pageLimit > 0) { jobs.limit = pageLimit; }
    if (replace) { jobs.items = list; }
    else { jobs.items = jobs.items.concat(list); }
  } catch (err) {
    if (page.jobs !== jobs || jobs.seq !== seq) { return; }
    jobs.error = errorMessage(err, '加载任务明细失败。');
  } finally {
    if (page.jobs === jobs && jobs.seq === seq) {
      jobs.loading = false;
      renderGroupJobs();
    }
  }
}

// ---------------------------------------------------------------------------
// 批次组：轮询 / 终态提醒
// ---------------------------------------------------------------------------
function startPolling() {
  stopPolling();
  var status = page.group ? page.group.status : '';
  if (!page.group || !page.group.group_id || isGroupTerminal(status)) { return; }
  page.polling = true;
  page.timer = setTimeout(pollGroup, POLL_MS);
}

function stopPolling() {
  page.polling = false;
  if (page.timer) { clearTimeout(page.timer); page.timer = null; }
}

/** 批次组处于非终态（queued/running/paused）时持续轮询；终态只提醒一次。 */
async function pollGroup() {
  page.timer = null;
  var group = page.group;
  if (!group || !group.group_id) { return; }
  try {
    var result = await api('/api/subtitles/batch-groups/' + encodeURIComponent(group.group_id),
      { timeout: 15000 });
    if (page.group !== group) { return; }
    if (result && result.group) {
      page.group = result.group;
      saveLastGroupId(result.group.group_id);
      renderGroup();
      var sig = groupSignature(result.group);
      if (page.jobs.refreshKey !== sig && !page.jobs.loading) {
        page.jobs.refreshKey = sig;
        loadGroupJobs('refresh');
      }
    }
  } catch (err) {
    if (page.group !== group) { return; }
    if (err && err.status === 404) {
      // 批次组已被清理：清掉恢复锚点并收起进度卡，避免每次进视图反复 404
      clearLastGroupId();
      stopPolling();
      page.group = null;
      page.notified = true;
      renderGroup();
      toast('批次组不存在或已过期（可能已被清理）。', 'info', 6000);
      return;
    }
    setText(q('subtitleGroupStatus'), '刷新批次组进度失败，稍后重试');
  }
  // 视图切换后 stopSubtitlePagePolling 会置 polling=false：本轮响应仍更新界面，但不再续期
  if (!page.polling) { return; }
  if (page.group && !isGroupTerminal(page.group.status)) {
    if (!page.timer) { page.timer = setTimeout(pollGroup, POLL_MS); }
  } else {
    notifyGroupFinished();
  }
}

/** 批次组进入终态时提醒一次（切换视图后返回也补提醒）。 */
function notifyGroupFinished() {
  var group = page.group;
  if (!group || !isGroupTerminal(group.status) || page.notified) { return; }
  page.notified = true;
  var counts = group.counts || {};
  var done = Number(counts.done) || 0;
  var failed = Number(counts.failed) || 0;
  var cancelled = Number(counts.cancelled) || 0;
  if (group.status === 'done') {
    toast('批次组已完成：成功 ' + fmtInt(done) + ' 个'
      + (failed ? '，失败 ' + fmtInt(failed) + ' 个' : '')
      + (cancelled ? '，取消 ' + fmtInt(cancelled) + ' 个' : '') + '。',
      failed ? 'error' : 'success', 7000);
  } else if (group.status === 'failed') {
    toast('批次组结束：失败 ' + fmtInt(failed) + ' 个'
      + (done ? '，成功 ' + fmtInt(done) + ' 个' : '')
      + (cancelled ? '，取消 ' + fmtInt(cancelled) + ' 个' : '')
      + '，可重试失败项。', 'error', 8000);
  } else {
    toast('批次组已取消：完成 ' + fmtInt(done) + ' 个'
      + (failed ? '，失败 ' + fmtInt(failed) + ' 个' : '') + '。', 'info', 6000);
  }
}

async function groupAction(action, button) {
  var group = page.group;
  if (!group || !group.group_id) { return; }
  setBusy(button, true, '处理中…');
  try {
    var result = await api('/api/subtitles/batch-groups/'
      + encodeURIComponent(group.group_id) + '/' + action,
      { method: 'POST', timeout: 20000 });
    if (result && result.group) {
      page.notified = false;
      page.group = result.group;
      saveLastGroupId(result.group.group_id);
      page.jobs.refreshKey = '';
      renderGroup();
      if (!isGroupTerminal(result.group.status)) {
        startPolling();
      }
      loadGroupJobs('reset');
    }
  } catch (err) {
    toast(errorMessage(err, '批次组操作失败。'), 'error');
  } finally {
    setBusy(button, false);
    renderGroupActions();
  }
}

// ---------------------------------------------------------------------------
// v0035：补翻译请求（组级 / 单任务级）
// 只把字幕文本发给已配置的 LLM（不上传音频/视频），后端新建派生任务，源任务不变。
// ---------------------------------------------------------------------------
function notifyTranslateResult(result) {
  var summary = result && result.summary ? result.summary : {};
  var created = Number(result && result.created) || 0;
  var bits = ['已创建 ' + fmtInt(created) + ' 个翻译任务'];
  var already = Number(summary.already_translated) || 0;
  var missing = Number(summary.missing_source) || 0;
  var duplicates = Number(summary.duplicates) || 0;
  var rejected = Array.isArray(summary.rejected) ? summary.rejected.length : 0;
  if (already > 0) { bits.push('已在翻译队列 ' + fmtInt(already) + ' 个'); }
  if (missing > 0) { bits.push('源字幕缺失 ' + fmtInt(missing) + ' 个'); }
  if (duplicates > 0) { bits.push('重复跳过 ' + fmtInt(duplicates) + ' 个'); }
  if (rejected > 0) { bits.push('被拒绝 ' + fmtInt(rejected) + ' 个'); }
  toast(bits.join('，') + '。', created > 0 ? 'success' : 'info', 7000);
}

/** created>0 且后端返回新组快照时，切换为当前组（同 groupAction 的切换写法）。 */
function adoptTranslatedGroup(result) {
  var newGroup = result && result.group ? result.group : null;
  var created = Number(result && result.created) || 0;
  if (!newGroup || !newGroup.group_id || created <= 0) { return; }
  stopPolling();
  page.notified = false;
  page.batchesExpanded = false;
  page.group = newGroup;
  saveLastGroupId(newGroup.group_id);
  resetJobsView(groupSignature(newGroup));
  renderGroup();
  loadGroupJobs('reset');
  if (!isGroupTerminal(newGroup.status)) {
    startPolling();
  }
}

async function translateGroup(button) {
  var group = page.group;
  if (!group || !group.group_id) { return; }
  var cap = translateCapability();
  if (!cap.ok) { toast(cap.reason, 'error', 7000); return; }
  var translatable = group.translatable || {};
  var count = Number(translatable.count) || 0;
  var partial = Number(translatable.partial) || 0;
  var targetLabel = targetLanguageLabel(cap.targetLang);
  var message = '将把字幕文本（不含音频/视频）上传到已配置的 LLM 服务翻译为'
    + targetLabel + '，并新建一个翻译批次组（不会重新识别音频）。';
  if (count > 0) {
    message += '本次提交 ' + fmtInt(count) + ' 个已完成但无译文的任务。';
  }
  if (partial > 0) {
    message += '另有 ' + fmtInt(partial) + ' 个是“部分译文回退原文”，会重新翻译。';
  }
  var ok = await confirmDialog({
    title: '补翻译批次组',
    message: message,
    confirmText: '开始补翻译',
    danger: false
  });
  if (!ok) { return; }
  setBusy(button, true, '正在提交…');
  try {
    var result = await api('/api/subtitles/batch-groups/'
      + encodeURIComponent(group.group_id) + '/translate', {
      method: 'POST',
      body: { confirm_cloud: true, target_lang: cap.targetLang || 'zh' },
      timeout: 60000
    });
    notifyTranslateResult(result);
    adoptTranslatedGroup(result);
  } catch (err) {
    toast(errorMessage(err, '补翻译失败，请稍后重试。'), 'error', 7000);
  } finally {
    setBusy(button, false);
    renderGroupActions();
  }
}

async function translateJob(job, button) {
  if (!job || !job.job_id) { return; }
  var cap = translateCapability();
  if (!cap.ok) { toast(cap.reason, 'error', 7000); return; }
  var partial = job.translation_partial === true;
  var targetLabel = targetLanguageLabel(cap.targetLang);
  var message = '将把字幕文本（不含音频/视频）上传到已配置的 LLM 服务翻译为'
    + targetLabel + '，并新建一个翻译任务。'
    + (partial ? '该任务此前为“部分译文回退原文”，会重新翻译。' : '');
  var ok = await confirmDialog({
    title: partial ? '重新翻译字幕任务' : '补翻译字幕任务',
    message: message,
    confirmText: partial ? '重新翻译' : '开始补翻译',
    danger: false
  });
  if (!ok) { return; }
  setBusy(button, true, '正在提交…');
  try {
    var result = await api('/api/subtitles/' + encodeURIComponent(job.job_id) + '/translate', {
      method: 'POST',
      body: { confirm_cloud: true, target_lang: cap.targetLang || 'zh' },
      timeout: 60000
    });
    if (result && result.created === false) {
      toast('该任务已在翻译队列中，无需重复提交。', 'info', 6000);
    } else {
      toast('已创建翻译任务，可在任务中心查看进度。', 'success', 6000);
    }
    if (result && result.group && page.group && result.group.group_id === page.group.group_id) {
      page.group = result.group;
      renderGroup();
    }
    loadGroupJobs('refresh');
  } catch (err) {
    toast(errorMessage(err, '补翻译失败，请稍后重试。'), 'error', 7000);
  } finally {
    setBusy(button, false);
  }
}

// ---------------------------------------------------------------------------
// 会话恢复：最近一次批次组（group_id 存 localStorage）
// ---------------------------------------------------------------------------
async function restoreLastGroup() {
  if (page.restoring || page.group) { return; }
  var groupId = loadLastGroupId();
  if (!groupId) { return; }
  page.restoring = true;
  try {
    var result = await api('/api/subtitles/batch-groups/' + encodeURIComponent(groupId),
      { timeout: 15000 });
    var group = result && result.group ? result.group : null;
    if (!group || !group.group_id) {
      clearLastGroupId();
      return;
    }
    if (page.group) { return; } // 恢复期间用户已提交了新批次组，不覆盖
    page.group = group;
    page.batchesExpanded = false;
    // 恢复出来的终态不再重复提醒；非终态恢复轮询并刷新任务明细
    page.notified = isGroupTerminal(group.status);
    resetJobsView(groupSignature(group));
    renderGroup();
    loadGroupJobs('reset');
    if (!isGroupTerminal(group.status)) {
      startPolling();
    }
  } catch (err) {
    if (err && err.status === 404) {
      clearLastGroupId();
    }
    // 网络等其它错误保留 key，下次进入视图再尝试
  } finally {
    page.restoring = false;
  }
}

// ---------------------------------------------------------------------------
// 事件绑定 / 视图生命周期
// ---------------------------------------------------------------------------
function restorePageUi() {
  var saved = loadPageConfig();
  if (!saved) { return; }
  var input = q('subtitleDirPath');
  if (input && !input.value) { input.value = saved.dir; }
  var recursive = q('subtitleRecursive');
  if (recursive) { recursive.checked = saved.recursive !== false; }
  if (saved.mode === 'existing_srt') {
    var modeSrt = q('subtitleModeSrt');
    if (modeSrt) { modeSrt.checked = true; }
  }
}

export function initSubtitlePage() {
  if (page.initialized) { return; }
  page.initialized = true;
  restorePageUi();

  var pickBtn = q('subtitleDirPickBtn');
  if (pickBtn) {
    pickBtn.addEventListener('click', openDirPicker);
  }
  var enumerateBtn = q('subtitleEnumerateBtn');
  if (enumerateBtn) { enumerateBtn.addEventListener('click', enumerateNow); }
  var dirInput = q('subtitleDirPath');
  if (dirInput) {
    dirInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); enumerateNow(); }
    });
  }
  var filter = q('subtitleVideoFilter');
  if (filter) {
    filter.addEventListener('change', function () {
      page.filter = filter.value;
      page.shown = PAGE_LIMIT;
      renderVideos();
    });
  }
  var selectAll = q('subtitleSelectAllBtn');
  if (selectAll) {
    selectAll.addEventListener('click', function () {
      // v0032：全选 = 当前筛选结果全部（不再按单批上限截断，超出单次提交上限由禁用原因提示）
      var filtered = filteredItems();
      var limits = metaLimits();
      filtered.forEach(function (item) { page.selected.add(item.path); });
      renderVideos();
      if (filtered.length > limits.groupMax) {
        toast('当前筛选结果共 ' + fmtInt(filtered.length) + ' 个，超过单次提交上限 '
          + fmtInt(limits.groupMax) + ' 个；请分多次提交或用筛选缩小范围。', 'info', 8000);
      }
    });
  }
  var clearBtn = q('subtitleClearSelectBtn');
  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      page.selected = new Set();
      renderVideos();
    });
  }
  var moreBtn = q('subtitleVideosMoreBtn');
  if (moreBtn) {
    moreBtn.addEventListener('click', function () {
      page.shown += PAGE_LIMIT;
      renderVideos();
    });
  }
  var jobsMoreBtn = q('subtitleGroupJobsMoreBtn');
  if (jobsMoreBtn) {
    jobsMoreBtn.addEventListener('click', function () { loadGroupJobs('more'); });
  }
  // v0034：任务明细状态筛选页签
  var jobsFilterBox = q('subtitleJobsFilter');
  if (jobsFilterBox) {
    $$('button[data-jobs-filter]', jobsFilterBox).forEach(function (btn) {
      btn.addEventListener('click', function () { selectJobsFilter(btn.dataset.jobsFilter); });
    });
  }
  var batchesToggle = q('subtitleGroupBatchesToggle');
  if (batchesToggle) {
    batchesToggle.addEventListener('click', function () {
      page.batchesExpanded = !page.batchesExpanded;
      renderBatches();
    });
  }
  var skip = q('subtitleSkipExisting');
  if (skip) {
    skip.addEventListener('change', function () { page.skipTouched = true; });
  }
  var language = q('subtitleLanguage');
  if (language) {
    language.addEventListener('change', function () { language.dataset.userTouched = '1'; });
  }
  var target = q('subtitleTargetLang');
  if (target) {
    target.addEventListener('change', function () { target.dataset.userTouched = '1'; });
  }
  var translate = q('subtitleTranslate');
  if (translate) {
    translate.addEventListener('change', function () {
      var targetSel = q('subtitleTargetLang');
      if (targetSel) { targetSel.disabled = !translate.checked; }
      updateSubmitState();
    });
  }
  var confirm = q('subtitleBatchConfirm');
  if (confirm) { confirm.addEventListener('change', updateSubmitState); }
  var submit = q('subtitleSubmitBtn');
  if (submit) { submit.addEventListener('click', submitGroup); }
  var modeAsr = q('subtitleModeAsr');
  var modeSrt = q('subtitleModeSrt');
  function onModeChange() {
    page.selected = new Set();
    updateCapabilityUi();
    renderVideos();
    savePageConfig();
  }
  if (modeAsr) { modeAsr.addEventListener('change', onModeChange); }
  if (modeSrt) { modeSrt.addEventListener('change', onModeChange); }
  var recursiveCheck = q('subtitleRecursive');
  if (recursiveCheck) {
    recursiveCheck.addEventListener('change', function () {
      page.recursive = !!recursiveCheck.checked;
      savePageConfig();
    });
  }
}

export function showSubtitlePage() {
  initSubtitlePage();
  updateCapabilityUi();
  updateSubmitState();
  if (!page.group) {
    // 首次进入 / 刷新页面后：按 localStorage 恢复最近一次批次组
    restoreLastGroup();
    return;
  }
  renderGroup();
  if (!page.jobs.items.length && !page.jobs.loading) {
    loadGroupJobs('reset');
  }
  if (!isGroupTerminal(page.group.status)) {
    if (!page.timer) { startPolling(); }
  } else {
    notifyGroupFinished();
  }
}

export function stopSubtitlePagePolling() {
  stopPolling();
}
