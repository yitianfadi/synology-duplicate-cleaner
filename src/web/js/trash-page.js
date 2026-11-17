/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 套件回收站与硬链接合并记录（v0057 从 app.js 拆出）
   --------------------------------------------------------------------------
   行为与拆出前完全一致；依赖 js/ 下的基础模块，不依赖 app.js。ES2019。
   规范：不使用 innerHTML，不可信文本一律 textContent。
   ========================================================================== */

import {
  api, errorMessage
} from './api.js';
import { RECYCLE_MODE_LABEL } from './constants.js';
import {
  clear, el, setHidden, setText, show
} from './dom.js';
import {
  fmtBytes, fmtDateTime, fmtInt
} from './format.js';
import {
  closeModal, confirmDialog, openModal
} from './modal.js';
import {
  els, state
} from './state.js';
import { toast } from './toast.js';
import { encodePath } from './util.js';

export async function loadTrashPanel() {
  if (els.trashLoading) { setHidden(els.trashLoading, false); }
  if (els.trashError) { setHidden(els.trashError, true); }
  if (els.trashEmpty) { setHidden(els.trashEmpty, true); }
  try {
    var res = await api('/api/trash?limit=100', { timeout: 20000 });
    if (els.trashLoading) { setHidden(els.trashLoading, true); }
    renderTrashList(res || {});
    if (els.trashRecycleMode) {
      var mode = (res && res.recycle_mode) || 'share';
      setText(els.trashRecycleMode,
        '当前删除去向：' + (RECYCLE_MODE_LABEL[mode] || mode));
    }
  } catch (err) {
    if (els.trashLoading) { setHidden(els.trashLoading, true); }
    setText(els.trashError, errorMessage(err, '加载回收站失败，请稍后重试。'));
    setHidden(els.trashError, false);
  }
  loadMergeHistory();
}

function renderTrashList(data) {
  if (!els.trashList) { return; }
  clear(els.trashList);
  var jobs = Array.isArray(data.trash) ? data.trash : [];
  setHidden(els.trashEmpty, jobs.length > 0);
  jobs.forEach(function (job) {
    els.trashList.appendChild(renderTrashJobCard(job));
  });
}

function renderTrashJobCard(job) {
  var card = el('div', {
    className: 'trash-card' + (job.purged ? ' is-purged' : '')
  });
  var metaBits = [
    fmtDateTime(job.created_at || ''),
    '文件 ' + fmtInt(job.entries_total || 0) + ' 个',
    '已恢复 ' + fmtInt(job.entries_restored || 0),
    '可恢复 ' + fmtInt(job.entries_remaining || 0),
    '剩余 ' + fmtBytes(job.bytes_remaining || 0)
  ];
  card.appendChild(el('div', { className: 'trash-card-head' }, [
    el('div', { className: 'trash-card-title', text: '删除批次 ' + (job.trash_id || '') }),
    el('div', { className: 'trash-card-meta', text: metaBits.join(' · ') })
  ]));
  var actions = el('div', { className: 'trash-card-actions' });
  var detailBtn = el('button', {
    className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '查看明细'
  });
  detailBtn.addEventListener('click', function () { openTrashDetail(job.trash_id); });
  actions.appendChild(detailBtn);
  if (job.purged) {
    actions.appendChild(el('span', { className: 'badge badge-gray', text: '已清理' }));
  } else if (Number(job.entries_remaining || 0) > 0) {
    var restoreBtn = el('button', {
      className: 'btn btn-primary btn-sm', attrs: { type: 'button' },
      text: '整组恢复'
    });
    restoreBtn.addEventListener('click', function () { restoreTrashJob(job); });
    actions.appendChild(restoreBtn);
    var purgeBtn = el('button', {
      className: 'btn btn-danger btn-sm', attrs: { type: 'button' },
      text: '清理（永久删除）'
    });
    purgeBtn.addEventListener('click', function () { purgeTrashJob(job); });
    actions.appendChild(purgeBtn);
  }
  card.appendChild(actions);
  return card;
}

