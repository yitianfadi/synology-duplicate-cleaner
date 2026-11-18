/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 字幕任务（v0057 从 app.js 拆出；字幕页「任务」标签）
   --------------------------------------------------------------------------
   行为与拆出前完全一致；依赖 js/ 下的基础模块，不依赖 app.js。ES2019。
   规范：不使用 innerHTML，不可信文本一律 textContent。
   ========================================================================== */

import {
  api, errorMessage
} from './api.js';
import {
  POLL_MS, SUBTITLE_ACTIVE_STATUS, SUBTITLE_FAILURE_LABEL, SUBTITLE_RUNNING_STATUS,
  SUBTITLE_STEP_LABEL, SUBTITLE_STEP_STATUS_LABEL
} from './constants.js';
import {
  byId, clear, el, icon, setBusy, setHidden, setText
} from './dom.js';
import {
  fmtBytes, fmtDateTime, fmtElapsed, fmtInt
} from './format.js';
import {
  closeModal, confirmDialog, openModal
} from './modal.js';
import {
  els, state
} from './state.js';
import { toast } from './toast.js';
import {
  clamp, encodePath
} from './util.js';
import {
  firstFiniteNumber,
  isSafeSubtitleFileName,
  isTerminalSubtitleStatus,
  renderSubtitleOutputs,
  subtitleCapability,
  subtitleJobStatusClass,
  subtitleJobStatusLabel,
  subtitleJobTranslatable,
  subtitleOutputModeLabel,
  subtitleSourceLabel,
  subtitleStageLabel,
  subtitleTargetLangLabel,
  subtitleWarningText
} from './subtitle-shared.js';

export var SUBTITLE_JOBS_POLL_MS = 1500;

function normalizeSubtitleJobsResponse(res) {
  if (!res) { return []; }
  var list = [];
  if (Array.isArray(res)) { list = res; }
  else if (Array.isArray(res.jobs)) { list = res.jobs; }
  else if (Array.isArray(res.subtitles)) { list = res.subtitles; }
  else if (Array.isArray(res.items)) { list = res.items; }
  else if (Array.isArray(res.data)) { list = res.data; }
  return list.filter(function (job) { return job && job.job_id; });
}

function anyActiveSubtitleJob(list) {
  var jobs = Array.isArray(list) ? list : (state.subtitleJobs ? state.subtitleJobs.list : []);
  return jobs.some(function (job) {
    return job && SUBTITLE_ACTIVE_STATUS.indexOf(job.status) !== -1;
  });
}

/**
 * v0034：合并「执行中」与「最近任务」列表（去重；active 在前）。
 * 后端按创建时间倒序分页时执行中任务可能沉在最后一页，这里单独取 active 置顶。
 */

function mergeSubtitleJobs(activeJobs, recentJobs) {
  var seen = Object.create(null);
  var merged = [];
  function push(job) {
    if (!job || !job.job_id || seen[job.job_id]) { return; }
    seen[job.job_id] = true;
    merged.push(job);
  }
  (Array.isArray(activeJobs) ? activeJobs : []).forEach(function (job) {
    if (job && SUBTITLE_RUNNING_STATUS.indexOf(job.status) !== -1) { push(job); }
  });
  (Array.isArray(recentJobs) ? recentJobs : []).forEach(push);
  return merged;
}

export function stopSubtitleJobsPolling() {
  var jobsState = state.subtitleJobs;
  if (!jobsState) { return; }
  if (jobsState.timer) {
    clearTimeout(jobsState.timer);
    jobsState.timer = null;
  }
}

function scheduleSubtitleJobsPoll() {
  stopSubtitleJobsPolling();
  if (state.view !== 'subtitles' || state.viewTab.subtitles !== 'jobs' || !state.subtitleJobs) { return; }
  var snapshot = state.subtitleJobs.snapshot;
  var hasActive = snapshot
    ? Number(snapshot.active_total || 0) > 0
    : anyActiveSubtitleJob(state.subtitleJobs.list);
  var hasQueued = snapshot ? Number(snapshot.queued_total || 0) > 0 : false;
  if (!hasActive && !hasQueued) { return; }
  // 有运行中任务时 1.5s 刷新；只有排队任务时降到 3s（队列位置变化不需要太快）
  var delay = hasActive ? SUBTITLE_JOBS_POLL_MS : 3000;
  state.subtitleJobs.timer = setTimeout(function () {
    state.subtitleJobs.timer = null;
    loadSubtitleJobs({ silent: true });
  }, delay);
}

function subtitleFailureLabel(key, data) {
  var labels = (data && data.category_labels)
    || (state.meta && state.meta.subtitle
      && state.meta.subtitle.failure_category_labels) || {};
  return labels[key] || SUBTITLE_FAILURE_LABEL[key] || key || '未知错误';
}

export async function loadSubtitleFailures() {
  if (!els.subtitleFailuresList) { return; }
  setHidden(els.subtitleFailuresLoading, false);
  setHidden(els.subtitleFailuresEmpty, true);
  var category = els.subtitleFailureFilter ? els.subtitleFailureFilter.value : '';
  var includeSkipped = !!(els.subtitleFailureIncludeSkipped
    && els.subtitleFailureIncludeSkipped.checked);
  var query = '/api/subtitles/failures?limit=200';
  if (category) { query += '&category=' + encodeURIComponent(category); }
  if (includeSkipped) { query += '&include_skipped=1'; }
  try {
    var res = await api(query, { timeout: 15000 });
    setHidden(els.subtitleFailuresLoading, true);
    renderSubtitleFailures(res || {});
    populateSubtitleFailureFilter(res && res.categories);
  } catch (err) {
    setHidden(els.subtitleFailuresLoading, true);
    setHidden(els.subtitleFailuresEmpty, false);
    if (els.subtitleFailuresHint) {
      setText(els.subtitleFailuresHint,
        errorMessage(err, '加载失败任务失败，请稍后重试。'));
    }
  }
}

function populateSubtitleFailureFilter(categories) {
  if (!els.subtitleFailureFilter || els.subtitleFailureFilter.dataset.filled === '1') {
    return;
  }
  var keys = categories ? Object.keys(categories) : [];
  if (!keys.length) { return; }
  keys.forEach(function (key) {
    var option = el('option', { attrs: { value: key },
      text: subtitleFailureLabel(key) + '（' + categories[key] + '）' });
    els.subtitleFailureFilter.appendChild(option);
  });
  els.subtitleFailureFilter.dataset.filled = '1';
}

function renderSubtitleFailures(data) {
  if (!els.subtitleFailuresList) { return; }
  clear(els.subtitleFailuresList);
  var items = data.items || [];
  setHidden(els.subtitleFailuresEmpty, items.length > 0);
  if (els.subtitleFailuresCount) {
    setText(els.subtitleFailuresCount,
      items.length ? '共 ' + fmtInt(data.total || items.length) + ' 个' : '');
  }
  if (els.subtitleFailuresHint) {
    setText(els.subtitleFailuresHint, items.length
      ? '可恢复错误可批量重试；“无语音/权限/码流”类默认不重试'
      : '');
  }
  items.forEach(function (job) {
    var card = el('div', { className: 'subtitle-failure-item' });
    var category = job.failure_category || 'unknown';
    var head = el('div', { className: 'subtitle-failure-head' }, [
      el('span', { className: 'badge badge-amber',
                   text: subtitleFailureLabel(category, data) }),
      el('span', { className: 'subtitle-job-path',
                   attrs: { title: job.path || '' },
                   text: job.path || '（无路径）' })
    ]);
    var bits = [];
    var error = job.error || {};
    if (error.code) { bits.push('错误码 ' + error.code); }
    if (job.attempt) { bits.push('已重试 ' + job.attempt + ' 次'); }
    if (job.retryable) { bits.push('可重试'); }
    if (job.skipped) { bits.push('无语音已跳过'); }
    if (job.finished_at) { bits.push('失败于 ' + job.finished_at); }
    card.appendChild(head);
    card.appendChild(el('div', { className: 'subtitle-failure-meta' }, [
      el('span', { text: job.message || error.message || '' }),
      el('span', { className: 'muted', text: bits.join(' · ') })
    ]));
    var actions = el('div', { className: 'subtitle-failure-actions' });
    var retryBtn = el('button', {
      className: 'btn btn-primary btn-sm', attrs: { type: 'button' }, text: '重试'
    });
    retryBtn.addEventListener('click', function () {
      subtitleRetryJob(job, false);
    });
    actions.appendChild(retryBtn);
    var forceBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '强制重试'
    });
    forceBtn.addEventListener('click', function () {
      subtitleRetryJob(job, true);
    });
    actions.appendChild(forceBtn);
    card.appendChild(actions);
    els.subtitleFailuresList.appendChild(card);
  });
}

