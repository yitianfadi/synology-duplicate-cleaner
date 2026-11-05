/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 目录批量字幕页（ES module，v0028）
   --------------------------------------------------------------------------
   与查重解耦的独立入口：选择目录 → 枚举视频 → 勾选 → 批量生成/翻译字幕。
   字幕写回视频同目录（只写 .srt）；写回失败仍可在任务中心下载。
   规范：ES2019、无第三方依赖、不使用 innerHTML、只做安全文本渲染。
   契约：docs/SUBTITLE_BATCH_CONTRACT.md
   ========================================================================== */

import { api, errorMessage } from './api.js';
import { state } from './state.js';
import {
  byId, clear, el, setBusy, setHidden, setText, show
} from './dom.js';
import { fmtBytes, fmtInt } from './format.js';
import { toast } from './toast.js';
import { confirmDialog } from './modal.js';

var PAGE_LIMIT = 200;
var POLL_MS = 1500;
var MAX_BATCH = 500;

var STATUS_LABEL = {
  queued: '排队中', running: '识别中', translating: '翻译中',
  paused: '已暂停', done: '已完成', failed: '失败', cancelled: '已取消'
};
var STATUS_CLASS = {
  queued: 'badge-gray', running: 'badge-blue', translating: 'badge-blue',
  paused: 'badge-amber', done: 'badge-green', failed: 'badge-red',
  cancelled: 'badge-gray'
};

var page = {
  initialized: false,
  dir: '',
  recursive: true,
  items: [],
  truncated: false,
  totalBytes: 0,
  selected: new Set(),
  filter: 'all',
  shown: PAGE_LIMIT,
  batch: null,
  timer: null,
  busy: false,
  enumerated: false,
  skipTouched: false
};

function q(id) { return byId(id); }

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

