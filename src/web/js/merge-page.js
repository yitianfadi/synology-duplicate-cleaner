/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 音视频合成页（ES module，v0059）
   --------------------------------------------------------------------------
   两个有序列表（视频片段 / 音频片段）→ 按选择顺序拼接 + 混流成一个文件。
   内置功能，无启用开关；参数默认值来自设置页「合成 · 视频 + 音频」。
   规范：ES2019、无第三方依赖、不使用 innerHTML、只做安全文本渲染。
   ========================================================================== */

import { api, errorMessage } from './api.js';
import { els, state } from './state.js';
import { clear, el, setHidden, setText } from './dom.js';
import { fmtBytes, fmtDateTime } from './format.js';
import { confirmDialog } from './modal.js';
import { toast } from './toast.js';

var POLL_MS = 2500;
var STATUS_LABEL = {
  queued: '排队中', running: '合成中', done: '已完成',
  failed: '失败', cancelled: '已取消'
};
var STATUS_BADGE = {
  queued: 'badge-gray', running: 'badge-blue', done: 'badge-green',
  failed: 'badge-red', cancelled: 'badge-gray'
};

var page = {
  caps: null,
  capsLoading: false,
  timer: null,
  loading: false,
  lastError: ''
};

function q(id) { return els[id] || document.getElementById(id); }

function mergeState() {
  return { caps: page.caps };
}

function setError(id, message) {
  var node = q(id);
  if (!node) { return; }
  setText(node, message || '');
  setHidden(node, !message);
}

function splitLines(value) {
  return String(value || '').split('\n').map(function (line) {
    return line.trim();
  }).filter(function (line) { return line.length > 0; });
}

function refreshListHints() {
  var videos = splitLines(q('mergeVideoPaths') ? q('mergeVideoPaths').value : '');
  var audios = splitLines(q('mergeAudioPaths') ? q('mergeAudioPaths').value : '');
  setText(q('mergeVideoHint'), videos.length + ' 个视频片段');
  setText(q('mergeAudioHint'), audios.length + ' 个音频片段');
  if (q('mergeSubmitBtn')) {
    q('mergeSubmitBtn').disabled = !(page.caps && page.caps.ffmpeg
      && page.caps.ffmpeg.available) || videos.length === 0;
  }
}

function appendPath(textareaId, path) {
  var area = q(textareaId);
  if (!area || !path) { return; }
  var lines = splitLines(area.value);
  if (lines.indexOf(path) !== -1) { return; }
  lines.push(path);
  area.value = lines.join('\n');
  refreshListHints();
}

function renderCandidates(data) {
  var card = q('mergeCandidatesCard');
  var list = q('mergeCandidates');
  var empty = q('mergeCandidatesEmpty');
  if (!card || !list) { return; }
  setHidden(card, false);
  clear(list);
  var items = (data && data.items) || [];
  setHidden(empty, items.length > 0);
  items.forEach(function (item) {
    var row = el('div', { className: 'merge-candidate' });
    row.appendChild(el('span', {
      className: 'badge ' + (item.kind === 'video' ? 'badge-blue' : 'badge-purple'),
      text: item.kind === 'video' ? '视频' : '音频'
    }));
    row.appendChild(el('span', { className: 'merge-candidate-path', text: item.path }));
    row.appendChild(el('span', { className: 'muted', text: item.size_label || fmtBytes(item.size || 0) }));
    var addVideo = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '加入视频'
    });
    addVideo.addEventListener('click', function () {
      appendPath('mergeVideoPaths', item.path);
    });
    var addAudio = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '加入音频'
    });
    addAudio.addEventListener('click', function () {
      appendPath('mergeAudioPaths', item.path);
    });
    row.appendChild(addVideo);
    row.appendChild(addAudio);
    list.appendChild(row);
  });
}