export async function subtitleFailuresRetry(force) {
  var category = els.subtitleFailureFilter
    ? els.subtitleFailureFilter.value : '';
  var body = { force: !!force };
  if (category) { body.category = category; }
  try {
    var res = await api('/api/subtitles/failures/retry', {
      method: 'POST', body: body, timeout: 30000
    });
    var skipped = (res && res.skipped) || [];
    toast('已重新排队 ' + fmtInt((res && res.requeued) || 0) + ' 个任务'
      + (skipped.length ? '，跳过 ' + fmtInt(skipped.length) + ' 个' : ''),
      'success', 5000);
    await loadSubtitleFailures();
    await loadSubtitleJobs({ silent: true });
  } catch (err) {
    toast(errorMessage(err, '批量重试失败，请稍后重试。'), 'error', 6000);
  }
}

async function subtitleRetryJob(job, force) {
  try {
    var res = await api('/api/subtitles/failures/retry', {
      method: 'POST', body: { job_ids: [job.job_id], force: !!force },
      timeout: 30000
    });
    if ((res && res.requeued) > 0) {
      toast('已重新排队该任务。', 'success', 3000);
    } else {
      var skipped = ((res && res.skipped) || [])[0] || {};
      toast('未能重试：' + (skipped.reason || '不可重试'), 'info', 5000);
    }
    await loadSubtitleFailures();
    await loadSubtitleJobs({ silent: true });
  } catch (err) {
    toast(errorMessage(err, '重试失败，请稍后重试。'), 'error', 6000);
  }
}

async function subtitleSetPriority(job, button) {
  if (button) { setBusy(button, true); }
  try {
    await api('/api/subtitles/' + encodePath(job.job_id) + '/priority', {
      method: 'POST', body: { top: true }, timeout: 15000
    });
    toast('已置顶该字幕任务。', 'success', 3000);
    await loadSubtitleJobs({ silent: true });
  } catch (err) {
    toast(errorMessage(err, '置顶失败，请稍后重试。'), 'error', 5000);
  } finally {
    if (button) { setBusy(button, false); }
  }
}

export async function loadSubtitleWatchStatus() {
  if (!els.subtitleWatchStatus) { return; }
  try {
    var res = await api('/api/subtitles/watch', { timeout: 10000 });
    var watch = (res && res.watch) || {};
    var bits = [watch.enabled ? '监控已启用' : '监控未启用',
                '目录 ' + ((watch.dirs || []).length) + ' 个',
                '间隔 ' + (watch.interval_sec || 0) + ' 秒'];
    if (watch.last_scan_at) {
      bits.push('上次扫描 ' + watch.last_scan_at
        + (watch.last_result && watch.last_result.enqueued
          ? '（入队 ' + watch.last_result.enqueued + '）' : ''));
    }
    setText(els.subtitleWatchStatus, bits.join(' · '));
  } catch (err) {
    setText(els.subtitleWatchStatus,
      '监控状态不可用：' + errorMessage(err, '加载失败'));
  }
}

export async function subtitleWatchScan() {
  var button = els.subtitleWatchScanBtn;
  if (button) { setBusy(button, true); }
  try {
    var res = await api('/api/subtitles/watch/scan', {
      method: 'POST', body: {}, timeout: 90000
    });
    var result = (res && res.result) || {};
    var errors = result.errors || [];
    toast('监控扫描：入队 ' + fmtInt(result.enqueued || 0)
      + '，跳过 ' + fmtInt(result.skipped || 0)
      + (errors.length ? ('；' + errors[0]) : ''), 'success', 6000);
    await loadSubtitleWatchStatus();
    await loadSubtitleJobs({ silent: true });
  } catch (err) {
    toast(errorMessage(err, '监控扫描失败，请稍后重试。'), 'error', 6000);
  } finally {
    if (button) { setBusy(button, false); }
  }
}

function subtitleJobFilters() {
  return {
    q: (els.subtitleJobSearch ? els.subtitleJobSearch.value : '').trim(),
    status: els.subtitleJobStatusFilter ? els.subtitleJobStatusFilter.value : '',
    suffix: (els.subtitleJobSuffix ? els.subtitleJobSuffix.value : '').trim(),
    directory: (els.subtitleJobDir ? els.subtitleJobDir.value : '').trim(),
    category: els.subtitleJobCategory ? els.subtitleJobCategory.value : ''
  };
}

function subtitleJobFiltersActive(filters) {
  return !!(filters.q || filters.status || filters.suffix
    || filters.directory || filters.category);
}

function subtitleJobQueryString(filters) {
  var parts = [];
  if (filters.q) { parts.push('q=' + encodeURIComponent(filters.q)); }
  if (filters.status) { parts.push('status=' + encodeURIComponent(filters.status)); }
  if (filters.suffix) { parts.push('suffix=' + encodeURIComponent(filters.suffix)); }
  if (filters.directory) { parts.push('directory=' + encodeURIComponent(filters.directory)); }
  if (filters.category) { parts.push('category=' + encodeURIComponent(filters.category)); }
  return parts.length ? '&' + parts.join('&') : '';
}

export function subtitleSelectedSet() {
  if (!(state.subtitleJobs.selected instanceof Set)) {
    state.subtitleJobs.selected = new Set();
  }
  return state.subtitleJobs.selected;
}

export function subtitleBulkCountUpdate() {
  if (!els.subtitleBulkCount) { return; }
  var count = subtitleSelectedSet().size;
  setText(els.subtitleBulkCount, '已选 ' + count + ' 个');
  var list = state.subtitleJobs.list || [];
  if (els.subtitleSelectAll) {
    var selectable = list.filter(function (job) {
      return job && job.job_id;
    });
    var selected = selectable.filter(function (job) {
      return subtitleSelectedSet().has(job.job_id);
    }).length;
    els.subtitleSelectAll.checked = selectable.length > 0
      && selected === selectable.length;
    els.subtitleSelectAll.indeterminate = selected > 0
      && selected < selectable.length;
  }
}

function subtitleToggleSelection(job, checked) {
  var selected = subtitleSelectedSet();
  if (checked) { selected.add(job.job_id); } else { selected.delete(job.job_id); }
  subtitleBulkCountUpdate();
}

function populateSubtitleJobCategoryFilter() {
  if (!els.subtitleJobCategory || els.subtitleJobCategory.dataset.filled === '1') {
    return;
  }
  Object.keys(SUBTITLE_FAILURE_LABEL).forEach(function (key) {
    els.subtitleJobCategory.appendChild(el('option', {
      attrs: { value: key },
      text: SUBTITLE_FAILURE_LABEL[key]
    }));
  });
  els.subtitleJobCategory.dataset.filled = '1';
}

export async function subtitleBulkAction(action) {
  var ids = Array.from(subtitleSelectedSet());
  if (!ids.length) {
    toast('请先勾选要操作的任务。', 'info', 3000);
    return;
  }
  if (action === 'cancel') {
    var ok = await confirmDialog({
      title: '批量取消 ' + ids.length + ' 个任务？',
      message: '取消后这些任务会停止并标记为已取消（已完成的任务不受影响）。',
      confirmText: '取消任务', danger: true
    });
    if (!ok) { return; }
  }
  try {
    var res = await api('/api/subtitles/batch-actions', {
      method: 'POST', body: { job_ids: ids, action: action }, timeout: 60000
    });
    toast('批量操作完成：成功 ' + fmtInt(res.succeeded || 0)
      + '，失败 ' + fmtInt(res.failed || 0), 'success', 6000);
    subtitleSelectedSet().clear();
    await loadSubtitleJobs({ silent: true });
    if (action === 'retry') { await loadSubtitleFailures(); }
  } catch (err) {
    toast(errorMessage(err, '批量操作失败，请稍后重试。'), 'error', 6000);
  }
}

