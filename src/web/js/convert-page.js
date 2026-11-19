/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 格式转换页（v0056，ES module）
   目录枚举 / 手动加路径 → 选预设 → 提交 → 队列（进度/取消/重试/删除）。
   只做 DOM 构造（不使用 innerHTML）；进度条宽度用 node.style 设置。
   ========================================================================== */

import { api, errorMessage } from './api.js';
import { els, state } from './state.js';
import { clear, el, setHidden, setText } from './dom.js';
import { fmtBytes, fmtDateTime, fmtElapsed } from './format.js';
import { confirmDialog } from './modal.js';
import { toast } from './toast.js';

var POLL_MS = 2000;

var STATUS_TEXT = {
  queued: '排队中',
  running: '转换中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消'
};

var STATUS_BADGE = {
  queued: 'badge-gray',
  running: 'badge-blue',
  done: 'badge-green',
  failed: 'badge-red',
  cancelled: 'badge-gray'
};

var SPEED_LABEL = {
  ultrafast: '最快（画质一般）',
  superfast: '很快',
  veryfast: '快（默认）',
  faster: '较快',
  medium: '中等（更省体积）'
};

var RESOLUTION_LABEL = {
  0: '保持原样',
  2160: '2160p（4K）',
  1080: '1080p',
  720: '720p',
  480: '480p'
};

var OVERWRITE_LABEL = {
  trash: '覆盖旧产物（旧文件移入回收站）',
  rename: '自动改名（加序号）',
  skip: '跳过已存在',
  overwrite: '直接覆盖（旧文件不再保留）'
};

var OUTPUT_MODE_LABEL = {
  alongside: '源文件同目录（默认）',
  fixed: '指定目录',
  subdir: '指定目录 + 子目录'
};

