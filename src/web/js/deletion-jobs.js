/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 删除任务列表（v0057 从 app.js 拆出；扫描页「删除与回收站」标签）
   --------------------------------------------------------------------------
   行为与拆出前完全一致；依赖 js/ 下的基础模块，不依赖 app.js。ES2019。
   规范：不使用 innerHTML，不可信文本一律 textContent。
   ========================================================================== */

import {
  api, errorMessage
} from './api.js';
import {
  clear, el, setBusy, setHidden, setText
} from './dom.js';
import {
  fmtBytes, fmtDateTime, fmtInt
} from './format.js';
import {
  els, state
} from './state.js';
import { toast } from './toast.js';
import { encodePath } from './util.js';

var DELETION_STATUS_LABEL = {
  queued: '排队中', running: '删除中', completed: '已完成',
  partial: '部分完成', failed: '失败', stopped: '已停止', cancelled: '已取消'
};

var DELETION_STATUS_BADGE = {
  queued: 'badge-gray', running: 'badge-blue', completed: 'badge-green',
  partial: 'badge-amber', failed: 'badge-red', stopped: 'badge-gray', cancelled: 'badge-gray'
};

function normalizeDeletionJobsResponse(res) {
  if (!res) { return []; }
  if (Array.isArray(res)) { return res; }
  var keys = ['jobs', 'deletions', 'items', 'data', 'results'];
  for (var i = 0; i < keys.length; i += 1) {
    if (Array.isArray(res[keys[i]])) { return res[keys[i]]; }
  }
  return [];
}

function deletionJobNumber(job, keys) {
  for (var i = 0; i < keys.length; i += 1) {
    var value = job[keys[i]];
    if (value !== undefined && value !== null && value !== '') {
      var num = Number(value);
      if (isFinite(num)) { return num; }
    }
  }
  return null;
}

function deletionJobText(job, keys) {
  for (var i = 0; i < keys.length; i += 1) {
    var value = job[keys[i]];
    if (value !== undefined && value !== null && value !== '') { return String(value); }
  }
  return '';
}

function deletionJobStartedText(job) {
  var iso = deletionJobText(job, ['started_at', 'created_at', 'started']);
  if (iso) { return fmtDateTime(iso); }
  var epoch = deletionJobNumber(job, ['started_epoch', 'created_epoch']);
  if (epoch !== null && epoch > 0) {
    try { return fmtDateTime(new Date(epoch * 1000).toISOString()); } catch (e) { return '—'; }
  }
  return '—';
}

export async function loadDeletionJobs() {
  if (!els.deletionJobsList || state.deletionJobs.loading) { return; }
  state.deletionJobs.loading = true;
  setHidden(els.deletionJobsLoading, false);
  setHidden(els.deletionJobsError, true);
  setHidden(els.deletionJobsEmpty, true);
  try {
    var res = await api('/api/deletions?limit=20', { timeout: 15000 });
    var jobs = normalizeDeletionJobsResponse(res).filter(function (job) {
      return job && (job.job_id || job.scan_id);
    });
    state.deletionJobs.list = jobs;
    state.deletionJobs.loaded = true;
    state.deletionJobs.lastError = '';
    renderDeletionJobs(jobs);
  } catch (err) {
    var message = errorMessage(err, '加载删除任务失败，请稍后重试。');
    if (err && err.status === 404) {
      message = '当前后端未提供删除任务历史接口；升级后端后可用。';
    } else if (err && err.status === 403) {
      message = '仅管理员可查看删除任务历史。';
    }
    state.deletionJobs.lastError = message;
    clear(els.deletionJobsList);
    setHidden(els.deletionJobsTableWrap, true);
    setText(els.deletionJobsStatus, '');
    setText(els.deletionJobsError, message);
    setHidden(els.deletionJobsError, false);
  } finally {
    state.deletionJobs.loading = false;
    setHidden(els.deletionJobsLoading, true);
  }
}

function renderDeletionJobs(jobs) {
  clear(els.deletionJobsList);
  setText(els.deletionJobsStatus, jobs.length ? '共 ' + fmtInt(jobs.length) + ' 个任务' : '');
  if (!jobs.length) {
    setHidden(els.deletionJobsTableWrap, true);
    setHidden(els.deletionJobsEmpty, false);
    return;
  }
  setHidden(els.deletionJobsEmpty, true);
  setHidden(els.deletionJobsTableWrap, false);
  jobs.forEach(function (job) {
    els.deletionJobsList.appendChild(renderDeletionJobRow(job));
  });
}

function renderDeletionJobRow(job) {
  var status = String(job.status || (job.finished_at ? 'completed' : 'running'));
  var files = deletionJobNumber(job, ['files', 'total', 'file_count', 'total_files']);
  var deleted = deletionJobNumber(job, ['deleted', 'done', 'deleted_files', 'ok_count']);
  var failed = deletionJobNumber(job, ['failed', 'failed_files', 'error_count']);
  var bytes = deletionJobNumber(job, ['bytes', 'bytes_done', 'bytes_total', 'deleted_bytes']);
  var scanId = deletionJobText(job, ['scan_id']);
  var row = el('tr');
  row.appendChild(el('td', { className: 'nowrap', text: deletionJobStartedText(job) }));
  row.appendChild(el('td', {}, [
    scanId
      ? el('span', { className: 'mono deletion-scan-id', attrs: { title: scanId }, text: scanId })
      : el('span', { className: 'muted', text: '—' })
  ]));
  row.appendChild(el('td', {}, [
    el('span', {
      className: 'badge ' + (DELETION_STATUS_BADGE[status] || 'badge-gray'),
      text: DELETION_STATUS_LABEL[status] || status
    })
  ]));
  row.appendChild(el('td', { className: 'nowrap', text: files === null ? '—' : fmtInt(files) }));
  row.appendChild(el('td', { className: 'nowrap', text: deleted === null ? '—' : fmtInt(deleted) }));
  row.appendChild(el('td', {
    className: 'nowrap' + (failed ? ' deletion-failed' : ''),
    text: failed === null ? '—' : fmtInt(failed)
  }));
  row.appendChild(el('td', { className: 'nowrap', text: bytes === null ? '—' : fmtBytes(bytes) }));
  var actionCell = el('td', { className: 'nowrap' });
  if (scanId) {
    var openBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '打开扫描'
    });
    openBtn.addEventListener('click', function () { openDeletionScan(scanId, openBtn); });
    actionCell.appendChild(openBtn);
  } else {
    actionCell.appendChild(el('span', { className: 'muted', text: '—' }));
  }
  row.appendChild(actionCell);
  return row;
}

async function openDeletionScan(scanId, button) {
  if (!scanId) { return; }
  var cached = (state.historyCache || []).filter(function (scan) {
    return scan && scan.scan_id === scanId;
  })[0];
  if (cached) { state.openScan(cached); return; }
  if (button) { setBusy(button, true, '正在打开…'); }
  try {
    var res = await api('/api/scans/' + encodePath(scanId), { timeout: 15000 });
    if (res && res.scan) { state.openScan(res.scan); }
    else { toast('未找到对应的扫描任务，可能已被清理。', 'error'); }
  } catch (err) {
    toast(errorMessage(err, '打开扫描任务失败，请稍后重试。'), 'error', 6000);
  } finally {
    if (button) { setBusy(button, false); }
  }
}

export function initDeletionJobs() {
  if (els.deletionJobsRefreshBtn) {
    els.deletionJobsRefreshBtn.addEventListener('click', function () { loadDeletionJobs(); });
  }
}