export async function loadSubtitleJobs(options) {
  options = options || {};
  if (!els.subtitleJobsList || state.subtitleJobs.loading) { return; }
  state.subtitleJobs.loading = true;
  if (!options.silent) {
    setHidden(els.subtitleJobsLoading, false);
    setHidden(els.subtitleJobsError, true);
    setHidden(els.subtitleJobsEmpty, true);
  }
  try {
    populateSubtitleJobCategoryFilter();
    // v0049：有检索条件时直接走筛选接口（队列视图只显示最近 10 条）
    var filters = subtitleJobFilters();
    if (subtitleJobFiltersActive(filters)) {
      state.subtitleJobs.snapshot = null;
      var filteredRes = await api('/api/subtitles?limit=200'
        + subtitleJobQueryString(filters), { timeout: 20000 });
      var filtered = normalizeSubtitleJobsResponse(filteredRes);
      state.subtitleJobs.list = filtered;
      state.subtitleJobs.lastError = '';
      renderSubtitleJobs(filtered);
      setHidden(els.subtitleJobsEmpty, filtered.length > 0);
      subtitleBulkCountUpdate();
      return;
    }
    // v0039：优先用队列视图（真实执行顺序：进行中/即将执行/已暂停/最近完成）
    var snapshot = null;
    try {
      snapshot = await api('/api/subtitles/queue?active_limit=10&queued_limit=20'
        + '&paused_limit=10&recent_limit=10', { timeout: 15000 });
    } catch (queueErr) {
      snapshot = null;
    }
    if (snapshot && Array.isArray(snapshot.active)) {
      state.subtitleJobs.snapshot = snapshot;
      state.subtitleJobs.list = [].concat(
        snapshot.active || [], snapshot.queued || [],
        snapshot.paused || [], snapshot.recent || []);
      state.subtitleJobs.lastError = '';
      renderSubtitleJobsSnapshot(snapshot);
      setHidden(els.subtitleJobsEmpty, state.subtitleJobs.list.length > 0);
      subtitleBulkCountUpdate();
    } else {
      // 老后端降级：执行中置顶 + 最近任务（按创建时间倒序）
      state.subtitleJobs.snapshot = null;
      var activeJobs = [];
      try {
        var activeRes = await api('/api/subtitles?status=active&limit=10', { timeout: 15000 });
        activeJobs = normalizeSubtitleJobsResponse(activeRes);
      } catch (activeErr) {
        activeJobs = [];
      }
      var res = await api('/api/subtitles?limit=20', { timeout: 15000 });
      var recentJobs = normalizeSubtitleJobsResponse(res);
      var jobs = mergeSubtitleJobs(activeJobs, recentJobs);
      state.subtitleJobs.list = jobs;
      state.subtitleJobs.lastError = '';
      renderSubtitleJobs(jobs);
      setHidden(els.subtitleJobsEmpty, jobs.length > 0);
      subtitleBulkCountUpdate();
    }
  } catch (err) {
    var message = errorMessage(err, '加载字幕任务失败，请稍后重试。');
    if (err && err.status === 404) {
      message = '当前后端未提供字幕任务历史接口；生成中的任务仍可在弹窗中查看和下载，升级后可用。';
    }
    state.subtitleJobs.lastError = message;
    if (!options.silent) {
      clear(els.subtitleJobsList);
      setText(els.subtitleJobsError, message);
      setHidden(els.subtitleJobsError, false);
      setHidden(els.subtitleJobsEmpty, true);
      setText(els.subtitleJobsStatus, '');
    } else if (state.subtitleJobs.list.length) {
      setText(els.subtitleJobsStatus, '刷新失败，稍后重试');
    }
  } finally {
    state.subtitleJobs.loading = false;
    setHidden(els.subtitleJobsLoading, true);
    scheduleSubtitleJobsPoll();
  }
}

function subtitleJobTimeText(job) {
  var value = job.created_at || job.created_epoch || job.updated_at || job.updated_epoch;
  if (value === null || value === undefined || value === '') { return '—'; }
  if (typeof value === 'number' || /^\d+(\.\d+)?$/.test(String(value))) {
    var num = Number(value);
    if (!isFinite(num)) { return String(value); }
    if (num > 100000000000) { num = num / 1000; }
    try { return fmtDateTime(new Date(num * 1000).toISOString()); } catch (e) { return String(value); }
  }
  return fmtDateTime(value);
}

function subtitleJobChunkText(job) {
  var total = firstFiniteNumber([
    job.chunk_total, job.chunks_total, job.total_chunks, job.chunk_count
  ]);
  if (total === null || total <= 0) { return ''; }
  // v0041：后端 chunk_index / chunks_done 已是 1-based 的“已完成分片数”，
  // 再 +1 会多算一片（曾显示“分块 7/9”而进度只有 53%）。只有 0-based 的
  // 备选字段（current_chunk / chunk）才需要 +1。
  var done = firstFiniteNumber([job.chunk_index, job.chunks_done]);
  var display = null;
  if (done !== null) {
    display = done;
  } else {
    var current = firstFiniteNumber([job.current_chunk, job.chunk]);
    if (current !== null) { display = current + 1; }
  }
  if (display === null) { return '分块 0/' + total; }
  return '分块 ' + clamp(display, 0, total) + '/' + total;
}

function subtitleJobSafeFiles(job) {
  return (Array.isArray(job.files) ? job.files : []).filter(function (item) {
    return item && isSafeSubtitleFileName(item.name);
  });
}

function subtitleJobDownloadUrl(job, name) {
  return '/api/subtitles/' + encodePath(job.job_id) + '/file/' + encodeURIComponent(name);
}

function subtitleJobDownloadLinks(job, compact) {
  var files = subtitleJobSafeFiles(job);
  if (!files.length) { return null; }
  var wrap = el('div', { className: compact ? 'subtitle-job-downloads' : 'subtitle-file-list' });
  files.forEach(function (item) {
    wrap.appendChild(el('a', {
      className: 'btn btn-ghost btn-sm',
      attrs: {
        href: subtitleJobDownloadUrl(job, item.name),
        download: item.name,
        title: '下载 ' + item.name
      }
    }, [icon('download'), compact ? item.name : ('下载 ' + item.name)]));
  });
  return wrap;
}

function subtitleJobProgressText(job) {
  var pct = Math.round(clamp(Number(job.progress) || 0, 0, 100));
  return pct + '%';
}

/** v0041：进度条节点。el() 不支持 style 选项（会被静默丢弃），宽度必须创建后再设置。 */

function subtitleProgressBar(pct) {
  var value = Math.round(clamp(Number(pct) || 0, 0, 100));
  var bar = el('div', {
    className: 'progress-bar',
    attrs: {
      role: 'progressbar', 'aria-valuemin': '0',
      'aria-valuemax': '100', 'aria-valuenow': String(value)
    }
  });
  var fill = el('div', { className: 'progress-fill' });
  fill.style.width = value + '%';
  bar.appendChild(fill);
  return bar;
}

/** v0041：当前步骤消息（后端 message）；排队/终态时与状态徽章重复，不重复展示。 */

function subtitleJobStepMessage(job) {
  var message = typeof job.message === 'string' ? job.message.trim() : '';
  if (!message) { return ''; }
  var status = job.status || 'queued';
  if (status === 'queued' || isTerminalSubtitleStatus(status)) { return ''; }
  if (message === subtitleStageLabel(job.stage)) { return ''; }
  return message;
}

/** v0041：消息里已带“7/9”这类分片数时，不再重复显示“分块 7/9”。 */

function subtitleJobDedupeChunkText(chunkText, message) {
  if (!chunkText || !message) { return chunkText; }
  var match = chunkText.match(/(\d+)\/(\d+)/);
  if (match && message.indexOf(match[1] + '/' + match[2]) !== -1) { return ''; }
  return chunkText;
}

/** v0041：已耗时（执行中）/ 总耗时（终态）。 */

function subtitleJobElapsedText(job) {
  if (job.duration_sec === null || job.duration_sec === undefined) { return ''; }
  var status = job.status || 'queued';
  var prefix = isTerminalSubtitleStatus(status) ? '耗时 ' : '已耗时 ';
  return prefix + subtitleDurationText(job.duration_sec);
}

/** v0041：预计剩余（后端 eta_seconds；样本不足时后端返回 null → 不显示，避免假精度）。 */

function subtitleJobEtaText(job) {
  if (job.eta_seconds === null || job.eta_seconds === undefined) { return ''; }
  var eta = Number(job.eta_seconds);
  if (!isFinite(eta) || eta <= 0) { return ''; }
  return '预计剩余 ' + subtitleDurationText(eta);
}

var SUBTITLE_STEP_BADGE = {
  running: 'badge-blue', done: 'badge-green', failed: 'badge-red',
  paused: 'badge-amber', cancelled: 'badge-gray', skipped: 'badge-gray'
};

