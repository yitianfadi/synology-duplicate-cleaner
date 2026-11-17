/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 扫描历史（v0057 从 app.js 拆出；扫描页「历史」标签）
   --------------------------------------------------------------------------
   行为与拆出前完全一致；依赖 js/ 下的基础模块，不依赖 app.js。ES2019。
   规范：不使用 innerHTML，不可信文本一律 textContent。
   ========================================================================== */

import {
  api, errorMessage
} from './api.js';
import {
  ACTIVE_STATUS, KEEP_MODE_LABEL
} from './constants.js';
import {
  clear, el, icon, setHidden, setText, statusBadge
} from './dom.js';
import {
  fmtBytes, fmtDateTime, fmtInt
} from './format.js';
import { detectScanMode } from './labels.js';
import { confirmDialog } from './modal.js';
import {
  els, state
} from './state.js';
import { toast } from './toast.js';
import {
  encodePath, normalizePathList
} from './util.js';

export async function loadHistory() {
  setHidden(els.historyLoading, false);
  setHidden(els.historyError, true);
  setHidden(els.historyEmpty, true);
  try {
    var res = await api('/api/scans', { timeout: 15000 });
    var scans = res && Array.isArray(res.scans) ? res.scans : [];
    state.historyCache = scans;
    renderHistory(scans);
  } catch (err) {
    setHidden(els.historyLoading, true);
    setText(els.historyError, errorMessage(err, '加载历史记录失败，请稍后重试。'));
    setHidden(els.historyError, false);
  }
}

function renderHistory(scans) {
  setHidden(els.historyLoading, true);
  clear(els.historyList);
  if (!scans.length) {
    setHidden(els.historyEmpty, false);
    return;
  }
  setHidden(els.historyEmpty, true);
  scans.forEach(function (scan) {
    els.historyList.appendChild(renderHistoryItem(scan));
  });
}

function pauseReasonLabel(reason) {
  if (!reason) { return ''; }
  var labels = {
    upgrade: '升级前暂停', user: '用户暂停', manual: '手动暂停', drain: '升级 drain 暂停'
  };
  return labels[reason] || String(reason);
}

async function resumeHistoryScan(scan, button) {
  if (!scan || !scan.scan_id) { return; }
  var ok = await confirmDialog({
    title: '继续扫描？',
    message: '暂停期间文件可能发生变化；继续时会重新校验并跳过已完成的缓存。任务不会自动继续，确定现在继续扫描？',
    confirmText: '继续扫描',
    danger: false
  });
  if (!ok) { return; }
  if (button) { button.disabled = true; setText(button, '正在继续…'); }
  try {
    await api('/api/scans/' + encodePath(scan.scan_id) + '/resume', {
      method: 'POST', body: {}, timeout: 15000
    });
    state.historyCache = null;
    toast('已提交继续扫描请求，可在历史记录中查看进度。', 'success', 5000);
    await loadHistory();
  } catch (err) {
    toast(errorMessage(err, '继续扫描失败，请稍后重试。'), 'error', 6000);
    if (button) { button.disabled = false; setText(button, '继续扫描'); }
  }
}