async function loadCapabilities() {
  if (!q('mergePageStatusBadge')) { return; }
  page.capsLoading = true;
  try {
    var caps = await api('/api/avmerge/capabilities', { timeout: 15000 });
    page.caps = caps || null;
    var badge = q('mergePageStatusBadge');
    if (badge) {
      if (!caps || !caps.ffmpeg || !caps.ffmpeg.available) {
        setText(badge, 'FFmpeg 不可用');
        badge.className = 'badge badge-red';
      } else {
        setText(badge, 'FFmpeg ' + caps.ffmpeg.version +
          ' · 同时 ' + caps.concurrency + ' 个');
        badge.className = 'badge badge-green';
      }
    }
    applyDefaults(caps && caps.defaults);
  } catch (err) {
    setError('mergeSubmitError', errorMessage(err, '加载合成能力失败，请稍后重试。'));
    var b = q('mergePageStatusBadge');
    if (b) { setText(b, '检测失败'); b.className = 'badge badge-red'; }
  } finally {
    page.capsLoading = false;
    refreshListHints();
  }
}

function applyDefaults(defaults) {
  if (!defaults) { return; }
  var settings = state.settings || {};
  var mode = q('mergeOutputMode');
  if (mode && mode.value === 'alongside' && defaults.output_mode) {
    mode.value = defaults.output_mode;
  }
  var dir = q('mergeOutputDir');
  if (dir && !dir.value) {
    dir.value = defaults.output_dir || settings.avmerge_output_dir || '';
  }
  var subdir = q('mergeOutputSubdir');
  if (subdir && !subdir.value) {
    subdir.value = settings.avmerge_output_subdir || 'merged';
  }
  var container = q('mergeContainer');
  if (container && defaults.container) { container.value = defaults.container; }
  var mergeMode = q('mergeMode');
  if (mergeMode && defaults.mode) { mergeMode.value = defaults.mode; }
  var quality = q('mergeQuality');
  if (quality && defaults.quality) { quality.value = defaults.quality; }
  var res = q('mergeMaxResolution');
  if (res) {
    var cap = parseInt(settings.avmerge_max_resolution, 10);
    res.value = [0, 2160, 1080, 720, 480].indexOf(cap) === -1 ? '0' : String(cap);
  }
  var template = q('mergeNameTemplate');
  if (template && !template.value) {
    template.value = settings.avmerge_name_template || '{name}_merged';
  }
}