function subtitleStepStatusLabel(status) {
  return SUBTITLE_STEP_STATUS_LABEL[status] || status || '—';
}

function subtitleStepLabel(key) {
  return SUBTITLE_STEP_LABEL[key] || key || '—';
}

function subtitleDurationText(seconds) {
  if (seconds === null || seconds === undefined || seconds === '') { return '—'; }
  var sec = Number(seconds);
  if (!isFinite(sec) || sec < 0) { return '—'; }
  if (sec > 0 && sec < 0.1) { return '<0.1 秒'; }
  return fmtElapsed(sec);
}

function subtitleStepRow(step) {
  var detail = step.detail || {};
  var bits = [];
  if (step.key === 'probe') {
    if (detail.duration_sec !== undefined) { bits.push('媒体时长 ' + subtitleDurationText(detail.duration_sec)); }
  } else if (step.key === 'asr') {
    if (detail.chunks !== undefined) { bits.push('分片 ' + fmtInt(detail.chunks)); }
    if (detail.cues !== undefined) { bits.push('字幕 ' + fmtInt(detail.cues) + ' 条'); }
    if (detail.fallback) { bits.push('服务器解码回退'); }
  } else if (step.key === 'translate') {
    if (detail.cues !== undefined) { bits.push('字幕 ' + fmtInt(detail.cues) + ' 条'); }
    if (detail.batches !== undefined) { bits.push('批次 ' + fmtInt(detail.batches)); }
    if (detail.partial) { bits.push('部分批次回退原文'); }
  } else if (step.key === 'write') {
    if (detail.files !== undefined) { bits.push('任务目录 ' + fmtInt(detail.files) + ' 个文件'); }
    if (detail.outputs !== undefined) { bits.push('写回 ' + fmtInt(detail.outputs)); }
    if (detail.output_errors) { bits.push('写回失败 ' + fmtInt(detail.output_errors)); }
  }
  if (step.attempts && step.attempts > 1) { bits.push('第 ' + fmtInt(step.attempts) + ' 次执行'); }
  return el('tr', {}, [
    el('td', { text: subtitleStepLabel(step.key) }),
    el('td', {}, [el('span', {
      className: 'badge ' + (SUBTITLE_STEP_BADGE[step.status] || 'badge-gray'),
      text: subtitleStepStatusLabel(step.status)
    })]),
    el('td', { className: 'mono', text: fmtDateTime(step.started_at) }),
    el('td', { className: 'mono', text: fmtDateTime(step.finished_at) }),
    el('td', { className: 'mono', text: subtitleDurationText(step.duration_sec) }),
    el('td', { className: 'hint', text: bits.join(' · ') || '—' })
  ]);
}

function subtitleTimelineSection(job) {
  var timeline = job && job.timeline;
  if (!timeline || typeof timeline !== 'object') {
    return el('p', { className: 'hint', text: '该任务创建于旧版本，未记录步骤时间。' });
  }
  var wrap = el('div', { className: 'subtitle-timeline' });
  wrap.appendChild(el('p', {
    className: 'hint',
    text: '总耗时 ' + subtitleDurationText(job.duration_sec) +
      ' · 开始 ' + fmtDateTime(job.started_at) +
      ' · 结束 ' + fmtDateTime(job.finished_at)
  }));
  var steps = Array.isArray(timeline.steps) ? timeline.steps : [];
  wrap.appendChild(el('div', { className: 'table-wrap' }, [
    el('table', { className: 'data-table subtitle-step-table' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '步骤' }), el('th', { text: '状态' }),
        el('th', { text: '开始' }), el('th', { text: '结束' }),
        el('th', { text: '耗时' }), el('th', { text: '明细' })
      ])]),
      el('tbody', {}, steps.map(subtitleStepRow))
    ])
  ]));

  var chunks = Array.isArray(timeline.asr_chunks) ? timeline.asr_chunks : [];
  if (chunks.length) {
    var chunkRows = chunks.map(function (item) {
      return el('tr', {}, [
        el('td', { className: 'mono', text: String((Number(item.index) || 0) + 1) }),
        el('td', {}, [el('span', {
          className: 'badge ' + (SUBTITLE_STEP_BADGE[item.status] || 'badge-gray'),
          text: subtitleStepStatusLabel(item.status)
        })]),
        el('td', { className: 'mono', text: fmtDateTime(item.started_at) }),
        el('td', { className: 'mono', text: subtitleDurationText(item.duration_sec) }),
        el('td', { className: 'mono', text: subtitleDurationText(item.extract_sec) }),
        el('td', { className: 'mono', text: subtitleDurationText(item.asr_sec) }),
        el('td', { className: 'hint', text: item.fallback ? '服务器解码回退' : '—' })
      ]);
    });
    wrap.appendChild(el('details', { className: 'subtitle-timeline-details' }, [
      el('summary', { text: 'ASR 分片明细（' + fmtInt(chunks.length) + '）' }),
      el('div', { className: 'table-wrap' }, [
        el('table', { className: 'data-table subtitle-step-table' }, [
          el('thead', {}, [el('tr', {}, [
            el('th', { text: '分片' }), el('th', { text: '状态' }), el('th', { text: '开始' }),
            el('th', { text: '总耗时' }), el('th', { text: '抽音频' }),
            el('th', { text: '识别' }), el('th', { text: '备注' })
          ])]),
          el('tbody', {}, chunkRows)
        ])
      ])
    ]));
  }

  var batches = Array.isArray(timeline.translate_batches) ? timeline.translate_batches : [];
  if (batches.length) {
    var batchRows = batches.map(function (item) {
      return el('tr', {}, [
        el('td', { className: 'mono', text: String((Number(item.index) || 0) + 1) }),
        el('td', {}, [el('span', {
          className: 'badge ' + (SUBTITLE_STEP_BADGE[item.status] || 'badge-gray'),
          text: subtitleStepStatusLabel(item.status)
        })]),
        el('td', { className: 'mono', text: fmtDateTime(item.started_at) }),
        el('td', { className: 'mono', text: subtitleDurationText(item.duration_sec) }),
        el('td', { className: 'mono', text: fmtInt(item.cues) }),
        el('td', { className: 'mono', text: fmtInt(item.attempts) }),
        el('td', { className: 'hint', text: item.partial ? '回退原文' : '—' })
      ]);
    });
    wrap.appendChild(el('details', { className: 'subtitle-timeline-details' }, [
      el('summary', { text: '翻译批次明细（' + fmtInt(batches.length) + '）' }),
      el('div', { className: 'table-wrap' }, [
        el('table', { className: 'data-table subtitle-step-table' }, [
          el('thead', {}, [el('tr', {}, [
            el('th', { text: '批次' }), el('th', { text: '状态' }), el('th', { text: '开始' }),
            el('th', { text: '耗时' }), el('th', { text: '字幕条数' }),
            el('th', { text: '尝试次数' }), el('th', { text: '备注' })
          ])]),
          el('tbody', {}, batchRows)
        ])
      ])
    ]));
  }
  return wrap;
}

