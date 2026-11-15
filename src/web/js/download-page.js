/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 下载页（v0051，ES module）
   直链 / m3u8 提交、队列与任务行、暂停继续取消重试。
   只做 DOM 构造（不使用 innerHTML）；进度条宽度用 node.style 设置。
   ========================================================================== */

import { api, errorMessage } from './api.js';
import { els, state } from './state.js';
import { clear, el, hide, setHidden, setText, show } from './dom.js';
import { fmtBytes, fmtDateTime, fmtElapsed } from './format.js';
import { toast } from './toast.js';

var POLL_MS = 2000;

var STATUS_TEXT = {
  queued: '排队中',
  running: '下载中',
  paused: '已暂停',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消'
};

var STATUS_BADGE = {
  queued: 'badge-gray',
  running: 'badge-blue',
  paused: 'badge-amber',
  done: 'badge-green',
  failed: 'badge-red',
  cancelled: 'badge-gray'
};

function downloadState() {
  return state.download;
}

function progressBar(percent) {
  var fill = el('div', { className: 'progress-fill' });
  var track = el('div', {
    className: 'progress-bar',
    attrs: { role: 'progressbar', 'aria-valuemin': '0',
             'aria-valuemax': '100', 'aria-valuenow': String(percent || 0) }
  }, [fill]);
  fill.style.width = Math.max(0, Math.min(100, Number(percent) || 0)) + '%';
  return track;
}

function jobBadge(job) {
  var status = job.status || 'queued';
  return el('span', {
    className: 'badge ' + (STATUS_BADGE[status] || 'badge-gray'),
    text: STATUS_TEXT[status] || status
  });
}

function stageText(job) {
  var label = job.stage_label || '';
  var parts = [];
  if (label) { parts.push(label); }
  if (job.segments_total) {
    parts.push('分片 ' + (job.segments_done || 0) + '/' + job.segments_total);
  }
  if (job.status === 'running') {
    if (job.speed_bps) { parts.push(fmtBytes(job.speed_bps) + '/s'); }
    if (job.eta_seconds) { parts.push('剩余 ' + fmtElapsed(job.eta_seconds)); }
  }
  return parts.join(' · ');
}

function jobMeta(job) {
  var items = [];
  items.push('创建 ' + fmtDateTime(job.created_at));
  if (job.duration_sec) { items.push('耗时 ' + fmtElapsed(job.duration_sec)); }
  if (job.variant) { items.push('清晰度 ' + job.variant); }
  if (job.encryption && job.encryption !== '未加密') {
    items.push(job.encryption);
  }
  if (job.bytes_total) {
    items.push(fmtBytes(job.bytes_done) + ' / ' + fmtBytes(job.bytes_total));
  } else if (job.bytes_done) {
    items.push(fmtBytes(job.bytes_done));
  }
  return items.join(' · ');
}

function actionButton(label, className, handler) {
  return el('button', {
    className: 'btn btn-sm ' + (className || 'btn-ghost'),
    attrs: { type: 'button' },
    text: label,
    on: { click: handler }
  });
}

function renderJobRow(job) {
  var info = el('div', { className: 'download-row-info' }, [
    el('div', { className: 'download-row-title' }, [
      jobBadge(job),
      el('span', { className: 'download-row-name', text: job.title || job.url || '未命名' })
    ]),
    el('div', { className: 'muted download-row-stage', text: stageText(job) }),
    el('div', { className: 'muted download-row-meta', text: jobMeta(job) })
  ]);
  if (job.status === 'failed' && job.error_message) {
    info.appendChild(el('div', {
      className: 'field-error download-row-error',
      text: job.error_message
    }));
  }
  if (job.status === 'done' && job.output_path) {
    info.appendChild(el('div', {
      className: 'muted download-row-path',
      text: job.output_path
    }));
  }
  if (job.status === 'running' || job.status === 'queued') {
    info.appendChild(progressBar(job.progress || 0));
  }

  var actions = el('div', { className: 'download-row-actions' });
  if (job.status === 'running' || job.status === 'queued') {
    actions.appendChild(actionButton('暂停', 'btn-ghost', function () {
      return downloadAction(job.job_id, 'pause');
    }));
    actions.appendChild(actionButton('取消', 'btn-danger-ghost', function () {
      return downloadAction(job.job_id, 'cancel');
    }));
  } else if (job.status === 'paused') {
    actions.appendChild(actionButton('继续', 'btn-primary', function () {
      return downloadAction(job.job_id, 'resume');
    }));
    actions.appendChild(actionButton('取消', 'btn-danger-ghost', function () {
      return downloadAction(job.job_id, 'cancel');
    }));
  } else if (job.status === 'failed' || job.status === 'cancelled') {
    actions.appendChild(actionButton('重试', 'btn-primary', function () {
      return downloadAction(job.job_id, 'retry');
    }));
  }
  if (job.status === 'done' && job.output_name) {
    actions.appendChild(el('a', {
      className: 'btn btn-ghost btn-sm',
      attrs: { href: '/api/downloads/' + encodeURIComponent(job.job_id) + '/file',
               download: job.output_name },
      text: '下载文件'
    }));
  }
  if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') {
    actions.appendChild(actionButton('删除', 'btn-link btn-sm', function () {
      return deleteJob(job.job_id);
    }));
  }
  if (job.group_id && job.status !== 'done' && job.status !== 'cancelled') {
    actions.appendChild(actionButton('整组取消', 'btn-link btn-sm', function () {
      return groupAction(job.group_id, 'cancel');
    }));
  }
  return el('div', { className: 'download-row', dataset: { jobId: job.job_id } },
           [info, actions]);
}