function modeRequirement(supported) {
  // 返回 {ok, message}：当前任务类型所需的凭据是否就绪
  var cap = capability();
  var mode = modeValue();
  if (!supported || !cap.supported) {
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

function updateSubmitState() {
  var confirm = q('subtitleBatchConfirm');
  var submit = q('subtitleSubmitBtn');
  var hint = q('subtitleSelectionHint');
  var count = page.selected.size;
  var requirement = modeRequirement(true);
  if (hint) {
    setText(hint, count
      ? '已选择 ' + count + ' 个视频' + (count > MAX_BATCH ? '（超过单批上限 ' + MAX_BATCH + '，请分批提交）' : '')
      : '尚未选择视频');
  }
  if (!submit) { return; }
  var ready = requirement.ok && count > 0 && count <= MAX_BATCH
    && confirm && confirm.checked && !page.busy;
  submit.disabled = !ready;
  submit.title = ready ? '' : (requirement.message || (
    !count ? '请先勾选视频'
      : count > MAX_BATCH ? '单批最多 ' + MAX_BATCH + ' 个视频'
        : '请先确认上传第三方服务'));
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
        max_items: 5000
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
// 提交批次
// ---------------------------------------------------------------------------
async function submitBatch() {
  if (page.busy) { return; }
  var cap = capability();
  var confirm = q('subtitleBatchConfirm');
  if (!confirm || !confirm.checked) { return; }
  var mode = modeValue();
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
  if (eligible.length > 20) {
    var ok = await confirmDialog({
      title: '确认批量生成字幕',
      message: '将提交 ' + eligible.length + ' 个视频的字幕任务，音频/字幕文本会上传到第三方服务，可能产生费用。确定继续？',
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
    var result = await api('/api/subtitles/batch', {
      method: 'POST',
      body: {
        paths: eligible,
        source: mode,
        language: (q('subtitleLanguage') || {}).value || 'auto',
        translate: translate,
        target_lang: (q('subtitleTargetLang') || {}).value || 'zh',
        skip_existing: !!(q('subtitleSkipExisting') && q('subtitleSkipExisting').checked),
        confirm_cloud: true
      },
      timeout: 120000
    });
    page.batch = result.batch || null;
    page.selected = new Set();
    renderVideos();
    renderBatch();
    startPolling();
    var rejected = page.batch && page.batch.rejected ? page.batch.rejected.length : 0;
    toast('已提交 ' + (page.batch ? page.batch.total : 0) + ' 个字幕任务'
      + (rejected ? '，' + rejected + ' 个被拒绝' : '') + '。',
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
// 批次进度
// ---------------------------------------------------------------------------
function startPolling() {
  stopPolling();
  page.timer = setTimeout(pollBatch, POLL_MS);
}

function stopPolling() {
  if (page.timer) { clearTimeout(page.timer); page.timer = null; }
}

function isTerminal(status) {
  return status === 'done' || status === 'failed' || status === 'cancelled';
}

async function pollBatch() {
  page.timer = null;
  if (!page.batch || !page.batch.batch_id) { return; }
  try {
    var result = await api('/api/subtitles/batches/' + encodeURIComponent(page.batch.batch_id),
      { timeout: 15000 });
    page.batch = result.batch || page.batch;
    renderBatch();
  } catch (err) {
    setText(q('subtitleBatchStatus'), '刷新批次失败，稍后重试');
  }
  if (page.batch && !isTerminal(page.batch.status)) {
    page.timer = setTimeout(pollBatch, POLL_MS);
  }
}

function renderBatch() {
  var card = q('subtitleBatchCard');
  if (!card) { return; }
  var batch = page.batch;
  if (!batch) { setHidden(card, true); return; }
  setHidden(card, false);
  var counts = batch.counts || {};
  var total = Number(batch.total) || 0;
  var finished = (counts.done || 0) + (counts.failed || 0) + (counts.cancelled || 0);
  var pct = total ? Math.round(100 * finished / total) : 0;
  setText(q('subtitleBatchStatus'),
    (STATUS_LABEL[batch.status] || batch.status || '处理中') + ' · ' + finished + '/' + total);
  var fill = q('subtitleBatchBar');
  if (fill) { fill.style.width = pct + '%'; }
  setText(q('subtitleBatchCounts'),
    '完成 ' + (counts.done || 0) + ' · 失败 ' + (counts.failed || 0)
    + ' · 取消 ' + (counts.cancelled || 0) + ' · 暂停 ' + (counts.paused || 0)
    + ' · 排队 ' + (counts.queued || 0));
  var list = q('subtitleBatchList');
  if (list) {
    clear(list);
    (batch.jobs || []).forEach(function (job) {
      list.appendChild(renderBatchJobRow(job));
    });
    if (batch.rejected && batch.rejected.length) {
      list.appendChild(el('div', { className: 'subtitle-batch-rejected' }, [
        el('p', { className: 'hint', text: '以下路径被拒绝（共 ' + batch.rejected.length + ' 个）：' }),
        el('div', { className: 'subtitle-batch-rejected-list' },
          batch.rejected.slice(0, 10).map(function (item) {
            return el('div', { className: 'mono', text: item.path + ' — ' + item.message });
          }))
      ]));
    }
  }
  renderBatchActions(batch);
}

function renderBatchJobRow(job) {
  var name = String(job.path || '').split(/[\\/]/).pop();
  var badges = [
    el('span', {
      className: 'badge ' + (STATUS_CLASS[job.status] || 'badge-gray'),
      text: STATUS_LABEL[job.status] || job.status
    })
  ];
  if (job.skipped) {
    badges.push(el('span', { className: 'badge badge-gray', text: '已跳过' }));
  }
  if (job.warning === 'OUTPUT_NOT_WRITABLE') {
    badges.push(el('span', { className: 'badge badge-amber', text: '字幕未写回（可下载）' }));
  } else if (job.warning === 'OUTPUT_WRITE_PARTIAL') {
    badges.push(el('span', { className: 'badge badge-amber', text: '部分写回失败' }));
  }
  var outputs = Array.isArray(job.outputs) ? job.outputs : [];
  if (outputs.length) {
    badges.push(el('span', {
      className: 'badge badge-green',
      text: '已写回 ' + outputs.length + ' 个 srt'
    }));
  }
  return el('div', { className: 'subtitle-batch-row' }, [
    el('div', { className: 'subtitle-batch-main' }, [
      el('span', { className: 'subtitle-batch-path', attrs: { title: job.path }, text: name }),
      el('span', { className: 'subtitle-batch-meta' }, badges)
    ])
  ]);
}

function renderBatchActions(batch) {
  var box = q('subtitleBatchActions');
  if (!box) { return; }
  clear(box);
  var counts = batch.counts || {};
  var active = (counts.queued || 0) + (counts.running || 0) + (counts.translating || 0);
  function actionBtn(label, action, primary) {
    var btn = el('button', {
      className: 'btn ' + (primary ? 'btn-primary' : 'btn-ghost') + ' btn-sm',
      attrs: { type: 'button' }, text: label
    });
    btn.addEventListener('click', function () { batchAction(action, btn); });
    box.appendChild(btn);
  }
  if (active > 0) {
    actionBtn('暂停', 'pause', false);
  }
  if ((counts.paused || 0) > 0) {
    actionBtn('继续', 'resume', true);
  }
  if (!isTerminal(batch.status)) {
    actionBtn('取消剩余', 'cancel', false);
  }
  if ((counts.failed || 0) + (counts.cancelled || 0) > 0) {
    actionBtn('重试失败项', 'retry', false);
  }
  var link = el('button', {
    className: 'btn btn-link btn-sm', attrs: { type: 'button' }, text: '在任务中心查看'
  });
  link.addEventListener('click', function () {
    window.location.hash = '#/history';
  });
  box.appendChild(link);
}

async function batchAction(action, button) {
  if (!page.batch || !page.batch.batch_id) { return; }
  setBusy(button, true, '处理中…');
  try {
    var result = await api('/api/subtitles/batches/'
      + encodeURIComponent(page.batch.batch_id) + '/' + action,
      { method: 'POST', timeout: 20000 });
    page.batch = result.batch || page.batch;
    renderBatch();
    startPolling();
  } catch (err) {
    toast(errorMessage(err, '批次操作失败。'), 'error');
  } finally {
    setBusy(button, false);
  }
}

// ---------------------------------------------------------------------------
// 事件绑定 / 视图生命周期
// ---------------------------------------------------------------------------
export function initSubtitlePage() {
  if (page.initialized) { return; }
  page.initialized = true;

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
      filteredItems().forEach(function (item) { page.selected.add(item.path); });
      renderVideos();
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
  if (submit) { submit.addEventListener('click', submitBatch); }
  var modeAsr = q('subtitleModeAsr');
  var modeSrt = q('subtitleModeSrt');
  function onModeChange() {
    page.selected = new Set();
    updateCapabilityUi();
    renderVideos();
  }
  if (modeAsr) { modeAsr.addEventListener('change', onModeChange); }
  if (modeSrt) { modeSrt.addEventListener('change', onModeChange); }
}

export function showSubtitlePage() {
  initSubtitlePage();
  updateCapabilityUi();
  updateSubmitState();
}

export function stopSubtitlePagePolling() {
  stopPolling();
}