function renderSubtitleJobItem(job) {
  var status = job.status || 'queued';
  var item = el('div', { className: 'subtitle-job-item', dataset: { jobId: job.job_id } });
  var head = el('div', { className: 'subtitle-job-head' }, [
    // v0049：批量选择
    el('input', {
      className: 'subtitle-job-select',
      attrs: { type: 'checkbox', 'aria-label': '选择任务 ' + (job.job_id || '') },
      on: {
        change: function (event) {
          subtitleToggleSelection(job, !!event.target.checked);
        }
      }
    }),
    el('span', { className: 'badge ' + subtitleJobStatusClass(status), text: subtitleJobStatusLabel(status) }),
    el('span', { className: 'subtitle-job-path', attrs: { title: job.path || '' }, text: job.path || '（无路径）' })
  ]);
  var jobSelect = head.querySelector('.subtitle-job-select');
  if (jobSelect) { jobSelect.checked = subtitleSelectedSet().has(job.job_id); }
  var chunkText = subtitleJobChunkText(job);
  // v0041：当前步骤消息（如“正在识别语音 7/9”）；与分块数重复时优先展示消息
  var stepMessage = subtitleJobStepMessage(job);
  if (stepMessage) { chunkText = subtitleJobDedupeChunkText(chunkText, stepMessage); }
  var metaBits = [subtitleStageLabel(job.stage)];
  if (chunkText) { metaBits.push(chunkText); }
  if (stepMessage) { metaBits.push(stepMessage); }
  var elapsedText = subtitleJobElapsedText(job);
  if (elapsedText) { metaBits.push(elapsedText); }
  var etaText = subtitleJobEtaText(job);
  if (etaText) { metaBits.push(etaText); }
  if (job.translate) { metaBits.push('译文 ' + (job.target_lang || '—')); }
  if (job.queue_position !== undefined && job.queue_position !== null) {
    metaBits.unshift('队列位置 #' + (Number(job.queue_position) + 1));
  }
  metaBits.push('创建 ' + subtitleJobTimeText(job));
  var meta = el('div', { className: 'subtitle-job-meta' }, metaBits.map(function (text) {
    return el('span', { text: text });
  }));
  // v0041：「来源」是提交时固定的生成方式，移到行尾并弱化，避免与阶段（云端语音识别）撞词
  var sourceText = subtitleSourceLabel(job);
  if (sourceText) {
    meta.appendChild(el('span', {
      className: 'subtitle-job-source', text: '来源：' + sourceText
    }));
  }
  var main = el('div', { className: 'subtitle-job-main' }, [head, meta]);
  var pct = Math.round(clamp(Number(job.progress) || 0, 0, 100));
  main.appendChild(el('div', { className: 'subtitle-job-progress' }, [
    subtitleProgressBar(pct),
    el('span', { className: 'subtitle-pct', text: subtitleJobProgressText(job) })
  ]));
  if (status === 'failed') {
    var errorText = '字幕任务失败。';
    if (job.error && typeof job.error === 'string') { errorText = job.error; }
    else if (job.error && job.error.message) { errorText = job.error.message; }
    else if (job.message) { errorText = job.message; }
    main.appendChild(el('div', { className: 'subtitle-job-error', text: errorText }));
  }
  if (job.skipped) {
    main.appendChild(el('div', { className: 'subtitle-job-note' }, [
      el('span', { className: 'badge badge-gray', text: '已跳过' }),
      el('span', { text: job.message || '已存在字幕，已跳过' })
    ]));
  }
  var warningText = subtitleWarningText(job);
  if (warningText) {
    main.appendChild(el('div', { className: 'subtitle-job-warning' }, [
      icon('warning'), el('span', { text: warningText })
    ]));
  }
  var outputsEl = renderSubtitleOutputs(job, true);
  if (outputsEl) { main.appendChild(outputsEl); }
  if (status === 'done') {
    var links = subtitleJobDownloadLinks(job, true);
    if (links) { main.appendChild(links); }
  }
  item.appendChild(main);

  var actions = el('div', { className: 'subtitle-job-actions' });
  actions.appendChild(el('button', {
    className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '查看',
    on: { click: function () { openSubtitleJobModal(job); } }
  }));
  // v0035：已完成但只做了 ASR、没翻译的任务，可在列表直接补翻译
  if (subtitleJobTranslatable(job)) {
    actions.appendChild(buildSubtitleTranslateButton(job));
  }
  if (status === 'paused') {
    var resumeBtn = el('button', {
      className: 'btn btn-primary btn-sm', attrs: { type: 'button' }, text: '继续'
    });
    if (job.resumable === false) {
      resumeBtn.disabled = true;
      resumeBtn.title = '该任务没有可用断点，请重新生成字幕。';
    } else {
      resumeBtn.addEventListener('click', function () { subtitleJobAction(job, 'resume', resumeBtn); });
    }
    actions.appendChild(resumeBtn);
    var cancelPausedBtn = el('button', {
      className: 'btn btn-danger-ghost btn-sm', attrs: { type: 'button' }, text: '取消'
    });
    cancelPausedBtn.addEventListener('click', function () { subtitleJobAction(job, 'cancel', cancelPausedBtn); });
    actions.appendChild(cancelPausedBtn);
  } else if (status === 'queued') {
    // v0048：队列任务可置顶（手动优先级）
    var queuedTopBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '置顶'
    });
    queuedTopBtn.addEventListener('click', function () { subtitleSetPriority(job, queuedTopBtn); });
    actions.appendChild(queuedTopBtn);
    var queuedPauseBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '暂停'
    });
    queuedPauseBtn.addEventListener('click', function () { subtitleJobAction(job, 'pause', queuedPauseBtn); });
    actions.appendChild(queuedPauseBtn);
    var queuedCancelBtn = el('button', {
      className: 'btn btn-danger-ghost btn-sm', attrs: { type: 'button' }, text: '取消'
    });
    queuedCancelBtn.addEventListener('click', function () { subtitleJobAction(job, 'cancel', queuedCancelBtn); });
    actions.appendChild(queuedCancelBtn);
  } else if (SUBTITLE_ACTIVE_STATUS.indexOf(status) !== -1) {
    var pauseBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '暂停'
    });
    pauseBtn.addEventListener('click', function () { subtitleJobAction(job, 'pause', pauseBtn); });
    actions.appendChild(pauseBtn);
    var cancelBtn = el('button', {
      className: 'btn btn-danger-ghost btn-sm', attrs: { type: 'button' }, text: '取消'
    });
    cancelBtn.addEventListener('click', function () { subtitleJobAction(job, 'cancel', cancelBtn); });
    actions.appendChild(cancelBtn);
  }
  if (status === 'failed') {
    // v0048：失败任务可重试；无语音/不可重试分类需“强制重试”
    var retryBtn = el('button', {
      className: 'btn btn-primary btn-sm', attrs: { type: 'button' },
      text: job.retryable ? '重试' : '强制重试'
    });
    retryBtn.addEventListener('click', function () {
      subtitleRetryJob(job, !job.retryable || job.skipped);
    });
    actions.appendChild(retryBtn);
  }
  item.appendChild(actions);
  return item;
}

export function renderSubtitleJobs(jobs) {
  clear(els.subtitleJobsList);
  var list = Array.isArray(jobs) ? jobs : [];
  var activeJobs = [];
  var recentJobs = [];
  var seen = Object.create(null);
  list.forEach(function (job) {
    if (!job || !job.job_id || seen[job.job_id]) { return; }
    seen[job.job_id] = true;
    // v0034：只有 running/translating 才算“进行中”；排队中的任务归入“最近任务”，
    // 否则数千个排队任务会把分区填满，仍然看不到真正执行的那一条。
    if (SUBTITLE_RUNNING_STATUS.indexOf(job.status) !== -1) { activeJobs.push(job); }
    else { recentJobs.push(job); }
  });
  var statusBits = [];
  if (activeJobs.length) { statusBits.push('进行中 ' + fmtInt(activeJobs.length)); }
  if (recentJobs.length) { statusBits.push('最近任务 ' + fmtInt(recentJobs.length)); }
  setText(els.subtitleJobsStatus, statusBits.join(' · '));
  if (!list.length) {
    setHidden(els.subtitleJobsEmpty, false);
    return;
  }
  setHidden(els.subtitleJobsEmpty, true);
  // v0034：执行中任务置顶为独立分区（最近任务按创建时间倒序）
  if (activeJobs.length) {
    var activeSection = el('section', { className: 'subtitle-jobs-section' }, [
      el('h3', {
        className: 'subtitle-jobs-section-title',
        text: '进行中（' + fmtInt(activeJobs.length) + '）'
      })
    ]);
    activeJobs.forEach(function (job) {
      activeSection.appendChild(renderSubtitleJobItem(job));
    });
    els.subtitleJobsList.appendChild(activeSection);
  }
  if (recentJobs.length) {
    var recentSection = el('section', { className: 'subtitle-jobs-section' }, [
      el('h3', { className: 'subtitle-jobs-section-title', text: '最近任务' })
    ]);
    recentJobs.forEach(function (job) {
      recentSection.appendChild(renderSubtitleJobItem(job));
    });
    els.subtitleJobsList.appendChild(recentSection);
  }
}

function appendSubtitleJobSection(title, jobs, extra) {
  var section = el('section', { className: 'subtitle-jobs-section' }, [
    el('h3', { className: 'subtitle-jobs-section-title', text: title })
  ]);
  (jobs || []).forEach(function (job) {
    section.appendChild(renderSubtitleJobItem(job));
  });
  if (extra) { section.appendChild(extra); }
  els.subtitleJobsList.appendChild(section);
}

/** v0039：队列视图（进行中 / 即将执行 / 已暂停 / 最近完成）。 */