function renderJobs(snapshot) {
  var list = els.downloadJobsList;
  if (!list) { return; }
  clear(list);
  var active = (snapshot && snapshot.active) || [];
  var queued = (snapshot && snapshot.queued) || [];
  var paused = (snapshot && snapshot.paused) || [];
  var recent = (snapshot && snapshot.recent) || [];
  var rows = active.concat(queued, paused, recent);
  rows.forEach(function (job) { list.appendChild(renderJobRow(job)); });
  setHidden(els.downloadEmpty, rows.length > 0);
  setHidden(els.downloadRecentEmpty, true);

  var counts = (snapshot && snapshot.counts) || {};
  setText(els.downloadQueueHint,
    '进行中 ' + (counts.running || 0) +
    ' · 排队 ' + (counts.queued || 0) +
    ' · 暂停 ' + (counts.paused || 0) +
    ' · 已完成 ' + (counts.done || 0) +
    ' · 失败 ' + (counts.failed || 0));
  setText(els.downloadConcurrencyHint,
    '同时下载 ' + (snapshot && snapshot.concurrency ? snapshot.concurrency : '—') + ' 个');
}

function renderError(message) {
  if (!els.downloadListError) { return; }
  setText(els.downloadListError, message || '');
  setHidden(els.downloadListError, !message);
}

async function refreshDownloadQueue() {
  var ds = downloadState();
  if (!els.downloadJobsList || ds.loading) { return; }
  ds.loading = true;
  try {
    var snapshot = await api('/api/downloads/queue', { timeout: 15000 });
    ds.snapshot = snapshot;
    ds.loaded = true;
    renderError('');
    renderJobs(snapshot);
  } catch (err) {
    ds.lastError = errorMessage(err, '加载下载任务失败。');
    renderError(ds.lastError);
  } finally {
    ds.loading = false;
  }
}

function stopDownloadPolling() {
  var ds = downloadState();
  if (ds.timer) {
    clearInterval(ds.timer);
    ds.timer = null;
  }
}

function startDownloadPolling() {
  stopDownloadPolling();
  var ds = downloadState();
  ds.timer = setInterval(function () {
    if (state.view !== 'download') { stopDownloadPolling(); return; }
    refreshDownloadQueue();
  }, POLL_MS);
}

async function downloadAction(jobId, action) {
  try {
    await api('/api/downloads/' + encodeURIComponent(jobId) + '/' + action,
              { method: 'POST', timeout: 20000 });
    toast(action === 'cancel' ? '已提交取消' : '操作已提交', 'success');
  } catch (err) {
    toast(errorMessage(err, '操作失败。'), 'error');
  }
  refreshDownloadQueue();
}

async function groupAction(groupId, action) {
  try {
    await api('/api/downloads/groups/' + encodeURIComponent(groupId) + '/' + action,
              { method: 'POST', timeout: 20000 });
    toast('已提交整组操作', 'success');
  } catch (err) {
    toast(errorMessage(err, '整组操作失败。'), 'error');
  }
  refreshDownloadQueue();
}

async function deleteJob(jobId) {
  try {
    await api('/api/downloads/' + encodeURIComponent(jobId), {
      method: 'DELETE', timeout: 20000
    });
    toast('任务已删除', 'success');
  } catch (err) {
    toast(errorMessage(err, '删除失败。'), 'error');
  }
  refreshDownloadQueue();
}

