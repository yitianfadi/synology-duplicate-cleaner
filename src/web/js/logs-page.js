/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 操作日志（v0057 从 app.js 拆出；设置页「操作日志」标签）
   --------------------------------------------------------------------------
   行为与拆出前完全一致；依赖 js/ 下的基础模块，不依赖 app.js。ES2019。
   规范：不使用 innerHTML，不可信文本一律 textContent。
   ========================================================================== */

import {
  api, errorMessage
} from './api.js';
import { AUDIT_LABEL } from './constants.js';
import {
  $$, clear, el, icon, setHidden, setText
} from './dom.js';
import {
  fmtBytes, fmtDateTime, fmtInt
} from './format.js';
import { confirmDialog } from './modal.js';
import {
  els, state
} from './state.js';
import { toast } from './toast.js';
import { encodePath } from './util.js';

var LOG_KIND_LABEL = { scan: '扫描日志', delete: '删除日志', audit: '审计日志', deletion: '删除日志', other: '其他' };

export async function loadLogs() {
  setHidden(els.logsLoading, false);
  setHidden(els.logsError, true);
  try {
    var res = await api('/api/logs?limit=' + encodeURIComponent(state.logsLimit), { timeout: 15000 });
    state.logsLoaded = true;
    renderLogs(res || {});
  } catch (err) {
    setHidden(els.logsLoading, true);
    setText(els.logsError, errorMessage(err, '加载操作日志失败，请稍后重试。'));
    setHidden(els.logsError, false);
  }
}

function syncAuditSelectionUi() {
  var selected = state.auditSelection;
  if (els.auditDeleteSelectedBtn) {
    els.auditDeleteSelectedBtn.disabled = selected.size === 0;
    els.auditDeleteSelectedBtn.textContent = selected.size
      ? '删除选中 (' + fmtInt(selected.size) + ')' : '删除选中';
  }
  if (els.auditSelectAll) {
    var boxes = $$('#auditList input.audit-check');
    var checked = boxes.filter(function (box) { return box.checked; }).length;
    els.auditSelectAll.checked = boxes.length > 0 && checked === boxes.length;
    els.auditSelectAll.indeterminate = checked > 0 && checked < boxes.length;
  }
}

function renderLogs(data) {
  setHidden(els.logsLoading, true);
  var logs = Array.isArray(data.logs) ? data.logs : [];
  var audit = Array.isArray(data.audit) ? data.audit : [];
  // 只保留仍在当前列表中的选中项（删除/刷新后不残留）
  var present = new Set();
  audit.forEach(function (entry) { if (entry && entry.id) { present.add(entry.id); } });
  Array.from(state.auditSelection).forEach(function (id) {
    if (!present.has(id)) { state.auditSelection.delete(id); }
  });

  clear(els.auditList);
  setText(els.auditCount, audit.length ? '共 ' + fmtInt(audit.length) + ' 条' : '');
  setHidden(els.auditEmpty, audit.length > 0);
  audit.forEach(function (entry) {
    var detailText = '';
    if (entry.detail !== null && entry.detail !== undefined) {
      if (typeof entry.detail === 'string') { detailText = entry.detail; }
      else {
        try { detailText = JSON.stringify(entry.detail); } catch (e) { detailText = String(entry.detail); }
      }
    }
    if (detailText.length > 400) { detailText = detailText.slice(0, 400) + '…'; }
    var actionLabel = AUDIT_LABEL[entry.action] || entry.action || '—';
    var actionCell = el('span', { className: 'audit-action', text: actionLabel });
    if (AUDIT_LABEL[entry.action]) {
      actionCell.appendChild(el('span', { className: 'muted small', text: ' (' + entry.action + ')' }));
    }
    var id = entry.id || '';
    var checkbox = el('input', {
      attrs: { type: 'checkbox', 'aria-label': '选择该条审计事件', 'data-id': id }
    });
    checkbox.className = 'audit-check';
    checkbox.checked = !!(id && state.auditSelection.has(id));
    checkbox.disabled = !id;
    if (id) {
      checkbox.addEventListener('change', function () {
        if (checkbox.checked) { state.auditSelection.add(id); }
        else { state.auditSelection.delete(id); }
        syncAuditSelectionUi();
      });
    }
    els.auditList.appendChild(el('tr', {}, [
      el('td', { className: 'col-check' }, [checkbox]),
      el('td', { text: fmtDateTime(entry.ts) }),
      el('td', {}, [actionCell]),
      el('td', { text: entry.user || '—' }),
      el('td', {}, [el('span', { className: 'audit-detail', attrs: { title: detailText || '' }, text: detailText || '—' })])
    ]));
  });
  syncAuditSelectionUi();

  clear(els.logFilesList);
  setText(els.logFilesCount, logs.length ? '共 ' + fmtInt(logs.length) + ' 个' : '');
  setHidden(els.logFilesEmpty, logs.length > 0);
  logs.forEach(function (log) {
    var name = log.name || '';
    var downloadLink = el('a', {
      className: 'icon-btn',
      attrs: {
        href: '/api/logs/download?name=' + encodePath(name),
        download: name,
        title: '下载日志',
        'aria-label': '下载日志 ' + name
      }
    }, [icon('download')]);
    var deleteButton = el('button', {
      className: 'icon-btn icon-btn-danger',
      attrs: { type: 'button', title: '删除该日志文件', 'aria-label': '删除日志文件 ' + name },
      on: { click: function () { deleteLogFile(name); } }
    }, [icon('trash')]);
    var metaText = '类型 ' + (LOG_KIND_LABEL[log.kind] || log.kind || '未知') +
      ' · ' + fmtBytes(log.size) + ' · ' + fmtDateTime(log.modified_at);
    if (log.scan_id) { metaText += ' · ' + log.scan_id; }
    els.logFilesList.appendChild(el('li', { className: 'log-file-item' }, [
      el('span', { className: 'log-file-icon' }, [icon('file')]),
      el('div', { className: 'log-file-main' }, [
        el('div', { className: 'log-name', text: name || '—' }),
        el('div', { className: 'log-meta', text: metaText })
      ]),
      downloadLink,
      deleteButton
    ]));
  });
}