function renderSubtitleJobsSnapshot(snapshot) {
  clear(els.subtitleJobsList);
  var active = Array.isArray(snapshot.active) ? snapshot.active : [];
  var queued = Array.isArray(snapshot.queued) ? snapshot.queued : [];
  var paused = Array.isArray(snapshot.paused) ? snapshot.paused : [];
  var recent = Array.isArray(snapshot.recent) ? snapshot.recent : [];
  var activeTotal = Number(snapshot.active_total || active.length);
  var queuedTotal = Number(snapshot.queued_total || queued.length);
  var pausedTotal = Number(snapshot.paused_total || paused.length);
  var statusBits = [];
  if (activeTotal) { statusBits.push('进行中 ' + fmtInt(activeTotal)); }
  if (queuedTotal) { statusBits.push('排队 ' + fmtInt(queuedTotal)); }
  if (pausedTotal) { statusBits.push('已暂停 ' + fmtInt(pausedTotal)); }
  setText(els.subtitleJobsStatus, statusBits.join(' · '));
  if (!active.length && !queued.length && !paused.length && !recent.length) {
    setHidden(els.subtitleJobsEmpty, false);
    return;
  }
  setHidden(els.subtitleJobsEmpty, true);
  if (active.length) {
    appendSubtitleJobSection('进行中（' + fmtInt(activeTotal) + '）', active);
  }
  if (queued.length) {
    var queuedTitle = '即将执行（' + fmtInt(queued.length)
      + (queuedTotal > queued.length
        ? ' / 共 ' + fmtInt(queuedTotal) + ' 个排队' : '') + '）';
    var hint = null;
    if (queuedTotal > queued.length) {
      hint = el('p', {
        className: 'hint',
        text: '还有 ' + fmtInt(queuedTotal - queued.length)
          + ' 个任务在队列中；这里按真实执行顺序显示前 '
          + fmtInt(queued.length) + ' 个。'
      });
    }
    appendSubtitleJobSection(queuedTitle, queued, hint);
  }
  if (paused.length) {
    appendSubtitleJobSection('已暂停（' + fmtInt(pausedTotal) + '）', paused);
  }
  if (recent.length) {
    appendSubtitleJobSection('最近完成', recent);
  }
}

async function subtitleJobAction(job, action, button) {
  if (!job || !job.job_id || !action) { return; }
  var labels = { pause: '暂停', resume: '继续', cancel: '取消' };
  if (button) { setBusy(button, true, '处理中…'); }
  try {
    var res = await api('/api/subtitles/' + encodePath(job.job_id) + '/' + action, {
      method: 'POST', body: {}, timeout: 15000
    });
    var updated = res && res.job ? res.job : res;
    toast('已请求' + (labels[action] || action) + '字幕任务。', 'info');
    if (state.subtitleJobs.detail && state.subtitleJobs.detail.job && state.subtitleJobs.detail.job.job_id === job.job_id) {
      if (updated && updated.job_id) { updateSubtitleJobDetail(updated); }
    }
    await loadSubtitleJobs({ silent: true });
  } catch (err) {
    toast(errorMessage(err, (labels[action] || '操作') + '字幕任务失败，请稍后重试。'), 'error', 6000);
  } finally {
    if (button) { setBusy(button, false); }
  }
}

function updateSubtitleJobDetail(job) {
  var detail = state.subtitleJobs.detail;
  if (!detail || !detail.modal || !job) { return; }
  detail.job = job;
  detail.modal.setBody(buildSubtitleJobDetailBody(job));
  detail.modal.setFooter(buildSubtitleJobDetailFooter(job));
  if (isTerminalSubtitleStatus(job.status)) { stopSubtitleJobDetailPolling(); }
  loadSubtitleJobs({ silent: true });
}

function stopSubtitleJobDetailPolling() {
  var detail = state.subtitleJobs.detail;
  if (!detail) { return; }
  if (detail.timer) { clearInterval(detail.timer); detail.timer = null; }
  if (detail.abort) { try { detail.abort.abort(); } catch (e) { /* 忽略 */ } detail.abort = null; }
}

function openSubtitleJobModal(job) {
  if (!job || !job.job_id) { return; }
  if (state.subtitleJobs.detail) { closeModal(true); }
  var detail = { job: job, modal: null, timer: null, abort: null };
  state.subtitleJobs.detail = detail;
  var modal = openModal({
    title: '字幕任务',
    subtitle: job.path || '',
    size: 'lg',
    body: buildSubtitleJobDetailBody(job),
    footer: buildSubtitleJobDetailFooter(job),
    onClose: function () {
      stopSubtitleJobDetailPolling();
      if (state.subtitleJobs.detail === detail) { state.subtitleJobs.detail = null; }
      loadSubtitleJobs({ silent: true });
    }
  });
  detail.modal = modal;
  if (SUBTITLE_ACTIVE_STATUS.indexOf(job.status) !== -1) {
    detail.timer = setInterval(function () {
      if (!state.subtitleJobs.detail || state.subtitleJobs.detail !== detail) { return; }
      if (detail.abort) { try { detail.abort.abort(); } catch (e) { /* 忽略 */ } }
      var controller = new AbortController();
      detail.abort = controller;
      api('/api/subtitles/' + encodePath(job.job_id), { signal: controller.signal, timeout: 15000 }).then(function (res) {
        var updated = res && res.job ? res.job : res;
        if (!updated || !updated.job_id) { return; }
        if (!state.subtitleJobs.detail || state.subtitleJobs.detail !== detail) { return; }
        updateSubtitleJobDetail(updated);
      }).catch(function () { /* 详情轮询失败静默，保留列表刷新 */ });
    }, POLL_MS);
  }
}

function buildSubtitleJobDetailBody(job) {
  var status = job.status || 'queued';
  var pct = Math.round(clamp(Number(job.progress) || 0, 0, 100));
  var chunkText = subtitleJobChunkText(job);
  var gridRows = [
    el('div', {}, [el('dt', { text: '路径' }), el('dd', { className: 'mono', text: job.path || '—' })]),
    el('div', {}, [el('dt', { text: '来源' }), el('dd', { text: subtitleSourceLabel(job) || '云端 ASR 生成' })]),
    el('div', {}, [el('dt', { text: '阶段' }), el('dd', { text: subtitleStageLabel(job.stage) })]),
    el('div', {}, [el('dt', { text: '分块进度' }), el('dd', { text: chunkText || '—' })]),
    el('div', {}, [el('dt', { text: '翻译' }), el('dd', { text: job.translate ? ('开启 · 目标 ' + (job.target_lang || '—')) : '未开启' })]),
    el('div', {}, [el('dt', { text: '写回' }), el('dd', { text: subtitleOutputModeLabel(job.output_mode) })])
  ];
  // v0041：已耗时 / 预计剩余（后端 eta_seconds，样本不足时不显示）
  var elapsedRow = subtitleJobElapsedText(job);
  if (elapsedRow) {
    gridRows.push(el('div', {}, [el('dt', { text: '耗时' }), el('dd', { text: elapsedRow })]));
  }
  if (job.eta_seconds !== null && job.eta_seconds !== undefined
    && isFinite(Number(job.eta_seconds)) && Number(job.eta_seconds) > 0) {
    gridRows.push(el('div', {}, [el('dt', { text: '预计剩余' }), el('dd', { text: subtitleDurationText(job.eta_seconds) })]));
  }
  if (job.batch_id) {
    gridRows.push(el('div', {}, [el('dt', { text: '批次' }), el('dd', { className: 'mono', text: job.batch_id })]));
  }
  gridRows.push(el('div', {}, [el('dt', { text: '任务编号' }), el('dd', { className: 'mono', text: job.job_id })]));
  gridRows.push(el('div', {}, [el('dt', { text: '更新时间' }), el('dd', { text: subtitleJobTimeText(job) })]));
  var body = el('div', { className: 'subtitle-progress-card' }, [
    el('div', { className: 'subtitle-stage-row' }, [
      el('span', { className: 'badge ' + subtitleJobStatusClass(status), text: subtitleJobStatusLabel(status) }),
      el('span', { className: 'subtitle-pct', text: pct + '%' })
    ]),
    subtitleProgressBar(pct),
    el('p', { className: 'modal-note', text: job.message || (isTerminalSubtitleStatus(status) ? '任务已结束。' : '正在处理…') }),
    el('dl', { className: 'subtitle-detail-grid' }, gridRows)
  ]);
  if (status === 'failed') {
    var errorText = '字幕任务失败。';
    if (job.error && typeof job.error === 'string') { errorText = job.error; }
    else if (job.error && job.error.message) { errorText = job.error.message; }
    else if (job.message) { errorText = job.message; }
    body.appendChild(el('div', { className: 'modal-error', attrs: { role: 'alert' }, text: errorText }));
  }
  if (job.skipped) {
    body.appendChild(el('div', { className: 'modal-warning' }, [
      icon('warning'),
      el('p', { text: job.message || '已存在字幕，已跳过（不会覆盖已有文件）。' })
    ]));
  }
  var warningText = subtitleWarningText(job);
  if (warningText) {
    body.appendChild(el('div', { className: 'modal-warning' }, [
      icon('warning'), el('p', { text: warningText })
    ]));
  }
  body.appendChild(el('h3', { text: '步骤耗时' }));
  body.appendChild(subtitleTimelineSection(job));
  var outputsEl = renderSubtitleOutputs(job, false);
  if (outputsEl) {
    body.appendChild(el('h3', { text: '已写入视频目录' }));
    body.appendChild(outputsEl);
  }
  if (status === 'done' || (job.files && job.files.length)) {
    var files = subtitleJobSafeFiles(job);
    if (files.length) {
      body.appendChild(el('h3', { text: '字幕文件（任务目录，可下载）' }));
      var wrap = el('div', { className: 'subtitle-file-list' });
      files.forEach(function (item) {
        wrap.appendChild(el('div', { className: 'subtitle-file-item' }, [
          el('span', { className: 'subtitle-file-name', text: item.name }),
          el('span', { className: 'subtitle-file-size', text: fmtBytes(item.size) }),
          el('a', {
            className: 'btn btn-ghost btn-sm',
            attrs: { href: subtitleJobDownloadUrl(job, item.name), download: item.name }
          }, [icon('download'), '下载'])
        ]));
      });
      body.appendChild(wrap);
      var previewBtn = el('button', {
        className: 'btn btn-ghost btn-sm subtitle-preview-btn',
        attrs: { type: 'button' },
        on: { click: function () { openSubtitlePreview(job); } }
      }, [icon('search'), '预览字幕']);
      body.appendChild(previewBtn);
    } else if (status === 'done' && !job.skipped) {
      body.appendChild(el('p', { className: 'subtitle-empty', text: '任务已完成，但任务目录中没有可下载的字幕文件。' }));
    }
  }
  body.appendChild(el('p', {
    className: 'hint',
    text: '写回视频目录的 srt 不通过网页下载（直接在 NAS 文件管理器中查看）；任务目录副本可下载。音频临时文件已在任务结束/取消后清理。'
  }));
  return body;
}