function readOptions() {
  var options = {};
  var maxHeight = els.downloadMaxHeight ? String(els.downloadMaxHeight.value) : '';
  if (maxHeight !== '') { options.max_height = parseInt(maxHeight, 10) || 0; }
  var subdirMode = els.downloadSubdirMode ? String(els.downloadSubdirMode.value) : '';
  if (subdirMode) { options.subdir_mode = subdirMode; }
  if (els.downloadOverwrite && els.downloadOverwrite.checked) {
    options.overwrite = true;
  }
  if (els.downloadKeepTemp && els.downloadKeepTemp.checked) {
    options.keep_temp = true;
  }
  var dir = els.downloadDirPath ? String(els.downloadDirPath.value || '').trim() : '';
  if (dir) { options.output_dir = dir; }
  return options;
}

// ---------------------------------------------------------------------
// v0052：播放网页解析
// ---------------------------------------------------------------------
var KIND_LABEL = { hls: 'HLS', dash: 'DASH', file: '直链', embed: '嵌入页' };

var SOURCE_LABEL = {
  'video-tag': '页面标签',
  'link-preload': '预加载',
  meta: '社交卡片',
  'json-ld': '结构化数据',
  script: '脚本文本',
  direct: '播放列表',
  curl: 'cURL'
};

function parseError(message) {
  if (!els.downloadParseError) { return; }
  setText(els.downloadParseError, message || '');
  setHidden(els.downloadParseError, !message);
}

function setParseBusy(busy) {
  var ds = downloadState();
  ds.parseBusy = !!busy;
  setHidden(els.downloadParseLoading, !busy);
  if (els.downloadParseBtn) { els.downloadParseBtn.disabled = !!busy; }
  if (els.downloadParseManualBtn) { els.downloadParseManualBtn.disabled = !!busy; }
}

function candidateRow(item, index) {
  var box = el('input', {
    attrs: { type: 'checkbox', 'data-index': String(index) }
  });
  box.checked = item.valid !== false && item.kind !== 'embed';
  var badge = el('span', {
    className: 'badge ' + (item.kind === 'hls' ? 'badge-blue'
      : (item.kind === 'embed' ? 'badge-gray' : 'badge-green')),
    text: KIND_LABEL[item.kind] || item.kind || '直链'
  });
  var valid = null;
  if (item.valid === true) {
    valid = el('span', { className: 'badge badge-green', text: '已校验' });
  } else if (item.valid === false) {
    valid = el('span', { className: 'badge badge-red', text: '校验失败' });
  }
  var metaParts = [];
  if (item.label) { metaParts.push('键名 ' + item.label); }
  if (item.source) {
    metaParts.push(SOURCE_LABEL[item.source] || item.source);
  }
  if (item.note) { metaParts.push(item.note); }
  return el('label', { className: 'download-candidate' }, [
    box,
    badge,
    el('span', { className: 'download-candidate-url', text: item.url || '' }),
    valid,
    metaParts.length ? el('span', {
      className: 'muted download-candidate-meta', text: metaParts.join(' · ')
    }) : null
  ]);
}

function renderCandidates() {
  var ds = downloadState();
  var list = els.downloadCandidatesList;
  if (!list) { return; }
  clear(list);
  (ds.parseCandidates || []).forEach(function (item, index) {
    list.appendChild(candidateRow(item, index));
  });
  setHidden(els.downloadParseResult, false);
  setHidden(els.downloadParseEmpty, (ds.parseCandidates || []).length > 0);
}