function renderHistoryItem(scan) {
  var status = scan.status || 'completed';
  var stats = scan.stats || {};
  var config = scan.config || {};
  var options = scan.options || {};
  var roots = normalizePathList(config.roots || options.roots || []);
  var keepMode = config.keep_mode || options.keep_mode;
  var startedAt = scan.started_at;
  var finishedAt = scan.finished_at;
  var scanMode = detectScanMode(scan);
  var item = el('div', { className: 'history-item' });

  var title = el('div', { className: 'history-title' }, [
    statusBadge(status),
    el('span', { text: '扫描任务' }),
    el('span', { className: 'mono', text: scan.scan_id || '—' }),
    el('span', {
      className: 'badge ' + (scanMode === 'similar' ? 'badge-purple' : 'badge-gray'),
      text: scanMode === 'similar' ? '内容相似模式' : '精准哈希模式'
    }),
    Number(stats.video_groups) > 0
      ? el('span', { className: 'badge badge-blue', text: '含视频视觉相似' })
      : null
  ]);
  var metaParts = ['开始：' + fmtDateTime(startedAt), '结束：' + (finishedAt ? fmtDateTime(finishedAt) : '—')];
  if (status === 'paused') {
    var reasonLabel = pauseReasonLabel(scan.pause_reason);
    metaParts.push(reasonLabel ? '暂停原因：' + reasonLabel : '已暂停，可继续');
  } else if (status === 'interrupted') {
    metaParts.push('已中断，需重新扫描（无可用断点）');
  }
  if (keepMode) { metaParts.push('保留策略：' + (KEEP_MODE_LABEL[keepMode] || keepMode)); }
  if (scanMode === 'similar') {
    var thresholdText = options.similarity_threshold !== undefined ? options.similarity_threshold
      : (config.similarity_threshold !== undefined ? config.similarity_threshold : null);
    if (thresholdText !== null) { metaParts.push('相似阈值：' + thresholdText + '%'); }
  }
  var meta = el('div', { className: 'history-meta' }, metaParts.map(function (text) {
    return el('span', { text: text });
  }));
  var statsItems = [];
  if (scanMode === 'similar') {
    statsItems = [
      el('span', {}, ['精准组 ', el('strong', { text: fmtInt(stats.exact_groups !== undefined ? stats.exact_groups : stats.duplicate_groups) })]),
      el('span', {}, ['相似组 ', el('strong', { text: fmtInt(stats.similar_groups) })]),
      el('span', {}, ['精准可释放 ', el('strong', { text: fmtBytes(stats.reclaimable_bytes) })]),
      el('span', {}, ['相似潜在 ', el('strong', { text: fmtBytes(stats.similar_reclaimable_bytes) })]),
      el('span', {}, ['已扫描 ', el('strong', { text: fmtInt(stats.files_scanned) })]),
      Number(stats.needs_manual_keep_groups) > 0
        ? el('span', {}, ['需人工确认 ', el('strong', { text: fmtInt(stats.needs_manual_keep_groups) }), ' 组'])
        : null,
      Number(stats.mtime_fallback_groups) > 0
        ? el('span', {}, ['修改时间降级 ', el('strong', { text: fmtInt(stats.mtime_fallback_groups) }), ' 组'])
        : null,
      Number(stats.video_groups) > 0
        ? el('span', {}, ['视频相似 ', el('strong', { text: fmtInt(stats.video_groups) }), ' 组'])
        : null
    ];
  } else {
    statsItems = [
      el('span', {}, ['精准组 ', el('strong', { text: fmtInt(stats.exact_groups !== undefined ? stats.exact_groups : stats.duplicate_groups) })]),
      el('span', {}, ['精准文件 ', el('strong', { text: fmtInt(stats.exact_files !== undefined ? stats.exact_files : stats.duplicate_files) })]),
      el('span', {}, ['可释放 ', el('strong', { text: fmtBytes(stats.reclaimable_bytes) })]),
      el('span', {}, ['已扫描 ', el('strong', { text: fmtInt(stats.files_scanned) })]),
      Number(stats.needs_manual_keep_groups) > 0
        ? el('span', {}, ['需人工确认 ', el('strong', { text: fmtInt(stats.needs_manual_keep_groups) }), ' 组'])
        : null,
      Number(stats.mtime_fallback_groups) > 0
        ? el('span', {}, ['修改时间降级 ', el('strong', { text: fmtInt(stats.mtime_fallback_groups) }), ' 组'])
        : null,
      Number(stats.video_groups) > 0
        ? el('span', {}, ['视频相似 ', el('strong', { text: fmtInt(stats.video_groups) }), ' 组'])
        : null
    ];
  }
  var statsEl = el('div', { className: 'history-stats' }, statsItems);
  var rootsEl = el('div', { className: 'history-roots' });
  roots.slice(0, 3).forEach(function (path) {
    rootsEl.appendChild(el('span', { className: 'root-chip', attrs: { title: path }, text: path }));
  });
  if (roots.length > 3) {
    rootsEl.appendChild(el('span', { className: 'root-chip', text: '等 ' + roots.length + ' 个目录' }));
  }
  var main = el('div', { className: 'history-main' }, [title, meta, statsEl, rootsEl]);

  var actions = el('div', { className: 'history-actions' });
  var canViewProgress = ACTIVE_STATUS.indexOf(status) !== -1;
  var canViewResults = status === 'completed' || status === 'stopped';
  if (status === 'paused') {
    var resumeHistoryBtn = el('button', {
      className: 'btn btn-primary btn-sm', attrs: { type: 'button' }, text: '继续扫描'
    });
    resumeHistoryBtn.addEventListener('click', function () { resumeHistoryScan(scan, resumeHistoryBtn); });
    actions.appendChild(resumeHistoryBtn);
  }
  var viewBtn = el('button', {
    className: 'btn btn-ghost btn-sm', attrs: { type: 'button' },
    text: canViewProgress ? '查看进度' : '查看结果',
    on: { click: function () { state.openScan(scan); } }
  });
  viewBtn.disabled = !(canViewProgress || canViewResults);
  if (status === 'interrupted') {
    viewBtn.title = '扫描已中断，需重新扫描；没有可用断点。';
  }
  actions.appendChild(viewBtn);
  if (canViewResults) {
    actions.appendChild(el('a', {
      className: 'btn btn-ghost btn-sm',
      attrs: {
        href: '/api/scans/' + encodePath(scan.scan_id) + '/csv',
        download: 'duplicate-scan-' + scan.scan_id + '.csv'
      }
    }, [icon('download'), '导出 CSV']));
    actions.appendChild(el('a', {
      className: 'btn btn-ghost btn-sm',
      attrs: {
        href: '/api/scans/' + encodePath(scan.scan_id) + '/xlsx',
        download: 'duplicate-scan-' + scan.scan_id + '.xlsx'
      }
    }, [icon('download'), '导出 Excel']));
  }
  item.appendChild(main);
  item.appendChild(actions);
  return item;
}

export function initScanHistory() {
  if (els.historyRefreshBtn) {
    els.historyRefreshBtn.addEventListener('click', function () { loadHistory(); });
  }
}