function logsKeepDays() {
  var value = parseInt(els.logsKeepDays && els.logsKeepDays.value, 10);
  return value > 0 ? value : (state.logsKeepDays || 30);
}

async function deleteAuditSelected() {
  var ids = Array.from(state.auditSelection);
  if (!ids.length) { return; }
  var ok = await confirmDialog({
    title: '删除选中的审计事件？',
    message: '将删除选中的 ' + fmtInt(ids.length) + ' 条审计事件，删除后不可恢复。',
    confirmText: '删除',
    danger: true
  });
  if (!ok) { return; }
  try {
    var res = await api('/api/logs/audit/delete', {
      method: 'POST', body: { ids: ids }, timeout: 20000
    });
    toast('已删除 ' + fmtInt((res && res.deleted) || 0) + ' 条审计事件。', 'success');
    state.auditSelection.clear();
    await loadLogs();
  } catch (err) {
    toast(errorMessage(err, '删除审计事件失败，请稍后重试。'), 'error', 6000);
  }
}

async function pruneAuditByDays() {
  var days = logsKeepDays();
  var ok = await confirmDialog({
    title: '清理审计事件？',
    message: '将删除 ' + days + ' 天之前的审计事件（保留最近 ' + days + ' 天），删除后不可恢复。',
    confirmText: '清理',
    danger: true
  });
  if (!ok) { return; }
  try {
    var res = await api('/api/logs/audit/delete', {
      method: 'POST', body: { keep_days: days }, timeout: 30000
    });
    toast('已清理 ' + fmtInt((res && res.deleted) || 0) + ' 条审计事件。', 'success');
    state.auditSelection.clear();
    await loadLogs();
  } catch (err) {
    toast(errorMessage(err, '清理审计事件失败，请稍后重试。'), 'error', 6000);
  }
}

async function pruneLogFilesByDays() {
  var days = logsKeepDays();
  var ok = await confirmDialog({
    title: '清理日志文件？',
    message: '将删除 ' + days + ' 天之前的扫描/删除日志文件（保留最近 ' + days + ' 天），删除后不可恢复。',
    confirmText: '清理',
    danger: true
  });
  if (!ok) { return; }
  try {
    var res = await api('/api/logs/files/delete', {
      method: 'POST', body: { keep_days: days }, timeout: 30000
    });
    toast('已清理 ' + fmtInt((res && res.deleted) || 0) + ' 个日志文件。', 'success');
    await loadLogs();
  } catch (err) {
    toast(errorMessage(err, '清理日志文件失败，请稍后重试。'), 'error', 6000);
  }
}

async function deleteLogFile(name) {
  if (!name) { return; }
  var ok = await confirmDialog({
    title: '删除日志文件？',
    message: '将删除日志文件「' + name + '」，删除后不可恢复。',
    confirmText: '删除',
    danger: true
  });
  if (!ok) { return; }
  try {
    var res = await api('/api/logs/files/delete', {
      method: 'POST', body: { names: [name] }, timeout: 20000
    });
    toast('已删除 ' + fmtInt((res && res.deleted) || 0) + ' 个日志文件。', 'success');
    await loadLogs();
  } catch (err) {
    toast(errorMessage(err, '删除日志文件失败，请稍后重试。'), 'error', 6000);
  }
}