async function enumerateMaterials() {
  var dirInput = q('mergeDirPath');
  var summary = q('mergeEnumerateSummary');
  var btn = q('mergeEnumerateBtn');
  setError('mergeEnumerateError', '');
  var path = dirInput ? dirInput.value.trim() : '';
  if (!path) {
    setError('mergeEnumerateError', '请先选择或填写目录。');
    return;
  }
  if (btn) { btn.disabled = true; }
  if (summary) { setText(summary, '枚举中…'); }
  try {
    var data = await api('/api/avmerge/enumerate', {
      method: 'POST', timeout: 20000,
      body: { path: path, recursive: !!(q('mergeRecursive') && q('mergeRecursive').checked) }
    });
    renderCandidates(data || {});
    if (summary) {
      setText(summary, '共 ' + ((data && data.count) || 0) + ' 个候选');
    }
  } catch (err) {
    if (summary) { setText(summary, '枚举失败'); }
    setError('mergeEnumerateError', errorMessage(err, '枚举素材失败，请检查目录。'));
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

function pickDir(targetId) {
  if (typeof state.openMergeDirPicker !== 'function') { return; }
  var current = q(targetId) ? q(targetId).value.trim() : '';
  state.openMergeDirPicker({
    initial: current,
    onConfirm: function (path) {
      if (q(targetId) && path) { q(targetId).value = path; }
    }
  });
}

async function submitMerge() {
  var submit = q('mergeSubmitBtn');
  setError('mergeSubmitError', '');
  var videos = splitLines(q('mergeVideoPaths') ? q('mergeVideoPaths').value : '');
  var audios = splitLines(q('mergeAudioPaths') ? q('mergeAudioPaths').value : '');
  if (!videos.length) {
    setError('mergeSubmitError', '请至少选择一个视频片段。');
    return;
  }
  if (!page.caps || !page.caps.ffmpeg || !page.caps.ffmpeg.available) {
    setError('mergeSubmitError', 'FFmpeg 不可用，无法合成。');
    return;
  }
  var deleteSource = !!(q('mergeDeleteSource') && q('mergeDeleteSource').checked);
  var confirm = '';
  if (deleteSource) {
    var ok = await confirmDialog({
      title: '合成成功后删除源文件',
      message: '已勾选「合成成功后删除源文件」：每个任务会先校验产物（存在视频轨、时长一致），'
        + '再删除所有源片段。此操作不可撤销，请确认已备份或确实不需要原文件。',
      confirmText: '删除源文件',
      danger: true
    });
    if (!ok) { return; }
    confirm = 'DELETE SOURCE';
  }
  var options = {
    container: q('mergeContainer') ? q('mergeContainer').value : 'mp4',
    mode: q('mergeMode') ? q('mergeMode').value : 'auto',
    quality: q('mergeQuality') ? q('mergeQuality').value : 'standard',
    max_resolution: q('mergeMaxResolution')
      ? parseInt(q('mergeMaxResolution').value, 10) || 0 : 0,
    output_mode: q('mergeOutputMode') ? q('mergeOutputMode').value : 'alongside',
    output_dir: q('mergeOutputDir') ? q('mergeOutputDir').value.trim() : '',
    output_subdir: q('mergeOutputSubdir') ? q('mergeOutputSubdir').value.trim() : '',
    name_template: q('mergeNameTemplate') ? q('mergeNameTemplate').value.trim() : '',
    delete_source: deleteSource
  };
  if (submit) { submit.disabled = true; }
  try {
    await api('/api/avmerge', {
      method: 'POST', timeout: 30000,
      body: { video_paths: videos, audio_paths: audios, options: options, confirm: confirm }
    });
    toast('合成任务已提交，可在下方队列查看进度。', 'success', 5000);
    if (q('mergeDeleteSource')) { q('mergeDeleteSource').checked = false; }
    await loadQueue();
  } catch (err) {
    setError('mergeSubmitError', errorMessage(err, '提交合成任务失败。'));
  } finally {
    if (submit) { submit.disabled = false; }
    refreshListHints();
  }
}

function jobAction(job, action, label, danger) {
  var btn = el('button', {
    className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: label
  });
  btn.addEventListener('click', async function () {
    if (action === 'delete') {
      var ok = await confirmDialog({
        title: '删除任务',
        message: '删除任务记录并同时删除产物文件？此操作不可恢复。',
        confirmText: '删除', danger: true
      });
      if (!ok) { return; }
    }
    btn.disabled = true;
    try {
      if (action === 'delete') {
        await api('/api/avmerge/' + encodeURIComponent(job.job_id) + '?delete_output=1',
          { method: 'DELETE', timeout: 15000 });
      } else {
        await api('/api/avmerge/' + encodeURIComponent(job.job_id) + '/' + action,
          { method: 'POST', body: {}, timeout: 15000 });
      }
      await loadQueue();
    } catch (err) {
      toast(errorMessage(err, '操作失败'), 'error', 6000);
    } finally {
      btn.disabled = false;
    }
  });
  return btn;
}

function renderJobRow(job) {
  var status = job.status || 'queued';
  var row = el('div', { className: 'download-job' });
  var head = el('div', { className: 'download-job-head' });
  head.appendChild(el('span', {
    className: 'badge ' + (STATUS_BADGE[status] || 'badge-gray'),
    text: STATUS_LABEL[status] || status
  }));
  head.appendChild(el('span', {
    className: 'mono', text: job.job_id || ''
  }));
  head.appendChild(el('span', {
    className: 'muted',
    text: '视频 ' + (job.video_count || 0) + ' · 音频 ' + (job.audio_count || 0)
      + (job.strategy ? ' · ' + (job.strategy === 'copy' ? '直接拼接' : '统一转码') : '')
  }));
  row.appendChild(head);

  var bar = el('div', { className: 'progress-bar' });
  var fill = el('div', { className: 'progress-fill' });
  fill.style.width = Math.max(0, Math.min(100, Number(job.progress) || 0)) + '%';
  bar.appendChild(fill);
  row.appendChild(bar);

  var meta = el('div', { className: 'muted' });
  var parts = [];
  if (job.stage_label) { parts.push(job.stage_label); }
  if (job.message) { parts.push(job.message); }
  if (job.duration_sec) { parts.push('时长 ' + Math.round(job.duration_sec) + 's'); }
  if (job.output_name) { parts.push(job.output_name); }
  if (job.created_at) { parts.push(fmtDateTime(job.created_at)); }
  if (job.error_message) { parts.push('错误：' + job.error_message); }
  setText(meta, parts.join(' · '));
  row.appendChild(meta);

  var actions = el('div', { className: 'download-job-actions' });
  if (status === 'queued' || status === 'running') {
    actions.appendChild(jobAction(job, 'cancel', '取消'));
  }
  if (status === 'failed' || status === 'cancelled' || status === 'done') {
    actions.appendChild(jobAction(job, 'retry', '重试'));
    actions.appendChild(jobAction(job, 'delete', '删除', true));
  }
  row.appendChild(actions);
  return row;
}

function renderJobs(snapshot) {
  var list = q('mergeJobsList');
  if (!list) { return; }
  clear(list);
  var running = (snapshot && snapshot.running) || [];
  var queued = (snapshot && snapshot.queued) || [];
  var recent = (snapshot && snapshot.recent) || [];
  var counts = (snapshot && snapshot.counts) || {};
  var rows = running.concat(queued, recent);
  setHidden(q('mergeJobsEmpty'), rows.length > 0);
  setText(q('mergeJobsStatus'),
    (counts.running ? '运行 ' + counts.running + ' · ' : '')
    + (counts.queued ? '排队 ' + counts.queued + ' · ' : '')
    + (counts.done ? '完成 ' + counts.done : ''));
  rows.forEach(function (job) { list.appendChild(renderJobRow(job)); });
}

async function loadQueue() {
  if (!q('mergeJobsList') || page.loading) { return; }
  page.loading = true;
  setError('mergeListError', '');
  try {
    var snapshot = await api('/api/avmerge/queue', { timeout: 15000 });
    renderJobs(snapshot || {});
  } catch (err) {
    setError('mergeListError', errorMessage(err, '加载合成队列失败。'));
  } finally {
    page.loading = false;
  }
}

function startPolling() {
  stopPolling();
  if (state.view !== 'merge') { return; }
  page.timer = setInterval(function () {
    if (state.view !== 'merge') { stopPolling(); return; }
    loadQueue();
  }, POLL_MS);
}

function stopPolling() {
  if (page.timer) {
    clearInterval(page.timer);
    page.timer = null;
  }
}

export function showMergePage() {
  if (!page.caps) { loadCapabilities(); }
  refreshListHints();
  loadQueue();
  startPolling();
}

export function hideMergePage() {
  stopPolling();
}

export function initMergePage() {
  var enumBtn = q('mergeEnumerateBtn');
  if (enumBtn) { enumBtn.addEventListener('click', enumerateMaterials); }
  [['mergeDirPickBtn', 'mergeDirPath'],
   ['mergeOutputDirPickBtn', 'mergeOutputDir']].forEach(function (pair) {
    var btn = q(pair[0]);
    if (btn) { btn.addEventListener('click', function () { pickDir(pair[1]); }); }
  });
  var submit = q('mergeSubmitBtn');
  if (submit) { submit.addEventListener('click', submitMerge); }
  ['mergeVideoPaths', 'mergeAudioPaths'].forEach(function (id) {
    var area = q(id);
    if (area) { area.addEventListener('input', refreshListHints); }
  });
  var refresh = q('mergeRefreshBtn');
  if (refresh) { refresh.addEventListener('click', loadQueue); }
  if (state.settings) { applyDefaults(null); }
  refreshListHints();
}