var SUBTITLE_SRT_TIME = /(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})/;

function parseSubtitleSrt(text) {
  var blocks = String(text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').split(/\n\s*\n/);
  var cues = [];
  blocks.forEach(function (block) {
    var lines = block.split('\n').filter(function (line) { return line.length; });
    if (!lines.length) { return; }
    var timeIndex = -1;
    for (var i = 0; i < Math.min(lines.length, 3); i++) {
      if (SUBTITLE_SRT_TIME.test(lines[i])) { timeIndex = i; break; }
    }
    if (timeIndex < 0) { return; }
    var match = lines[timeIndex].match(SUBTITLE_SRT_TIME);
    var bodyLines = lines.slice(timeIndex + 1).join(' ').trim();
    if (!bodyLines) { return; }
    cues.push({
      start: match[1].replace(',', '.'),
      end: match[2].replace(',', '.'),
      text: bodyLines
    });
  });
  return cues;
}

async function fetchSubtitleText(job, name) {
  var response = await fetch(subtitleJobDownloadUrl(job, name), {
    credentials: 'same-origin', cache: 'no-store'
  });
  if (!response.ok) { throw new Error('HTTP ' + response.status); }
  return response.text();
}

async function openSubtitlePreview(job) {
  var files = subtitleJobSafeFiles(job);
  if (!files.length) { toast('没有可预览的字幕文件。', 'info'); return; }
  var names = files.map(function (item) { return String(item.name).toLowerCase(); });
  var original = files[names.indexOf('original.srt') >= 0 ? names.indexOf('original.srt') : 0].name;
  var target = '';
  if (job.translate && job.target_lang) {
    var wanted = String(job.target_lang).toLowerCase();
    var exact = files.filter(function (item) { return String(item.name).toLowerCase() === wanted + '.srt'; });
    if (exact.length) { target = exact[0].name; }
    else {
      var bilingual = files.filter(function (item) {
        return String(item.name).toLowerCase().indexOf('bilingual.') === 0 && String(item.name).toLowerCase().slice(-4) === '.srt';
      });
      if (bilingual.length) { target = bilingual[0].name; }
    }
  }
  var previewClose = el('button', {
    className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
    on: { click: function () { closeModal(true); } }
  });
  var modal = openModal({
    title: '字幕预览', subtitle: job.path || '', size: 'xl',
    body: el('p', { className: 'subtitle-empty', text: '正在加载字幕…' }),
    footer: [previewClose]
  });
  try {
    var originalCues = parseSubtitleSrt(await fetchSubtitleText(job, original));
    var translatedCues = target ? parseSubtitleSrt(await fetchSubtitleText(job, target)) : [];
    var rows = [];
    originalCues.slice(0, 500).forEach(function (cue, index) {
      rows.push(el('tr', {}, [
        el('td', { className: 'mono', text: String(index + 1) }),
        el('td', { className: 'mono subtitle-cue-time', text: cue.start + ' → ' + cue.end }),
        el('td', { text: cue.text }),
        el('td', { text: translatedCues[index] ? translatedCues[index].text : '—' })
      ]));
    });
    var table = el('table', { className: 'subtitle-cue-table' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '#' }), el('th', { text: '时间' }),
        el('th', { text: '原文' }), el('th', { text: target ? '译文' : '译文（未启用）' })
      ])]),
      el('tbody', {}, rows)
    ]);
    var body = el('div', {}, [
      el('p', { className: 'hint', text: '共 ' + fmtInt(originalCues.length) + ' 条字幕；最多预览前 500 条。原文：' + original + (target ? '；译文：' + target : '') }),
      table
    ]);
    if (!rows.length) { body = el('p', { className: 'subtitle-empty', text: '字幕内容为空或时间轴无法解析。' }); }
    modal.setBody(body);
  } catch (err) {
    modal.setBody(el('p', { className: 'modal-error', text: '字幕预览失败：' + errorMessage(err, '无法读取字幕文件。') }));
  }
}

async function subtitleShowAudioTracks(job, button) {
  if (button) { setBusy(button, true); }
  try {
    var res = await api('/api/subtitles/audio-tracks', {
      method: 'POST', body: { path: job.path }, timeout: 30000
    });
    var tracks = (res && res.audio && res.audio.tracks) || [];
    if (!tracks.length) {
      toast('未检测到音轨。', 'info', 5000);
      return;
    }
    var lines = tracks.map(function (track) {
      var bits = ['#' + track.index];
      if (track.language) { bits.push('[' + track.language + ']'); }
      if (track.title) { bits.push(track.title); }
      if (track.codec) { bits.push(track.codec); }
      if (track.channels) { bits.push(track.channels + 'ch'); }
      return bits.join(' ');
    });
    toast('音轨：' + lines.join('；'), 'info', 10000);
  } catch (err) {
    toast(errorMessage(err, '查询音轨失败，请稍后重试。'), 'error', 6000);
  } finally {
    if (button) { setBusy(button, false); }
  }
}