export function bindLogsEvents() {
  els.logsRefreshBtn.addEventListener('click', loadLogs);
  els.logLimitSelect.addEventListener('change', function () {
    var value = parseInt(els.logLimitSelect.value, 10);
    state.logsLimit = value > 0 ? value : 50;
    loadLogs();
  });
  if (els.logsKeepDays) {
    els.logsKeepDays.value = String(state.logsKeepDays || 30);
    els.logsKeepDays.addEventListener('change', function () {
      var value = parseInt(els.logsKeepDays.value, 10);
      state.logsKeepDays = value > 0 ? value : 30;
    });
  }
  if (els.auditSelectAll) {
    els.auditSelectAll.addEventListener('change', function () {
      var checked = !!els.auditSelectAll.checked;
      state.auditSelection.clear();
      $$('#auditList input.audit-check').forEach(function (box) {
        if (box.disabled) { return; }
        box.checked = checked;
        var id = box.getAttribute('data-id');
        if (checked && id) { state.auditSelection.add(id); }
      });
      syncAuditSelectionUi();
    });
  }
  if (els.auditDeleteSelectedBtn) {
    els.auditDeleteSelectedBtn.addEventListener('click', deleteAuditSelected);
  }
  if (els.auditPruneBtn) { els.auditPruneBtn.addEventListener('click', pruneAuditByDays); }
  if (els.logFilesPruneBtn) { els.logFilesPruneBtn.addEventListener('click', pruneLogFilesByDays); }
  // v0049：日志检索 / 可读导出
  if (els.logSearchBtn) { els.logSearchBtn.addEventListener('click', searchLogs); }
  if (els.logSearchClearBtn) {
    els.logSearchClearBtn.addEventListener('click', function () {
      if (els.logSearchQuery) { els.logSearchQuery.value = ''; }
      if (els.logSearchJobId) { els.logSearchJobId.value = ''; }
      if (els.logSearchResults) { clear(els.logSearchResults); }
      setHidden(els.logSearchEmpty, true);
      setText(els.logSearchStatus, '');
      updateLogExportHref();
    });
  }
  if (els.logSearchQuery) {
    els.logSearchQuery.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { searchLogs(); }
    });
  }
  if (els.logSearchJobId) {
    els.logSearchJobId.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { searchLogs(); }
    });
  }
}

function logSearchParams() {
  return {
    q: (els.logSearchQuery ? els.logSearchQuery.value : '').trim(),
    jobId: (els.logSearchJobId ? els.logSearchJobId.value : '').trim()
  };
}

function updateLogExportHref() {
  if (!els.logSearchExportBtn) { return; }
  var params = logSearchParams();
  var query = [];
  if (params.q) { query.push('q=' + encodeURIComponent(params.q)); }
  if (params.jobId) { query.push('job_id=' + encodeURIComponent(params.jobId)); }
  els.logSearchExportBtn.href = '/api/logs/export'
    + (query.length ? '?' + query.join('&') : '');
}

async function searchLogs() {
  var params = logSearchParams();
  if (!params.q && !params.jobId) {
    toast('请输入关键字或任务 ID 再搜索。', 'info', 3000);
    return;
  }
  setHidden(els.logSearchLoading, false);
  setHidden(els.logSearchEmpty, true);
  updateLogExportHref();
  var query = [];
  if (params.q) { query.push('q=' + encodeURIComponent(params.q)); }
  if (params.jobId) { query.push('job_id=' + encodeURIComponent(params.jobId)); }
  try {
    var res = await api('/api/logs/search?' + query.join('&') + '&limit=100',
      { timeout: 20000 });
    setHidden(els.logSearchLoading, true);
    renderLogSearchResults(res || {});
  } catch (err) {
    setHidden(els.logSearchLoading, true);
    setHidden(els.logSearchEmpty, false);
    setText(els.logSearchStatus,
      errorMessage(err, '日志检索失败，请稍后重试。'));
  }
}

function renderLogSearchResults(data) {
  if (!els.logSearchResults) { return; }
  clear(els.logSearchResults);
  var logs = data.logs || [];
  var audit = data.audit || [];
  setHidden(els.logSearchEmpty, logs.length > 0 || audit.length > 0);
  setText(els.logSearchStatus, '匹配日志 ' + fmtInt(logs.length)
    + ' 个 · 审计 ' + fmtInt(audit.length) + ' 条 · 扫描 '
    + fmtInt(data.scanned_logs || 0) + ' 个文件');
  logs.forEach(function (hit) {
    var card = el('div', { className: 'log-search-item' }, [
      el('div', { className: 'log-search-head' }, [
        el('span', { className: 'mono', text: hit.name || '' }),
        el('span', { className: 'muted',
          text: fmtBytes(hit.size || 0) + ' · ' + (hit.modified_at || '') }),
        el('span', { className: 'badge badge-blue',
          text: '匹配 ' + fmtInt(hit.match_count || 0) + ' 行' })
      ])
    ]);
    var pre = el('pre', { className: 'log-match-lines' });
    pre.textContent = (hit.matches || []).join('\n')
      + (hit.truncated ? '\n…（仅显示前 30 行匹配）' : '');
    card.appendChild(pre);
    els.logSearchResults.appendChild(card);
  });
  audit.forEach(function (entry) {
    var card = el('div', { className: 'log-search-item' });
    card.appendChild(el('div', { className: 'log-search-head' }, [
      el('span', { className: 'badge badge-gray',
        text: '审计 ' + (entry.action || '') }),
      el('span', { className: 'muted', text: entry.time || '' })
    ]));
    var pre = el('pre', { className: 'log-match-lines' });
    pre.textContent = JSON.stringify(entry, null, 2);
    card.appendChild(pre);
    els.logSearchResults.appendChild(card);
  });
}