function applyParseResult(res, options) {
  var ds = downloadState();
  ds.parseCandidates = (res && res.candidates) || [];
  ds.parseSession = (res && res.parse_session) || '';
  ds.parseSeq += 1;
  parseError('');
  renderCandidates();
  var summary = (res && res.summary) || '';
  setText(els.downloadParseSummary,
    summary ? ('共 ' + ds.parseCandidates.length + ' 个候选（' + summary + '）')
      : ('共 ' + ds.parseCandidates.length + ' 个候选'));
  setText(els.downloadParseResultHeading,
    (res && res.title) ? ('候选地址 — ' + res.title) : '候选地址');
  if (res && res.hint) {
    parseError(res.hint);
  } else if (!ds.parseCandidates.length) {
    parseError('没有找到媒体地址。');
  }
  var hint = [];
  if (res && res.page_url) { hint.push('页面 ' + res.page_url); }
  if (ds.parseSession) { hint.push('已暂存登录态（15 分钟）'); }
  setText(els.downloadParseManualHint, hint.join(' · '));
  if (options && options.scroll) {
    var target = els.downloadParseResult;
    if (target && target.scrollIntoView) {
      try { target.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
      catch (e) { target.scrollIntoView(); }
    }
  }
}

async function runParse(body, options) {
  var ds = downloadState();
  if (ds.parseBusy) { return; }
  setParseBusy(true);
  parseError('');
  try {
    var res = await api('/api/downloads/parse', {
      method: 'POST', body: body, timeout: 60000
    });
    applyParseResult(res, options);
    toast('解析完成，共 ' + (res.candidates || []).length + ' 个候选', 'success');
  } catch (err) {
    setHidden(els.downloadParseResult, true);
    parseError(errorMessage(err, '解析失败。'));
  } finally {
    setParseBusy(false);
  }
}

function parsePage() {
  var url = els.downloadParseUrl ? String(els.downloadParseUrl.value || '').trim() : '';
  if (!url) {
    parseError('请先填写网页地址。');
    return;
  }
  runParse({ url: url }, { scroll: true });
}

function parseManual() {
  var html = els.downloadParseHtml ? String(els.downloadParseHtml.value || '').trim() : '';
  var curl = els.downloadParseCurl ? String(els.downloadParseCurl.value || '').trim() : '';
  if (!html && !curl) {
    parseError('请粘贴网页源码或 cURL 命令。');
    return;
  }
  var body = {};
  if (html) { body.html = html; }
  if (curl) { body.curl = curl; }
  var url = els.downloadParseUrl ? String(els.downloadParseUrl.value || '').trim() : '';
  if (url) { body.url = url; }
  runParse(body, { scroll: true });
}

function selectedCandidateUrls() {
  var ds = downloadState();
  var list = els.downloadCandidatesList;
  if (!list) { return []; }
  var boxes = Array.prototype.slice.call(
    list.querySelectorAll('input[type="checkbox"]'));
  var urls = [];
  boxes.forEach(function (box) {
    if (!box.checked) { return; }
    var index = parseInt(box.getAttribute('data-index'), 10);
    var item = (ds.parseCandidates || [])[index];
    if (item && item.url) { urls.push(item.url); }
  });
  return urls;
}

function applyCandidates() {
  var urls = selectedCandidateUrls();
  if (!urls.length) {
    toast('请先勾选要下载的地址', 'error');
    return;
  }
  if (!els.downloadUrls) { return; }
  var existing = String(els.downloadUrls.value || '').split(/\r?\n/)
    .map(function (line) { return line.trim(); })
    .filter(Boolean);
  var added = 0;
  urls.forEach(function (url) {
    if (existing.indexOf(url) === -1) {
      existing.push(url);
      added += 1;
    }
  });
  els.downloadUrls.value = existing.join('\n');
  setText(els.downloadApplyHint,
    '已加入 ' + added + ' 个地址（共 ' + existing.length + ' 行）');
  toast('已加入 ' + added + ' 个下载地址', 'success');
  updateSubmitHint();
}

function toggleAllCandidates() {
  var ds = downloadState();
  var list = els.downloadCandidatesList;
  if (!list) { return; }
  var boxes = Array.prototype.slice.call(
    list.querySelectorAll('input[type="checkbox"]'));
  var allChecked = boxes.length > 0 && boxes.every(function (box) {
    return box.checked;
  });
  boxes.forEach(function (box) {
    var index = parseInt(box.getAttribute('data-index'), 10);
    var item = (ds.parseCandidates || [])[index];
    box.checked = !allChecked && (!item || item.kind !== 'embed');
  });
}

function clearCandidates() {
  var ds = downloadState();
  ds.parseCandidates = [];
  ds.parseSession = '';
  clear(els.downloadCandidatesList);
  setHidden(els.downloadParseResult, true);
  setHidden(els.downloadParseEmpty, true);
  setText(els.downloadParseSummary, '');
  setText(els.downloadApplyHint, '');
  parseError('');
}

async function submitDownloads() {
  var ds = downloadState();
  if (ds.submitting || !els.downloadUrls) { return; }
  var raw = String(els.downloadUrls.value || '');
  var urls = raw.split(/\r?\n/).map(function (line) {
    return line.trim();
  }).filter(function (line) {
    return line && line.charAt(0) !== '#';
  });
  if (!urls.length) {
    setText(els.downloadSubmitError, '请至少填写一个下载地址（每行一个）。');
    show(els.downloadSubmitError);
    return;
  }
  hide(els.downloadSubmitError);
  ds.submitting = true;
  if (els.downloadSubmitBtn) { els.downloadSubmitBtn.disabled = true; }
  try {
    var submitOptions = readOptions();
    if (ds.parseSession) { submitOptions.parse_session = ds.parseSession; }
    var payload = { urls: urls, options: submitOptions };
    var res = await api('/api/downloads', {
      method: 'POST', body: payload, timeout: 30000
    });
    var created = (res && res.jobs) || [];
    toast('已提交 ' + created.length + ' 个下载任务', 'success');
    els.downloadUrls.value = '';
    refreshDownloadQueue();
  } catch (err) {
    setText(els.downloadSubmitError, errorMessage(err, '提交失败。'));
    show(els.downloadSubmitError);
  } finally {
    ds.submitting = false;
    if (els.downloadSubmitBtn) { els.downloadSubmitBtn.disabled = false; }
    updateSubmitHint();
  }
}

function updateSubmitHint() {
  if (!els.downloadSubmitHint) { return; }
  var ds = downloadState();
  var snapshot = ds.snapshot || {};
  var counts = snapshot.counts || {};
  var parts = [];
  if (counts.running) { parts.push('进行中 ' + counts.running); }
  if (counts.queued) { parts.push('排队 ' + counts.queued); }
  setText(els.downloadSubmitHint,
    parts.length ? parts.join(' · ') : '提交后自动开始，超出并发会排队。');
}

function updatePageBadge() {
  var meta = state.meta || {};
  var download = meta.download || {};
  var enabled = download.enabled === true;
  var badge = els.downloadPageStatusBadge;
  if (badge) {
    setText(badge, enabled ? '已启用' : '未启用');
    badge.className = 'badge ' + (enabled ? 'badge-green' : 'badge-gray');
  }
  var settingsBadge = els.downloadStatusBadgeSettings;
  if (settingsBadge) {
    setText(settingsBadge, enabled ? '已启用' : '未启用');
    settingsBadge.className = 'badge ' + (enabled ? 'badge-green' : 'badge-gray');
  }
  if (els.downloadSubmitBtn) {
    els.downloadSubmitBtn.disabled = !enabled;
    els.downloadSubmitBtn.title = enabled ? '' : '下载功能未启用，请先在设置中开启';
  }
  var defaultDir = (state.settings && state.settings.download_output_dir) || '';
  if (els.downloadDirPath && !els.downloadDirPath.value && defaultDir) {
    els.downloadDirPath.placeholder = defaultDir;
  }
}

export function showDownloadPage() {
  updatePageBadge();
  refreshDownloadQueue();
  startDownloadPolling();
}

export function hideDownloadPage() {
  stopDownloadPolling();
}

export function initDownloadPage() {
  if (els.downloadSubmitBtn) {
    els.downloadSubmitBtn.addEventListener('click', submitDownloads);
  }
  if (els.downloadRefreshBtn) {
    els.downloadRefreshBtn.addEventListener('click', function () {
      refreshDownloadQueue();
    });
  }
  if (els.downloadDirPickBtn) {
    els.downloadDirPickBtn.addEventListener('click', function () {
      if (typeof state.openDownloadDirPicker !== 'function') { return; }
      state.openDownloadDirPicker({
        initial: els.downloadDirPath ? els.downloadDirPath.value : '',
        onConfirm: function (path) {
          if (els.downloadDirPath) { els.downloadDirPath.value = path || ''; }
        }
      });
    });
  }
  // v0052：播放网页解析
  if (els.downloadParseBtn) {
    els.downloadParseBtn.addEventListener('click', parsePage);
  }
  if (els.downloadParseUrl) {
    els.downloadParseUrl.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); parsePage(); }
    });
  }
  if (els.downloadParseManualBtn) {
    els.downloadParseManualBtn.addEventListener('click', parseManual);
  }
  if (els.downloadApplyCandidatesBtn) {
    els.downloadApplyCandidatesBtn.addEventListener('click', applyCandidates);
  }
  if (els.downloadSelectAllCandidatesBtn) {
    els.downloadSelectAllCandidatesBtn.addEventListener('click',
      toggleAllCandidates);
  }
  if (els.downloadClearCandidatesBtn) {
    els.downloadClearCandidatesBtn.addEventListener('click', clearCandidates);
  }
  updateSubmitHint();
}