function convertState() {
  return state.convert;
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

function capsPreset(presetId) {
  var cs = convertState();
  var list = (cs.caps && cs.caps.presets) || [];
  for (var i = 0; i < list.length; i += 1) {
    if (list[i].id === presetId) { return list[i]; }
  }
  return null;
}

function selectedPreset() {
  var group = els.convertPresetGroup ? els.convertPresetGroup.value : 'video';
  var id = els.convertPreset ? els.convertPreset.value : '';
  if (!id) {
    var list = (convertState().caps && convertState().caps.groups) || {};
    var ids = (group === 'audio' ? list.audio : list.video) || [];
    id = ids[0] || 'mp4_h264';
  }
  return id;
}

// ---------------------------------------------------------------------
// 能力探测：预设 / 默认值 / 状态徽标
// ---------------------------------------------------------------------
async function loadCapabilities() {
  var cs = convertState();
  if (cs.capsLoading) { return; }
  cs.capsLoading = true;
  try {
    var caps = await api('/api/convert/capabilities', { timeout: 20000 });
    cs.caps = caps;
    fillPresetOptions();
    fillDefaults();
    renderCapabilityBadge();
  } catch (err) {
    cs.lastError = errorMessage(err, '读取转换能力失败。');
    renderError(cs.lastError);
  } finally {
    cs.capsLoading = false;
  }
}

function renderCapabilityBadge() {
  var cs = convertState();
  var badge = els.convertPageStatusBadge;
  if (!badge) { return; }
  var caps = cs.caps;
  if (!caps) {
    setText(badge, '检测中…');
    badge.className = 'badge badge-gray';
    return;
  }
  if (!caps.ffmpeg || !caps.ffmpeg.available) {
    setText(badge, 'FFmpeg 不可用');
    badge.className = 'badge badge-red';
  } else {
    setText(badge, 'FFmpeg ' + caps.ffmpeg.version +
      ' · 同时 ' + caps.concurrency + ' 个');
    badge.className = 'badge badge-green';
  }
  // v0058：转换是独立功能，恒可用；只有 FFmpeg 不可用时才禁用提交。
  var disabled = !(caps.ffmpeg && caps.ffmpeg.available);
  if (els.convertSubmitBtn) { els.convertSubmitBtn.disabled = disabled; }
  setText(els.convertPresetHint, disabled
    ? 'FFmpeg 不可用，请在设置里指定路径'
    : (caps.offpeak && caps.offpeak.enabled && !caps.offpeak.open
       ? '当前不在空闲时段（' + caps.offpeak.start + '–' + caps.offpeak.end +
         '），任务会排队等待'
       : ''));
}

function fillPresetOptions() {
  var cs = convertState();
  var caps = cs.caps;
  if (!caps || !els.convertPreset || !els.convertPresetGroup) { return; }
  var group = els.convertPresetGroup.value || 'video';
  var ids = (caps.groups && caps.groups[group]) || [];
  clear(els.convertPreset);
  ids.forEach(function (id) {
    var meta = capsPreset(id);
    var label = meta ? meta.label + (meta.copy ? ' · 秒级' : '') : id;
    var option = el('option', { attrs: { value: id }, text: label });
    if (meta && meta.available === false) {
      option.disabled = true;
      option.textContent = label + '（不可用）';
    }
    els.convertPreset.appendChild(option);
  });
  var preferred = (caps.defaults && caps.defaults.preset) || '';
  if (preferred && ids.indexOf(preferred) !== -1) {
    els.convertPreset.value = preferred;
  }
  updatePresetHint();
}

function fillDefaults() {
  var caps = convertState().caps;
  if (!caps) { return; }
  var defaults = caps.defaults || {};
  if (els.convertQuality && defaults.quality) {
    els.convertQuality.value = defaults.quality;
  }
  if (els.convertSpeed) {
    fillSelect(els.convertSpeed, (caps.speeds || []).map(function (value) {
      return { value: value, label: SPEED_LABEL[value] || value };
    }), defaults.speed);
  }
  if (els.convertMaxResolution) {
    fillSelect(els.convertMaxResolution,
      (caps.max_resolutions || [0]).map(function (value) {
        return { value: String(value),
                 label: RESOLUTION_LABEL[value] || (value + 'p') };
      }), String(defaults.max_resolution || 0));
  }
  if (els.convertOutputMode) {
    fillSelect(els.convertOutputMode,
      (caps.output_modes || ['alongside']).map(function (value) {
        return { value: value, label: OUTPUT_MODE_LABEL[value] || value };
      }), defaults.output_mode);
  }
  if (els.convertOverwrite) {
    fillSelect(els.convertOverwrite,
      (caps.overwrite_modes || ['rename']).map(function (value) {
        return { value: value, label: OVERWRITE_LABEL[value] || value };
      }), defaults.overwrite);
  }
  if (els.convertOutputDir && !els.convertOutputDir.value) {
    els.convertOutputDir.value = defaults.output_dir || '';
  }
  if (els.convertOutputSubdir && !els.convertOutputSubdir.value) {
    els.convertOutputSubdir.value = defaults.output_subdir || 'converted';
  }
  if (els.convertNameTemplate && !els.convertNameTemplate.value) {
    els.convertNameTemplate.value = defaults.name_template || '{name}';
  }
  // v0061：默认「转换成功后删除源文件」；旧版「保留源文件」复选框已移除。
  if (els.convertDeleteSource) {
    els.convertDeleteSource.checked = defaults.delete_source !== false;
  }
  if (els.convertCopySubtitles) {
    els.convertCopySubtitles.checked = defaults.copy_subtitles !== false;
  }
  initExtensions();
  updateOutputModeUi();
}

function fillSelect(select, items, preferred) {
  if (!select) { return; }
  clear(select);
  var found = false;
  items.forEach(function (item) {
    var option = el('option', { attrs: { value: item.value }, text: item.label });
    if (String(item.value) === String(preferred)) { found = true; }
    select.appendChild(option);
  });
  if (found) { select.value = String(preferred); }
}

function updateOutputModeUi() {
  var mode = els.convertOutputMode ? els.convertOutputMode.value : 'alongside';
  var needsDir = mode !== 'alongside';
  setHidden(els.convertOutputDirField, !needsDir);
  setHidden(els.convertOutputSubdirField, mode !== 'subdir');
}

// ---------------------------------------------------------------------
// v0061：源文件后缀多选（只列出勾选的后缀，勾选结果自动保存）
// ---------------------------------------------------------------------
function extMeta() {
  var caps = convertState().caps || {};
  return caps.source_extensions || {};
}

function allExtensions() {
  var meta = extMeta();
  return (meta.all || []).slice();
}

function initExtensions() {
  var cs = convertState();
  var meta = extMeta();
  var all = (meta.all || []).slice();
  var saved = ((convertState().caps || {}).defaults || {}).source_extensions || [];
  if (!all.length) { cs.exts = new Set(); return; }
  var chosen = saved.filter(function (ext) { return all.indexOf(ext) !== -1; });
  cs.exts = new Set(chosen.length ? chosen : all);
  cs.extsTouched = false;
  renderExtensionPicker();
}

function renderExtensionPicker() {
  var picker = els.convertExtPicker;
  if (!picker) { return; }
  var cs = convertState();
  var meta = extMeta();
  clear(picker);
  var groups = [
    { label: '视频', exts: meta.video || [] },
    { label: '音频', exts: meta.audio || [] }
  ];
  groups.forEach(function (group) {
    (group.exts || []).forEach(function (ext) {
      var checkbox = el('input', {
        attrs: { type: 'checkbox', checked: cs.exts.has(ext) },
        dataset: { ext: ext },
        on: {
          change: function (event) {
            if (event.target.checked) { cs.exts.add(ext); }
            else { cs.exts.delete(ext); }
            cs.extsTouched = true;
            updateExtHint();
            schedulePersistExtensions();
          }
        }
      });
      picker.appendChild(el('label', { className: 'check' }, [checkbox, ext]));
    });
  });
  updateExtHint();
}

function updateExtHint() {
  if (!els.convertExtHint) { return; }
  var chosen = selectedExtensions();
  var total = allExtensions().length;
  if (!total) {
    setText(els.convertExtHint, '尚未读取到支持的后缀。');
    return;
  }
  if (!chosen.length) {
    setText(els.convertExtHint, '未勾选任何后缀：请至少勾选一个再列出文件。');
    return;
  }
  if (chosen.length === total) {
    setText(els.convertExtHint,
      '已勾选全部 ' + total + ' 个后缀（视频 + 音频）。可同时勾选多个不同后缀。');
    return;
  }
  setText(els.convertExtHint,
    '只列出 ' + chosen.length + ' 个后缀：' + chosen.join(' '));
}

function selectedExtensions() {
  var cs = convertState();
  return allExtensions().filter(function (ext) { return cs.exts.has(ext); });
}

function schedulePersistExtensions() {
  var cs = convertState();
  if (cs.extSaving) { clearTimeout(cs.extSaving); }
  cs.extSaving = setTimeout(function () {
    cs.extSaving = null;
    persistExtensions();
  }, 500);
}

async function persistExtensions() {
  var chosen = selectedExtensions();
  try {
    await api('/api/settings', {
      method: 'PUT',
      body: { settings: { convert_source_extensions: chosen } },
      timeout: 15000
    });
  } catch (err) {
    // 保存失败不影响本次筛选，只提示一次
    toast(errorMessage(err, '保存后缀筛选失败。'), 'error');
  }
}

function setExtensionSelection(kind) {
  var cs = convertState();
  var meta = extMeta();
  if (kind === 'all') { cs.exts = new Set(allExtensions()); }
  else if (kind === 'none') { cs.exts = new Set(); }
  else if (kind === 'video') { cs.exts = new Set(meta.video || []); }
  else if (kind === 'audio') { cs.exts = new Set(meta.audio || []); }
  cs.extsTouched = true;
  renderExtensionPicker();
  schedulePersistExtensions();
}

function updatePresetHint() {
  var meta = capsPreset(selectedPreset());
  if (!els.convertPresetDesc) { return; }
  if (!meta) {
    setText(els.convertPresetDesc, '');
    return;
  }
  var bits = [meta.hint || ''];
  if (meta.copy) {
    bits.push('仅换封装：不重新编码，秒级完成、画质不变');
  } else {
    bits.push('需要重新编码：CPU 占用高，耗时约为时长的数倍');
  }
  if (meta.ext) { bits.push('输出 ' + meta.ext); }
  setText(els.convertPresetDesc, bits.filter(Boolean).join(' · '));
}

// ---------------------------------------------------------------------
// 源文件选择
// ---------------------------------------------------------------------
function addSelected(items) {
  var cs = convertState();
  var added = 0;
  (items || []).forEach(function (item) {
    if (!item || !item.path) { return; }
    if (cs.selected.has(item.path)) { return; }
    cs.selected.set(item.path, {
      path: item.path,
      name: item.name || item.path.split(/[\\/]/).pop(),
      size: Number(item.size) || 0
    });
    added += 1;
  });
  if (added) { renderSelection(); }
  return added;
}

function removeSelected(path) {
  var cs = convertState();
  cs.selected.delete(path);
  renderSelection();
}

function renderSelection() {
  var cs = convertState();
  var list = els.convertCandidatesList;
  if (!list) { return; }
  clear(list);
  var items = Array.from(cs.selected.values());
  setHidden(els.convertCandidatesPanel, items.length === 0);
  if (!items.length) {
    setText(els.convertCandidatesSummary, '');
    if (els.convertSelectNoneBtn) { els.convertSelectNoneBtn.disabled = true; }
    updateSubmitHint();
    return;
  }
  var total = 0;
  items.forEach(function (item) {
    total += item.size || 0;
    var checkbox = el('input', {
      attrs: { type: 'checkbox', checked: true },
      on: {
        change: function (event) {
          if (!event.target.checked) { removeSelected(item.path); }
        }
      }
    });
    var row = el('div', { className: 'download-candidate-row' }, [
      el('label', { className: 'download-candidate-check' }, [checkbox]),
      el('div', { className: 'download-candidate-text' }, [
        el('div', { className: 'download-candidate-name', text: item.name }),
        el('div', { className: 'muted download-candidate-meta',
                    text: (item.size ? fmtBytes(item.size) + ' · ' : '') +
                          item.path })
      ])
    ]);
    list.appendChild(row);
  });
  setText(els.convertCandidatesSummary,
    items.length + ' 个文件' + (total ? ' · 共 ' + fmtBytes(total) : ''));
  if (els.convertSelectAllBtn) {
    els.convertSelectAllBtn.disabled = cs.candidates.length === 0;
  }
  if (els.convertSelectNoneBtn) { els.convertSelectNoneBtn.disabled = false; }
  updateSubmitHint();
}

function updateSubmitHint() {
  var cs = convertState();
  var count = cs.selected.size;
  var preset = capsPreset(selectedPreset());
  if (!els.convertSubmitHint) { return; }
  if (!count) {
    setText(els.convertSubmitHint, '还没有选择文件');
    return;
  }
  setText(els.convertSubmitHint,
    '已选 ' + count + ' 个文件' +
    (preset ? ' → ' + preset.label : '') +
    (preset && preset.copy ? '（仅换封装，秒级）' : '（需要重编码）'));
}

async function enumerateFiles() {
  var cs = convertState();
  var path = els.convertDirPath ? String(els.convertDirPath.value || '').trim() : '';
  setConvertError('');
  if (!path) {
    setConvertError('请先填写或选择目录。');
    return;
  }
  var exts = selectedExtensions();
  if (!exts.length) {
    setConvertError('请至少勾选一个源文件后缀。');
    return;
  }
  cs.enumerating = true;
  setHidden(els.convertEnumerateLoading, false);
  try {
    var res = await api('/api/convert/enumerate', {
      method: 'POST',
      body: {
        path: path,
        recursive: els.convertRecursive ? !!els.convertRecursive.checked : true,
        extensions: exts
      },
      timeout: 60000
    });
    var items = (res && res.items) || [];
    clear(cs.candidates);
    items.forEach(function (item) { cs.candidates.push(item); });
    addSelected(items);
    var skipped = (res && res.skipped) || {};
    var skippedBits = [];
    Object.keys(skipped).forEach(function (key) {
      if (skipped[key]) { skippedBits.push(SKIP_LABEL[key] + ' ' + skipped[key]); }
    });
    setText(els.convertCandidatesSummary,
      (res && res.count ? res.count : 0) + ' 个可转换文件' +
      (res && res.truncated ? '（超过上限，只列出前 ' + res.limit + ' 个）' : '') +
      (skippedBits.length ? ' · 跳过：' + skippedBits.join(' / ') : ''));
  } catch (err) {
    setConvertError(errorMessage(err, '列出文件失败。'));
  } finally {
    cs.enumerating = false;
    setHidden(els.convertEnumerateLoading, true);
  }
}

var SKIP_LABEL = {
  system: '系统目录',
  hidden: '隐藏',
  symlink: '符号链接',
  permission: '无权限',
  other: '其他',
  extension: '非音视频'
};

function addManualPaths() {
  var text = els.convertPaths ? String(els.convertPaths.value || '') : '';
  var lines = text.split(/\r?\n/).map(function (line) { return line.trim(); })
    .filter(function (line) { return !!line; });
  if (!lines.length) {
    setConvertError('请先粘贴至少一个路径（每行一个）。');
    return;
  }
  var added = addSelected(lines.map(function (path) {
    return { path: path, name: path.split(/[\\/]/).pop(), size: 0 };
  }));
  setConvertError('');
  toast(added ? ('已加入 ' + added + ' 个文件') : '这些路径已经在列表里', 'info');
  if (els.convertPaths) { els.convertPaths.value = ''; }
}

// ---------------------------------------------------------------------
// 提交
// ---------------------------------------------------------------------
function readOptions() {
  var options = {
    quality: els.convertQuality ? els.convertQuality.value : undefined,
    speed: els.convertSpeed ? els.convertSpeed.value : undefined,
    channels: els.convertChannels ? els.convertChannels.value : '',
    output_mode: els.convertOutputMode ? els.convertOutputMode.value : undefined,
    output_dir: els.convertOutputDir
      ? String(els.convertOutputDir.value || '').trim() : '',
    output_subdir: els.convertOutputSubdir
      ? String(els.convertOutputSubdir.value || '').trim() : '',
    overwrite: els.convertOverwrite ? els.convertOverwrite.value : undefined,
    name_template: els.convertNameTemplate
      ? String(els.convertNameTemplate.value || '').trim() : '',
    // v0061：删源改为默认开启；保留源文件 = 取消勾选
    delete_source: els.convertDeleteSource ? !!els.convertDeleteSource.checked : true,
    copy_subtitles: els.convertCopySubtitles
      ? !!els.convertCopySubtitles.checked : true
  };
  if (els.convertMaxResolution) {
    options.max_resolution = parseInt(els.convertMaxResolution.value, 10) || 0;
  }
  return options;
}

async function submitConvert() {
  var cs = convertState();
  if (cs.submitting) { return; }
  var paths = Array.from(cs.selected.keys());
  setSubmitError('');
  if (!paths.length) {
    setSubmitError('请先选择要转换的文件。');
    return;
  }
  // v0061：默认删除源文件，不再弹二次确认；接口仍要求显式删除时带 token。
  var willDelete = els.convertDeleteSource
    ? !!els.convertDeleteSource.checked : true;
  var confirmText = willDelete ? 'DELETE SOURCE' : '';
  cs.submitting = true;
  setText(els.convertSubmitHint, '正在提交…');
  try {
    var res = await api('/api/convert', {
      method: 'POST',
      body: {
        paths: paths,
        preset: selectedPreset(),
        options: readOptions(),
        confirm: confirmText
      },
      timeout: 60000
    });
    var count = (res && res.count) || 0;
    toast('已提交 ' + count + ' 个转换任务' +
      (willDelete ? '（转换成功后会删除源文件）' : ''), 'success');
    cs.selected.clear();
    cs.candidates.length = 0;
    renderSelection();
    refreshConvertQueue();
  } catch (err) {
    setSubmitError(errorMessage(err, '提交失败。'));
    setText(els.convertSubmitHint, '');
  } finally {
    cs.submitting = false;
  }
}

function setConvertError(message) {
  if (!els.convertEnumerateError) { return; }
  setText(els.convertEnumerateError, message || '');
  setHidden(els.convertEnumerateError, !message);
}

function setSubmitError(message) {
  if (!els.convertSubmitError) { return; }
  setText(els.convertSubmitError, message || '');
  setHidden(els.convertSubmitError, !message);
}

// ---------------------------------------------------------------------
// 队列
// ---------------------------------------------------------------------
function stageText(job) {
  var parts = [];
  if (job.stage_label && job.status !== 'done') { parts.push(job.stage_label); }
  if (job.status === 'running') {
    if (job.speed) { parts.push(job.speed + 'x'); }
    if (job.fps) { parts.push(job.fps + ' fps'); }
    if (job.eta_seconds) { parts.push('剩余 ' + fmtElapsed(job.eta_seconds)); }
  }
  if (job.status === 'queued' && job.queue_position) {
    parts.push('第 ' + job.queue_position + ' 位');
  }
  if (job.source_duration) { parts.push(fmtElapsed(job.source_duration)); }
  return parts.join(' · ');
}

function jobMeta(job) {
  var bits = [];
  bits.push('创建 ' + fmtDateTime(job.created_at));
  if (job.source_bytes) { bits.push('源 ' + job.source_bytes_text); }
  if (job.output_size) { bits.push('产物 ' + job.output_size_text); }
  if (job.duration_sec && job.status === 'running') {
    bits.push('耗时 ' + fmtElapsed(job.duration_sec));
  }
  bits.push(job.preset_label || job.preset);
  return bits.join(' · ');
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
      el('span', { className: 'download-row-name',
                   text: job.source_name || job.title || job.source_path })
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
  if (job.output_path && job.status === 'done') {
    info.appendChild(el('div', {
      className: 'muted download-row-path',
      text: job.output_path + (job.deleted_source ? '（源文件已删除）' : '') +
        (job.trashed_count ? '（旧产物已移入回收站）' : '')
    }));
  }
  if (job.status === 'running' || job.status === 'queued') {
    info.appendChild(progressBar(job.progress || 0));
  }

  var actions = el('div', { className: 'download-row-actions' });
  if (job.status === 'queued' || job.status === 'running') {
    actions.appendChild(actionButton('取消', 'btn-danger-ghost', function () {
      return convertAction(job.job_id, 'cancel');
    }));
  } else if (job.status === 'failed' || job.status === 'cancelled') {
    actions.appendChild(actionButton('重试', 'btn-primary', function () {
      return convertAction(job.job_id, 'retry');
    }));
    actions.appendChild(actionButton('删除', 'btn-link btn-sm', function () {
      return deleteConvertJob(job.job_id, false);
    }));
  } else if (job.status === 'done') {
    actions.appendChild(actionButton('删除任务', 'btn-link btn-sm', function () {
      return deleteConvertJob(job.job_id, false);
    }));
    actions.appendChild(actionButton('删除产物', 'btn-link btn-sm', function () {
      return deleteConvertJob(job.job_id, true);
    }));
  }
  return el('div', { className: 'download-row', dataset: { jobId: job.job_id } },
           [info, actions]);
}

function renderJobs(snapshot) {
  var list = els.convertJobsList;
  if (!list) { return; }
  clear(list);
  var active = (snapshot && snapshot.active) || [];
  var queued = (snapshot && snapshot.queued) || [];
  var recent = (snapshot && snapshot.recent) || [];
  var rows = active.concat(queued, recent);
  rows.forEach(function (job) { list.appendChild(renderJobRow(job)); });
  setHidden(els.convertEmpty, rows.length > 0);

  var counts = (snapshot && snapshot.counts) || {};
  setText(els.convertQueueHint,
    '进行中 ' + (counts.running || 0) +
    ' · 排队 ' + (counts.queued || 0) +
    ' · 已完成 ' + (counts.done || 0) +
    ' · 失败 ' + (counts.failed || 0));
  renderStorage(snapshot);
}

function renderStorage(snapshot) {
  if (!els.convertStorageHint) { return; }
  var storage = snapshot && snapshot.storage;
  if (!storage) {
    setText(els.convertStorageHint, '');
    return;
  }
  var warn = storage.min_free_gb > 0 &&
    storage.free_bytes < storage.min_free_gb * 1073741824;
  setText(els.convertStorageHint,
    '输出目录剩余 ' + fmtBytes(storage.free_bytes) +
    '（已用 ' + storage.used_pct + '%）' + (warn ? ' · 低于保留阈值' : ''));
  els.convertStorageHint.className =
    'muted download-storage' + (warn ? ' is-warn' : '');
}

function renderError(message) {
  if (!els.convertListError) { return; }
  setText(els.convertListError, message || '');
  setHidden(els.convertListError, !message);
}

async function refreshConvertQueue() {
  var cs = convertState();
  if (!els.convertJobsList || cs.loading) { return; }
  cs.loading = true;
  try {
    var snapshot = await api('/api/convert/queue', { timeout: 15000 });
    cs.snapshot = snapshot;
    cs.loaded = true;
    renderError('');
    renderJobs(snapshot);
  } catch (err) {
    cs.lastError = errorMessage(err, '加载转换队列失败。');
    renderError(cs.lastError);
  } finally {
    cs.loading = false;
  }
}

function stopConvertPolling() {
  var cs = convertState();
  if (cs.timer) {
    clearInterval(cs.timer);
    cs.timer = null;
  }
}

function startConvertPolling() {
  stopConvertPolling();
  var cs = convertState();
  cs.timer = setInterval(function () {
    if (state.view !== 'convert') { stopConvertPolling(); return; }
    refreshConvertQueue();
  }, POLL_MS);
}

async function convertAction(jobId, action) {
  try {
    await api('/api/convert/' + encodeURIComponent(jobId) + '/' + action,
              { method: 'POST', timeout: 20000 });
    toast(action === 'cancel' ? '已提交取消' : '已重新排队', 'success');
  } catch (err) {
    toast(errorMessage(err, '操作失败。'), 'error');
  }
  refreshConvertQueue();
}

async function deleteConvertJob(jobId, withOutput) {
  var ok = await confirmDialog({
    title: withOutput ? '删除任务和产物？' : '删除转换任务？',
    message: withOutput
      ? '会同时删除已生成的转换产物文件（源文件不受影响）。此操作不可撤销。'
      : '只删除任务记录，不会删除已生成的文件。',
    confirmText: withOutput ? '删除产物' : '删除',
    danger: true
  });
  if (!ok) { return; }
  try {
    await api('/api/convert/' + encodeURIComponent(jobId) +
              (withOutput ? '?delete_output=1' : ''),
              { method: 'DELETE', timeout: 20000 });
    toast('任务已删除', 'success');
  } catch (err) {
    toast(errorMessage(err, '删除失败。'), 'error');
  }
  refreshConvertQueue();
}

async function bulkConvert(action) {
  var label = { cancel_all: '取消全部', retry_failed: '重试失败',
                clear_finished: '清理已结束' }[action] || action;
  if (action !== 'retry_failed') {
    var ok = await confirmDialog({
      title: label + '？',
      message: action === 'cancel_all'
        ? '会取消所有排队和运行中的转换任务（运行中的 FFmpeg 会被终止，临时文件清理）。'
        : '会删除已结束任务的记录（不会删除产物文件）。',
      confirmText: label,
      danger: action === 'cancel_all'
    });
    if (!ok) { return; }
  }
  try {
    var res = await api('/api/convert/bulk', {
      method: 'POST', body: { action: action, scope: 'all' }, timeout: 30000
    });
    toast(label + '：影响 ' + ((res && res.affected) || 0) + ' 个任务', 'success');
  } catch (err) {
    toast(errorMessage(err, '批量操作失败。'), 'error');
  }
  refreshConvertQueue();
}

// ---------------------------------------------------------------------
// 页面生命周期
// ---------------------------------------------------------------------
export function showConvertPage() {
  if (!convertState().caps) { loadCapabilities(); }
  else { renderCapabilityBadge(); }
  refreshConvertQueue();
  startConvertPolling();
}

export function hideConvertPage() {
  stopConvertPolling();
}

export function initConvertPage() {
  if (els.convertDirPickBtn) {
    els.convertDirPickBtn.addEventListener('click', function () {
      if (typeof state.openConvertDirPicker !== 'function') { return; }
      state.openConvertDirPicker({
        initial: els.convertDirPath ? els.convertDirPath.value : '',
        onConfirm: function (path) {
          if (els.convertDirPath) { els.convertDirPath.value = path || ''; }
        }
      });
    });
  }
  if (els.convertEnumerateBtn) {
    els.convertEnumerateBtn.addEventListener('click', enumerateFiles);
  }
  // v0061：源文件后缀多选
  if (els.convertExtAllBtn) {
    els.convertExtAllBtn.addEventListener('click', function () {
      setExtensionSelection('all');
    });
  }
  if (els.convertExtNoneBtn) {
    els.convertExtNoneBtn.addEventListener('click', function () {
      setExtensionSelection('none');
    });
  }
  if (els.convertExtVideoBtn) {
    els.convertExtVideoBtn.addEventListener('click', function () {
      setExtensionSelection('video');
    });
  }
  if (els.convertExtAudioBtn) {
    els.convertExtAudioBtn.addEventListener('click', function () {
      setExtensionSelection('audio');
    });
  }
  if (els.convertAddManualBtn) {
    els.convertAddManualBtn.addEventListener('click', addManualPaths);
  }
  if (els.convertSelectAllBtn) {
    els.convertSelectAllBtn.addEventListener('click', function () {
      var cs = convertState();
      addSelected(cs.candidates);
      toast('已全选 ' + cs.selected.size + ' 个文件', 'info');
    });
  }
  if (els.convertSelectNoneBtn) {
    els.convertSelectNoneBtn.addEventListener('click', function () {
      convertState().selected.clear();
      renderSelection();
    });
  }
  if (els.convertPresetGroup) {
    els.convertPresetGroup.addEventListener('change', function () {
      fillPresetOptions();
    });
  }
  if (els.convertPreset) {
    els.convertPreset.addEventListener('change', updatePresetHint);
  }
  if (els.convertOutputMode) {
    els.convertOutputMode.addEventListener('change', updateOutputModeUi);
  }
  if (els.convertOutputPickBtn) {
    els.convertOutputPickBtn.addEventListener('click', function () {
      if (typeof state.openConvertDirPicker !== 'function') { return; }
      state.openConvertDirPicker({
        initial: els.convertOutputDir ? els.convertOutputDir.value : '',
        onConfirm: function (path) {
          if (els.convertOutputDir) { els.convertOutputDir.value = path || ''; }
        }
      });
    });
  }
  if (els.convertSubmitBtn) {
    els.convertSubmitBtn.addEventListener('click', submitConvert);
  }
  if (els.convertCancelAllBtn) {
    els.convertCancelAllBtn.addEventListener('click', function () {
      bulkConvert('cancel_all');
    });
  }
  if (els.convertRetryFailedBtn) {
    els.convertRetryFailedBtn.addEventListener('click', function () {
      bulkConvert('retry_failed');
    });
  }
  if (els.convertClearFinishedBtn) {
    els.convertClearFinishedBtn.addEventListener('click', function () {
      bulkConvert('clear_finished');
    });
  }
  if (els.convertExportLink) {
    els.convertExportLink.setAttribute('href', '/api/convert/export');
  }
  updateOutputModeUi();
}