async function openTrashDetail(trashId) {
  var closeBtn = el('button', {
    className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
    on: { click: function () { closeModal(true); } }
  });
  var modal = openModal({
    title: '回收站明细',
    subtitle: trashId,
    size: 'lg',
    body: el('div', { className: 'modal-loading' }, [
      el('span', { className: 'spinner spinner-lg' }), '正在加载明细…'
    ]),
    footer: [closeBtn],
    initialFocus: closeBtn
  });
  try {
    var detail = await api('/api/trash/' + encodePath(trashId), { timeout: 20000 });
    var entries = detail.entries || [];
    var list = el('div', { className: 'trash-detail-list' });
    entries.forEach(function (entry) {
      var stateText = entry.restored_at ? '已恢复'
        : (entry.purged_at ? '已清理'
          : (entry.trash_exists ? (entry.original_exists ? '原位置已存在' : '可恢复') : '回收站内已缺失'));
      var stateClass = entry.restored_at ? 'badge-green'
        : (entry.purged_at ? 'badge-gray'
          : (entry.trash_exists ? (entry.original_exists ? 'badge-amber' : 'badge-blue') : 'badge-red'));
      list.appendChild(el('div', { className: 'trash-detail-row' }, [
        el('div', { className: 'trash-detail-path', attrs: { title: entry.original }, text: entry.original }),
        el('span', { className: 'badge ' + stateClass, text: stateText }),
        el('span', { className: 'muted', text: fmtBytes(entry.size || 0) })
      ]));
    });
    if (!entries.length) {
      list.appendChild(el('p', { className: 'hint', text: '该批次没有记录。' }));
    }
    modal.setBody(el('div', { className: 'trash-detail-body' }, [
      el('p', { className: 'hint', text: '文件位置：' + ((detail.summary && detail.summary.trash_id) || trashId) }),
      list
    ]));
  } catch (err) {
    modal.setBody(el('div', { className: 'state-box state-error', text: errorMessage(err, '加载回收站明细失败。') }));
  }
}

async function restoreTrashJob(job) {
  var ok = await confirmDialog({
    title: '整组恢复？',
    message: '将把该批次中仍在回收站里的文件恢复到原位置（共 '
      + fmtInt(job.entries_remaining || 0) + ' 个，'
      + fmtBytes(job.bytes_remaining || 0) + '）。原位置已有同名文件时不会覆盖。',
    confirmText: '恢复'
  });
  if (!ok) { return; }
  try {
    var res = await api('/api/trash/' + encodePath(job.trash_id) + '/restore', {
      method: 'POST', body: { confirm: true }, timeout: 120000
    });
    var skipped = (res.results || []).filter(function (item) {
      return item.status === 'skipped';
    });
    var failed = (res.results || []).filter(function (item) {
      return item.status === 'failed';
    });
    var message = '已恢复 ' + fmtInt(res.restored || 0) + ' 个文件';
    if (skipped.length) { message += '；跳过 ' + fmtInt(skipped.length) + ' 个（冲突/缺失）'; }
    if (failed.length) { message += '；失败 ' + fmtInt(failed.length) + ' 个'; }
    toast(message, failed.length ? 'error' : 'success', 6000);
    await loadTrashPanel();
  } catch (err) {
    toast(errorMessage(err, '恢复失败，请稍后重试。'), 'error', 6000);
  }
}

function purgeTrashJob(job) {
  var input = el('input', {
    attrs: { type: 'text', placeholder: '请输入“永久删除”', autocomplete: 'off', spellcheck: 'false' }
  });
  var errorEl = el('p', { className: 'field-error', attrs: { role: 'alert' }, text: '' });
  var cancelBtn = el('button', {
    className: 'btn btn-ghost', attrs: { type: 'button' }, text: '取消',
    on: { click: function () { closeModal(true); } }
  });
  var modal = openModal({
    title: '清理回收站（不可恢复）',
    message: '将永久删除该批次在回收站里的 ' + fmtInt(job.entries_remaining || 0)
      + ' 个文件（' + fmtBytes(job.bytes_remaining || 0) + '）。',
    size: 'sm',
    body: el('div', { className: 'form-grid' }, [
      el('div', { className: 'field field-wide' }, [
        el('label', { text: '确认短语' }), input, errorEl
      ])
    ]),
    footer: [cancelBtn, el('button', {
      className: 'btn btn-danger', attrs: { type: 'button' }, text: '永久删除',
      on: {
        click: async function () {
          if (input.value.trim() !== '永久删除') {
            setText(errorEl, '请输入完整的“永久删除”。');
            show(errorEl);
            return;
          }
          try {
            var res = await api('/api/trash/' + encodePath(job.trash_id) + '/purge', {
              method: 'POST',
              body: { confirm: true, permanent_phrase: input.value.trim() },
              timeout: 120000
            });
            closeModal(true);
            toast('已清理 ' + fmtInt(res.removed || 0) + ' 个文件，释放 '
              + fmtBytes(res.bytes_freed || 0), 'success', 6000);
            await loadTrashPanel();
          } catch (err) {
            setText(errorEl, errorMessage(err, '清理失败，请稍后重试。'));
            show(errorEl);
          }
        }
      }
    })]
  });
  setTimeout(function () { try { input.focus(); } catch (e) { /* 忽略 */ } }, 50);
  return modal;
}

export async function loadMergeHistory() {
  if (!els.trashMergeList) { return; }
  setHidden(els.trashMergeLoading, false);
  try {
    var res = await api('/api/merges?limit=100', { timeout: 20000 });
    setHidden(els.trashMergeLoading, true);
    var jobs = Array.isArray(res.merges) ? res.merges : [];
    clear(els.trashMergeList);
    setHidden(els.trashMergeEmpty, jobs.length > 0);
    setText(els.trashMergeCount, jobs.length ? '共 ' + fmtInt(jobs.length) + ' 次' : '');
    jobs.forEach(function (job) {
      els.trashMergeList.appendChild(renderMergeJobCard(job));
    });
  } catch (err) {
    setHidden(els.trashMergeLoading, true);
    setHidden(els.trashMergeEmpty, false);
    if (els.trashMergeCount) {
      setText(els.trashMergeCount, '加载失败：' + errorMessage(err, '请稍后重试'));
    }
  }
}