function buildSubtitleJobDetailFooter(job) {
  var status = job.status || 'queued';
  var buttons = [];
  if (status === 'paused') {
    var resumeBtn = el('button', { className: 'btn btn-primary', attrs: { type: 'button' }, text: '继续' });
    if (job.resumable === false) {
      resumeBtn.disabled = true;
      resumeBtn.title = '该任务没有可用断点，请重新生成字幕。';
    } else {
      resumeBtn.addEventListener('click', function () { subtitleJobDetailAction(job, 'resume', resumeBtn); });
    }
    buttons.push(resumeBtn);
  } else if (SUBTITLE_ACTIVE_STATUS.indexOf(status) !== -1) {
    var pauseBtn = el('button', { className: 'btn btn-ghost', attrs: { type: 'button' }, text: '暂停' });
    pauseBtn.addEventListener('click', function () { subtitleJobDetailAction(job, 'pause', pauseBtn); });
    buttons.push(pauseBtn);
  }
  if (status === 'paused' || SUBTITLE_ACTIVE_STATUS.indexOf(status) !== -1) {
    var cancelBtn = el('button', { className: 'btn btn-danger-ghost', attrs: { type: 'button' }, text: '取消任务' });
    cancelBtn.addEventListener('click', function () { subtitleJobDetailAction(job, 'cancel', cancelBtn); });
    buttons.push(cancelBtn);
  }
  // v0035：详情 footer 同样提供补翻译入口（done 且缺译文时才出现）
  if (subtitleJobTranslatable(job)) {
    buttons.push(buildSubtitleTranslateButton(job, { large: true }));
  }
  // v0048：查询音轨（用于手动指定音轨/排查配音轨）
  if (job.path && job.source !== 'existing_srt') {
    var audioTracksBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '查询音轨'
    });
    audioTracksBtn.addEventListener('click', function () {
      subtitleShowAudioTracks(job, audioTracksBtn);
    });
    buttons.push(audioTracksBtn);
  }
  buttons.push(el('button', {
    className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
    on: { click: function () { closeModal(true); } }
  }));
  return buttons;
}

async function subtitleJobDetailAction(job, action, button) {
  if (!job || !job.job_id) { return; }
  var labels = { pause: '暂停', resume: '继续', cancel: '取消' };
  if (button) { setBusy(button, true, '处理中…'); }
  try {
    var res = await api('/api/subtitles/' + encodePath(job.job_id) + '/' + action, {
      method: 'POST', body: {}, timeout: 15000
    });
    var updated = res && res.job ? res.job : res;
    toast('已请求' + (labels[action] || action) + '字幕任务。', 'info');
    if (updated && updated.job_id) { updateSubtitleJobDetail(updated); }
    await loadSubtitleJobs({ silent: true });
  } catch (err) {
    toast(errorMessage(err, (labels[action] || '操作') + '字幕任务失败，请稍后重试。'), 'error', 6000);
  } finally {
    if (button) { setBusy(button, false); }
  }
}

export function subtitleTranslateGate() {
  var sub = subtitleCapability();
  if (sub.supported !== true) { return { ok: false, reason: '当前后端未提供云端字幕能力' }; }
  if (sub.enabled !== true) { return { ok: false, reason: '请先在设置中启用云端字幕' }; }
  // v0060：翻译不再受设置总开关限制，配了 LLM API Key 即可
  if (sub.llmKeySet !== true) { return { ok: false, reason: '请先在设置中配置 LLM API Key' }; }
  return { ok: true, reason: '' };
}

export async function subtitleTranslateJob(job, button) {
  if (!job || !job.job_id) { return; }
  var gate = subtitleTranslateGate();
  if (!gate.ok) { toast(gate.reason, 'error', 7000); return; }
  var sub = subtitleCapability();
  var partial = job.translation_partial === true;
  var targetLabel = subtitleTargetLangLabel(sub.targetLang || 'zh');
  var ok = await confirmDialog({
    title: partial ? '重新翻译字幕任务' : '补翻译字幕任务',
    message: '将把字幕文本（不含音频/视频）上传到已配置的 LLM 服务翻译为' + targetLabel
      + '，并新建任务。' + (partial ? '该任务此前为“部分译文回退原文”，会重新翻译。' : ''),
    confirmText: partial ? '重新翻译' : '开始补翻译',
    danger: false
  });
  if (!ok) { return; }
  if (button) { setBusy(button, true, '提交中…'); }
  try {
    var res = await api('/api/subtitles/' + encodePath(job.job_id) + '/translate', {
      method: 'POST',
      body: { confirm_cloud: true, target_lang: sub.targetLang || 'zh' },
      timeout: 60000
    });
    if (res && res.created === false) {
      toast('该任务已在翻译队列，无需重复提交。', 'info', 6000);
    } else {
      toast('已创建翻译任务，可在「最近任务」查看进度。', 'success', 6000);
    }
    // 详情弹窗打开时关闭，避免继续显示源任务的旧状态
    if (state.subtitleJobs.detail && state.subtitleJobs.detail.job
      && state.subtitleJobs.detail.job.job_id === job.job_id) {
      closeModal(true);
    }
    await loadSubtitleJobs({ silent: true });
  } catch (err) {
    toast(errorMessage(err, '补翻译失败，请稍后重试。'), 'error', 7000);
  } finally {
    if (button) { setBusy(button, false); }
  }
}

export function buildSubtitleTranslateButton(job, options) {
  options = options || {};
  var partial = job && job.translation_partial === true;
  var btn = el('button', {
    className: 'btn btn-ghost' + (options.large ? '' : ' btn-sm'),
    attrs: { type: 'button' },
    text: partial ? '重新翻译' : '补翻译'
  });
  var gate = subtitleTranslateGate();
  if (!gate.ok) {
    btn.disabled = true;
    btn.title = gate.reason;
  } else {
    btn.title = '将字幕文本上传到已配置的 LLM 服务翻译并新建任务（不上传音频/视频）';
    btn.addEventListener('click', function () { subtitleTranslateJob(job, btn); });
  }
  return btn;
}

export function initSubtitleJobs() {
  if (els.subtitleJobsRefreshBtn) {
    els.subtitleJobsRefreshBtn.addEventListener('click', function () { loadSubtitleJobs(); });
  }
  // v0049：任务检索筛选 + 批量操作
  if (els.subtitleJobFilterBtn) {
    els.subtitleJobFilterBtn.addEventListener('click', function () {
      subtitleSelectedSet().clear();
      loadSubtitleJobs();
    });
  }
  if (els.subtitleJobFilterResetBtn) {
    els.subtitleJobFilterResetBtn.addEventListener('click', function () {
      ['subtitleJobSearch', 'subtitleJobSuffix', 'subtitleJobDir'].forEach(
        function (id) {
          var input = byId(id);
          if (input) { input.value = ''; }
        });
      ['subtitleJobStatusFilter', 'subtitleJobCategory'].forEach(
        function (id) {
          var select = byId(id);
          if (select) { select.value = ''; }
        });
      subtitleSelectedSet().clear();
      loadSubtitleJobs();
    });
  }
  if (els.subtitleJobSearch) {
    els.subtitleJobSearch.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { loadSubtitleJobs(); }
    });
  }
  if (els.subtitleSelectAll) {
    els.subtitleSelectAll.addEventListener('change', function () {
      var checked = !!els.subtitleSelectAll.checked;
      var selected = subtitleSelectedSet();
      (state.subtitleJobs.list || []).forEach(function (job) {
        if (!job || !job.job_id) { return; }
        if (checked) { selected.add(job.job_id); } else { selected.delete(job.job_id); }
      });
      renderSubtitleJobs(state.subtitleJobs.list || []);
      subtitleBulkCountUpdate();
    });
  }
  if (els.subtitleBulkCancelBtn) {
    els.subtitleBulkCancelBtn.addEventListener('click', function () {
      subtitleBulkAction('cancel');
    });
  }
  if (els.subtitleBulkRetryBtn) {
    els.subtitleBulkRetryBtn.addEventListener('click', function () {
      subtitleBulkAction('retry');
    });
  }
  if (els.subtitleBulkPriorityBtn) {
    els.subtitleBulkPriorityBtn.addEventListener('click', function () {
      subtitleBulkAction('priority');
    });
  }
  // v0048：失败任务面板 / 目录监控按钮
  if (els.subtitleFailuresRefreshBtn) {
    els.subtitleFailuresRefreshBtn.addEventListener('click', function () {
      loadSubtitleFailures();
    });
  }
  if (els.subtitleFailureFilter) {
    els.subtitleFailureFilter.addEventListener('change', function () {
      loadSubtitleFailures();
    });
  }
  if (els.subtitleFailureIncludeSkipped) {
    els.subtitleFailureIncludeSkipped.addEventListener('change', function () {
      loadSubtitleFailures();
    });
  }
  if (els.subtitleFailuresRetryBtn) {
    els.subtitleFailuresRetryBtn.addEventListener('click', function () {
      subtitleFailuresRetry(false);
    });
  }
  if (els.subtitleFailuresRetryForceBtn) {
    els.subtitleFailuresRetryForceBtn.addEventListener('click', function () {
      subtitleFailuresRetry(true);
    });
  }
  if (els.subtitleWatchScanBtn) {
    els.subtitleWatchScanBtn.addEventListener('click', subtitleWatchScan);
  }
}