function renderMergeJobCard(job) {
  var card = el('div', { className: 'trash-card' });
  card.appendChild(el('div', { className: 'trash-card-head' }, [
    el('div', { className: 'trash-card-title', text: '合并任务 ' + (job.job_id || '') }),
    el('div', {
      className: 'trash-card-meta',
      text: [fmtDateTime(job.started_at || ''),
             '合并 ' + fmtInt(job.done || job.entries || job.total || 0) + ' 个路径',
             fmtBytes(job.bytes_done || job.bytes_total || 0),
             job.status || ''].join(' · ')
    })
  ]));
  var actions = el('div', { className: 'trash-card-actions' });
  var detailBtn = el('button', {
    className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '查看明细'
  });
  detailBtn.addEventListener('click', function () { openMergeDetail(job.job_id); });
  actions.appendChild(detailBtn);
  if (Number(job.done || job.entries || 0) > 0) {
    var restoreBtn = el('button', {
      className: 'btn btn-primary btn-sm', attrs: { type: 'button' },
      text: '恢复为独立副本'
    });
    restoreBtn.addEventListener('click', function () { restoreMergeJob(job); });
    actions.appendChild(restoreBtn);
  }
  card.appendChild(actions);
  return card;
}

async function openMergeDetail(jobId) {
  var closeBtn = el('button', {
    className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
    on: { click: function () { closeModal(true); } }
  });
  var modal = openModal({
    title: '合并明细',
    subtitle: jobId,
    size: 'lg',
    body: el('div', { className: 'modal-loading' }, [
      el('span', { className: 'spinner spinner-lg' }), '正在加载明细…'
    ]),
    footer: [closeBtn],
    initialFocus: closeBtn
  });
  try {
    var detail = await api('/api/merges/' + encodePath(jobId), { timeout: 20000 });
    var list = el('div', { className: 'trash-detail-list' });
    (detail.entries || []).forEach(function (entry) {
      var labels = {
        linked: '硬链接中', independent: '已独立', missing: '文件不存在'
      };
      var classes = { linked: 'badge-blue', independent: 'badge-green', missing: 'badge-red' };
      list.appendChild(el('div', { className: 'trash-detail-row' }, [
        el('div', { className: 'trash-detail-path', attrs: { title: entry.path }, text: entry.path }),
        el('span', {
          className: 'badge ' + (classes[entry.state] || 'badge-gray'),
          text: labels[entry.state] || entry.state
        }),
        el('span', { className: 'muted', text: '→ ' + (entry.master || '') })
      ]));
    });
    if (!(detail.entries || []).length) {
      list.appendChild(el('p', { className: 'hint', text: '没有可显示的合并路径。' }));
    }
    modal.setBody(el('div', { className: 'trash-detail-body' }, [list]));
  } catch (err) {
    modal.setBody(el('div', { className: 'state-box state-error', text: errorMessage(err, '加载合并明细失败。') }));
  }
}

async function restoreMergeJob(job) {
  var ok = await confirmDialog({
    title: '恢复为独立副本？',
    message: '将为每条路径复制一份独立内容（Btrfs 使用 FICLONE 秒级复制，其它文件系统回退普通复制，'
      + '需要与文件同等大小的可用空间）。恢复后这些路径不再共享内容。',
    confirmText: '恢复'
  });
  if (!ok) { return; }
  try {
    var res = await api('/api/merges/' + encodePath(job.job_id) + '/restore', {
      method: 'POST', body: { confirm: true }, timeout: 180000
    });
    var skipped = (res.results || []).filter(function (item) {
      return item.status === 'skipped';
    });
    var failed = (res.results || []).filter(function (item) {
      return item.status === 'failed';
    });
    var message = '已恢复 ' + fmtInt(res.restored || 0) + ' 个独立副本'
      + (res.reflink ? '（其中 FICLONE ' + fmtInt(res.reflink) + ' 个）' : '');
    if (skipped.length) { message += '；跳过 ' + fmtInt(skipped.length) + ' 个'; }
    if (failed.length) { message += '；失败 ' + fmtInt(failed.length) + ' 个'; }
    toast(message, failed.length ? 'error' : 'success', 6000);
    await loadMergeHistory();
  } catch (err) {
    toast(errorMessage(err, '恢复失败，请稍后重试。'), 'error', 6000);
  }
}

export function initTrashPage() {
  if (els.trashRefreshBtn) {
    els.trashRefreshBtn.addEventListener('click', function () { loadTrashPanel(); });
  }
}
