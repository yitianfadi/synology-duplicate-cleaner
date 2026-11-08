/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 前端应用（ES module 入口）
   --------------------------------------------------------------------------
   · 纯静态 ES modules（无框架、无构建、无 CDN），ES2019 语法。
   · 仅调用 docs/CONTRACT.md 中约定的同源 REST API：
       POST /api/auth/login  GET /api/auth/me  POST /api/auth/logout
       GET  /api/meta  GET /api/shares  GET /api/browse  GET /api/settings
       PUT  /api/settings  POST /api/scans  GET /api/scans
       GET  /api/scans/{id}  POST /api/scans/{id}/pause|resume|stop
       GET  /api/scans/{id}/groups  GET /api/scans/{id}/stats
       GET  /api/scans/{id}/csv
       POST /api/scans/{id}/delete-plan  POST /api/scans/{id}/delete
       GET  /api/scans/{id}/delete/{job_id}
       GET  /api/preview  GET /api/logs  GET /api/logs/download
   · 安全约定：服务端返回的路径 / 哈希 / 日志内容一律通过 textContent 渲染，
     绝不使用 innerHTML 拼接；不读取 localStorage 保存会话；不使用 eval。
   · 常量 / 状态 / 工具 / API / Toast / 弹窗已拆到 ./js/*.js，本文件保留业务：
     登录 → 导航 → 文件夹选择器 → 扫描选项 → 进度 → 结果 → 选择 → 预览 →
     删除 → 历史 / 日志 / 设置 → 启动。
   ========================================================================== */

import {
  ACTIVE_STATUS, AUDIT_LABEL, CSRF_KEY, DEFAULT_SETTINGS, DEFAULT_SIMILARITY_EXTENSIONS,
  DEFAULT_VIDEO_EXTENSIONS, DELETE_REASON_LABEL, DELETE_STATUS_LABEL,
  ERROR_FALLBACK, JOB_STATUS_LABEL, KEEP_LABEL, KEEP_MODE_LABEL,
  KEEP_RULE_LABEL, SIDECAR_MODE_LABEL, PROTECT_REASON_LABEL,
  RECYCLE_MODE_LABEL, MERGE_REASON_LABEL, NOTIFY_CHANNEL_LABEL,
  VIDEO_CONFIDENCE_LABEL, VIDEO_CONFIDENCE_CLASS, SUBTITLE_FAILURE_LABEL, LEGACY_VIEW_ALIASES,
  MTIME_FALLBACK_LABEL, MTIME_FALLBACK_REASONS, POLL_MS,
  SIZE_UNITS, SOURCE_INFO, STAGE_LABEL, STATUS_BADGE, STATUS_LABEL,
  SUBTITLE_ACTIVE_STATUS, SUBTITLE_LANGUAGES, SUBTITLE_OUTPUT_KIND_LABEL,
  SUBTITLE_RUNNING_STATUS, SUBTITLE_SOURCE_LABEL, SUBTITLE_STAGE_LABEL,
  SUBTITLE_STEP_LABEL, SUBTITLE_STEP_STATUS_LABEL,
  SUBTITLE_STATUS_BADGE, SUBTITLE_STATUS_LABEL, SUBTITLE_TARGET_LANGUAGES,
  SUBTITLE_TRANSLATE_OUTPUTS, SUBTITLE_WARNING_LABEL,
  TERMINAL_STATUS, VIDEO_HWACCEL_LABEL, VIDEO_HWACCEL_MODES, VIEWS, VIEW_HEADINGS
} from './js/constants.js';
import { els, IDS, state } from './js/state.js';
import {
  $, $$, appendChildren, byId, clear, el, focusSoon, getFocusable, hide, icon,
  scrollToNode, setBusy, setHidden, setText, show, statusBadge, syncTopStackHeight
} from './js/dom.js';
import { fmtBytes, fmtDateTime, fmtElapsed, fmtInt } from './js/format.js';
import {
  clamp, clonePlain, copyText, debounce, encodePath, extListToText, isUnderRoots,
  normalizeExtList, normalizePathList
} from './js/util.js';
import { ApiError, api, errorMessage, saveCsrf, setUnauthorizedHandler } from './js/api.js';
import {
  fetchModelList, formatModelListHint, formatProbeError, formatProbeResult,
  normalizeModelList, normalizeProbeKind, testProvider
} from './js/subtitle-probe.js';
import {
  initSubtitlePage, showSubtitlePage, stopSubtitlePagePolling
} from './js/subtitle-page.js';
import { toast } from './js/toast.js';
import {
  closeModal, confirmDialog, getActiveModal, isModalOpen, openModal, setModalDismissible,
  trapModalKeys
} from './js/modal.js';

(function () {
  'use strict';

  // 401 未授权处理：api.js 通过回调通知 app.js，避免模块循环依赖。
  setUnauthorizedHandler(handleUnauthorized);

  // ==================== 通用工具（依赖 toast / copyText） ====================
  function copyWithFeedback(text, btn) {
    copyText(text).then(function () {
      toast('已复制到剪贴板。', 'success', 1800);
      if (btn) {
        btn.classList.add('is-copied');
        setTimeout(function () { btn.classList.remove('is-copied'); }, 1200);
      }
    }).catch(function (err) {
      toast((err && err.message) ? err.message : '复制失败，请手动选择文本复制。', 'error');
    });
  }

  // ==================== 6. 登录与会话 ====================
  async function initSession() {
    try {
      state.csrf = sessionStorage.getItem(CSRF_KEY) || '';
    } catch (e) { state.csrf = ''; }
    try {
      var me = await api('/api/auth/me', { timeout: 10000 });
      if (!me || !me.user) { throw new ApiError(401, 'AUTH_REQUIRED', ERROR_FALLBACK.AUTH_REQUIRED); }
      state.user = me.user;
      if (me.csrf_token) { saveCsrf(me.csrf_token); }
      await bootApp();
    } catch (err) {
      state.user = null;
      if (state.csrf) { saveCsrf(''); }
      showLogin('');
    }
  }

  async function handleLogin(event) {
    event.preventDefault();
    if (state.loginSubmitting) { return; }
    var username = els.loginUsername.value.trim();
    var password = els.loginPassword.value;
    var otp = els.loginOtp.value.trim();
    hide(els.loginError);
    if (!username || !password) {
      setText(els.loginError, '请输入 DSM 账号和密码。');
      show(els.loginError);
      focusSoon(!username ? els.loginUsername : els.loginPassword);
      return;
    }
    state.loginSubmitting = true;
    setBusy(els.loginSubmit, true, '正在登录…');
    try {
      var body = { username: username, password: password };
      if (otp) { body.otp = otp; }
      var res = await api('/api/auth/login', { method: 'POST', body: body, skipAuthHandler: true, timeout: 20000 });
      if (!res || !res.ok) { throw new ApiError(401, 'AUTH_FAILED', '登录失败，请检查账号和密码。'); }
      state.user = res.user || { name: username, is_admin: true };
      saveCsrf(res.csrf_token || '');
      els.loginPassword.value = '';
      els.loginOtp.value = '';
      await bootApp();
      toast('登录成功，欢迎使用重复文件清理与字幕。', 'success');
    } catch (err) {
      setText(els.loginError, errorMessage(err, '登录失败，请检查账号和密码。'));
      show(els.loginError);
      focusSoon(els.loginPassword);
    } finally {
      state.loginSubmitting = false;
      setBusy(els.loginSubmit, false);
    }
  }

  async function handleLogout() {
    try { await api('/api/auth/logout', { method: 'POST', body: {} }); } catch (e) { /* 退出失败也回到登录页 */ }
    try { sessionStorage.removeItem(CSRF_KEY); } catch (e2) { /* 忽略 */ }
    state.csrf = '';
    resetAppState();
    showLogin('');
    toast('已退出登录。', 'info');
  }

  function handleUnauthorized() {
    if (!state.booted) { return; }
    stopAllPolling();
    closeModal(true);
    resetAppState();
    showLogin('登录已过期，请重新登录。');
  }

  function showLogin(message) {
    state.booted = false;
    closeModal(true);
    stopAllPolling();
    hide(els.app);
    show(els.loginView);
    if (message) { setText(els.loginError, message); show(els.loginError); }
    else { setText(els.loginError, ''); hide(els.loginError); }
    focusSoon(els.loginUsername);
  }

  function resetAppState() {
    stopAllPolling();
    state.user = null;
    state.csrf = '';
    try { sessionStorage.removeItem(CSRF_KEY); } catch (csrfError) { /* 忽略 */ }
    state.meta = null;
    state.settings = null;
    state.defaults = null;
    state.roots = [];
    state.excludes = [];
    state.selectionHydrated = false;
    state.pinnedPath = '';
    state.recycleDefault = true;
    state.scanMode = 'exact';
    state.similarity = { supported: true, extensions: DEFAULT_SIMILARITY_EXTENSIONS.slice(), defaultThreshold: 85, maxConcurrency: 4, binaryExtensions: [] };
    state.video = { supported: true, available: true, extensions: DEFAULT_VIDEO_EXTENSIONS.slice(), ffmpegPath: '', ffmpegVersion: '', hwaccels: [], gpuHint: '', cache: '' };
    state.subtitle = { supported: false, enabled: false, asrKeySet: false, llmKeySet: false, asrLanguage: 'auto', translateEnabled: false, targetLang: 'zh' };
    state.historyCache = null;
    state.logsLoaded = false;
    state.settingsLoaded = false;
    state.subtitleJobs.snapshot = null;
    state.subtitleJobs.list = [];
    if (state.auditSelection) { state.auditSelection.clear(); }
    state.scan = { id: null, data: null, stats: null, timer: null, abort: null, polling: false, errors: 0, handled: false };
    resetResultsState();
    state.deleteFlow = { plan: null, job: null, timer: null, abort: null, running: false, busy: false, modal: null, recycle: true };
    hide(els.progressPanel);
    hide(els.resultsSection);
    hide(els.similarityCounters);
    setText(els.userName, '—');
    hide(els.envBadge);
    setScanMode('exact', { silent: true });
    clear(els.historyList);
    clear(els.logFilesList);
    clear(els.auditList);
    renderRoots();
  }

  async function bootApp() {
    state.booted = true;
    hide(els.loginView);
    show(els.app);
    syncTopStackHeight();
    setText(els.userName, state.user && state.user.name ? state.user.name : '—');
    els.userChip.title = state.user ? '当前登录账号：' + state.user.name : '当前登录账号';
    await loadBootstrapData();
    setView(state.view, { initial: true });
    checkActiveScanOnBoot();
  }

  async function loadBootstrapData() {
    var meta = null;
    var settingsData = null;
    try { meta = await api('/api/meta', { timeout: 10000 }); } catch (e) { meta = null; }
    if (meta) { state.meta = meta; applyMeta(meta); }
    try { settingsData = await api('/api/settings', { timeout: 10000 }); } catch (e2) { settingsData = null; }
    var rawSettings = settingsData && settingsData.settings ? settingsData.settings : null;
    if (rawSettings) {
      state.settings = Object.assign({}, DEFAULT_SETTINGS, rawSettings);
      state.defaults = settingsData.defaults ? Object.assign({}, DEFAULT_SETTINGS, settingsData.defaults) : clonePlain(DEFAULT_SETTINGS);
    } else {
      state.settings = clonePlain(DEFAULT_SETTINGS);
      state.defaults = clonePlain(DEFAULT_SETTINGS);
      toast('读取默认设置失败，已使用内置默认值。', 'error');
    }
    state.recycleDefault = state.settings.recycle_default !== false;
    state.similarity = similarityCapability();
    state.video = videoCapability();
    updateSubtitleFromRaw(rawSettings);
    state.scanMode = state.settings.scan_mode === 'similar' ? 'similar' : 'exact';
    applySettingsToScanForm(state.settings);
    restoreLastSelection();
    state.selectionHydrated = true;
    renderRoots();
    renderExcludeChips();
    updatePinnedDisplay();
    updateScanSummary();
  }

  function applyMeta(meta) {
    if (!meta) { return; }
    if (meta.dev_mode) { setText(els.envBadge, '开发模式'); show(els.envBadge); }
    var parts = [];
    if (meta.app_version) { parts.push('v' + meta.app_version); }
    if (meta.dsm_version) { parts.push('DSM ' + meta.dsm_version); }
    if (meta.platform) { parts.push(meta.platform); }
    if (meta.python_version) { parts.push('Python ' + meta.python_version); }
    setText(els.footerMeta, parts.join(' · '));
    state.similarity = similarityCapability();
    state.video = videoCapability();
    updateModeAvailability();
    updateVideoAvailabilityUi();
    renderSettingsStatusSummary();
    updateSettingsSummaries();
  }

  // ==================== 7. 导航 ====================
  function bindNavigation() {
    $$('.nav-item').forEach(function (btn) {
      btn.addEventListener('click', function () { setView(btn.dataset.nav); });
    });
    if (els.navSettingsBtn) {
      els.navSettingsBtn.addEventListener('click', function () { setView('settings'); });
    }
    window.addEventListener('hashchange', function () {
      var raw = String(window.location.hash || '').replace(/^#\/?/, '');
      var view = LEGACY_VIEW_ALIASES[raw] || raw;
      if (raw === 'logs') {
        // v0038：旧 #/logs 书签兼容 → 任务中心「操作日志」标签页
        state.taskTab = 'logs';
        try { history.replaceState(null, '', '#/history'); } catch (e) { /* 忽略 */ }
      }
      if (VIEWS.indexOf(view) !== -1) { setView(view, { fromHash: true }); }
    });
  }

  function setView(view, options) {
    options = options || {};
    if (VIEWS.indexOf(view) === -1) { view = 'scan'; }
    state.view = view;
    $$('.view').forEach(function (node) { node.hidden = node.dataset.view !== view; });
    $$('.nav-item').forEach(function (btn) {
      if (btn.dataset.nav === view) { btn.setAttribute('aria-current', 'page'); }
      else { btn.removeAttribute('aria-current'); }
    });
    if (els.navSettingsBtn) {
      if (view === 'settings') { els.navSettingsBtn.setAttribute('aria-current', 'page'); }
      else { els.navSettingsBtn.removeAttribute('aria-current'); }
    }
    if (!options.fromHash) {
      var target = '#/' + view;
      if (window.location.hash !== target) {
        try { history.replaceState(null, '', target); } catch (e) { window.location.hash = target; }
      }
    }
    if (!options.initial) {
      var heading = byId(VIEW_HEADINGS[view]);
      if (heading) { heading.setAttribute('tabindex', '-1'); focusSoon(heading); }
    }
    if (view !== 'history') { stopSubtitleJobsPolling(); }
    if (view !== 'subtitles') { stopSubtitlePagePolling(); }
    if (view === 'history') { setTaskTab(state.taskTab, { load: true }); }
    else if (view === 'subtitles') { showSubtitlePage(); }
    else if (view === 'settings') {
      loadSettingsPage();
      if (!state.diagnostics.loaded) { loadDiagnostics(); }
    }
  }

  // ==================== 7.1 任务中心标签页（键盘 / aria 可访问） ====================
  var TASK_TABS = ['history', 'subtitles', 'deletions', 'trash', 'logs'];

  function normalizeTaskTab(tab) {
    return TASK_TABS.indexOf(tab) === -1 ? 'history' : tab;
  }

  function taskTabButtonId(tab) {
    return 'taskTab' + tab.charAt(0).toUpperCase() + tab.slice(1);
  }

  /**
   * 切换任务中心标签页：更新 aria-selected / roving tabindex / 面板 hidden，
   * 并按标签页懒加载数据（字幕任务仅在选中时轮询）。
   */
  function setTaskTab(tab, options) {
    options = options || {};
    tab = normalizeTaskTab(tab);
    state.taskTab = tab;
    $$('.task-tab').forEach(function (btn) {
      var selected = btn.dataset.taskTab === tab;
      btn.setAttribute('aria-selected', selected ? 'true' : 'false');
      btn.tabIndex = selected ? 0 : -1;
      if (selected) { btn.classList.add('is-active'); }
      else { btn.classList.remove('is-active'); }
    });
    $$('.task-panel').forEach(function (panel) {
      panel.hidden = panel.dataset.taskPanel !== tab;
    });
    if (tab !== 'subtitles') { stopSubtitleJobsPolling(); }
    if (options.focus) {
      var btn = byId(taskTabButtonId(tab));
      if (btn) {
        try { btn.focus({ preventScroll: true }); } catch (e) { btn.focus(); }
      }
    }
    if (options.load !== false) { loadTaskTab(tab, options); }
  }

  function loadTaskTab(tab, options) {
    options = options || {};
    tab = normalizeTaskTab(tab);
    if (tab === 'history') { loadHistory(); }
    else if (tab === 'subtitles') { loadSubtitleJobs(); loadSubtitleFailures(); }
    else if (tab === 'deletions') { loadDeletionJobs(); }
    else if (tab === 'trash') { loadTrashPanel(); }
    else if (tab === 'logs') { loadLogs(); }
  }

  function refreshTaskCenter() {
    loadTaskTab(state.taskTab, { force: true });
  }

  function bindTaskTabs() {
    var tablist = $('.task-tabs');
    if (!tablist) { return; }
    $$('.task-tab', tablist).forEach(function (btn) {
      btn.addEventListener('click', function () { setTaskTab(btn.dataset.taskTab, { load: true }); });
    });
    tablist.addEventListener('keydown', function (event) {
      var current = event.target;
      if (!current || current.getAttribute('role') !== 'tab') { return; }
      var index = TASK_TABS.indexOf(current.dataset.taskTab);
      if (index === -1) { return; }
      var next = index;
      if (event.key === 'ArrowRight') { next = (index + 1) % TASK_TABS.length; }
      else if (event.key === 'ArrowLeft') { next = (index - 1 + TASK_TABS.length) % TASK_TABS.length; }
      else if (event.key === 'Home') { next = 0; }
      else if (event.key === 'End') { next = TASK_TABS.length - 1; }
      else { return; }
      event.preventDefault();
      setTaskTab(TASK_TABS[next], { focus: true, load: true });
    });
  }

  function initialViewFromHash() {
    var raw = String(window.location.hash || '').replace(/^#\/?/, '');
    if (raw === 'logs') {
      // v0038：旧 #/logs 兼容到任务中心「操作日志」
      state.taskTab = 'logs';
      return 'history';
    }
    var view = LEGACY_VIEW_ALIASES[raw] || raw;
    return VIEWS.indexOf(view) !== -1 ? view : 'scan';
  }

  // ==================== 8. 文件夹选择器（共享文件夹 / 懒加载目录树） ====================
  var PICKER_TITLES = {
    roots: '选择扫描范围', excludes: '选择排除目录',
    pinned: '选择指定保留路径', subtitle: '选择视频目录'
  };
  var PICKER_HINTS = {
    roots: '勾选目录作为扫描范围；选中父目录会递归包含全部子目录（子目录显示为已选中但不可单独取消）。点击文件夹名称进入下一级，左侧箭头展开子目录。',
    excludes: '勾选需要排除的目录；排除目录必须位于扫描范围内，其中的文件不会被扫描或删除。',
    pinned: '选择目录或文件作为“指定路径”保留策略的匹配前缀。',
    subtitle: '选择要批量生成/翻译字幕的视频目录（单选）。点击文件夹名称进入下一级，左侧箭头展开子目录，点“选择当前目录”后再“完成选择”。'
  };

  function openFolderPicker(mode, options) {
    if (VIEWS.indexOf(state.view) === -1) { return; }
    options = options || {};
    var picker = {
      mode: mode,
      currentPath: '',
      parent: null,
      breadcrumbs: [],
      entries: [],
      loading: false,
      error: '',
      expanded: new Map(),
      childCache: new Map(),
      search: '',
      roots: state.roots.map(function (r) { return { path: r.path, name: r.name }; }),
      excludes: state.excludes.slice(),
      pinned: state.pinnedPath,
      // 字幕页目录选择：单选 + 完成后回调（不修改扫描范围）
      subtitleDir: typeof options.initial === 'string' ? options.initial : '',
      onConfirm: typeof options.onConfirm === 'function' ? options.onConfirm : null,
      reqSeq: 0,
      bodyEl: null,
      listEl: null,
      breadcrumbEl: null,
      searchInput: null,
      currentBtn: null,
      chipsEl: null,
      countEl: null,
      hintEl: null,
      doneBtn: null,
      modal: null
    };
    state.picker = picker;
    buildPickerModal(picker);
    loadPickerLevel(picker, '');
  }

  function buildPickerModal(picker) {
    var breadcrumbEl = el('div', { className: 'picker-breadcrumbs', attrs: { 'aria-label': '当前位置' } });
    var searchInput = el('input', {
      attrs: { type: 'search', placeholder: '搜索当前层级…', 'aria-label': '搜索当前层级' },
      on: {
        input: debounce(function () {
          if (state.picker !== picker) { return; }
          picker.search = searchInput.value.trim();
          renderPicker(picker);
        }, 150)
      }
    });
    var searchWrap = el('div', { className: 'picker-search' }, [searchInput]);
    var currentBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '选择当前目录', hidden: true,
      on: {
        click: function () {
          if (!picker.currentPath) { return; }
          var selected = pickerIsSelected(picker, picker.currentPath);
          selectPickerPath(picker, picker.currentPath, !selected, null);
          renderPicker(picker);
        }
      }
    });
    var toolbar = el('div', { className: 'picker-toolbar' }, [breadcrumbEl, searchWrap, currentBtn]);
    var listEl = el('div', { className: 'picker-list', attrs: { role: 'group', 'aria-label': '目录列表' } });
    var hintEl = el('p', { className: 'picker-hint', text: PICKER_HINTS[picker.mode] || '' });
    var chipsEl = el('div', { className: 'chips' });
    var selectedWrap = el('div', { className: 'picker-selected' }, [chipsEl]);
    var bodyEl = el('div', { className: 'picker-body' }, [toolbar, listEl, hintEl, selectedWrap]);
    var countEl = el('span', { className: 'picker-count', text: '' });
    var cancelBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '取消',
      on: { click: function () { closeModal(true); } }
    });
    var doneBtn = el('button', {
      className: 'btn btn-primary', attrs: { type: 'button' }, text: '完成选择',
      on: {
        click: function () {
          applyPickerSelection(picker);
          closeModal(true);
        }
      }
    });
    picker.breadcrumbEl = breadcrumbEl;
    picker.searchInput = searchInput;
    picker.currentBtn = currentBtn;
    picker.listEl = listEl;
    picker.chipsEl = chipsEl;
    picker.countEl = countEl;
    picker.hintEl = hintEl;
    picker.doneBtn = doneBtn;
    picker.modal = openModal({
      title: PICKER_TITLES[picker.mode] || '选择目录',
      subtitle: '数据来自共享文件夹与目录浏览接口，系统目录与符号链接不可选择。',
      size: 'lg',
      body: bodyEl,
      footer: { left: countEl, right: [cancelBtn, doneBtn] },
      initialFocus: currentBtn,
      onClose: function () { state.picker = null; }
    });
  }

  async function loadPickerLevel(picker, path) {
    if (!picker) { return; }
    var seq = picker.reqSeq + 1;
    picker.reqSeq = seq;
    picker.loading = true;
    picker.error = '';
    picker.currentPath = path || '';
    picker.breadcrumbs = [];
    picker.entries = [];
    picker.search = '';
    if (picker.searchInput) { picker.searchInput.value = ''; }
    renderPicker(picker);
    try {
      var data;
      if (!path) {
        // 根层级使用共享文件夹接口（只返回真实存在的共享目录）
        data = await api('/api/shares', { timeout: 20000 });
      } else {
        var q = '?path=' + encodePath(path);
        if (picker.mode === 'pinned') { q += '&include_files=true'; }
        data = await api('/api/browse' + q, { timeout: 20000 });
      }
      if (state.picker !== picker || seq !== picker.reqSeq) { return; }
      var entries;
      if (!path) {
        var shares = Array.isArray(data.shares) ? data.shares : [];
        picker.unreadableShares = shares.filter(function (share) {
          return share.is_readable === false;
        }).map(function (share) { return share.path; });
        entries = shares.map(function (share) {
          var readable = share.is_readable !== false;
          return {
            name: share.name, path: share.path, type: 'dir', size: 0,
            created_at: null, modified_at: null, hidden: false, is_symlink: false,
            excluded: !readable, exclude_reason: readable ? null : 'permission',
            share: share
          };
        });
      } else {
        entries = Array.isArray(data.entries) ? data.entries : [];
        picker.parent = data.parent === undefined ? null : data.parent;
        picker.breadcrumbs = Array.isArray(data.breadcrumbs) ? data.breadcrumbs : [];
        if (!entries.length && Array.isArray(data.shares)) {
          entries = data.shares.map(function (share) {
            return {
              name: share.name, path: share.path, type: 'dir', size: 0,
              hidden: false, is_symlink: false, excluded: false, exclude_reason: null, share: share
            };
          });
        }
        if (data.path) { picker.currentPath = data.path; }
      }
      picker.entries = entries;
      picker.expanded.clear();
      picker.childCache.clear();
      picker.loading = false;
      renderPicker(picker);
    } catch (err) {
      if (state.picker !== picker || seq !== picker.reqSeq) { return; }
      picker.loading = false;
      picker.error = errorMessage(err, '加载目录失败。');
      renderPicker(picker);
    }
  }

  function pickerIsSelected(picker, path) {
    if (!path) { return false; }
    if (picker.mode === 'roots') {
      return picker.roots.some(function (r) { return r.path === path; });
    }
    if (picker.mode === 'excludes') { return picker.excludes.indexOf(path) !== -1; }
    if (picker.mode === 'subtitle') { return picker.subtitleDir === path; }
    return picker.pinned === path;
  }

  function pickerInheritedByRoot(picker, path) {
    if (!picker || picker.mode !== 'roots' || !path) { return false; }
    return picker.roots.some(function (r) {
      return r.path && r.path !== path && (path === r.path || path.indexOf(r.path.replace(/\/+$/, '') + '/') === 0);
    });
  }

  function pickerPathState(picker, path, entry) {
    var result = { disabled: false, note: '', warn: false, included: false };
    if (entry && entry.is_symlink) {
      result.disabled = true; result.note = '符号链接，不可选择'; result.warn = true; return result;
    }
    if (entry && entry.excluded) {
      result.disabled = true;
      if (entry.exclude_reason === 'permission') {
        result.note = '无读取权限，请给 duplicatecleaner 授权';
      } else {
        result.note = entry.exclude_reason ? ('不可选择：' + entry.exclude_reason) : '系统或不可访问目录';
      }
      result.warn = true;
      return result;
    }
    if (!path) { result.disabled = true; return result; }
    if (picker.mode === 'roots' && pickerInheritedByRoot(picker, path)) {
      result.included = true;
      result.note = '已包含在扫描范围（父目录已选中）';
      return result;
    }
    if (picker.mode === 'excludes') {
      var roots = picker.roots;
      if (!roots.length) {
        result.disabled = true; result.note = '请先选择扫描范围'; result.warn = true; return result;
      }
      if (!isUnderRoots(path, roots)) {
        result.disabled = true; result.note = '不在扫描范围内'; return result;
      }
      var isRootItself = roots.some(function (r) { return r.path === path; });
      if (isRootItself) {
        result.disabled = true; result.note = '扫描根目录本身不能排除'; result.warn = true; return result;
      }
    }
    return result;
  }

  function pickerLimit(picker, kind) {
    var limits = state.meta && state.meta.limits ? state.meta.limits : null;
    if (kind === 'roots') { return (limits && limits.max_roots) || 20; }
    return (limits && limits.max_excludes) || 200;
  }

  function selectPickerPath(picker, path, checked, entry) {
    if (!picker || !path) { return; }
    var stateInfo = pickerPathState(picker, path, entry);
    if (stateInfo.disabled) {
      if (stateInfo.note) { toast(stateInfo.note, 'error'); }
      return;
    }
    var name = entry && entry.name ? entry.name : (String(path).split('/').filter(Boolean).pop() || path);
    if (picker.mode === 'roots') {
      var index = -1;
      picker.roots.forEach(function (r, i) { if (r.path === path) { index = i; } });
      if (checked && index === -1) {
        // 选择父目录时，自动移除已被它递归包含的子目录根，避免重复扫描。
        picker.roots = picker.roots.filter(function (r) {
          return r.path !== path && !isUnderRoots(r.path, [path]);
        });
        index = -1;
        var maxRoots = pickerLimit(picker, 'roots');
        if (picker.roots.length >= maxRoots) { toast('最多只能选择 ' + maxRoots + ' 个扫描目录。', 'error'); return; }
        picker.roots.push({ path: path, name: name });
      } else if (!checked && index !== -1) {
        picker.roots.splice(index, 1);
      }
    } else if (picker.mode === 'excludes') {
      var at = picker.excludes.indexOf(path);
      if (checked && at === -1) {
        var maxExcludes = pickerLimit(picker, 'excludes');
        if (picker.excludes.length >= maxExcludes) { toast('最多只能添加 ' + maxExcludes + ' 个排除目录。', 'error'); return; }
        picker.excludes.push(path);
      } else if (!checked && at !== -1) {
        picker.excludes.splice(at, 1);
      }
    } else if (picker.mode === 'subtitle') {
      picker.subtitleDir = checked ? path : '';
    } else {
      picker.pinned = checked ? path : '';
    }
    syncPickerRows(picker);
    renderPickerFooter(picker);
  }

  function syncPickerRows(picker) {
    if (!picker || !picker.listEl) { return; }
    $$('.tree-row', picker.listEl).forEach(function (row) {
      var path = row.dataset.path;
      var info = pickerPathState(picker, path, null);
      var active = pickerIsSelected(picker, path) || info.included;
      if (active) { row.classList.add('is-selected'); }
      else { row.classList.remove('is-selected'); }
      if (info.included) { row.classList.add('is-inherited'); }
      else { row.classList.remove('is-inherited'); }
    });
    $$('.tree-check', picker.listEl).forEach(function (input) {
      var info = pickerPathState(picker, input.dataset.path, null);
      input.checked = pickerIsSelected(picker, input.dataset.path) || info.included;
      input.disabled = info.disabled || info.included;
      if (info.note) { input.title = info.note; }
    });
  }

  function renderPickerFooter(picker) {
    var countText = '';
    clear(picker.chipsEl);
    if (picker.mode === 'roots') {
      countText = '已选择 ' + picker.roots.length + ' / ' + pickerLimit(picker, 'roots') + ' 个扫描范围';
      if (!picker.roots.length) {
        picker.chipsEl.appendChild(el('span', { className: 'chip-empty', text: '尚未选择扫描目录' }));
      } else {
        picker.roots.forEach(function (r) {
          picker.chipsEl.appendChild(buildPickerChip(picker, r.path, r.path));
        });
      }
    } else if (picker.mode === 'excludes') {
      countText = '已添加 ' + picker.excludes.length + ' / ' + pickerLimit(picker, 'excludes') + ' 个排除目录';
      if (!picker.excludes.length) {
        picker.chipsEl.appendChild(el('span', { className: 'chip-empty', text: '尚未添加排除目录' }));
      } else {
        picker.excludes.forEach(function (path) {
          picker.chipsEl.appendChild(buildPickerChip(picker, path, path));
        });
      }
    } else if (picker.mode === 'subtitle') {
      countText = picker.subtitleDir ? '已选择视频目录' : '尚未选择视频目录';
      if (picker.subtitleDir) {
        picker.chipsEl.appendChild(buildPickerChip(picker, picker.subtitleDir, picker.subtitleDir));
      } else {
        picker.chipsEl.appendChild(el('span', { className: 'chip-empty', text: '请选择视频目录' }));
      }
    } else {
      countText = picker.pinned ? '已指定保留路径' : '尚未指定保留路径';
      if (picker.pinned) {
        picker.chipsEl.appendChild(buildPickerChip(picker, picker.pinned, picker.pinned));
      } else {
        picker.chipsEl.appendChild(el('span', { className: 'chip-empty', text: '请选择目录或文件' }));
      }
    }
    setText(picker.countEl, countText);
    if (picker.currentBtn) {
      var label = '选择当前目录';
      var currentState = pickerPathState(picker, picker.currentPath, null);
      var disabled = !picker.currentPath || currentState.disabled || currentState.included;
      if (picker.mode === 'excludes') { label = pickerIsSelected(picker, picker.currentPath) ? '取消排除当前目录' : '排除当前目录'; }
      else if (picker.mode === 'pinned') { label = pickerIsSelected(picker, picker.currentPath) ? '当前目录已指定' : '使用当前目录'; }
      else if (picker.mode === 'subtitle') { label = pickerIsSelected(picker, picker.currentPath) ? '取消选择当前目录' : '选择当前目录'; }
      else if (currentState.included) { label = '已包含在扫描范围'; }
      else { label = pickerIsSelected(picker, picker.currentPath) ? '取消选择当前目录' : '选择当前目录'; }
      setText(picker.currentBtn, label);
      picker.currentBtn.disabled = disabled;
      picker.currentBtn.hidden = !picker.currentPath;
    }
    if (picker.unreadableShares && picker.unreadableShares.length) {
      setText(picker.hintEl,
        '以下共享文件夹没有读取权限，已置灰不可选：' + picker.unreadableShares.join('、') +
        '。请在“控制面板 → 共享文件夹 → 权限 → 系统内部用户”中给 duplicatecleaner 授权后刷新。');
      picker.hintEl.classList.add('is-warn');
    } else if (picker.mode === 'excludes' && !picker.roots.length) {
      setText(picker.hintEl, '提示：尚未选择扫描范围，请先关闭此选择器，在“扫描范围”中选择目录后再添加排除目录。');
      picker.hintEl.classList.add('is-warn');
    } else {
      setText(picker.hintEl, PICKER_HINTS[picker.mode] || '');
      picker.hintEl.classList.remove('is-warn');
    }
  }

  function buildPickerChip(picker, path, label) {
    var removeBtn = el('button', {
      className: 'chip-x', attrs: { type: 'button', 'aria-label': '移除 ' + label },
      on: {
        click: function (event) {
          event.stopPropagation();
          selectPickerPath(picker, path, false, null);
          renderPicker(picker);
        }
      }
    }, [icon('close')]);
    return el('span', { className: 'chip', attrs: { title: path } }, [
      el('span', { className: 'chip-text', text: label }), removeBtn
    ]);
  }

  function renderPicker(picker) {
    if (!picker || state.picker !== picker) { return; }
    var breadcrumbEl = picker.breadcrumbEl;
    clear(breadcrumbEl);
    var rootBtn = el('button', {
      className: 'crumb' + (picker.currentPath ? '' : ' is-current'), attrs: { type: 'button' },
      text: '共享文件夹',
      on: { click: function () { if (picker.currentPath) { loadPickerLevel(picker, ''); } } }
    });
    breadcrumbEl.appendChild(rootBtn);
    var crumbs = picker.breadcrumbs || [];
    if (!crumbs.length && picker.currentPath) {
      crumbs = [{ name: picker.currentPath, path: picker.currentPath }];
    }
    crumbs.forEach(function (crumb, index) {
      breadcrumbEl.appendChild(el('span', { className: 'crumb-sep', attrs: { 'aria-hidden': 'true' }, text: '/' }));
      var isCurrent = index === crumbs.length - 1 && picker.currentPath === crumb.path;
      var btn = el('button', {
        className: 'crumb' + (isCurrent ? ' is-current' : ''), attrs: { type: 'button', title: crumb.path },
        text: crumb.name || crumb.path,
        on: { click: function () { if (!isCurrent) { loadPickerLevel(picker, crumb.path); } } }
      });
      breadcrumbEl.appendChild(btn);
    });
    var listEl = picker.listEl;
    clear(listEl);
    if (picker.loading) {
      listEl.appendChild(el('div', { className: 'tree-loading' }, [el('span', { className: 'spinner' }), ' 正在加载目录…']));
    } else if (picker.error) {
      listEl.appendChild(el('div', { className: 'state-box state-error', attrs: { role: 'alert' } }, [
        picker.error,
        el('button', {
          className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '重试',
          on: { click: function () { loadPickerLevel(picker, picker.currentPath); } }
        })
      ]));
    } else if (!picker.entries.length) {
      listEl.appendChild(el('div', { className: 'tree-empty', text: '该目录下没有可显示的目录。' }));
    } else {
      var matched = 0;
      var query = picker.search.toLowerCase();
      picker.entries.forEach(function (entry) {
        if (query && String(entry.name || '').toLowerCase().indexOf(query) === -1) { return; }
        matched += 1;
        listEl.appendChild(buildTreeRow(picker, entry, 0));
      });
      if (!matched) {
        listEl.appendChild(el('div', { className: 'tree-empty', text: '当前层级没有匹配“' + picker.search + '”的目录。' }));
      }
    }
    renderPickerFooter(picker);
  }

  function buildTreeRow(picker, entry, depth) {
    var isDir = entry.type === 'dir';
    var selectionState = pickerPathState(picker, entry.path, entry);
    var selected = pickerIsSelected(picker, entry.path);
    var included = selectionState.included === true;
    var rowClass = 'tree-row' + ((selected || included) ? ' is-selected' : '');
    if (selectionState.disabled) { rowClass += entry.excluded ? ' is-excluded' : ' is-disabled'; }
    if (included) { rowClass += ' is-inherited'; }
    var row = el('div', {
      className: rowClass,
      dataset: { path: entry.path, key: encodePath(entry.path) }
    });
    row.style.paddingLeft = (10 + depth * 18) + 'px';

    var caret;
    if (isDir) {
      var expanded = picker.expanded.has(entry.path);
      caret = el('button', {
        className: 'tree-caret',
        attrs: {
          type: 'button', 'aria-expanded': expanded ? 'true' : 'false',
          'aria-label': (expanded ? '收起 ' : '展开 ') + (entry.name || entry.path) + ' 子目录',
          'data-key': encodePath(entry.path)
        },
        on: { click: function (event) { event.stopPropagation(); togglePickerExpand(picker, entry, row); } }
      }, [icon('chevron')]);
    } else {
      caret = el('span', { className: 'tree-caret-spacer', attrs: { 'aria-hidden': 'true' } });
    }

    var input = el('input', {
      className: 'tree-check',
      attrs: {
        type: picker.mode === 'pinned' ? 'radio' : 'checkbox',
        name: picker.mode === 'pinned' ? 'picker-pinned' : 'picker-' + picker.mode,
        disabled: selectionState.disabled || included,
        'aria-label': (included ? '已包含在扫描范围：' : (selected ? '取消选择 ' : '选择 ')) + (entry.name || entry.path),
        'data-key': encodePath(entry.path)
      }
    });
    input.checked = selected || included;
    input.dataset.path = entry.path;
    if (selectionState.disabled || included) { input.title = selectionState.note || '不可选择'; }
    input.addEventListener('change', function () {
      selectPickerPath(picker, entry.path, input.checked, entry);
      input.checked = pickerIsSelected(picker, entry.path);
    });

    var label = el('button', {
      className: 'tree-label',
      attrs: {
        type: 'button',
        title: entry.path + (selectionState.note ? ' — ' + selectionState.note : ''),
        disabled: selectionState.disabled && isDir
      },
      on: {
        click: function () {
          if (isDir) {
            if (!selectionState.disabled) { loadPickerLevel(picker, entry.path); }
          } else if (!selectionState.disabled && picker.mode === 'pinned') {
            selectPickerPath(picker, entry.path, true, entry);
            renderPickerFooter(picker);
          }
        }
      }
    }, [
      icon(isDir ? 'folder' : 'file', 'tree-icon' + (isDir ? '' : ' is-file')),
      el('span', { className: 'tree-name', text: entry.name || entry.path })
    ]);

    var noteText = '';
    var noteWarn = selectionState.warn;
    if (selectionState.note) { noteText = selectionState.note; }
    else if (entry.share) {
      var shareName = entry.share.volume || '';
      noteText = shareName + (entry.share.filesystem ? ' · ' + entry.share.filesystem : '');
      if (entry.share.is_usb) { noteText += ' · USB'; }
      if (entry.share.is_read_only || entry.share.is_writable === false) { noteText += ' · 只读'; }
    } else if (!isDir) {
      noteText = fmtBytes(entry.size);
    } else if (entry.hidden) {
      noteText = '隐藏目录';
    }
    var note = noteText ? el('span', { className: 'tree-note' + (noteWarn ? ' is-warn' : ''), text: noteText }) : null;

    appendChildren(row, [caret, input, label, note]);
    var wrapper = el('div', {}, [row]);
    if (isDir && picker.expanded.has(entry.path) && picker.childCache.has(entry.path)) {
      var children = picker.childCache.get(entry.path);
      var childBox = el('div', { className: 'tree-children' });
      if (!children.length) {
        childBox.appendChild(el('div', { className: 'tree-level-msg', text: '该目录下没有可显示的目录。' }));
      } else {
        children.forEach(function (child) { childBox.appendChild(buildTreeRow(picker, child, depth + 1)); });
      }
      wrapper.appendChild(childBox);
    }
    return wrapper;
  }

  async function togglePickerExpand(picker, entry, rowEl) {
    if (!picker || state.picker !== picker) { return; }
    if (picker.expanded.has(entry.path)) {
      picker.expanded.delete(entry.path);
      renderPicker(picker);
      focusPickerCaret(picker, entry.path);
      return;
    }
    if (picker.childCache.has(entry.path)) {
      picker.expanded.set(entry.path, true);
      renderPicker(picker);
      focusPickerCaret(picker, entry.path);
      return;
    }
    var caret = $('.tree-caret', rowEl);
    if (caret) { caret.setAttribute('aria-expanded', 'true'); }
    var loading = el('div', { className: 'tree-level-msg' }, [el('span', { className: 'spinner' }), ' 正在加载…']);
    rowEl.parentNode.insertBefore(loading, rowEl.nextSibling);
    try {
      var q = '?path=' + encodePath(entry.path);
      if (picker.mode === 'pinned') { q += '&include_files=true'; }
      var data = await api('/api/browse' + q, { timeout: 20000 });
      if (state.picker !== picker) { return; }
      picker.childCache.set(entry.path, Array.isArray(data.entries) ? data.entries : []);
      picker.expanded.set(entry.path, true);
      renderPicker(picker);
      focusPickerCaret(picker, entry.path);
    } catch (err) {
      if (state.picker !== picker) { return; }
      loading.textContent = errorMessage(err, '加载子目录失败。');
      loading.classList.add('is-warn');
    }
  }

  function focusPickerCaret(picker, path) {
    if (!picker || !picker.listEl) { return; }
    var caret = $('.tree-caret[data-key="' + encodePath(path) + '"]', picker.listEl);
    if (caret) { focusSoon(caret); }
  }

  // 记住上一次扫描范围（浏览器本地；按 origin 持久化，默认下次打开自动回填）
  var LAST_SELECTION_KEY = 'dupcleaner.lastSelection.v1';

  function restoreLastSelection() {
    try {
      var raw = window.localStorage ? window.localStorage.getItem(LAST_SELECTION_KEY) : null;
      if (!raw) { return; }
      var data = JSON.parse(raw);
      if (!data || !Array.isArray(data.roots)) { return; }
      var roots = [];
      var seen = {};
      data.roots.slice(0, 20).forEach(function (item) {
        var path = item && typeof item.path === 'string' ? item.path : '';
        if (!path || seen[path]) { return; }
        seen[path] = true;
        roots.push({ path: path, name: item.name || path.split('/').filter(Boolean).pop() || path });
      });
      if (!roots.length) { return; }
      state.roots = roots;
      var rootPaths = roots.map(function (r) { return r.path; });
      state.excludes = (Array.isArray(data.excludes) ? data.excludes : []).filter(function (p) {
        return typeof p === 'string' && p && isUnderRoots(p, rootPaths);
      }).slice(0, 200);
    } catch (e) { /* localStorage 不可用或数据损坏时忽略 */ }
  }

  function saveLastSelection() {
    if (!state.selectionHydrated) { return; }
    try {
      if (!window.localStorage) { return; }
      var payload = {
        roots: (state.roots || []).map(function (r) { return { path: r.path, name: r.name }; }),
        excludes: (state.excludes || []).slice(0, 200),
        saved_at: Date.now()
      };
      window.localStorage.setItem(LAST_SELECTION_KEY, JSON.stringify(payload));
    } catch (e) { /* 忽略隐私模式/配额错误 */ }
  }

  function applyPickerSelection(picker) {
    if (picker.mode === 'subtitle') {
      // 字幕页目录选择：只回传路径，不修改扫描范围设置
      if (picker.onConfirm) { picker.onConfirm(picker.subtitleDir || ''); }
      return;
    }
    if (picker.mode === 'roots') {
      state.roots = picker.roots.map(function (r) { return { path: r.path, name: r.name }; });
      var roots = state.roots.map(function (r) { return r.path; });
      var removed = [];
      state.excludes = state.excludes.filter(function (p) {
        var keep = isUnderRoots(p, roots);
        if (!keep) { removed.push(p); }
        return keep;
      });
      if (removed.length) { toast('已自动移除 ' + removed.length + ' 个不再位于扫描范围内的排除目录。', 'info'); }
      renderRoots();
      renderExcludeChips();
    } else if (picker.mode === 'excludes') {
      state.excludes = picker.excludes.slice();
      renderExcludeChips();
      renderRoots();
    } else {
      state.pinnedPath = picker.pinned || '';
      updatePinnedDisplay();
    }
    saveLastSelection();
    updateScanSummary();
  }

  // 字幕页复用同一个共享文件夹选择器（独立模块通过 state 注入调用）
  state.openSubtitleDirPicker = function (options) {
    openFolderPicker('subtitle', options || {});
  };

  // ==================== 9. 扫描范围与选项表单 ====================
  var OPTION_ERROR_FIELDS = {
    minSize: { input: 'minSize', error: 'minSizeError' },
    maxSize: { input: 'maxSize', error: 'maxSizeError' },
    includeExt: { input: 'includeExt', error: 'includeExtError' },
    excludeExt: { input: 'excludeExt', error: 'excludeExtError' },
    pinned: { input: 'pinnedValue', error: 'pinnedError' },
    quickSample: { input: 'quickSampleKb', error: 'quickSampleError' },
    concurrency: { input: 'concurrency', error: 'concurrencyError' },
    hashRate: { input: 'hashRate', error: 'hashRateError' },
    maxFiles: { input: 'maxFiles', error: 'maxFilesError' },
    simThreshold: { input: 'simThreshold', error: 'simThresholdError' },
    simConcurrency: { input: 'simConcurrency', error: 'simConcurrencyError' },
    simMaxFile: { input: 'simMaxFileMb', error: 'simMaxFileError' },
    simExt: { input: 'simExtList', error: 'simExtError' },
    videoThreshold: { input: 'videoThreshold', error: 'videoThresholdError' },
    videoFrames: { input: 'videoSampleFrames', error: 'videoFramesError' },
    videoInterval: { input: 'videoMaxInterval', error: 'videoIntervalError' },
    videoConcurrency: { input: 'videoConcurrency', error: 'videoConcurrencyError' },
    videoMaxFile: { input: 'videoMaxFileMb', error: 'videoMaxFileError' },
    videoTimeout: { input: 'videoTimeout', error: 'videoTimeoutError' },
    videoExt: { input: 'videoExtList', error: 'videoExtError' },
    videoFfmpegPath: { input: 'videoFfmpegPath', error: 'videoFfmpegPathError' },
    videoHwaccel: { input: 'videoHwaccel', error: 'videoHwaccelError' },
    videoAsrCommand: { input: 'videoAsrCommand', error: 'videoAsrCommandError' },
    videoAsrWeight: { input: 'videoAsrWeight', error: 'videoAsrWeightError' },
    videoAsrTimeout: { input: 'videoAsrTimeout', error: 'videoAsrTimeoutError' },
    videoAsrMaxSec: { input: 'videoAsrMaxSec', error: 'videoAsrMaxSecError' },
    // v0043：排除规则
    excludeNameRegex: { input: 'excludeNameRegex', error: 'excludeNameRegexError' },
    excludeNameGlob: { input: 'excludeNameGlob', error: 'excludeNameGlobError' },
    keepRules: { input: 'keepPrimaryRule', error: 'keepRulesError' }
  };

  function renderRoots() {
    if (!els.rootsList) { return; }
    clear(els.rootsList);
    if (!state.roots.length) {
      els.rootsList.appendChild(el('div', { className: 'empty-inline', text: '尚未选择扫描目录。请点击“选择文件夹”从共享文件夹中添加。' }));
    } else {
      state.roots.forEach(function (root) {
        var removeBtn = el('button', {
          className: 'root-remove',
          attrs: { type: 'button', 'aria-label': '移除扫描目录 ' + root.path, title: '移除' },
          on: {
            click: function () {
              state.roots = state.roots.filter(function (r) { return r.path !== root.path; });
              syncExcludesWithRoots();
              renderRoots();
              renderExcludeChips();
              updateScanSummary();
            }
          }
        }, [icon('close')]);
        els.rootsList.appendChild(el('div', { className: 'root-item' }, [
          icon('folder'),
          el('span', { className: 'root-path', attrs: { title: root.path }, text: root.path }),
          removeBtn
        ]));
      });
    }
    setText(els.rootsCount, state.roots.length ? '已选择 ' + state.roots.length + ' 个目录' : '未选择');
    saveLastSelection();
  }

  function syncExcludesWithRoots() {
    var roots = state.roots.map(function (r) { return r.path; });
    var removed = [];
    state.excludes = state.excludes.filter(function (p) {
      var keep = isUnderRoots(p, roots);
      if (!keep) { removed.push(p); }
      return keep;
    });
    if (removed.length) { toast('已移除 ' + removed.length + ' 个不在扫描范围内的排除目录。', 'info'); }
  }

  function renderExcludeChips() {
    if (!els.excludeChips) { return; }
    clear(els.excludeChips);
    setText(els.excludeCount, state.excludes.length + ' 个');
    if (!state.excludes.length) {
      els.excludeChips.appendChild(el('span', { className: 'chip-empty', text: '未添加排除目录' }));
      saveLastSelection();
      return;
    }
    state.excludes.forEach(function (path) {
      var removeBtn = el('button', {
        className: 'chip-x', attrs: { type: 'button', 'aria-label': '移除排除目录 ' + path },
        on: {
          click: function () {
            state.excludes = state.excludes.filter(function (p) { return p !== path; });
            renderExcludeChips();
            updateScanSummary();
          }
        }
      }, [icon('close')]);
      els.excludeChips.appendChild(el('span', { className: 'chip', attrs: { title: path } }, [
        el('span', { className: 'chip-text', text: path }), removeBtn
      ]));
    });
    saveLastSelection();
  }

  function updatePinnedDisplay() {
    if (!els.pinnedValue) { return; }
    var empty = !state.pinnedPath;
    setText(els.pinnedValue, empty ? '未选择路径' : state.pinnedPath);
    els.pinnedValue.classList.toggle('is-empty', empty);
    els.pinnedValue.title = state.pinnedPath || '';
    if (els.pinnedError && state.pinnedPath) { hide(els.pinnedError); }
  }

  function updateKeepModeUi() {
    if (!els.keepMode) { return; }
    var mode = els.keepMode.value;
    els.pinnedField.hidden = mode !== 'pinned';
    setText(els.keepModeHint,
      mode === 'earliest' ? '保留创建时间最早的文件为“保留项”。'
        : mode === 'pinned' ? '匹配指定路径或目录前缀的副本优先保留；未匹配时按时间兜底。'
          : '保留创建时间最晚的文件为“保留项”。');
  }

  function updateScanSummary() {
    if (!els.scanSummary) { return; }
    var modeText = state.scanMode === 'similar' ? '内容相似模式' : '精准哈希模式';
    if (!state.roots.length) {
      setText(els.scanSummary, modeText + '：请选择扫描目录后开始扫描。');
      return;
    }
    var parts = [modeText, '扫描 ' + state.roots.length + ' 个目录'];
    if (state.excludes.length) { parts.push('排除 ' + state.excludes.length + ' 个目录'); }
    var mode = els.keepMode ? els.keepMode.value : 'latest';
    parts.push('保留策略：' + (KEEP_MODE_LABEL[mode] || mode));
    var extraRules = composeKeepRules('keep');
    if (extraRules.length) {
      var ruleNames = extraRules.map(function (rule) {
        var name = String(rule).split(':')[0];
        return KEEP_RULE_LABEL[name] || name;
      });
      parts.push('高级保留规则 ' + ruleNames.length + ' 条');
    }
    if (els.incrementalScan && els.incrementalScan.checked) { parts.push('增量缓存'); }
    var regexCount = textToLines(els.excludeNameRegex ? els.excludeNameRegex.value : '').length;
    var globCount = textToLines(els.excludeNameGlob ? els.excludeNameGlob.value : '').length;
    if (regexCount || globCount) { parts.push('文件名排除 ' + (regexCount + globCount) + ' 条'); }
    if (els.mediaSidecarMode && els.mediaSidecarMode.value !== 'include') { parts.push('跳过影视附属文件'); }
    if (els.hashAlgo) { parts.push(els.hashAlgo.value === 'md5' ? 'MD5 哈希' : 'SHA-256 哈希'); }
    if (state.scanMode === 'similar') {
      var threshold = parseInt(els.simThreshold ? els.simThreshold.value : '85', 10);
      parts.push('相似阈值 ' + (isFinite(threshold) ? clamp(threshold, 0, 100) : 85) + '%');
      if (els.simConcurrency && els.simConcurrency.value) { parts.push('相似并发 ' + els.simConcurrency.value); }
    }
    if (state.video && state.video.supported !== false && els.videoEnabled && els.videoEnabled.checked) {
      var frames = parseInt(els.videoSampleFrames ? els.videoSampleFrames.value : '20', 10);
      parts.push('视频视觉相似（抽样 ' + (isFinite(frames) ? frames : 20) + ' 帧）');
    }
    setText(els.scanSummary, parts.join(' · ') + '。');
  }

  // ---------- 内容相似模式：能力探测、模式切换与选项 ----------
  function similarityCapability() {
    var meta = state.meta;
    var sim = meta && meta.similarity;
    var supported = meta ? !!(sim && typeof sim === 'object') : true;
    if (sim && (sim.enabled === false || sim.supported === false)) { supported = false; }
    var defaults = (meta && meta.defaults) || {};
    var settings = state.settings || {};
    var list = [];
    if (sim && Array.isArray(sim.default_extensions) && sim.default_extensions.length) { list = sim.default_extensions.slice(); }
    if (!list.length && Array.isArray(defaults.similarity_extensions) && defaults.similarity_extensions.length) {
      list = defaults.similarity_extensions.slice();
    }
    if (!list.length && Array.isArray(settings.similarity_extensions) && settings.similarity_extensions.length) {
      list = settings.similarity_extensions.slice();
    }
    if (!list.length) { list = DEFAULT_SIMILARITY_EXTENSIONS.slice(); }
    var threshold = (sim && typeof sim.default_threshold === 'number') ? sim.default_threshold
      : (typeof defaults.similarity_threshold === 'number' ? defaults.similarity_threshold : 85);
    return {
      supported: supported,
      extensions: list,
      defaultThreshold: clamp(threshold, 0, 100),
      maxConcurrency: 4,
      binaryExtensions: (sim && Array.isArray(sim.binary_extensions)) ? sim.binary_extensions : []
    };
  }

  function updateModeAvailability() {
    var sim = state.similarity || {};
    var supported = sim.supported !== false;
    if (els.modeSimilarBtn) {
      els.modeSimilarBtn.disabled = !supported;
      els.modeSimilarBtn.title = supported ? '' : '当前后端尚未提供内容相似能力，请升级后端或联系管理员。';
    }
    if (els.modeCapabilityBadge) {
      if (!supported) {
        setText(els.modeCapabilityBadge, '后端未启用内容相似');
        els.modeCapabilityBadge.className = 'badge badge-amber';
        show(els.modeCapabilityBadge);
      } else if (state.meta && sim.extensions) {
        setText(els.modeCapabilityBadge, '相似类型 ' + sim.extensions.length + ' 种');
        els.modeCapabilityBadge.className = 'badge badge-gray';
        show(els.modeCapabilityBadge);
      } else {
        hide(els.modeCapabilityBadge);
      }
    }
    if (!supported && state.scanMode === 'similar') { setScanMode('exact', { silent: true }); }
  }

  function setScanMode(mode, options) {
    options = options || {};
    if (mode === 'similar' && state.similarity && state.similarity.supported === false) {
      if (!options.silent) { toast('当前后端未启用内容相似能力，已保持精准哈希模式。', 'error'); }
      mode = 'exact';
    }
    state.scanMode = mode === 'similar' ? 'similar' : 'exact';
    var similar = state.scanMode === 'similar';
    [[els.modeExactBtn, !similar], [els.modeSimilarBtn, similar]].forEach(function (pair) {
      var btn = pair[0];
      if (!btn) { return; }
      btn.classList.toggle('is-active', pair[1]);
      btn.setAttribute('aria-checked', pair[1] ? 'true' : 'false');
    });
    if (els.similarityOptions) { setHidden(els.similarityOptions, !similar); }
    if (els.modeDesc) {
      setText(els.modeDesc, similar
        ? '先精准哈希判重，再对允许的文件类型做内容特征相似检索；相似组必须人工复核后才能选择删除。'
        : '仅按哈希判定内容完全一致的文件，速度快、可信度高。');
    }
    if (!options.silent) { updateScanSummary(); }
  }

  function syncThresholdInputs(source) {
    if (!els.simThreshold || !els.simThresholdRange) { return; }
    var raw = source === 'range' ? els.simThresholdRange.value : els.simThreshold.value;
    if (raw === '') { return; }
    var num = parseInt(raw, 10);
    if (!isFinite(num)) { return; }
    num = clamp(num, 0, 100);
    if (source === 'range') { els.simThreshold.value = String(num); }
    else { els.simThresholdRange.value = String(num); }
  }

  function setSimilarityThreshold(value) {
    var num = clamp(parseInt(value, 10) || 0, 0, 100);
    if (els.simThreshold) { els.simThreshold.value = String(num); }
    if (els.simThresholdRange) { els.simThresholdRange.value = String(num); }
  }

  function renderExtChips(container, allExtensions, selected, onChange) {
    if (!container) { return; }
    clear(container);
    var selectedMap = {};
    (selected || []).forEach(function (ext) { selectedMap[String(ext).toLowerCase()] = true; });
    (allExtensions || []).forEach(function (ext) {
      var input = el('input', { attrs: { type: 'checkbox', value: ext } });
      input.checked = !!selectedMap[String(ext).toLowerCase()];
      var chip = el('label', { className: 'ext-chip' + (input.checked ? ' is-checked' : '') }, [input, document.createTextNode(String(ext))]);
      input.addEventListener('change', function () {
        chip.classList.toggle('is-checked', input.checked);
        if (onChange) { onChange(); }
      });
      container.appendChild(chip);
    });
  }

  function readExtChips(container) {
    var values = [];
    if (!container) { return values; }
    $$('input[type="checkbox"]', container).forEach(function (input) {
      if (input.checked) { values.push(input.value); }
    });
    return values;
  }

  function applySimilaritySettingsToForm(settings) {
    if (!settings) { settings = state.settings || DEFAULT_SETTINGS; }
    var sim = state.similarity || similarityCapability();
    var threshold = (settings.similarity_threshold === undefined || settings.similarity_threshold === null)
      ? sim.defaultThreshold : settings.similarity_threshold;
    setSimilarityThreshold(threshold);
    if (els.simConcurrency) {
      var conc = parseInt(settings.similarity_concurrency, 10);
      els.simConcurrency.value = String(clamp(isFinite(conc) ? conc : 2, 1, 4));
    }
    if (els.simMaxFileMb) {
      var maxMb = parseInt(settings.similarity_max_file_mb, 10);
      els.simMaxFileMb.value = String(clamp(isFinite(maxMb) ? maxMb : 32, 1, 1024));
    }
    if (els.simCache) { els.simCache.checked = settings.similarity_cache !== false; }
    if (els.simAllowBinary) { els.simAllowBinary.checked = settings.similarity_allow_binary === true; }
    var selected = (Array.isArray(settings.similarity_extensions) && settings.similarity_extensions.length)
      ? settings.similarity_extensions : sim.extensions;
    renderExtChips(els.simExtList, sim.extensions, selected, null);
  }

  function detectScanMode(scan) {
    if (!scan) { return 'exact'; }
    var candidates = [
      scan.scan_mode,
      scan.config && scan.config.scan_mode,
      scan.options && scan.options.scan_mode
    ];
    for (var i = 0; i < candidates.length; i += 1) {
      if (candidates[i] === 'similar') { return 'similar'; }
      if (candidates[i] === 'exact') { return 'exact'; }
    }
    return 'exact';
  }

  // ---------- 视频视觉相似（FFmpeg 抽样帧） ----------
  function videoCapability() {
    var meta = state.meta;
    var video = meta && meta.video;
    var supported = meta ? !!(video && typeof video === 'object') : true;
    var videoDefaults = (video && (video.default_settings || video.defaults)) || (meta && meta.defaults) || (state.settings || {});
    var extensions = [];
    if (video && Array.isArray(video.video_extensions) && video.video_extensions.length) {
      extensions = video.video_extensions.slice();
    } else if (Array.isArray(videoDefaults.video_extensions) && videoDefaults.video_extensions.length) {
      extensions = videoDefaults.video_extensions.slice();
    }
    if (!extensions.length) { extensions = DEFAULT_VIDEO_EXTENSIONS.slice(); }
    // v0021 P1：后端 video.ffmpeg 提供解析结果（available/path/version/source/configured_path/hwaccels）；
    // 同时兼容旧键 ffmpeg_path/ffmpeg_version/hwaccels。
    var ffmpeg = (video && video.ffmpeg && typeof video.ffmpeg === 'object') ? video.ffmpeg : {};
    var ffmpegPath = ffmpeg.path || (video && video.ffmpeg_path) || '';
    var ffmpegVersion = ffmpeg.version || (video && video.ffmpeg_version) || '';
    var hwaccels = Array.isArray(ffmpeg.hwaccels) ? ffmpeg.hwaccels.slice()
      : (video && Array.isArray(video.hwaccels) ? video.hwaccels.slice() : []);
    var ffmpegAvailable = false;
    if (typeof ffmpeg.available === 'boolean') {
      ffmpegAvailable = ffmpeg.available;
    } else if (video && typeof video.available === 'boolean') {
      ffmpegAvailable = video.available;
    } else {
      ffmpegAvailable = !!ffmpegPath && !!ffmpegVersion && ffmpegVersion !== 'unavailable';
    }
    var fallbackModes = (typeof VIDEO_HWACCEL_MODES !== 'undefined' && VIDEO_HWACCEL_MODES.length)
      ? VIDEO_HWACCEL_MODES.slice()
      : ['auto', 'off', 'vaapi', 'qsv', 'nvdec', 'videotoolbox'];
    var hwaccelModes = (video && Array.isArray(video.hwaccel_modes) && video.hwaccel_modes.length)
      ? video.hwaccel_modes.slice() : fallbackModes;
    return {
      supported: supported,
      available: supported && ffmpegAvailable,
      extensions: extensions,
      ffmpegPath: ffmpegPath,
      ffmpegVersion: ffmpegVersion,
      hwaccels: hwaccels,
      gpuHint: (video && video.gpu_hint) || '',
      cache: (video && video.cache) || '',
      ffmpegSource: ffmpeg.source || '',
      configuredPath: ffmpeg.configured_path || '',
      ffmpeg: {
        available: ffmpegAvailable,
        path: ffmpegPath,
        version: ffmpegVersion,
        source: ffmpeg.source || '',
        configuredPath: ffmpeg.configured_path || '',
        hwaccels: hwaccels
      },
      hwaccelModes: hwaccelModes,
      // hwaccel_supported 为后端归一后的“实际支持”列表（nvdec 已包含 cuda/cuvid 归一）；
      // 缺少该字段时回退到 hwaccels 与完整枚举的交集，保持旧后端可用。
      hwaccelSupported: (video && Array.isArray(video.hwaccel_supported))
        ? video.hwaccel_supported.slice() : [],
      hwaccelMode: (video && video.hwaccel_mode) || 'auto',
      hwaccelEffective: (video && video.hwaccel_effective) || '',
      hwaccelFallback: !!(video && video.hwaccel_fallback)
    };
  }

  function videoFfmpegSourceLabel(source) {
    var labels = {
      settings: '设置', env: '环境变量/内置', which: 'PATH 自动探测',
      explicit: '显式参数', unavailable: '不可用'
    };
    var key = String(source || '').toLowerCase();
    return labels[key] || key || '未知';
  }

  function videoFfmpegInfo(cap) {
    cap = cap || state.video || videoCapability();
    var ffmpeg = cap.ffmpeg || {};
    return {
      available: ffmpeg.available !== undefined ? ffmpeg.available === true : cap.available === true,
      path: ffmpeg.path || cap.ffmpegPath || '',
      version: ffmpeg.version || cap.ffmpegVersion || '',
      source: ffmpeg.source || cap.ffmpegSource || '',
      configuredPath: ffmpeg.configuredPath || cap.configuredPath || '',
      hwaccels: Array.isArray(ffmpeg.hwaccels) ? ffmpeg.hwaccels : (cap.hwaccels || []),
      supported: videoHwaccelSupported(cap)
    };
  }

  /** 当前 ffmpeg 实际支持的显式硬解模式（后端 hwaccel_supported 优先）。 */
  function videoHwaccelSupported(cap) {
    cap = cap || state.video || videoCapability();
    var modes = ['vaapi', 'qsv', 'nvdec', 'videotoolbox'];
    var supported = [];
    if (Array.isArray(cap.hwaccelSupported) && cap.hwaccelSupported.length) {
      cap.hwaccelSupported.forEach(function (name) {
        var value = String(name || '').toLowerCase();
        if (modes.indexOf(value) !== -1 && supported.indexOf(value) === -1) { supported.push(value); }
      });
      return supported;
    }
    (cap.hwaccels || []).forEach(function (name) {
      var value = String(name || '').toLowerCase();
      if (modes.indexOf(value) !== -1 && supported.indexOf(value) === -1) { supported.push(value); }
      else if ((value === 'cuda' || value === 'cuvid') && supported.indexOf('nvdec') === -1) { supported.push('nvdec'); }
    });
    return supported;
  }

  function videoFfmpegHintText() {
    var cap = state.video || videoCapability();
    var info = videoFfmpegInfo(cap);
    if (cap.supported === false) { return '后端未提供视频能力，无法配置外部 FFmpeg。'; }
    if (!cap.available) {
      return '当前解析：未找到 FFmpeg。可填写外部 FFmpeg 的绝对路径，或检查 SPK 内置路径 / 环境变量 DUP_FFMPEG_PATH。';
    }
    var text = '当前解析：' + (info.path || '—') + ' · 版本 ' + (info.version || '—') +
      ' · 来源 ' + videoFfmpegSourceLabel(info.source);
    if (info.configuredPath && info.source !== 'settings') {
      text += '；设置中的路径未被采用，已回退到其他来源。';
    }
    return text;
  }

  function videoHwaccelEffectiveFor(mode, cap) {
    cap = cap || state.video || videoCapability();
    var supported = videoHwaccelSupported(cap);
    var value = String(mode === null || mode === undefined || mode === '' ? 'auto' : mode).toLowerCase();
    if (value === 'off') { return { label: 'none', fallback: false }; }
    if (value === 'auto') {
      var autoLabel = cap.hwaccelEffective || (supported[0] || 'none');
      if (autoLabel === 'auto') { autoLabel = supported[0] || 'none'; }
      return { label: autoLabel, fallback: false };
    }
    if (supported.indexOf(value) !== -1) { return { label: value, fallback: false }; }
    return { label: 'none', fallback: true };
  }

  function videoHwaccelHintForSelect(select) {
    var cap = state.video || videoCapability();
    if (cap.supported === false) { return '后端未提供视频能力。'; }
    if (!cap.available) { return 'FFmpeg 不可用，视频视觉相似无法启用。'; }
    var modeValue = select && select.value ? select.value : 'auto';
    var effective = videoHwaccelEffectiveFor(modeValue, cap);
    var supported = videoHwaccelSupported(cap);
    var detected = supported.length
      ? supported.map(function (name) { return videoHwaccelLabel(name); }).join(' / ')
      : '未检测到受支持的硬件加速';
    var text = '实际生效：' + videoHwaccelLabel(effective.label) + ' · 支持：' + detected;
    if (effective.fallback) {
      text += '。所选模式未出现在 ffmpeg -hwaccels，运行时回退软件解码（hwaccel_fallback）。';
    }
    return text;
  }

  function renderVideoHwaccelOptions(select, selectedValue) {
    if (!select) { return; }
    var cap = state.video || videoCapability();
    var modes = (cap.hwaccelModes && cap.hwaccelModes.length) ? cap.hwaccelModes.slice() : ['auto', 'off'];
    var detected = videoHwaccelSupported(cap);
    var current = String(selectedValue === null || selectedValue === undefined || selectedValue === ''
      ? 'auto' : selectedValue).toLowerCase();
    var base = [
      { value: 'auto', label: '自动（检测到时启用硬件加速）' },
      { value: 'off', label: '关闭（仅软件解码）' }
    ];
    clear(select);
    base.forEach(function (item) {
      select.appendChild(el('option', { attrs: { value: item.value }, text: item.label }));
    });
    modes.forEach(function (mode) {
      var value = String(mode).toLowerCase();
      if (value === 'auto' || value === 'off') { return; }
      var isDetected = detected.indexOf(value) !== -1;
      var isCurrent = value === current;
      var label = videoHwaccelLabel(value) +
        (isDetected ? '（已检测到）' : '（未检测到，运行时回退软件解码）');
      var optionNode = el('option', { attrs: { value: value }, text: label });
      optionNode.disabled = !isDetected && !isCurrent;
      select.appendChild(optionNode);
    });
    select.value = current;
    if (select.value !== current) { select.value = 'auto'; }
  }

  function updateVideoFfmpegHints() {
    var text = videoFfmpegHintText();
    [els.videoFfmpegHint, els.setVideoFfmpegHint].forEach(function (hint) {
      if (!hint) { return; }
      setText(hint, text);
      hint.setAttribute('title', text);
    });
  }

  function updateVideoHwaccelHints() {
    [els.videoHwaccelHint, els.setVideoHwaccelHint].forEach(function (hint) {
      if (!hint) { return; }
      var select = hint === els.videoHwaccelHint ? els.videoHwaccel : els.setVideoHwaccel;
      var text = videoHwaccelHintForSelect(select);
      setText(hint, text);
      hint.setAttribute('title', text);
    });
  }

  function updateVideoHwaccelOptions() {
    var cap = state.video || videoCapability();
    var scanValue = state.settings && state.settings.video_hwaccel ? state.settings.video_hwaccel : 'auto';
    var settingsValue = scanValue;
    if (els.videoHwaccel) {
      if (els.videoHwaccel.value && els.videoHwaccel.options.length) { scanValue = els.videoHwaccel.value; }
      renderVideoHwaccelOptions(els.videoHwaccel, scanValue);
    }
    if (els.setVideoHwaccel) {
      if (els.setVideoHwaccel.value && els.setVideoHwaccel.options.length) { settingsValue = els.setVideoHwaccel.value; }
      renderVideoHwaccelOptions(els.setVideoHwaccel, settingsValue);
    }
    updateVideoHwaccelHints();
  }

  function videoStatusInfo() {
    var cap = state.video || videoCapability();
    if (cap.supported === false) {
      return {
        text: '后端未提供视频相似能力',
        cls: 'is-unavailable',
        hint: '当前后端未启用视频视觉相似，视频选项不可用；精准哈希与内容相似功能不受影响。'
      };
    }
    if (cap.available === false) {
      return {
        text: 'FFmpeg 不可用',
        cls: 'is-unavailable',
        hint: '未检测到可用的 FFmpeg，视频视觉相似不可用；请安装 FFmpeg 或检查 SPK 内置路径。'
      };
    }
    var details = [];
    if (cap.ffmpegVersion) { details.push('FFmpeg ' + cap.ffmpegVersion); }
    if (cap.hwaccels && cap.hwaccels.length) { details.push('硬件加速：' + cap.hwaccels.join('/')); }
    else if (cap.gpuHint === true) { details.push('检测到硬件加速提示'); }
    return {
      text: 'FFmpeg 可用',
      cls: 'is-available',
      hint: '使用 FFmpeg 抽样帧比对；' +
        (details.length ? details.join(' · ') : '未检测到硬件加速，将使用 CPU，可能较慢。') +
        ' 短视频（≤180 秒）可匹配；片段 vs 完整长视频不承诺。ASR 辅助为可选本地命令，默认关闭（无内置模型、不联网）。'
    };
  }

  function updateVideoAvailabilityUi() {
    var cap = state.video || videoCapability();
    var info = videoStatusInfo();
    [els.videoStatusBadge, els.setVideoStatusBadge].forEach(function (badge) {
      if (!badge) { return; }
      setText(badge, info.text);
      badge.className = 'badge video-status-badge ' + info.cls;
    });
    [els.videoStatusHint, els.setVideoStatusHint].forEach(function (hint) {
      if (hint) { setText(hint, info.hint); }
    });
    var canEnable = cap.supported !== false && cap.available !== false;
    if (els.videoEnabled) {
      els.videoEnabled.disabled = !canEnable;
      if (!canEnable) { els.videoEnabled.checked = false; }
    }
    if (els.setVideoEnabled) {
      els.setVideoEnabled.disabled = !canEnable;
      if (!canEnable) { els.setVideoEnabled.checked = false; }
    }
    updateVideoFieldsState();
    updateSettingsVideoFieldsState();
    updateAsrStatusUi();
    updateVideoFfmpegHints();
    updateVideoHwaccelOptions();
  }

  function updateVideoFieldsState() {
    if (!els.videoFields) { return; }
    var canUse = state.video && state.video.supported !== false && state.video.available !== false;
    els.videoFields.disabled = !(canUse && els.videoEnabled && els.videoEnabled.checked);
  }

  function updateSettingsVideoFieldsState() {
    if (!els.setVideoFields) { return; }
    var canUse = state.video && state.video.supported !== false && state.video.available !== false;
    els.setVideoFields.disabled = !(canUse && els.setVideoEnabled && els.setVideoEnabled.checked);
    // v0047：未启用音频签名时权重输入置灰（值仍保留）
    if (els.setVideoAudioWeight) {
      els.setVideoAudioWeight.disabled = !(els.setVideoAudioSignature
        && els.setVideoAudioSignature.checked);
    }
  }

  function setVideoNumberValue(input, value, fallback, min, max) {
    if (!input) { return; }
    var num = parseInt(value, 10);
    input.value = String(clamp(isFinite(num) ? num : fallback, min, max));
  }

  function applyVideoSettingsToForm(settings) {
    if (!settings) { settings = state.settings || DEFAULT_SETTINGS; }
    var cap = state.video || videoCapability();
    if (els.videoEnabled) {
      els.videoEnabled.checked = settings.video_similarity_enabled === true && cap.available !== false;
    }
    setVideoNumberValue(els.videoThreshold, settings.video_similarity_threshold, 90, 0, 100);
    setVideoNumberValue(els.videoSampleFrames, settings.video_sample_frames, 20, 5, 100);
    setVideoNumberValue(els.videoMaxInterval, settings.video_max_sample_interval_sec, 300, 60, 600);
    setVideoNumberValue(els.videoConcurrency, settings.video_concurrency, 1, 1, 2);
    setVideoNumberValue(els.videoMaxFileMb, settings.video_max_file_mb, 8192, 1, 102400);
    setVideoNumberValue(els.videoTimeout, settings.video_timeout_sec, 180, 30, 1800);
    if (els.videoFfmpegPath) { els.videoFfmpegPath.value = settings.ffmpeg_path || ''; }
    if (els.videoHwaccel) { renderVideoHwaccelOptions(els.videoHwaccel, settings.video_hwaccel || 'auto'); }
    if (els.videoCache) { els.videoCache.checked = settings.video_cache !== false; }
    var selected = (Array.isArray(settings.video_extensions) && settings.video_extensions.length)
      ? settings.video_extensions : cap.extensions;
    renderExtChips(els.videoExtList, cap.extensions, selected, null);
    applyAsrSettingsToForm(settings);
    updateVideoAvailabilityUi();
  }

  function fillVideoSettingsForm(settings) {
    if (!settings) { settings = state.settings || DEFAULT_SETTINGS; }
    var cap = state.video || videoCapability();
    if (els.setVideoEnabled) {
      els.setVideoEnabled.checked = settings.video_similarity_enabled === true && cap.available !== false;
    }
    setVideoNumberValue(els.setVideoThreshold, settings.video_similarity_threshold, 90, 0, 100);
    setVideoNumberValue(els.setVideoSampleFrames, settings.video_sample_frames, 20, 5, 100);
    setVideoNumberValue(els.setVideoMaxInterval, settings.video_max_sample_interval_sec, 300, 60, 600);
    setVideoNumberValue(els.setVideoConcurrency, settings.video_concurrency, 1, 1, 2);
    setVideoNumberValue(els.setVideoMaxMb, settings.video_max_file_mb, 8192, 1, 102400);
    setVideoNumberValue(els.setVideoTimeout, settings.video_timeout_sec, 180, 30, 1800);
    if (els.setVideoFfmpegPath) { els.setVideoFfmpegPath.value = settings.ffmpeg_path || ''; }
    if (els.setVideoHwaccel) { renderVideoHwaccelOptions(els.setVideoHwaccel, settings.video_hwaccel || 'auto'); }
    if (els.setVideoCache) { els.setVideoCache.checked = settings.video_cache !== false; }
    // v0047：边缘裁剪 / 时间轴偏移 / 音频签名
    if (els.setVideoCenterCrop) {
      els.setVideoCenterCrop.checked = settings.video_center_crop_enabled !== false;
    }
    if (els.setVideoOffsetAlign) {
      els.setVideoOffsetAlign.checked = settings.video_offset_align_enabled !== false;
    }
    if (els.setVideoAudioSignature) {
      els.setVideoAudioSignature.checked = settings.video_audio_signature_enabled === true;
    }
    if (els.setVideoAudioWeight) {
      var audioWeight = Number(settings.video_audio_weight);
      els.setVideoAudioWeight.value = String(
        isFinite(audioWeight) && audioWeight >= 0 ? audioWeight : 0.2);
    }
    updateSettingsVideoFieldsState();
    var selected = (Array.isArray(settings.video_extensions) && settings.video_extensions.length)
      ? settings.video_extensions : cap.extensions;
    renderExtChips(els.setVideoExtList, cap.extensions, selected, null);
    fillAsrSettingsForm(settings);
    updateVideoAvailabilityUi();
  }

  function readHwaccelSelect(select) {
    var value = String(select && select.value ? select.value : 'auto').toLowerCase();
    return VIDEO_HWACCEL_MODES.indexOf(value) === -1 ? 'auto' : value;
  }

  function readFfmpegPathInput(input) {
    var raw = String(input && input.value !== null && input.value !== undefined ? input.value : '').trim();
    if (!raw) { return { value: '', error: '' }; }
    if (raw.length > 512) { return { value: '', error: 'FFmpeg 路径过长（最多 512 个字符）。' }; }
    if (!/^(?:\/|[A-Za-z]:[\\/])/.test(raw)) {
      return { value: '', error: '请填写 FFmpeg 的绝对路径（以 / 开头）；留空表示使用内置/自动探测。' };
    }
    return { value: raw, error: '' };
  }

  function readVideoScanOptions(options, errors) {
    var cap = state.video || videoCapability();
    if (cap.supported === false) { return; }
    var enabled = !!(els.videoEnabled && els.videoEnabled.checked) && cap.available !== false;
    options.video_similarity_enabled = enabled;
    if (!enabled) { return; }
    var threshold = readIntOption(els.videoThreshold, 0, 100, false, 0, '视频相似度阈值');
    if (threshold.error) { errors.videoThreshold = threshold.error; } else { options.video_similarity_threshold = threshold.value; }
    var frames = readIntOption(els.videoSampleFrames, 5, 100, false, 0, '视频抽样帧数');
    if (frames.error) { errors.videoFrames = frames.error; } else { options.video_sample_frames = frames.value; }
    var interval = readIntOption(els.videoMaxInterval, 60, 600, false, 0, '最大采样间隔（秒）');
    if (interval.error) { errors.videoInterval = interval.error; } else { options.video_max_sample_interval_sec = interval.value; }
    var concurrency = readIntOption(els.videoConcurrency, 1, 2, false, 0, '视频并发数');
    if (concurrency.error) { errors.videoConcurrency = concurrency.error; } else { options.video_concurrency = concurrency.value; }
    var maxFile = readIntOption(els.videoMaxFileMb, 1, 102400, false, 0, '单视频大小上限（MB）');
    if (maxFile.error) { errors.videoMaxFile = maxFile.error; } else { options.video_max_file_mb = maxFile.value; }
    var timeout = readIntOption(els.videoTimeout, 30, 1800, false, 0, '单视频超时（秒）');
    if (timeout.error) { errors.videoTimeout = timeout.error; } else { options.video_timeout_sec = timeout.value; }
    var ffmpegPath = readFfmpegPathInput(els.videoFfmpegPath);
    if (ffmpegPath.error) { errors.videoFfmpegPath = ffmpegPath.error; }
    else { options.ffmpeg_path = ffmpegPath.value; }
    options.video_hwaccel = readHwaccelSelect(els.videoHwaccel);
    options.video_cache = !!(els.videoCache && els.videoCache.checked);
    var ext = readExtChips(els.videoExtList);
    if (!ext.length) { errors.videoExt = '请至少选择一种视频后缀。'; } else { options.video_extensions = ext; }
    readAsrScanOptions(options, errors);
  }

  function readVideoSettings(settings, errors) {
    var cap = state.video || videoCapability();
    if (cap.supported === false) { return; }
    settings.video_similarity_enabled = !!(els.setVideoEnabled && els.setVideoEnabled.checked) && cap.available !== false;
    var threshold = readIntOption(els.setVideoThreshold, 0, 100, false, 0, '视频相似度阈值');
    if (threshold.error) { errors.videoThreshold = threshold.error; } else { settings.video_similarity_threshold = threshold.value; }
    var frames = readIntOption(els.setVideoSampleFrames, 5, 100, false, 0, '视频抽样帧数');
    if (frames.error) { errors.videoFrames = frames.error; } else { settings.video_sample_frames = frames.value; }
    var interval = readIntOption(els.setVideoMaxInterval, 60, 600, false, 0, '最大采样间隔（秒）');
    if (interval.error) { errors.videoInterval = interval.error; } else { settings.video_max_sample_interval_sec = interval.value; }
    var concurrency = readIntOption(els.setVideoConcurrency, 1, 2, false, 0, '视频并发数');
    if (concurrency.error) { errors.videoConcurrency = concurrency.error; } else { settings.video_concurrency = concurrency.value; }
    var maxFile = readIntOption(els.setVideoMaxMb, 1, 102400, false, 0, '单视频大小上限（MB）');
    if (maxFile.error) { errors.videoMaxFile = maxFile.error; } else { settings.video_max_file_mb = maxFile.value; }
    var timeout = readIntOption(els.setVideoTimeout, 30, 1800, false, 0, '单视频超时（秒）');
    if (timeout.error) { errors.videoTimeout = timeout.error; } else { settings.video_timeout_sec = timeout.value; }
    var ffmpegPath = readFfmpegPathInput(els.setVideoFfmpegPath);
    if (ffmpegPath.error) { errors.videoFfmpegPath = ffmpegPath.error; }
    else { settings.ffmpeg_path = ffmpegPath.value; }
    settings.video_hwaccel = readHwaccelSelect(els.setVideoHwaccel);
    settings.video_cache = !!(els.setVideoCache && els.setVideoCache.checked);
    // v0047：边缘裁剪 / 时间轴偏移 / 音频签名
    settings.video_center_crop_enabled = els.setVideoCenterCrop
      ? !!els.setVideoCenterCrop.checked : true;
    settings.video_offset_align_enabled = els.setVideoOffsetAlign
      ? !!els.setVideoOffsetAlign.checked : true;
    settings.video_audio_signature_enabled = !!(els.setVideoAudioSignature
      && els.setVideoAudioSignature.checked);
    var audioWeightRaw = String(els.setVideoAudioWeight
      ? els.setVideoAudioWeight.value : '').trim();
    var audioWeight = parseFloat(audioWeightRaw);
    if (audioWeightRaw === '' || !isFinite(audioWeight)
        || audioWeight < 0 || audioWeight > 0.5) {
      errors.videoAudioWeight = '音频签名权重需在 0–0.5 之间。';
    } else {
      settings.video_audio_weight = Math.round(audioWeight * 100) / 100;
    }
    var ext = readExtChips(els.setVideoExtList);
    if (!ext.length) { errors.videoExt = '请至少选择一种视频后缀。'; } else { settings.video_extensions = ext; }
    readAsrSettings(settings, errors);
  }

  function videoScanEnabledForResults() {
    if (state.video && state.video.supported === false) { return false; }
    var stats = currentScanStats();
    if (Number(stats.video_groups) > 0 || Number(stats.video_candidates) > 0 ||
        Number(stats.video_ffmpeg_used) > 0) { return true; }
    var scan = state.scan.data;
    if (!scan) { return false; }
    var options = scan.options || {};
    var config = scan.config || {};
    return options.video_similarity_enabled === true || config.video_similarity_enabled === true;
  }

  function fmtVideoDuration(seconds) {
    var num = Number(seconds);
    if (!isFinite(num) || num <= 0) { return '—'; }
    num = Math.round(num);
    var hours = Math.floor(num / 3600);
    var minutes = Math.floor((num % 3600) / 60);
    var secs = num % 60;
    if (hours > 0) { return hours + ' 小时 ' + minutes + ' 分'; }
    if (minutes > 0) { return minutes + ' 分 ' + secs + ' 秒'; }
    return secs + ' 秒';
  }

  function videoHwaccelLabel(value) {
    var key = String(value || '').toLowerCase();
    return VIDEO_HWACCEL_LABEL[key] || key || '—';
  }

  function videoSkippedCount(stats) {
    var skipped = stats && stats.video_skipped;
    if (!skipped || typeof skipped !== 'object') { return 0; }
    var total = 0;
    Object.keys(skipped).forEach(function (key) { total += Number(skipped[key]) || 0; });
    return total;
  }

  // ---------- ASR 语音辅助（可选外部本地命令；默认关闭） ----------
  function asrCapability() {
    var video = state.meta && state.meta.video;
    var asr = video && video.asr;
    var supported = !!(asr && typeof asr === 'object');
    var enabled = supported ? asr.enabled !== false : false;
    var available = false;
    var configured = false;
    var reason = '';
    if (supported) {
      if (typeof asr.available === 'boolean') { available = asr.available; }
      if (typeof asr.configured === 'boolean') { configured = asr.configured; }
      if (typeof asr.available !== 'boolean' && typeof asr.configured === 'boolean') { available = configured; }
      if (typeof asr.configured !== 'boolean' && typeof asr.available === 'boolean') { configured = available; }
      if (asr.reason) { reason = String(asr.reason); }
      else if (asr.message) { reason = String(asr.message); }
    }
    var status = 'unsupported';
    var text = '后端未提供 ASR';
    var cls = 'is-muted';
    if (supported) {
      if (!enabled) {
        status = 'disabled';
        text = 'ASR 未启用（默认关闭）';
        cls = 'is-muted';
      } else if (available) {
        status = 'configured';
        text = 'ASR 已配置（本地命令）';
        cls = 'is-ok';
      } else if (configured || asr.command) {
        status = 'unavailable';
        text = 'ASR 不可用';
        cls = 'is-warn';
      } else {
        status = 'not-configured';
        text = 'ASR 未配置';
        cls = 'is-warn';
      }
    }
    return {
      supported: supported,
      enabled: enabled,
      available: available,
      configured: configured,
      status: status,
      text: text,
      cls: cls,
      reason: reason,
      weight: (supported && typeof asr.weight === 'number') ? clamp(asr.weight, 0, 0.5) : 0.15
    };
  }

  function updateAsrStatusUi() {
    var cap = asrCapability();
    [els.videoAsrStatusBadge, els.setVideoAsrStatusBadge].forEach(function (badge) {
      if (!badge) { return; }
      setText(badge, cap.text);
      badge.className = 'badge asr-status-badge ' + cap.cls;
    });
    var hintText = 'ASR 仅调用 NAS 本地外部命令（不附带模型、不联网）；转录文本只参与相似度评分，不会在界面展示或写入结果。';
    if (!cap.supported) {
      hintText = '当前后端未提供 ASR 辅助能力；ASR 选项不可用，视频视觉相似不受影响。';
    } else if (cap.status === 'not-configured') {
      hintText = 'ASR 未配置：请填写本地 ASR 命令模板。未配置时视频扫描仍可正常进行（纯视觉评分），不阻塞扫描。';
    } else if (cap.status === 'unavailable') {
      hintText = 'ASR 不可用' + (cap.reason ? '：' + cap.reason : '') +
        '；视频扫描将自动回退纯视觉评分，不会失败。请检查本地命令/模型后再启用。';
    } else if (cap.status === 'configured') {
      hintText = 'ASR 已配置：使用本地命令生成文本签名参与评分；转录内容不展示、不回传前端。';
    } else if (cap.status === 'disabled') {
      hintText = 'ASR 未启用（默认关闭）。可填写本地命令模板并启用；不附带模型、不联网。';
    }
    [els.videoAsrStatusHint, els.setVideoAsrStatusHint].forEach(function (hint) {
      if (hint) { setText(hint, hintText); }
    });
    updateAsrFieldsState();
  }

  function updateAsrFieldsState() {
    var cap = asrCapability();
    var scanVideoOn = !!(els.videoEnabled && els.videoEnabled.checked);
    var scanAsrOn = !!(els.videoAsrEnabled && els.videoAsrEnabled.checked);
    var scanCanEnable = cap.supported && scanVideoOn && cap.status !== 'unavailable';
    if (els.videoAsrEnabled) { els.videoAsrEnabled.disabled = !scanCanEnable; }
    if (els.videoAsrFields) {
      els.videoAsrFields.disabled = !(scanCanEnable && scanAsrOn);
    }
    var settingsVideoOn = !!(els.setVideoEnabled && els.setVideoEnabled.checked);
    var settingsAsrOn = !!(els.setVideoAsrEnabled && els.setVideoAsrEnabled.checked);
    var settingsCanEnable = cap.supported && settingsVideoOn && cap.status !== 'unavailable';
    if (els.setVideoAsrEnabled) { els.setVideoAsrEnabled.disabled = !settingsCanEnable; }
    if (els.setVideoAsrFields) {
      els.setVideoAsrFields.disabled = !(settingsCanEnable && settingsAsrOn);
    }
  }

  function setDecimalField(input, value, fallback, min, max) {
    if (!input) { return; }
    var num = parseFloat(value);
    if (!isFinite(num)) { num = fallback; }
    num = clamp(num, min, max);
    input.value = String(Math.round(num * 100) / 100);
  }

  function applyAsrSettingsToForm(settings) {
    if (!settings) { settings = state.settings || DEFAULT_SETTINGS; }
    var cap = asrCapability();
    var enabled = cap.supported && settings.video_asr_enabled === true && cap.status !== 'unavailable';
    if (els.videoAsrEnabled) { els.videoAsrEnabled.checked = enabled; }
    if (els.videoAsrCommand) { els.videoAsrCommand.value = settings.video_asr_command || ''; }
    setDecimalField(els.videoAsrWeight, settings.video_asr_weight, 0.15, 0, 0.5);
    setVideoNumberValue(els.videoAsrTimeout, settings.video_asr_timeout_sec, 300, 30, 1800);
    setVideoNumberValue(els.videoAsrMaxSec, settings.video_asr_max_sec, 600, 30, 3600);
    updateAsrStatusUi();
  }

  function fillAsrSettingsForm(settings) {
    if (!settings) { settings = state.settings || DEFAULT_SETTINGS; }
    var cap = asrCapability();
    var enabled = cap.supported && settings.video_asr_enabled === true && cap.status !== 'unavailable';
    if (els.setVideoAsrEnabled) { els.setVideoAsrEnabled.checked = enabled; }
    if (els.setVideoAsrCommand) { els.setVideoAsrCommand.value = settings.video_asr_command || ''; }
    setDecimalField(els.setVideoAsrWeight, settings.video_asr_weight, 0.15, 0, 0.5);
    setVideoNumberValue(els.setVideoAsrTimeout, settings.video_asr_timeout_sec, 300, 30, 1800);
    setVideoNumberValue(els.setVideoAsrMaxSec, settings.video_asr_max_sec, 600, 30, 3600);
    updateAsrStatusUi();
  }

  function readFloatOption(input, min, max, label) {
    var raw = String(input && input.value !== null && input.value !== undefined ? input.value : '').trim();
    if (!raw) { return { value: null, error: label + '不能为空。' }; }
    if (!/^\d+(\.\d+)?$/.test(raw)) { return { value: null, error: label + '必须是数字（可带小数）。' }; }
    var num = parseFloat(raw);
    if (!isFinite(num) || num < min || num > max) {
      return { value: null, error: label + '必须在 ' + min + ' 到 ' + max + ' 之间。' };
    }
    return { value: num, error: '' };
  }

  function readAsrScanOptions(options, errors) {
    var cap = asrCapability();
    if (!cap.supported) { return; }
    if (!(els.videoEnabled && els.videoEnabled.checked)) { return; }
    var enabled = !!(els.videoAsrEnabled && els.videoAsrEnabled.checked) && cap.status !== 'unavailable';
    options.video_asr_enabled = enabled;
    if (!enabled) { return; }
    var command = els.videoAsrCommand ? els.videoAsrCommand.value.trim() : '';
    if (command.length > 2000) {
      errors.videoAsrCommand = 'ASR 命令模板最多 2000 个字符。';
    } else {
      options.video_asr_command = command;
    }
    var weight = readFloatOption(els.videoAsrWeight, 0, 0.5, 'ASR 文本分数权重');
    if (weight.error) { errors.videoAsrWeight = weight.error; } else { options.video_asr_weight = weight.value; }
    var timeout = readIntOption(els.videoAsrTimeout, 30, 1800, false, 0, 'ASR 单次超时（秒）');
    if (timeout.error) { errors.videoAsrTimeout = timeout.error; } else { options.video_asr_timeout_sec = timeout.value; }
    var maxSec = readIntOption(els.videoAsrMaxSec, 30, 3600, false, 0, 'ASR 最大处理时长（秒）');
    if (maxSec.error) { errors.videoAsrMaxSec = maxSec.error; } else { options.video_asr_max_sec = maxSec.value; }
  }

  function readAsrSettings(settings, errors) {
    var cap = asrCapability();
    if (!cap.supported) { return; }
    settings.video_asr_enabled = !!(els.setVideoAsrEnabled && els.setVideoAsrEnabled.checked) && cap.status !== 'unavailable';
    var command = els.setVideoAsrCommand ? els.setVideoAsrCommand.value.trim() : '';
    if (command.length > 2000) {
      errors.videoAsrCommand = 'ASR 命令模板最多 2000 个字符。';
    } else {
      settings.video_asr_command = command;
    }
    var weight = readFloatOption(els.setVideoAsrWeight, 0, 0.5, 'ASR 文本分数权重');
    if (weight.error) { errors.videoAsrWeight = weight.error; } else { settings.video_asr_weight = weight.value; }
    var timeout = readIntOption(els.setVideoAsrTimeout, 30, 1800, false, 0, 'ASR 单次超时（秒）');
    if (timeout.error) { errors.videoAsrTimeout = timeout.error; } else { settings.video_asr_timeout_sec = timeout.value; }
    var maxSec = readIntOption(els.setVideoAsrMaxSec, 30, 3600, false, 0, 'ASR 最大处理时长（秒）');
    if (maxSec.error) { errors.videoAsrMaxSec = maxSec.error; } else { settings.video_asr_max_sec = maxSec.value; }
  }

  function firstFiniteNumber(values) {
    for (var i = 0; i < values.length; i += 1) {
      var num = Number(values[i]);
      if (values[i] !== null && values[i] !== undefined && values[i] !== '' && isFinite(num)) {
        return num;
      }
    }
    return null;
  }

  // ---------- 云端字幕（OpenAI 兼容 ASR/LLM；默认关闭，API Key 不回显） ----------
  function subtitleCapability() {
    return state.subtitle || {
      supported: false, enabled: false, asrKeySet: false, llmKeySet: false,
      asrLanguage: 'auto', translateEnabled: false, targetLang: 'zh',
      writeVtt: false
    };
  }

  function subtitleHasKey(obj, key) {
    return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
  }

  function updateSubtitleFromRaw(rawSettings) {
    var raw = rawSettings || null;
    var supported = !!raw && (
      subtitleHasKey(raw, 'subtitle_enabled') ||
      subtitleHasKey(raw, 'subtitle_asr_base_url') ||
      subtitleHasKey(raw, 'subtitle_asr_api_key_set')
    );
    state.subtitle = {
      supported: supported,
      enabled: supported && raw.subtitle_enabled === true,
      asrKeySet: supported && raw.subtitle_asr_api_key_set === true,
      llmKeySet: supported && raw.subtitle_llm_api_key_set === true,
      asrLanguage: (raw && raw.subtitle_asr_language) || 'auto',
      translateEnabled: supported && raw.subtitle_translate_enabled === true,
      targetLang: (raw && raw.subtitle_translate_target) || 'zh',
      writeVtt: supported && raw.subtitle_write_vtt === true
    };
    updateSubtitleAvailabilityUi();
    refreshSubtitleGenerateButtons();
  }

  function subtitleReady() {
    var sub = subtitleCapability();
    return sub.supported === true && sub.enabled === true && sub.asrKeySet === true;
  }

  function subtitleUnavailableReason() {
    var sub = subtitleCapability();
    if (sub.supported !== true) { return '当前后端未提供云端字幕能力'; }
    if (sub.enabled !== true) { return '云端字幕未启用（请在设置中开启）'; }
    if (sub.asrKeySet !== true) { return '未配置 ASR API Key（请在设置中配置）'; }
    return '';
  }

  function updateSubtitleAvailabilityUi() {
    var sub = subtitleCapability();
    var badge = els.subtitleStatusBadge;
    if (badge) {
      var text = '后端未提供云端字幕';
      var cls = 'is-muted';
      if (sub.supported) {
        if (!sub.enabled) { text = '未启用（默认关闭）'; cls = 'is-muted'; }
        else if (!sub.asrKeySet) { text = '已启用 · 未配置 ASR Key'; cls = 'is-warn'; }
        else { text = '已启用 · ASR 已配置'; cls = 'is-ok'; }
      }
      setText(badge, text);
      badge.className = 'badge subtitle-status-badge ' + cls;
    }
    if (els.subtitleStatusHint) {
      var hint = '使用 OpenAI 兼容的云端 ASR / LLM 服务生成字幕；音频会上传到第三方。API Key 只写不读，GET 不会回传明文。';
      if (!sub.supported) {
        hint = '当前后端未提供云端字幕能力；该设置不可用，视频查重与视频相似功能不受影响。';
      } else if (!sub.enabled) {
        hint = '云端字幕默认关闭；启用并配置 ASR API Key 后，视频结果行和「字幕」页都会出现批量生成入口。未启用不影响视频查重。';
      } else if (!sub.asrKeySet) {
        hint = '已启用但未配置 ASR API Key：生成按钮保持禁用。API Key 只写不读，保存后仅显示“已配置”状态。';
      } else if (!sub.llmKeySet) {
        hint = 'ASR 已配置；LLM API Key 未配置，弹窗中“生成译文”将不可用。音频会上传到所配置的第三方服务。';
      }
      setText(els.subtitleStatusHint, hint);
    }
    if (els.subtitleFields) { els.subtitleFields.disabled = !sub.supported; }
    if (els.subtitleAsrKeySetBadge) {
      setText(els.subtitleAsrKeySetBadge, sub.asrKeySet ? '已配置' : '未配置');
      els.subtitleAsrKeySetBadge.className = 'badge ' + (sub.asrKeySet ? 'badge-green' : 'badge-gray');
    }
    if (els.subtitleLlmKeySetBadge) {
      setText(els.subtitleLlmKeySetBadge, sub.llmKeySet ? '已配置' : '未配置');
      els.subtitleLlmKeySetBadge.className = 'badge ' + (sub.llmKeySet ? 'badge-green' : 'badge-gray');
    }
    if (els.subtitleAsrApiKey) {
      els.subtitleAsrApiKey.placeholder = sub.asrKeySet ? '已配置（留空保持原值，勾选清除可删除）' : '未配置（留空保持原值）';
    }
    if (els.subtitleLlmApiKey) {
      els.subtitleLlmApiKey.placeholder = sub.llmKeySet ? '已配置（留空保持原值，勾选清除可删除）' : '未配置（留空保持原值）';
    }
  }

  function refreshSubtitleGenerateButtons() {
    $$('.subtitle-generate-btn').forEach(function (btn) {
      var ready = subtitleReady();
      btn.disabled = !ready;
      btn.title = ready ? '生成云端字幕（音频将上传到第三方服务）' : subtitleUnavailableReason();
    });
    updateBatchSubtitleButton();
  }

  function fillSubtitleSettingsForm(settings) {
    var s = settings || {};
    if (els.subtitleEnabled) { els.subtitleEnabled.checked = s.subtitle_enabled === true; }
    if (els.subtitleAsrBaseUrl) { els.subtitleAsrBaseUrl.value = s.subtitle_asr_base_url || ''; }
    if (els.subtitleAsrModel) { els.subtitleAsrModel.value = s.subtitle_asr_model || ''; }
    if (els.subtitleAsrLanguage) { fillLanguageSelect(els.subtitleAsrLanguage, s.subtitle_asr_language, 'auto'); }
    // v0032：音频来源 nas（默认，由 NAS 转音频后上传）| server（按段上传原视频给 ASR 服务端转音频）
    if (els.subtitleAudioSource) {
      els.subtitleAudioSource.value = s.subtitle_audio_source === 'server' ? 'server' : 'nas';
    }
    if (els.subtitleWriteVtt) {
      els.subtitleWriteVtt.checked = s.subtitle_write_vtt === true;
    }
    // v0040：分片音频预取（默认开；显式 false 才关闭以兼容旧设置）
    if (els.subtitleAudioPrefetch) {
      els.subtitleAudioPrefetch.checked = s.subtitle_audio_prefetch !== false;
    }
    setVideoNumberValue(els.subtitleAsrTimeout, s.subtitle_asr_timeout_sec, 300, 30, 1800);
    setVideoNumberValue(els.subtitleAsrChunk, s.subtitle_asr_chunk_sec, 600, 60, 1800);
    setVideoNumberValue(els.subtitleAsrMaxAudioMb, s.subtitle_asr_max_audio_mb, 24, 1, 100);
    if (els.subtitleTranslateEnabled) { els.subtitleTranslateEnabled.checked = s.subtitle_translate_enabled === true; }
    if (els.subtitleLlmBaseUrl) { els.subtitleLlmBaseUrl.value = s.subtitle_llm_base_url || ''; }
    if (els.subtitleLlmModel) { els.subtitleLlmModel.value = s.subtitle_llm_model || ''; }
    if (els.subtitleTranslateTarget) { fillLanguageSelect(els.subtitleTranslateTarget, s.subtitle_translate_target, 'zh'); }
    setVideoNumberValue(els.subtitleTranslateTimeout, s.subtitle_translate_timeout_sec, 120, 30, 900);
    setVideoNumberValue(els.subtitleTranslateBatchSize, s.subtitle_translate_batch_size, 20, 1, 40);
    if (els.subtitleLlmDisableThinking) {
      els.subtitleLlmDisableThinking.checked = s.subtitle_llm_disable_thinking === true;
    }
    if (els.setSubtitleOutputMode) {
      els.setSubtitleOutputMode.value = s.subtitle_output_mode === 'download_only'
        ? 'download_only' : 'alongside_video';
    }
    if (els.setSubtitleSkipExisting) {
      els.setSubtitleSkipExisting.checked = s.subtitle_skip_existing !== false;
    }
    // v0036：译文输出组合（both/target_only/bilingual_only，默认 both）
    if (els.setSubtitleTranslateOutputs) {
      var translateOutputs = s.subtitle_translate_outputs;
      els.setSubtitleTranslateOutputs.value =
        (translateOutputs === 'target_only' || translateOutputs === 'bilingual_only')
          ? translateOutputs : 'both';
    }
    setVideoNumberValue(els.setSubtitleJobLimit, s.subtitle_job_limit, 200, 20, 1000);
    // v0034/v0037：同时执行的字幕任务数（1..4，默认 1 串行；保存后立即生效，无需重启）
    setVideoNumberValue(els.setSubtitleConcurrency, s.subtitle_concurrency, 1, 1, 4);
    // v0048：自动重试 / 负载阈值 / 目录优先级 / 音轨 / 目录监控
    if (els.setSubtitleAutoRetry) {
      els.setSubtitleAutoRetry.checked = s.subtitle_auto_retry_enabled !== false;
    }
    setVideoNumberValue(els.setSubtitleAutoRetryMax, s.subtitle_auto_retry_max, 1, 0, 3);
    if (els.setSubtitleNoSpeechSkip) {
      els.setSubtitleNoSpeechSkip.checked = s.subtitle_no_speech_skip !== false;
    }
    if (els.setSubtitleMaxLoad) {
      var maxLoad = Number(s.subtitle_max_load);
      els.setSubtitleMaxLoad.value = String(isFinite(maxLoad) && maxLoad > 0 ? maxLoad : 0);
    }
    if (els.setSubtitleDirPriorities) {
      els.setSubtitleDirPriorities.value = linesToText(s.subtitle_dir_priorities);
    }
    if (els.setSubtitleAudioTrack) {
      els.setSubtitleAudioTrack.value = s.subtitle_audio_track || '';
    }
    if (els.setSubtitleAudioTrackLanguage) {
      els.setSubtitleAudioTrackLanguage.value = s.subtitle_audio_track_language || '';
    }
    if (els.setSubtitleWatchEnabled) {
      els.setSubtitleWatchEnabled.checked = s.subtitle_watch_enabled === true;
    }
    if (els.setSubtitleWatchDirs) {
      els.setSubtitleWatchDirs.value = linesToText(s.subtitle_watch_dirs);
    }
    setVideoNumberValue(els.setSubtitleWatchInterval, s.subtitle_watch_interval_sec, 300, 60, 86400);
    if (els.subtitleAsrApiKey) { els.subtitleAsrApiKey.value = ''; }
    if (els.subtitleLlmApiKey) { els.subtitleLlmApiKey.value = ''; }
    if (els.subtitleClearAsrKey) { els.subtitleClearAsrKey.checked = false; }
    if (els.subtitleClearLlmKey) { els.subtitleClearLlmKey.checked = false; }
    if (els.subtitleAsrApiKey) { els.subtitleAsrApiKey.disabled = false; }
    if (els.subtitleLlmApiKey) { els.subtitleLlmApiKey.disabled = false; }
    clearSubtitleProbeUi('asr');
    clearSubtitleProbeUi('llm');
    updateSubtitleAvailabilityUi();
  }

  function normalizeSubtitleUrl(raw, label, errors, key) {
    var text = String(raw || '').trim().replace(/\/+$/, '');
    if (!text) { errors[key] = label + '不能为空。'; return ''; }
    if (!/^https?:\/\/[^\s]+$/i.test(text)) {
      errors[key] = label + ' 必须是 http:// 或 https:// 开头的地址。';
      return '';
    }
    return text;
  }

  function readSubtitleInt(input, min, max, label, errors, key) {
    var raw = String(input && input.value !== null && input.value !== undefined ? input.value : '').trim();
    if (!raw || !/^\d+$/.test(raw)) { errors[key] = label + '必须是整数。'; return null; }
    var num = parseInt(raw, 10);
    if (!isFinite(num) || num < min || num > max) {
      errors[key] = label + '必须在 ' + min + ' 到 ' + max + ' 之间。';
      return null;
    }
    return num;
  }

  function readSubtitleSettings(settings, errors) {
    var sub = subtitleCapability();
    if (!sub.supported) { return; }
    settings.subtitle_enabled = !!(els.subtitleEnabled && els.subtitleEnabled.checked);
    settings.subtitle_asr_base_url = normalizeSubtitleUrl(
      els.subtitleAsrBaseUrl ? els.subtitleAsrBaseUrl.value : '',
      'ASR Base URL', errors, 'subtitleAsrBaseUrl');
    var asrModel = String(els.subtitleAsrModel ? els.subtitleAsrModel.value : '').trim();
    if (!asrModel) { errors.subtitleAsrModel = 'ASR 模型不能为空。'; }
    else if (asrModel.length > 200) { errors.subtitleAsrModel = 'ASR 模型名称过长。'; }
    else { settings.subtitle_asr_model = asrModel; }
    if (els.subtitleAsrLanguage) {
      var asrLanguage = String(els.subtitleAsrLanguage.value || '').trim();
      settings.subtitle_asr_language = SUBTITLE_LANGUAGE_RE.test(asrLanguage)
        ? asrLanguage : 'auto';
    }
    // v0032：音频来源只接受 nas / server，缺失或非法值一律回落到 nas（默认）；
    // server 表示把原视频按段上传给 ASR 服务器转音频（更快，但会上传原视频）。
    var audioSource = els.subtitleAudioSource
      ? String(els.subtitleAudioSource.value || '').trim()
      : '';
    settings.subtitle_audio_source = audioSource === 'server' ? 'server' : 'nas';
    settings.subtitle_write_vtt = !!(els.subtitleWriteVtt && els.subtitleWriteVtt.checked);
    settings.subtitle_audio_prefetch = !!(els.subtitleAudioPrefetch && els.subtitleAudioPrefetch.checked);
    var asrTimeout = readSubtitleInt(els.subtitleAsrTimeout, 30, 1800, 'ASR 超时', errors, 'subtitleAsrTimeout');
    if (asrTimeout !== null) { settings.subtitle_asr_timeout_sec = asrTimeout; }
    var asrChunk = readSubtitleInt(els.subtitleAsrChunk, 60, 1800, '音频分块', errors, 'subtitleAsrChunk');
    if (asrChunk !== null) { settings.subtitle_asr_chunk_sec = asrChunk; }
    var asrMaxAudio = readSubtitleInt(els.subtitleAsrMaxAudioMb, 1, 100, '单块音频上限', errors, 'subtitleAsrMaxAudioMb');
    if (asrMaxAudio !== null) { settings.subtitle_asr_max_audio_mb = asrMaxAudio; }
    settings.subtitle_translate_enabled = !!(els.subtitleTranslateEnabled && els.subtitleTranslateEnabled.checked);
    settings.subtitle_llm_base_url = normalizeSubtitleUrl(
      els.subtitleLlmBaseUrl ? els.subtitleLlmBaseUrl.value : '',
      'LLM Base URL', errors, 'subtitleLlmBaseUrl');
    var llmModel = String(els.subtitleLlmModel ? els.subtitleLlmModel.value : '').trim();
    if (!llmModel) { errors.subtitleLlmModel = 'LLM 模型不能为空。'; }
    else if (llmModel.length > 200) { errors.subtitleLlmModel = 'LLM 模型名称过长。'; }
    else { settings.subtitle_llm_model = llmModel; }
    if (els.subtitleTranslateTarget) {
      var translateTarget = String(els.subtitleTranslateTarget.value || '').trim();
      settings.subtitle_translate_target =
        (SUBTITLE_LANGUAGE_RE.test(translateTarget)
          && translateTarget.toLowerCase() !== 'auto')
          ? translateTarget : 'zh';
    }
    var translateTimeout = readSubtitleInt(els.subtitleTranslateTimeout, 30, 900, '翻译超时', errors, 'subtitleTranslateTimeout');
    if (translateTimeout !== null) { settings.subtitle_translate_timeout_sec = translateTimeout; }
    var batchSize = readSubtitleInt(els.subtitleTranslateBatchSize, 1, 40, '翻译批大小', errors, 'subtitleTranslateBatchSize');
    if (batchSize !== null) { settings.subtitle_translate_batch_size = batchSize; }
    // v0030：本地推理模型可关闭“思考”（默认关，避免不兼容服务拒收）
    settings.subtitle_llm_disable_thinking = !!(els.subtitleLlmDisableThinking && els.subtitleLlmDisableThinking.checked);
    var jobLimit = readSubtitleInt(els.setSubtitleJobLimit, 20, 1000, '字幕任务保留上限', errors, 'setSubtitleJobLimit');
    if (jobLimit !== null) { settings.subtitle_job_limit = jobLimit; }
    // v0034/v0037：字幕并发任务数（1..4，默认 1；保存后立即生效，无需重启套件）
    var subtitleConcurrency = readSubtitleInt(els.setSubtitleConcurrency, 1, 4, '字幕并发任务数', errors, 'setSubtitleConcurrency');
    if (subtitleConcurrency !== null) { settings.subtitle_concurrency = subtitleConcurrency; }
    // v0048：自动重试 / 负载阈值 / 目录优先级 / 音轨 / 目录监控
    settings.subtitle_auto_retry_enabled = !!(els.setSubtitleAutoRetry
      && els.setSubtitleAutoRetry.checked);
    var autoRetryMax = readSubtitleInt(els.setSubtitleAutoRetryMax, 0, 3,
      '自动重试次数上限', errors, 'setSubtitleAutoRetryMax');
    if (autoRetryMax !== null) { settings.subtitle_auto_retry_max = autoRetryMax; }
    settings.subtitle_no_speech_skip = !!(els.setSubtitleNoSpeechSkip
      && els.setSubtitleNoSpeechSkip.checked);
    var maxLoadRaw = String(els.setSubtitleMaxLoad ? els.setSubtitleMaxLoad.value : '0').trim();
    var maxLoad = parseFloat(maxLoadRaw || '0');
    if (!isFinite(maxLoad) || maxLoad < 0 || maxLoad > 256) {
      errors.subtitleMaxLoad = '负载阈值需在 0–256 之间（0 = 不限制）。';
    } else {
      settings.subtitle_max_load = Math.round(maxLoad * 100) / 100;
    }
    var dirPriorities = textToLines(els.setSubtitleDirPriorities
      ? els.setSubtitleDirPriorities.value : '');
    var badDirPriority = '';
    dirPriorities.forEach(function (item) {
      if (!badDirPriority && !isAbsolutePath(item)) { badDirPriority = item; }
    });
    if (badDirPriority) {
      errors.subtitleDirPriorities = '目录优先级必须是绝对路径：' + badDirPriority;
    } else if (dirPriorities.length > 50) {
      errors.subtitleDirPriorities = '目录优先级最多 50 条。';
    } else {
      settings.subtitle_dir_priorities = dirPriorities;
    }
    var audioTrack = String(els.setSubtitleAudioTrack
      ? els.setSubtitleAudioTrack.value : '').trim().toLowerCase();
    if (!audioTrack || audioTrack === 'auto' || audioTrack === 'default') {
      settings.subtitle_audio_track = '';
    } else if (!/^\d+$/.test(audioTrack) || parseInt(audioTrack, 10) > 31) {
      errors.subtitleAudioTrack = '默认音轨只能是 auto 或 0–31 的序号。';
    } else {
      settings.subtitle_audio_track = audioTrack;
    }
    var audioLang = String(els.setSubtitleAudioTrackLanguage
      ? els.setSubtitleAudioTrackLanguage.value : '').trim();
    if (audioLang && !/^[A-Za-z]{2,8}([_-][A-Za-z0-9]{1,8})?$/.test(audioLang)) {
      errors.subtitleAudioTrackLanguage = '音轨语言标签需形如 jpn / chi / eng / zh-CN。';
    } else {
      settings.subtitle_audio_track_language = audioLang;
    }
    settings.subtitle_watch_enabled = !!(els.setSubtitleWatchEnabled
      && els.setSubtitleWatchEnabled.checked);
    var watchDirs = textToLines(els.setSubtitleWatchDirs
      ? els.setSubtitleWatchDirs.value : '');
    var badWatchDir = '';
    watchDirs.forEach(function (item) {
      if (!badWatchDir && !isAbsolutePath(item)) { badWatchDir = item; }
    });
    if (badWatchDir) {
      errors.subtitleWatchDirs = '监控目录必须是绝对路径：' + badWatchDir;
    } else if (watchDirs.length > 50) {
      errors.subtitleWatchDirs = '监控目录最多 50 条。';
    } else {
      settings.subtitle_watch_dirs = watchDirs;
    }
    var watchInterval = readSubtitleInt(els.setSubtitleWatchInterval, 60, 86400,
      '监控扫描间隔', errors, 'setSubtitleWatchInterval');
    if (watchInterval !== null) { settings.subtitle_watch_interval_sec = watchInterval; }
    if (els.setSubtitleOutputMode) {
      settings.subtitle_output_mode = els.setSubtitleOutputMode.value === 'download_only'
        ? 'download_only' : 'alongside_video';
    }
    if (els.setSubtitleSkipExisting) {
      settings.subtitle_skip_existing = !!els.setSubtitleSkipExisting.checked;
    }
    // v0036：译文输出组合（both/target_only/bilingual_only，默认 both）
    if (els.setSubtitleTranslateOutputs) {
      var translateOutputsValue = String(
        els.setSubtitleTranslateOutputs.value || '').trim();
      var allowedTranslateOutputs = SUBTITLE_TRANSLATE_OUTPUTS.map(
        function (item) { return item.value; });
      settings.subtitle_translate_outputs =
        allowedTranslateOutputs.indexOf(translateOutputsValue) !== -1
          ? translateOutputsValue : 'both';
    }
    var asrKey = els.subtitleAsrApiKey ? els.subtitleAsrApiKey.value.trim() : '';
    if (els.subtitleClearAsrKey && els.subtitleClearAsrKey.checked) {
      settings.subtitle_clear_asr_api_key = true;
    } else if (asrKey) {
      settings.subtitle_asr_api_key = asrKey;
    }
    var llmKey = els.subtitleLlmApiKey ? els.subtitleLlmApiKey.value.trim() : '';
    if (els.subtitleClearLlmKey && els.subtitleClearLlmKey.checked) {
      settings.subtitle_clear_llm_api_key = true;
    } else if (llmKey) {
      settings.subtitle_llm_api_key = llmKey;
    }
  }

  // ---------- 云端字幕：获取模型列表 / 测试连接（v0026） ----------
  var SUBTITLE_PROBE_UI = {
    asr: {
      modelsBtn: 'subtitleAsrModelsBtn', testBtn: 'subtitleAsrTestBtn',
      picker: 'subtitleAsrModelPicker', modelsHint: 'subtitleAsrModelsHint',
      testResult: 'subtitleAsrTestResult', modelInput: 'subtitleAsrModel'
    },
    llm: {
      modelsBtn: 'subtitleLlmModelsBtn', testBtn: 'subtitleLlmTestBtn',
      picker: 'subtitleLlmModelPicker', modelsHint: 'subtitleLlmModelsHint',
      testResult: 'subtitleLlmTestResult', modelInput: 'subtitleLlmModel'
    }
  };

  function subtitleProbeUi(kind) {
    var map = SUBTITLE_PROBE_UI[normalizeProbeKind(kind)];
    if (!map) { return null; }
    return {
      modelsBtn: byId(map.modelsBtn),
      testBtn: byId(map.testBtn),
      picker: byId(map.picker),
      modelsHint: byId(map.modelsHint),
      testResult: byId(map.testResult),
      modelInput: byId(map.modelInput)
    };
  }

  /** 读取当前表单值；API Key 只用于请求体，绝不回填/展示。 */
  function readSubtitleProbeProvider(kind) {
    var isLlm = normalizeProbeKind(kind) === 'llm';
    var baseUrlInput = isLlm ? els.subtitleLlmBaseUrl : els.subtitleAsrBaseUrl;
    var modelInput = isLlm ? els.subtitleLlmModel : els.subtitleAsrModel;
    var apiKeyInput = isLlm ? els.subtitleLlmApiKey : els.subtitleAsrApiKey;
    var clearInput = isLlm ? els.subtitleClearLlmKey : els.subtitleClearAsrKey;
    var provider = {
      baseUrl: baseUrlInput ? String(baseUrlInput.value || '').trim() : '',
      model: modelInput ? String(modelInput.value || '').trim() : '',
      apiKey: ''
    };
    if (apiKeyInput && !(clearInput && clearInput.checked)) {
      provider.apiKey = String(apiKeyInput.value || '').trim();
    }
    if (isLlm) {
      provider.targetLang = els.subtitleTranslateTarget ? els.subtitleTranslateTarget.value : '';
    } else {
      provider.language = els.subtitleAsrLanguage ? els.subtitleAsrLanguage.value : '';
    }
    return provider;
  }

  function setSubtitleProbeBusy(ui, busy) {
    if (!ui) { return; }
    if (ui.modelsBtn) { setBusy(ui.modelsBtn, busy, '获取中…'); }
    if (ui.testBtn) { setBusy(ui.testBtn, busy, '测试中…'); }
  }

  function resetSubtitleProbePicker(ui) {
    if (!ui || !ui.picker) { return; }
    clear(ui.picker);
    ui.picker.appendChild(el('option', { attrs: { value: '' }, text: '选择模型…' }));
    ui.picker.value = '';
    setHidden(ui.picker, true);
  }

  function hideSubtitleProbeHint(ui) {
    if (!ui || !ui.modelsHint) { return; }
    setText(ui.modelsHint, '');
    setHidden(ui.modelsHint, true);
  }

  function hideSubtitleProbeResult(ui) {
    if (!ui || !ui.testResult) { return; }
    setText(ui.testResult, '');
    ui.testResult.className = 'probe-result';
    setHidden(ui.testResult, true);
  }

  function setSubtitleProbeResult(ui, level, text) {
    if (!ui || !ui.testResult) { return; }
    ui.testResult.className = 'probe-result is-' + level;
    setText(ui.testResult, text);
    setHidden(ui.testResult, false);
  }

  function clearSubtitleProbeUi(kind) {
    var ui = subtitleProbeUi(kind);
    if (!ui) { return; }
    resetSubtitleProbePicker(ui);
    hideSubtitleProbeHint(ui);
    hideSubtitleProbeResult(ui);
  }

  /** base URL / 模型 / API Key 改变后：缓存的模型列表与测试结果失效。 */
  function markSubtitleProbeConfigChanged(kind) {
    var ui = subtitleProbeUi(kind);
    if (!ui) { return; }
    resetSubtitleProbePicker(ui);
    hideSubtitleProbeHint(ui);
    setSubtitleProbeResult(ui, 'warn', '配置已修改，请重新测试。');
  }

  function renderSubtitleModelPicker(kind, normalized) {
    var ui = subtitleProbeUi(kind);
    if (!ui || !ui.picker) { return; }
    clear(ui.picker);
    ui.picker.appendChild(el('option', { attrs: { value: '' }, text: '选择模型…' }));
    normalized.models.forEach(function (item) {
      var label = item.id + (item.owned_by ? ' · ' + item.owned_by : '');
      ui.picker.appendChild(el('option', { attrs: { value: item.id }, text: label }));
    });
    ui.picker.value = '';
    setHidden(ui.picker, false);
  }

  async function handleFetchSubtitleModels(kind) {
    var ui = subtitleProbeUi(kind);
    if (!ui) { return; }
    setSubtitleProbeBusy(ui, true);
    hideSubtitleProbeResult(ui);
    hideSubtitleProbeHint(ui);
    try {
      var res = await fetchModelList(kind, readSubtitleProbeProvider(kind));
      var normalized = normalizeModelList(res);
      renderSubtitleModelPicker(kind, normalized);
      if (ui.modelsHint) {
        setText(ui.modelsHint, formatModelListHint(kind, normalized));
        setHidden(ui.modelsHint, false);
      }
      if (normalized.warning) {
        setSubtitleProbeResult(ui, 'warn', normalized.warning);
      }
    } catch (err) {
      var formatted = formatProbeError(err);
      setSubtitleProbeResult(ui, formatted.level, formatted.text);
    } finally {
      setSubtitleProbeBusy(ui, false);
    }
  }

  async function handleTestSubtitleProvider(kind) {
    var ui = subtitleProbeUi(kind);
    if (!ui) { return; }
    setSubtitleProbeBusy(ui, true);
    hideSubtitleProbeResult(ui);
    try {
      var res = await testProvider(kind, readSubtitleProbeProvider(kind));
      var formatted = formatProbeResult(kind, res);
      setSubtitleProbeResult(ui, formatted.level, formatted.text);
    } catch (err) {
      var formattedError = formatProbeError(err);
      setSubtitleProbeResult(ui, formattedError.level, formattedError.text);
    } finally {
      setSubtitleProbeBusy(ui, false);
    }
  }

  function onSubtitlePickerChange(kind) {
    var ui = subtitleProbeUi(kind);
    if (!ui || !ui.picker) { return; }
    var value = ui.picker.value;
    if (!value) { return; }
    if (ui.modelInput) { ui.modelInput.value = value; }
    hideSubtitleProbeHint(ui);
    hideSubtitleProbeResult(ui);
  }

  function bindSubtitleProbeEvents() {
    ['asr', 'llm'].forEach(function (kind) {
      var ui = subtitleProbeUi(kind);
      if (!ui) { return; }
      if (ui.modelsBtn) {
        ui.modelsBtn.addEventListener('click', function () { handleFetchSubtitleModels(kind); });
      }
      if (ui.testBtn) {
        ui.testBtn.addEventListener('click', function () { handleTestSubtitleProvider(kind); });
      }
      if (ui.picker) {
        ui.picker.addEventListener('change', function () { onSubtitlePickerChange(kind); });
      }
      var watched = [
        ui.modelInput,
        kind === 'llm' ? els.subtitleLlmBaseUrl : els.subtitleAsrBaseUrl,
        kind === 'llm' ? els.subtitleLlmApiKey : els.subtitleAsrApiKey,
        kind === 'llm' ? els.subtitleClearLlmKey : els.subtitleClearAsrKey
      ];
      watched.forEach(function (input) {
        if (input) {
          input.addEventListener('change', function () { markSubtitleProbeConfigChanged(kind); });
        }
      });
    });
  }

  function sizeFieldValue(bytes) {
    var n = Number(bytes) || 0;
    if (n <= 0) { return { value: '0', unit: 'mb' }; }
    if (n % SIZE_UNITS.gb === 0) { return { value: String(n / SIZE_UNITS.gb), unit: 'gb' }; }
    if (n % SIZE_UNITS.mb === 0) { return { value: String(n / SIZE_UNITS.mb), unit: 'mb' }; }
    if (n % SIZE_UNITS.kb === 0) { return { value: String(n / SIZE_UNITS.kb), unit: 'kb' }; }
    return { value: String(Math.round((n / 1024) * 100) / 100), unit: 'kb' };
  }

  function applySizeFields(minBytes, maxBytes) {
    var min = sizeFieldValue(minBytes);
    var max = sizeFieldValue(maxBytes);
    els.minSize.value = min.value;
    els.minUnit.value = min.unit;
    els.maxSize.value = max.value;
    els.maxUnit.value = max.unit;
  }

  function readSizeField(input, unitSelector) {
    if (!input || !unitSelector) { return 0; }
    var raw = String(input.value === null || input.value === undefined ? '' : input.value).trim();
    if (!raw) { return 0; }
    if (!/^\d+(\.\d+)?$/.test(raw)) { return null; }
    var num = parseFloat(raw);
    if (!isFinite(num) || num < 0) { return null; }
    return Math.round(num * (SIZE_UNITS[unitSelector.value] || 1024));
  }

  // ---------- v0043：保留规则链 / 排除规则 / 增量扫描（扫描选项与设置页共用） ----------
  var KEEP_PRIMARY_VALUES = ['created_latest', 'created_earliest', 'mtime_latest',
    'mtime_earliest', 'size_largest', 'size_smallest', 'atime_latest'];

  function textToLines(value) {
    return String(value === undefined || value === null ? '' : value)
      .split('\n')
      .map(function (line) { return line.trim(); })
      .filter(function (line) { return !!line; });
  }

  function linesToText(list) {
    return Array.isArray(list) ? list.join('\n') : '';
  }

  /** 绝对路径判定（POSIX / Windows 盘符 / UNC）。保护目录要求绝对路径。 */
  function isAbsolutePath(value) {
    var text = String(value || '').trim();
    if (!text) { return false; }
    return text.charAt(0) === '/' || /^[A-Za-z]:[\\/]/.test(text)
      || /^\\\\/.test(text);
  }

  /** 由表单控件组合出 keep_rules；空数组表示沿用 keep_mode/pinned_path。 */
  function composeKeepRules(prefix) {
    var primary = byId(prefix + 'PrimaryRule');
    var pathInput = byId(prefix + 'PathPriority');
    var nameInput = byId(prefix + 'NamePriority');
    var preferSub = byId(prefix + 'PreferSubtitle');
    var preferAudio = byId(prefix + 'PreferMultiAudio');
    var rules = [];
    var primaryValue = primary ? String(primary.value || '').trim() : '';
    if (KEEP_PRIMARY_VALUES.indexOf(primaryValue) !== -1) { rules.push(primaryValue); }
    textToLines(pathInput ? pathInput.value : '').forEach(function (path) {
      rules.push('path_priority:' + path);
    });
    textToLines(nameInput ? nameInput.value : '').forEach(function (pattern) {
      rules.push('filename_priority:' + pattern);
    });
    if (preferSub && preferSub.checked) { rules.push('has_subtitle_stream'); }
    if (preferAudio && preferAudio.checked) { rules.push('has_audio_streams'); }
    // 表单无法表达的规则（例如 API 直接写入的第二主规则）原样保留，避免保存设置时被静默丢弃。
    var primaryNode = byId(prefix + 'PrimaryRule');
    var extraRaw = primaryNode && primaryNode.dataset ? primaryNode.dataset.extraRules : '';
    if (extraRaw) {
      try {
        var extra = JSON.parse(extraRaw);
        if (Array.isArray(extra)) {
          extra.forEach(function (rule) {
            if (typeof rule === 'string' && rule && rules.indexOf(rule) === -1) {
              rules.push(rule);
            }
          });
        }
      } catch (e) { /* 损坏的缓存值忽略 */ }
    }
    return rules;
  }

  /** 把 keep_rules 反解回控件（设置页/默认值回填用）。 */
  function decomposeKeepRules(rules) {
    var result = { primary: '', paths: [], names: [], preferSub: false, preferAudio: false, extra: [] };
    (Array.isArray(rules) ? rules : []).forEach(function (raw) {
      if (typeof raw !== 'string' || !raw) { return; }
      var index = raw.indexOf(':');
      var name = index === -1 ? raw : raw.slice(0, index);
      var arg = index === -1 ? '' : raw.slice(index + 1);
      if (KEEP_PRIMARY_VALUES.indexOf(name) !== -1) {
        if (!result.primary) { result.primary = name; } else { result.extra.push(raw); }
        return;
      }
      if (name === 'path_priority' && arg) { result.paths.push(arg); return; }
      if (name === 'filename_priority' && arg) { result.names.push(arg); return; }
      if (name === 'has_subtitle_stream') {
        if (result.preferSub) { result.extra.push(raw); } else { result.preferSub = true; }
        return;
      }
      if (name === 'has_audio_streams') {
        if (result.preferAudio) { result.extra.push(raw); } else { result.preferAudio = true; }
        return;
      }
      result.extra.push(raw);
    });
    return result;
  }

  function fillKeepRuleControls(prefix, rules) {
    var parsed = decomposeKeepRules(rules);
    var primary = byId(prefix + 'PrimaryRule');
    var pathInput = byId(prefix + 'PathPriority');
    var nameInput = byId(prefix + 'NamePriority');
    var preferSub = byId(prefix + 'PreferSubtitle');
    var preferAudio = byId(prefix + 'PreferMultiAudio');
    if (primary) {
      primary.value = parsed.primary;
      if (primary.dataset) { primary.dataset.extraRules = JSON.stringify(parsed.extra); }
    }
    if (pathInput) { pathInput.value = parsed.paths.join('\n'); }
    if (nameInput) { nameInput.value = parsed.names.join('\n'); }
    if (preferSub) { preferSub.checked = parsed.preferSub; }
    if (preferAudio) { preferAudio.checked = parsed.preferAudio; }
  }

  function applyIncrementalSettingsToScanForm(settings) {
    if (!settings) { return; }
    if (els.excludeNameRegex) {
      els.excludeNameRegex.value = linesToText(settings.exclude_name_regex);
    }
    if (els.excludeNameGlob) {
      els.excludeNameGlob.value = linesToText(settings.exclude_name_glob);
    }
    if (els.ignoreFileEnabled) {
      els.ignoreFileEnabled.checked = settings.ignore_file_enabled !== false;
    }
    if (els.incrementalScan) {
      els.incrementalScan.checked = settings.incremental_scan !== false;
    }
    if (els.mediaSidecarMode) {
      els.mediaSidecarMode.value =
        settings.media_sidecar_mode === 'include' ? 'include' : 'skip';
    }
    fillKeepRuleControls('keep', settings.keep_rules);
  }

  function readIncrementalOptions(options, errors) {
    var regexList = textToLines(els.excludeNameRegex ? els.excludeNameRegex.value : '');
    var globList = textToLines(els.excludeNameGlob ? els.excludeNameGlob.value : '');
    if (regexList.length > 50) {
      errors.excludeNameRegex = '文件名正则最多 50 条。';
    } else {
      options.exclude_name_regex = regexList;
    }
    if (globList.length > 50) {
      errors.excludeNameGlob = '文件名通配符最多 50 条。';
    } else {
      options.exclude_name_glob = globList;
    }
    options.ignore_file_enabled = els.ignoreFileEnabled ? !!els.ignoreFileEnabled.checked : true;
    options.incremental_scan = els.incrementalScan ? !!els.incrementalScan.checked : true;
    options.media_sidecar_mode = els.mediaSidecarMode && els.mediaSidecarMode.value === 'include'
      ? 'include' : 'skip';
    options.keep_rules = composeKeepRules('keep');
  }

  function applySettingsToScanForm(settings) {
    if (!settings) { return; }
    els.hashAlgo.value = settings.hash_algo === 'md5' ? 'md5' : 'sha256';
    els.keepMode.value = ['latest', 'earliest', 'pinned'].indexOf(settings.keep_mode) !== -1 ? settings.keep_mode : 'latest';
    els.includeExt.value = extListToText(settings.include_extensions);
    els.excludeExt.value = extListToText(settings.exclude_extensions);
    els.ignoreCaseExt.checked = settings.ignore_case_ext !== false;
    els.skipEmpty.checked = settings.skip_empty !== false;
    els.skipHidden.checked = settings.skip_hidden !== false;
    els.recycleDefault.checked = settings.recycle_default !== false;
    els.quickSampleKb.value = settings.quick_sample_kb === undefined || settings.quick_sample_kb === null ? 64 : settings.quick_sample_kb;
    els.concurrency.value = settings.concurrency === undefined || settings.concurrency === null ? 2 : settings.concurrency;
    els.hashRate.value = settings.hash_rate_mbps === undefined || settings.hash_rate_mbps === null ? 0 : settings.hash_rate_mbps;
    els.maxFiles.value = settings.max_files === undefined || settings.max_files === null ? 500000 : settings.max_files;
    els.allowCtimeFallback.checked = settings.allow_ctime_fallback === true;
    els.createRecycleDir.checked = settings.create_recycle_dir === true;
    applySizeFields(settings.min_size_bytes, settings.max_size_bytes);
    state.similarity = similarityCapability();
    state.scanMode = settings.scan_mode === 'similar' ? 'similar' : 'exact';
    setScanMode(state.scanMode, { silent: true });
    applySimilaritySettingsToForm(settings);
    applyVideoSettingsToForm(settings);
    applyIncrementalSettingsToScanForm(settings);
    updateKeepModeUi();
    clearOptionsErrors();
    updateScanSummary();
  }

  var BACKEND_VIDEO_FIELD_KEYS = {
    ffmpeg_path: 'videoFfmpegPath',
    video_hwaccel: 'videoHwaccel',
    subtitle_job_limit: 'setSubtitleJobLimit',
    subtitle_concurrency: 'setSubtitleConcurrency',
    // v0032：音频来源无独立错误元素，映射到设置页字段名后由通用错误摘要展示
    subtitle_audio_source: 'subtitleAudioSource'
  };

  /** 把后端 INVALID_ARGUMENT 的 details.key 映射到前端字段错误（中文文案用后端 message）。 */
  function videoFieldErrorsFromApi(err) {
    if (!err || err.code !== 'INVALID_ARGUMENT' || !err.details || !err.details.key) { return null; }
    var field = BACKEND_VIDEO_FIELD_KEYS[err.details.key];
    if (!field) { return null; }
    var errors = {};
    errors[field] = err.message || '该选项值不合法，请检查后重试。';
    return errors;
  }

  function clearOptionsErrors() {
    Object.keys(OPTION_ERROR_FIELDS).forEach(function (key) {
      var map = OPTION_ERROR_FIELDS[key];
      var input = byId(map.input);
      var errEl = byId(map.error);
      if (input) {
        input.removeAttribute('aria-invalid');
        var field = input.closest ? input.closest('.field') : null;
        if (field) { field.classList.remove('has-error'); }
      }
      if (errEl) { setText(errEl, ''); hide(errEl); }
    });
    if (els.optionsError) { setText(els.optionsError, ''); hide(els.optionsError); }
    if (els.rootsError) { setText(els.rootsError, ''); hide(els.rootsError); }
  }

  function showOptionErrors(errors) {
    clearOptionsErrors();
    var keys = Object.keys(errors || {});
    if (!keys.length) { return true; }
    var firstInput = null;
    keys.forEach(function (key) {
      var map = OPTION_ERROR_FIELDS[key];
      if (!map) { return; }
      var input = byId(map.input);
      var errEl = byId(map.error);
      if (input) {
        input.setAttribute('aria-invalid', 'true');
        var field = input.closest ? input.closest('.field') : null;
        if (field) { field.classList.add('has-error'); }
        if (!firstInput) { firstInput = input; }
      }
      if (errEl) { setText(errEl, errors[key]); show(errEl); }
    });
    var summary = keys.map(function (key) { return errors[key]; }).join('；');
    setText(els.optionsError, '请检查扫描选项：' + summary);
    show(els.optionsError);
    if (firstInput) { focusSoon(firstInput); }
    return false;
  }

  function readIntOption(input, min, max, allowEmpty, emptyValue, label) {
    var raw = String(input.value === null || input.value === undefined ? '' : input.value).trim();
    if (!raw) {
      if (allowEmpty) { return { value: emptyValue, error: '' }; }
      return { value: null, error: label + '不能为空。' };
    }
    if (!/^\d+$/.test(raw)) { return { value: null, error: label + '必须是整数。' }; }
    var num = parseInt(raw, 10);
    if (!isFinite(num) || num < min || num > max) {
      return { value: null, error: label + '必须在 ' + min + ' 到 ' + max + ' 之间。' };
    }
    return { value: num, error: '' };
  }

  function readScanOptions() {
    var errors = {};
    var options = {};
    options.hash_algo = els.hashAlgo.value === 'md5' ? 'md5' : 'sha256';
    options.keep_mode = els.keepMode.value === 'earliest' ? 'earliest' : (els.keepMode.value === 'pinned' ? 'pinned' : 'latest');
    options.pinned_path = state.pinnedPath || '';
    options.skip_empty = !!els.skipEmpty.checked;
    options.skip_hidden = !!els.skipHidden.checked;
    options.skip_symlinks = true;
    options.ignore_case_ext = !!els.ignoreCaseExt.checked;
    options.recycle_default = !!els.recycleDefault.checked;
    options.create_recycle_dir = !!els.createRecycleDir.checked;
    options.allow_ctime_fallback = !!els.allowCtimeFallback.checked;

    var include = normalizeExtList(els.includeExt.value);
    if (include.error) { errors.includeExt = include.error; } else { options.include_extensions = include.list; }
    var exclude = normalizeExtList(els.excludeExt.value);
    if (exclude.error) { errors.excludeExt = exclude.error; } else { options.exclude_extensions = exclude.list; }

    var minBytes = readSizeField(els.minSize, els.minUnit);
    var maxBytes = readSizeField(els.maxSize, els.maxUnit);
    if (minBytes === null) { errors.minSize = '请输入有效的非负数字，可带小数（如 1.5）。'; }
    else { options.min_size_bytes = minBytes; }
    if (maxBytes === null) { errors.maxSize = '请输入有效的非负数字，可带小数（如 1.5）。'; }
    else if (maxBytes > 0 && minBytes !== null && minBytes > maxBytes) { errors.maxSize = '最大值必须大于或等于最小值。'; }
    else { options.max_size_bytes = maxBytes; }

    if (options.keep_mode === 'pinned' && !state.pinnedPath) {
      errors.pinned = '选择“指定路径”策略时，请先选择要保留的路径。';
    }

    var quick = readIntOption(els.quickSampleKb, 4, 1024, false, 0, '快速指纹采样（KB）');
    if (quick.error) { errors.quickSample = quick.error; } else { options.quick_sample_kb = quick.value; }
    var concurrency = readIntOption(els.concurrency, 1, 8, false, 0, '哈希并发数');
    if (concurrency.error) { errors.concurrency = concurrency.error; } else { options.concurrency = concurrency.value; }
    var rate = readIntOption(els.hashRate, 0, 2000, true, 0, '全量哈希限速（MB/s）');
    if (rate.error) { errors.hashRate = rate.error; } else { options.hash_rate_mbps = rate.value; }
    var maxFiles = readIntOption(els.maxFiles, 1, 10000000, false, 0, '最大扫描文件数');
    if (maxFiles.error) { errors.maxFiles = maxFiles.error; } else { options.max_files = maxFiles.value; }

    if (state.scanMode === 'similar') {
      options.scan_mode = 'similar';
      var threshold = readIntOption(els.simThreshold, 0, 100, false, 0, '相似度阈值');
      if (threshold.error) { errors.simThreshold = threshold.error; } else { options.similarity_threshold = threshold.value; }
      var simConc = readIntOption(els.simConcurrency, 1, 4, false, 0, '相似特征并发数');
      if (simConc.error) { errors.simConcurrency = simConc.error; } else { options.similarity_concurrency = simConc.value; }
      var simMax = readIntOption(els.simMaxFileMb, 1, 1024, false, 0, '单文件相似计算上限（MB）');
      if (simMax.error) { errors.simMaxFile = simMax.error; } else { options.similarity_max_file_mb = simMax.value; }
      var extSelected = readExtChips(els.simExtList);
      if (!extSelected.length) {
        errors.simExt = '请至少选择一种允许相似计算的文件类型。';
      } else {
        options.similarity_extensions = extSelected;
      }
      var settings = state.settings || {};
      options.similarity_exclude_extensions = Array.isArray(settings.similarity_exclude_extensions)
        ? settings.similarity_exclude_extensions.slice() : [];
      options.similarity_cache = !!els.simCache.checked;
      options.similarity_allow_binary = !!els.simAllowBinary.checked;
    }
    readVideoScanOptions(options, errors);
    readIncrementalOptions(options, errors);
    return { options: options, errors: errors, valid: Object.keys(errors).length === 0 };
  }

  function resetOptionsToDefaults() {
    var defaults = state.defaults || clonePlain(DEFAULT_SETTINGS);
    applySettingsToScanForm(defaults);
    state.recycleDefault = defaults.recycle_default !== false;
    toast('已恢复默认扫描选项。', 'info');
  }

  function bindScanEvents() {
    els.pickRootsBtn.addEventListener('click', function () { openFolderPicker('roots'); });
    els.pickExcludesBtn.addEventListener('click', function () { openFolderPicker('excludes'); });
    els.pickPinnedBtn.addEventListener('click', function () { openFolderPicker('pinned'); });
    els.clearPinnedBtn.addEventListener('click', function () {
      state.pinnedPath = '';
      updatePinnedDisplay();
      updateScanSummary();
    });
    els.clearRootsBtn.addEventListener('click', function () {
      if (!state.roots.length && !state.excludes.length) { return; }
      state.roots = [];
      state.excludes = [];
      renderRoots();
      renderExcludeChips();
      updateScanSummary();
      toast('已清空扫描范围。', 'info');
    });
    els.startScanBtn.addEventListener('click', startScan);
    els.optionsForm.addEventListener('input', function () { clearOptionsErrors(); updateScanSummary(); });
    els.optionsForm.addEventListener('change', function () { clearOptionsErrors(); updateScanSummary(); });
    els.recycleDefault.addEventListener('change', function () { state.recycleDefault = !!els.recycleDefault.checked; });
    els.resetOptionsBtn.addEventListener('click', resetOptionsToDefaults);
    els.keepMode.addEventListener('change', updateKeepModeUi);
    els.pauseBtn.addEventListener('click', function () { scanAction('pause'); });
    els.resumeBtn.addEventListener('click', function () { scanAction('resume'); });
    els.stopBtn.addEventListener('click', function () {
      confirmDialog({
        title: '停止当前扫描？',
        message: '停止后将结束本次扫描，已发现的结果仍保存在历史记录中，可稍后查看。',
        confirmText: '停止扫描'
      }).then(function (ok) { if (ok) { scanAction('stop'); } });
    });
    els.viewResultsBtn.addEventListener('click', function () {
      loadResults(true);
      scrollToNode(els.resultsSection);
    });
    if (els.modeExactBtn) { els.modeExactBtn.addEventListener('click', function () { setScanMode('exact'); }); }
    if (els.modeSimilarBtn) { els.modeSimilarBtn.addEventListener('click', function () { setScanMode('similar'); }); }
    if (els.simThresholdRange) {
      els.simThresholdRange.addEventListener('input', function () {
        syncThresholdInputs('range');
        clearOptionsErrors();
        updateScanSummary();
      });
    }
    if (els.simThreshold) {
      els.simThreshold.addEventListener('input', function () {
        syncThresholdInputs('number');
        clearOptionsErrors();
        updateScanSummary();
      });
      els.simThreshold.addEventListener('change', function () {
        var num = parseInt(els.simThreshold.value, 10);
        if (isFinite(num)) { setSimilarityThreshold(num); }
        updateScanSummary();
      });
    }
    if (els.simExtList) {
      els.simExtList.addEventListener('change', function () { clearOptionsErrors(); updateScanSummary(); });
    }
    if (els.videoEnabled) {
      els.videoEnabled.addEventListener('change', function () {
        updateVideoFieldsState();
        updateAsrFieldsState();
        clearOptionsErrors();
        updateScanSummary();
      });
    }
    if (els.videoExtList) {
      els.videoExtList.addEventListener('change', function () { clearOptionsErrors(); updateScanSummary(); });
    }
    if (els.videoAsrEnabled) {
      els.videoAsrEnabled.addEventListener('change', function () {
        updateAsrFieldsState();
        clearOptionsErrors();
      });
    }
    if (els.videoAsrSection) {
      els.videoAsrSection.addEventListener('change', function () { clearOptionsErrors(); });
    }
  }

  // ==================== 10. 启动扫描 / 进度轮询 ====================
  async function startScan() {
    clearOptionsErrors();
    var roots = state.roots.map(function (r) { return r.path; });
    if (!roots.length) {
      setText(els.rootsError, '请至少选择一个扫描目录。');
      show(els.rootsError);
      focusSoon(els.pickRootsBtn);
      toast('请先选择扫描范围。', 'error');
      return;
    }
    var parsed = readScanOptions();
    if (!showOptionErrors(parsed.errors)) {
      toast('扫描选项有误，请检查红色提示。', 'error');
      return;
    }
    if (state.scan.polling) {
      toast('已有扫描任务正在运行，请等待结束或停止后重试。', 'error');
      return;
    }
    state.recycleDefault = !!els.recycleDefault.checked;
    setBusy(els.startScanBtn, true, '正在提交…');
    try {
      var res = await api('/api/scans', {
        method: 'POST',
        timeout: 20000,
        body: { roots: roots, excludes: state.excludes.slice(), options: parsed.options }
      });
      if (!res || !res.scan_id) { throw new ApiError(200, 'INVALID_RESPONSE', '服务未返回 scan_id，请稍后重试。'); }
      resetResultsState();
      state.results.scanMode = state.scanMode;
      hide(els.resultsSection);
      startScanPolling(res.scan_id);
      var videoOn = !!(els.videoEnabled && els.videoEnabled.checked) && (!state.video || state.video.available !== false);
      var scanStartText = state.scanMode === 'similar'
        ? '扫描任务已开始：先进行精准哈希，随后进行内容相似度分析'
        : '扫描任务已开始，正在枚举文件';
      if (videoOn) { scanStartText += '，随后进行视频视觉分析'; }
      toast(scanStartText + '。', 'success');
      scrollToNode(els.progressPanel);
    } catch (err) {
      var msg = errorMessage(err, '启动扫描失败，请稍后重试。');
      if (err && err.code === 'SCAN_BUSY') { msg = '已有扫描任务正在运行，同一时间只允许一个扫描任务。'; }
      var scanFieldErrors = videoFieldErrorsFromApi(err);
      if (scanFieldErrors) {
        showOptionErrors(scanFieldErrors);
      } else {
        setText(els.optionsError, msg);
        show(els.optionsError);
      }
      toast(msg, 'error');
    } finally {
      setBusy(els.startScanBtn, false);
      els.startScanBtn.disabled = false;
    }
  }

  function startScanPolling(scanId) {
    stopScanPolling();
    state.scan.id = scanId;
    state.scan.data = null;
    state.scan.handled = false;
    state.scan.errors = 0;
    state.scan.polling = true;
    show(els.progressPanel);
    renderScanProgress({ scan_id: scanId, status: 'queued', stage: '', progress: 0, message: '正在启动扫描…', stats: {} });
    pollScanOnce();
    state.scan.timer = setInterval(pollScanOnce, POLL_MS);
  }

  function stopScanPolling() {
    if (state.scan.timer) { clearInterval(state.scan.timer); state.scan.timer = null; }
    if (state.scan.abort) {
      try { state.scan.abort.abort(); } catch (e) { /* 忽略 */ }
      state.scan.abort = null;
    }
    state.scan.polling = false;
  }

  function stopAllPolling() {
    stopScanPolling();
    stopDeletePolling();
  }

  async function pollScanOnce() {
    if (!state.scan.polling || !state.scan.id) { return; }
    if (state.scan.abort) { try { state.scan.abort.abort(); } catch (e) { /* 忽略 */ } }
    var controller = new AbortController();
    state.scan.abort = controller;
    try {
      var res = await api('/api/scans/' + encodePath(state.scan.id), { signal: controller.signal, timeout: 15000 });
      if (!state.scan.polling) { return; }
      if (!res || !res.scan) { return; }
      state.scan.errors = 0;
      state.scan.data = res.scan;
      renderScanProgress(res.scan);
      if (TERMINAL_STATUS.indexOf(res.scan.status) !== -1) {
        stopScanPolling();
        handleScanTerminal(res.scan);
      }
    } catch (err) {
      if (err && err.name === 'AbortError') { return; }
      state.scan.errors += 1;
      if (state.scan.errors >= 5) {
        stopScanPolling();
        setText(els.progressStage, '进度轮询中断');
        setText(els.progressMessage, '无法获取扫描进度：' + errorMessage(err));
        toast('进度轮询中断，请刷新页面或稍后在历史记录中查看结果。', 'error');
      }
    }
  }

  function renderScanProgress(scan) {
    if (!scan) { return; }
    show(els.progressPanel);
    var status = scan.status || 'queued';
    setText(els.progressStatus, STATUS_LABEL[status] || status || '未知');
    els.progressStatus.className = 'badge ' + (STATUS_BADGE[status] || 'badge-gray');
    setText(els.progressScanId, scan.scan_id || state.scan.id || '');
    var stageText = STAGE_LABEL[scan.stage] || scan.stage || '准备中';
    if (status === 'paused') { stageText += (scan.pause_reason === 'upgrade' ? '（升级前暂停）' : '（已暂停）'); }
    else if (status === 'stopped') { stageText = '扫描已停止'; }
    else if (status === 'failed') { stageText = '扫描失败'; }
    else if (status === 'completed') { stageText = '扫描完成'; }
    else if (status === 'interrupted') { stageText = '扫描已中断'; }
    setText(els.progressStage, stageText);
    var pct = clamp(Math.round((Number(scan.progress) || 0) * 100), 0, 100);
    if (status === 'completed') { pct = 100; }
    setText(els.progressPct, pct + '%');
    els.progressFill.style.width = pct + '%';
    els.progressBar.setAttribute('aria-valuenow', String(pct));
    els.progressBar.classList.toggle('is-error', status === 'failed');
    els.progressBar.classList.toggle('is-warn', status === 'paused' || status === 'stopped' || status === 'stopping' || status === 'interrupted');
    var message = scan.message || '';
    if (status === 'paused' && scan.pause_reason === 'upgrade') {
      message = '扫描已在升级前暂停，断点已保存；升级完成后点击“继续扫描”恢复。';
    }
    if (status === 'completed' && !message) { message = '扫描完成。'; }
    if (status === 'interrupted' && !message) {
      message = '扫描因服务重启或套件升级中断，请重新发起扫描。';
    }
    if (status === 'failed' && !message) {
      message = (scan.error && (scan.error.message || scan.error)) || '扫描失败，请查看操作日志。';
    }
    setText(els.progressMessage, message || '正在处理…');
    var stats = scan.stats || {};
    if (status === 'completed') {
      var completedExtras = [];
      if (Number(stats.mtime_fallback_groups) > 0) {
        completedExtras.push('其中 ' + fmtInt(stats.mtime_fallback_groups) + ' 组无创建时间，按修改时间保留');
      }
      if (Number(stats.video_groups) > 0) {
        completedExtras.push('检测到 ' + fmtInt(stats.video_groups) + ' 组视频视觉相似');
      }
      if (completedExtras.length) {
        setText(els.progressMessage, (message || '扫描完成。') + ' ' + completedExtras.join('；') + '。');
      }
    }
    setText(els.counterFilesSeen, fmtInt(stats.files_seen));
    setText(els.counterFilesScanned, fmtInt(stats.files_scanned));
    setText(els.counterCandidates, fmtInt(stats.candidate_files));
    setText(els.counterGroups, fmtInt(stats.duplicate_groups || stats.groups));
    setText(els.counterDuplicateFiles, fmtInt(stats.duplicate_files));
    setText(els.counterReclaimable, fmtBytes(stats.reclaimable_bytes));
    setText(els.counterRate, fmtBytes(stats.bytes_per_sec) + '/s');
    setText(els.counterErrors, fmtInt(stats.errors));
    var currentPath = scan.current_path || '';
    setText(els.progressPath, currentPath || '—');
    els.progressPath.title = currentPath;
    setText(els.progressElapsed, fmtElapsed(scan.elapsed_sec));
    var scanMode = detectScanMode(scan);
    if (scanMode === 'similar' || stats.similarity_features !== undefined || stats.similar_groups !== undefined) {
      show(els.similarityCounters);
      setText(els.simFeatures, fmtInt(stats.similarity_features));
      setText(els.simCacheHits, fmtInt(stats.similarity_cache_hits));
      setText(els.simCandidates, fmtInt(stats.similarity_candidates));
      var simSkipped = stats.similarity_skipped || {};
      var skippedTotal = 0;
      Object.keys(simSkipped).forEach(function (key) { skippedTotal += Number(simSkipped[key]) || 0; });
      setText(els.simSkipped, fmtInt(skippedTotal));
    } else {
      hide(els.similarityCounters);
    }
    if (scan.stage === 'video' || Number(stats.video_candidates) > 0 ||
        Number(stats.video_groups) > 0 || Number(stats.video_ffmpeg_used) > 0 ||
        videoScanEnabledForResults()) {
      show(els.videoCounters);
      setText(els.videoCandidates, fmtInt(stats.video_candidates));
      setText(els.videoCacheHits, fmtInt(stats.video_cache_hits));
      setText(els.videoFfmpegUsed, fmtInt(stats.video_ffmpeg_used));
      setText(els.videoSkipped, fmtInt(videoSkippedCount(stats)));
    } else {
      hide(els.videoCounters);
    }
    renderProgressActions(scan);
  }

  function renderProgressActions(scan) {
    var status = scan && scan.status ? scan.status : '';
    var terminal = TERMINAL_STATUS.indexOf(status) !== -1;
    setHidden(els.pauseBtn, status !== 'running');
    setHidden(els.resumeBtn, status !== 'paused');
    setHidden(els.stopBtn, !(status === 'running' || status === 'paused' || status === 'queued'));
    setHidden(els.viewResultsBtn, !(terminal && (status === 'completed' || status === 'stopped')));
  }

  function handleScanTerminal(scan) {
    if (!scan) { return; }
    state.scan.data = scan;
    state.historyCache = null;
    state.results.scanMode = detectScanMode(scan);
    if (scan.status === 'completed') {
      if (!state.scan.handled) {
        state.scan.handled = true;
        var doneStats = scan.stats || {};
        var exactGroups = doneStats.exact_groups !== undefined ? doneStats.exact_groups : doneStats.duplicate_groups;
        var similarGroups = doneStats.similar_groups || 0;
        var fallbackGroups = Number(doneStats.mtime_fallback_groups) || 0;
        var videoGroups = Number(doneStats.video_groups) || 0;
        toast('扫描完成：精准组 ' + fmtInt(exactGroups) + ' 组' +
          (similarGroups > 0 ? '，内容相似组 ' + fmtInt(similarGroups) + ' 组（需人工复核）' : '') +
          (videoGroups > 0 ? '，视频视觉相似组 ' + fmtInt(videoGroups) + ' 组' : '') +
          (fallbackGroups > 0 ? '；其中 ' + fmtInt(fallbackGroups) + ' 组无创建时间，按修改时间保留' : '') + '。',
          'success', 6000);
      }
      show(els.resultsSection);
      loadResults(true);
    } else if (scan.status === 'stopped') {
      if (!state.scan.handled) {
        state.scan.handled = true;
        toast('扫描已停止，可查看已发现的扫描结果。', 'info');
      }
      show(els.resultsSection);
      loadResults(true);
    } else if (scan.status === 'interrupted') {
      state.scan.handled = true;
      setText(els.progressStage, '扫描已中断');
      setText(els.progressMessage, scan.message || '扫描因服务重启或套件升级中断，请重新发起扫描。');
      toast('扫描已中断，请重新发起扫描。已完成的缓存和历史仍然保留。', 'info', 6000);
    } else if (scan.status === 'failed') {
      state.scan.handled = true;
      var msg = '扫描失败，请查看操作日志。';
      if (scan.error) { msg = typeof scan.error === 'string' ? scan.error : (scan.error.message || msg); }
      setText(els.progressStage, '扫描失败');
      setText(els.progressMessage, msg);
      toast(msg, 'error', 6000);
    }
  }

  async function scanAction(action) {
    if (!state.scan.id) { toast('当前没有进行中的扫描。', 'error'); return; }
    if (state.scan.busy) { return; }
    state.scan.busy = true;
    [els.pauseBtn, els.resumeBtn, els.stopBtn].forEach(function (btn) { if (btn) { btn.disabled = true; } });
    try {
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/' + action, {
        method: 'POST', body: {}, timeout: 15000
      });
      if (res && res.scan) {
        state.scan.data = res.scan;
        renderScanProgress(res.scan);
        if (TERMINAL_STATUS.indexOf(res.scan.status) !== -1) {
          stopScanPolling();
          handleScanTerminal(res.scan);
        }
      }
      toast(action === 'pause' ? '已请求暂停扫描。' : (action === 'resume' ? '已请求继续扫描。' : '已请求停止扫描。'), 'info');
    } catch (err) {
      toast(errorMessage(err, '操作失败，请稍后重试。'), 'error');
    } finally {
      state.scan.busy = false;
      [els.pauseBtn, els.resumeBtn, els.stopBtn].forEach(function (btn) { if (btn) { btn.disabled = false; } });
      renderProgressActions(state.scan.data || { status: '' });
    }
  }

  async function checkActiveScanOnBoot() {
    try {
      var res = await api('/api/scans', { timeout: 10000 });
      var scans = res && Array.isArray(res.scans) ? res.scans : [];
      state.historyCache = scans;
      if (state.scan.polling) { return; }
      var active = null;
      for (var i = 0; i < scans.length; i += 1) {
        if (ACTIVE_STATUS.indexOf(scans[i].status) !== -1) { active = scans[i]; break; }
      }
      if (!active) { return; }
      startScanPolling(active.scan_id);
      toast('检测到未完成的扫描任务，已恢复进度显示。', 'info');
    } catch (e) { /* 历史列表加载失败不影响主流程 */ }
  }

  // ==================== 11. 扫描结果：分页、汇总与分组渲染 ====================
  function resetResultsState() {
    var results = state.results;
    results.groups = [];
    results.total = 0;
    results.offset = 0;
    results.q = '';
    results.sort = 'wasted_desc';
    results.mode = 'none';
    results.selected = new Map();
    results.excluded = new Map();
    results.manualKeep = new Map();
    results.loading = false;
    results.seq += 1;
    results.loaded = false;
    results.scanMode = 'exact';
    results.exactTotal = 0;
    results.similarTotal = 0;
    results.similarGroups = [];
    results.similarOffset = 0;
    results.similarLoading = false;
    results.similarLoaded = false;
    results.similarSeq += 1;
    results.similarReview = new Set();
    results.similarSelected = new Map();
    results.videoGroups = [];
    results.videoTotal = 0;
    results.videoOffset = 0;
    results.videoLoading = false;
    results.videoLoaded = false;
    results.videoSeq += 1;
    results.videoSelected = new Map();
    results.videoSelectAllMode = false;
    if (els.searchGroups) { els.searchGroups.value = ''; }
    if (els.sortGroups) { els.sortGroups.value = 'wasted_desc'; }
    if (els.groupsList) { clear(els.groupsList); }
    if (els.similarGroupsList) { clear(els.similarGroupsList); }
    if (els.videoGroupsList) { clear(els.videoGroupsList); }
    if (els.resultsSection) { hide(els.resultsSection); }
    if (els.resultsError) { hide(els.resultsError); }
    if (els.resultsLoading) { hide(els.resultsLoading); }
    if (els.resultsEmpty) { hide(els.resultsEmpty); }
    if (els.similarError) { hide(els.similarError); }
    if (els.similarLoading) { hide(els.similarLoading); }
    if (els.similarEmpty) { hide(els.similarEmpty); }
    if (els.videoError) { hide(els.videoError); }
    if (els.videoLoading) { hide(els.videoLoading); }
    if (els.videoEmpty) { hide(els.videoEmpty); }
    if (els.exactPartitionCount) { setText(els.exactPartitionCount, ''); }
    if (els.similarPartitionCount) { setText(els.similarPartitionCount, ''); }
    if (els.videoPartitionCount) { setText(els.videoPartitionCount, ''); }
    if (els.resultsCountLabel) { setText(els.resultsCountLabel, ''); }
    if (els.loadMoreStatus) { setText(els.loadMoreStatus, ''); }
    if (els.similarLoadMoreStatus) { setText(els.similarLoadMoreStatus, ''); }
    if (els.videoLoadMoreStatus) { setText(els.videoLoadMoreStatus, ''); }
    if (els.loadMoreBtn) { hide(els.loadMoreBtn); }
    if (els.similarLoadMoreBtn) { hide(els.similarLoadMoreBtn); }
    if (els.videoLoadMoreBtn) { hide(els.videoLoadMoreBtn); }
    if (els.similarPartition) { hide(els.similarPartition); }
    if (els.videoPartition) { hide(els.videoPartition); }
    if (els.mtimeFallbackNotice) { hide(els.mtimeFallbackNotice); }
    if (els.csvLink) {
      els.csvLink.setAttribute('href', '#');
      els.csvLink.setAttribute('aria-disabled', 'true');
      els.csvLink.classList.add('is-disabled');
    }
    if (els.xlsxLink) {
      els.xlsxLink.setAttribute('href', '#');
      els.xlsxLink.setAttribute('aria-disabled', 'true');
      els.xlsxLink.classList.add('is-disabled');
    }
    updateSelectionSummary();
  }

  function resetExactPartition() {
    var results = state.results;
    results.groups = [];
    results.total = 0;
    results.offset = 0;
    results.loaded = false;
    results.loading = false;
    results.seq += 1;
    if (els.groupsList) { clear(els.groupsList); }
    setHidden(els.resultsLoading, true);
    setHidden(els.resultsError, true);
    setHidden(els.resultsEmpty, true);
  }

  function resetSimilarPartition() {
    var results = state.results;
    results.similarGroups = [];
    results.similarTotal = 0;
    results.similarOffset = 0;
    results.similarLoaded = false;
    results.similarLoading = false;
    results.similarSeq += 1;
    if (els.similarGroupsList) { clear(els.similarGroupsList); }
    setHidden(els.similarLoading, true);
    setHidden(els.similarError, true);
    setHidden(els.similarEmpty, true);
  }

  function resetVideoPartition() {
    var results = state.results;
    results.videoGroups = [];
    results.videoTotal = 0;
    results.videoOffset = 0;
    results.videoLoaded = false;
    results.videoLoading = false;
    results.videoSeq += 1;
    results.videoSelected = new Map();
    results.videoSelectAllMode = false;
    if (els.videoGroupsList) { clear(els.videoGroupsList); }
    setHidden(els.videoLoading, true);
    setHidden(els.videoError, true);
    setHidden(els.videoEmpty, true);
    setHidden(els.videoPartition, true);
  }

  function setResultsStateBoxes(options) {
    options = options || {};
    setHidden(els.resultsLoading, !options.loading);
    setHidden(els.resultsError, !options.error);
    if (options.error) { setText(els.resultsError, options.error); }
    setHidden(els.resultsEmpty, !options.empty);
    if (options.empty && options.emptyText) { setText(els.resultsEmpty, options.emptyText); }
  }

  function currentScanStats() {
    var stats = {};
    if (state.scan.data && state.scan.data.stats) { Object.assign(stats, state.scan.data.stats); }
    if (state.scan.stats) { Object.assign(stats, state.scan.stats); }
    if (state.scan.data) {
      if (typeof state.scan.data.needs_manual_keep_groups === 'number') {
        stats.needs_manual_keep_groups = state.scan.data.needs_manual_keep_groups;
      }
      if (state.scan.data.creation_time_unsafe !== undefined) {
        stats.creation_time_unsafe = state.scan.data.creation_time_unsafe;
      }
      if (typeof state.scan.data.mtime_fallback_groups === 'number') {
        stats.mtime_fallback_groups = state.scan.data.mtime_fallback_groups;
      }
    }
    return stats;
  }

  async function loadScanStats() {
    if (!state.scan.id) { return; }
    var scanId = state.scan.id;
    try {
      var res = await api('/api/scans/' + encodePath(scanId) + '/stats', { timeout: 15000 });
      if (state.scan.id !== scanId) { return; }
      var detail = res && (res.stats || res);
      if (detail && typeof detail === 'object') {
        var merged = {};
        ['files_seen', 'dirs_scanned', 'files_scanned', 'candidate_files', 'groups', 'duplicate_groups',
          'duplicate_files', 'reclaimable_bytes', 'selectable_duplicate_files', 'selectable_reclaimable_bytes',
          'exact_groups', 'exact_files', 'similar_groups', 'similar_files', 'similar_reclaimable_bytes',
          'similarity_candidates', 'similarity_features', 'similarity_cache_hits', 'similarity_cache_misses',
          'similarity_pairs_checked', 'mtime_fallback_groups',
          'video_groups', 'video_files', 'video_candidates', 'video_cache_hits', 'video_cache_misses',
          'video_ffmpeg_used', 'bytes_hashed', 'bytes_per_sec', 'errors'].forEach(function (key) {
          if (typeof detail[key] === 'number') { merged[key] = detail[key]; }
        });
        if (Object.keys(merged).length) {
          state.scan.stats = merged;
          renderResultsSummary();
          updateSelectionSummary();
        }
        state.scan.statsDetail = detail;
      }
    } catch (e) { /* 统计接口失败不阻塞结果展示 */ }
  }

  async function loadResults(reset) {
    if (!state.scan.id) { return; }
    var similarMode = state.results.scanMode === 'similar';
    var videoOn = videoScanEnabledForResults();
    if (reset) {
      resetExactPartition();
      if (similarMode) { resetSimilarPartition(); }
      if (videoOn) { resetVideoPartition(); }
      clearSelectionState();
    }
    if (!similarMode) { setHidden(els.similarPartition, true); }
    if (!videoOn) { setHidden(els.videoPartition, true); }
    var tasks = [loadExactPartition(reset)];
    if (similarMode) { tasks.push(loadSimilarPartition(reset)); }
    if (videoOn) { tasks.push(loadVideoPartition(reset)); }
    await Promise.all(tasks);
  }

  function newOnlyQuery() {
    return state.results.newOnly ? '&new_only=1' : '';
  }

  function syncNewOnlyUi() {
    if (!els.newOnlyToggle) { return; }
    var visible = state.results.newOnly
      || Number(state.results.newTotal || 0) > 0
      || Number((currentScanStats() || {}).new_groups || 0) > 0;
    setHidden(els.newOnlyLabel, !visible);
    els.newOnlyToggle.checked = !!state.results.newOnly;
    els.newOnlyToggle.title = visible
      ? '只显示本次扫描新增或内容变化的重复组'
      : '本次扫描没有新增/变化的重复组（首次全量扫描时正常）';
  }

  async function loadExactPartition(reset, offsetOverride) {
    var results = state.results;
    if (!state.scan.id) { return; }
    if (results.loading && !reset) { return; }
    if (results.loading && reset) { results.seq += 1; }
    var seq = results.seq + 1;
    results.seq = seq;
    results.loading = true;
    setHidden(els.resultsLoading, false);
    setHidden(els.resultsError, true);
    setHidden(els.resultsEmpty, true);
    var offset = reset ? 0 : results.groups.length;
    if (typeof offsetOverride === 'number' && isFinite(offsetOverride) && offsetOverride >= 0) {
      offset = Math.floor(offsetOverride);
    }
    var query = '?offset=' + offset + '&limit=' + results.limit +
      '&q=' + encodeURIComponent(results.q) + '&sort=' + encodeURIComponent(results.sort) +
      newOnlyQuery();
    if (results.scanMode === 'similar' || videoScanEnabledForResults()) { query += '&match_type=exact'; }
    try {
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/groups' + query, { timeout: 20000 });
      if (seq !== results.seq) { return; }
      var groups = Array.isArray(res.groups) ? res.groups : [];
      results.total = Number(res.exact_total !== undefined ? res.exact_total : res.total) || 0;
      results.exactTotal = results.total;
      if (typeof res.similar_total === 'number') { results.similarTotal = res.similar_total; }
      if (typeof res.new_total === 'number') { results.newTotal = res.new_total; }
      results.offset = res.offset === undefined ? offset : Number(res.offset);
      results.groups = reset ? groups : results.groups.concat(groups);
      results.loaded = true;
      results.loading = false;
      syncNewOnlyUi();
      renderResults();
      if (reset) { await loadScanStats(); }
    } catch (err) {
      if (seq !== results.seq) { return; }
      results.loading = false;
      setHidden(els.resultsLoading, true);
      setHidden(els.resultsError, false);
      setText(els.resultsError, errorMessage(err, '加载精准组失败，请稍后重试。'));
      updateSelectionSummary();
    }
  }

  async function loadSimilarPartition(reset, offsetOverride) {
    var results = state.results;
    if (!state.scan.id) { return; }
    if (results.similarLoading && !reset) { return; }
    if (results.similarLoading && reset) { results.similarSeq += 1; }
    var seq = results.similarSeq + 1;
    results.similarSeq = seq;
    results.similarLoading = true;
    setHidden(els.similarLoading, false);
    setHidden(els.similarError, true);
    setHidden(els.similarEmpty, true);
    var offset = reset ? 0 : results.similarGroups.length;
    if (typeof offsetOverride === 'number' && isFinite(offsetOverride) && offsetOverride >= 0) {
      offset = Math.floor(offsetOverride);
    }
    var query = '?offset=' + offset + '&limit=' + results.limit + '&match_type=similar&sort=similarity_desc' +
      '&q=' + encodeURIComponent(results.q) + newOnlyQuery();
    try {
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/groups' + query, { timeout: 20000 });
      if (seq !== results.similarSeq) { return; }
      var groups = Array.isArray(res.groups) ? res.groups : [];
      results.similarTotal = Number(res.similar_total !== undefined ? res.similar_total : res.total) || 0;
      if (typeof res.exact_total === 'number') {
        results.exactTotal = res.exact_total;
        results.total = res.exact_total;
      }
      results.similarOffset = res.offset === undefined ? offset : Number(res.offset);
      results.similarGroups = reset ? groups : results.similarGroups.concat(groups);
      results.similarLoaded = true;
      results.similarLoading = false;
      renderResults();
    } catch (err) {
      if (seq !== results.similarSeq) { return; }
      results.similarLoading = false;
      setHidden(els.similarLoading, true);
      setHidden(els.similarError, false);
      setText(els.similarError, errorMessage(err, '加载内容相似组失败，请稍后重试。'));
    }
  }

  async function loadVideoPartition(reset, offsetOverride) {
    var results = state.results;
    if (!state.scan.id) { return; }
    if (results.videoLoading && !reset) { return; }
    if (results.videoLoading && reset) { results.videoSeq += 1; }
    var seq = results.videoSeq + 1;
    results.videoSeq = seq;
    results.videoLoading = true;
    setHidden(els.videoLoading, false);
    setHidden(els.videoError, true);
    setHidden(els.videoEmpty, true);
    var offset = reset ? 0 : results.videoGroups.length;
    if (typeof offsetOverride === 'number' && isFinite(offsetOverride) && offsetOverride >= 0) {
      offset = Math.floor(offsetOverride);
    }
    var query = '?offset=' + offset + '&limit=' + results.limit + '&match_type=video&sort=similarity_desc' +
      '&q=' + encodeURIComponent(results.q) + newOnlyQuery();
    try {
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/groups' + query, { timeout: 25000 });
      if (seq !== results.videoSeq) { return; }
      var groups = Array.isArray(res.groups) ? res.groups : [];
      results.videoTotal = Number(res.video_total !== undefined ? res.video_total : res.total) || 0;
      if (typeof res.exact_total === 'number') {
        results.exactTotal = res.exact_total;
        results.total = res.exact_total;
      }
      if (typeof res.similar_total === 'number') { results.similarTotal = res.similar_total; }
      results.videoOffset = res.offset === undefined ? offset : Number(res.offset);
      results.videoGroups = reset ? groups : results.videoGroups.concat(groups);
      results.videoLoaded = true;
      results.videoLoading = false;
      if (results.videoSelectAllMode) { autoSelectVideoGroups(groups); }
      renderResults();
    } catch (err) {
      if (seq !== results.videoSeq) { return; }
      results.videoLoading = false;
      setHidden(els.videoLoading, true);
      setHidden(els.videoError, false);
      setText(els.videoError, errorMessage(err, '加载视频视觉相似组失败，请稍后重试。'));
    }
  }

  function renderResults() {
    show(els.resultsSection);
    var results = state.results;
    var similarMode = results.scanMode === 'similar';
    var videoOn = videoScanEnabledForResults();
    var stats = currentScanStats();
    renderResultsSummary(stats);
    renderMtimeFallbackNotice(stats);
    renderResultsHeader(similarMode, results);
    if (els.csvLink && state.scan.id) {
      els.csvLink.setAttribute('href', '/api/scans/' + encodePath(state.scan.id) + '/csv');
      els.csvLink.setAttribute('download', 'duplicate-scan-' + state.scan.id + '.csv');
      els.csvLink.removeAttribute('aria-disabled');
      els.csvLink.classList.remove('is-disabled');
    }
    if (els.xlsxLink && state.scan.id) {
      els.xlsxLink.setAttribute('href', '/api/scans/' + encodePath(state.scan.id) + '/xlsx');
      els.xlsxLink.setAttribute('download', 'duplicate-scan-' + state.scan.id + '.xlsx');
      els.xlsxLink.removeAttribute('aria-disabled');
      els.xlsxLink.classList.remove('is-disabled');
    }
    renderGroups();
    if (similarMode) { renderSimilarGroups(); }
    if (videoOn) {
      renderVideoGroups();
    } else {
      setHidden(els.videoPartition, true);
    }
    updateSelectionSummary();
  }

  function renderResultsHeader(similarMode, results) {
    if (els.resultsModeBadge) {
      setText(els.resultsModeBadge, similarMode ? '内容相似模式' : '精准哈希模式');
      els.resultsModeBadge.className = 'badge results-mode-badge ' + (similarMode ? 'badge-purple' : 'badge-gray');
    }
    if (els.resultsSubtitle) {
      setText(els.resultsSubtitle, similarMode
        ? '结果按“哈希一致（精准重复）”与“内容相似”分区展示。相似组存在误匹配概率，必须逐组人工复核后逐条选择，禁止批量删除。'
        : '按组列出内容相同的文件。保留项已高亮，需手动选择的组请先指定保留项。');
    }
    if (els.exactPartitionTitle) { setText(els.exactPartitionTitle, similarMode ? '哈希一致（精准重复）' : '重复文件'); }
    setHidden(els.exactPartitionBadge, !similarMode);
    if (els.sortLabel) { setText(els.sortLabel, similarMode ? '精准组排序' : '排序'); }
    var countText = '共 ' + fmtInt(results.total) + ' 组';
    if (results.q) { countText += ' · 搜索“' + results.q + '”'; }
    countText += ' · 已加载 ' + fmtInt(results.groups.length) + ' 组';
    setText(els.resultsCountLabel, countText);
    setText(els.exactPartitionCount, countText);
    setText(els.loadMoreStatus, '已加载 ' + fmtInt(results.groups.length) + ' / ' + fmtInt(results.total) + ' 组');
    var hasMore = results.groups.length < results.total;
    setHidden(els.loadMoreBtn, !hasMore);
    setText(els.loadMoreBtn, '加载更多（剩余 ' + fmtInt(Math.max(0, results.total - results.groups.length)) + ' 组）');
    setHidden(els.resultsLoading, !results.loading);
    var exactEmpty = results.loaded && !results.loading && results.total === 0;
    setHidden(els.resultsEmpty, !exactEmpty);
    if (exactEmpty) {
      setText(els.resultsEmpty, results.q
        ? '未找到匹配“' + results.q + '”的精准组。'
        : (similarMode ? '哈希一致（精准重复）组为空，请查看下方内容相似分区。' : '没有找到重复文件。'));
    }
    setHidden(els.similarPartition, !similarMode);
    var videoOn = videoScanEnabledForResults();
    setHidden(els.videoPartition, !videoOn);
    if (videoOn) {
      setText(els.videoPartitionCount, '共 ' + fmtInt(results.videoTotal) + ' 组 · 已加载 ' + fmtInt(results.videoGroups.length) + ' 组');
      setText(els.videoLoadMoreStatus, '已加载 ' + fmtInt(results.videoGroups.length) + ' / ' + fmtInt(results.videoTotal) + ' 组');
      var videoHasMore = results.videoGroups.length < results.videoTotal;
      setHidden(els.videoLoadMoreBtn, !videoHasMore);
      setText(els.videoLoadMoreBtn, '加载更多视频组（剩余 ' + fmtInt(Math.max(0, results.videoTotal - results.videoGroups.length)) + ' 组）');
      setHidden(els.videoLoading, !results.videoLoading);
      var videoEmpty = results.videoLoaded && !results.videoLoading && results.videoTotal === 0;
      setHidden(els.videoEmpty, !videoEmpty);
      if (videoEmpty) {
        setText(els.videoEmpty, results.q ? '未找到匹配“' + results.q + '”的视频相似组。' : '没有视频视觉相似的组。');
      }
      var baseSubtitle = similarMode
        ? '结果按“哈希一致（精准重复）”与“内容相似”分区展示。相似组存在误匹配概率，必须逐组人工复核后逐条选择，禁止批量删除。'
        : '按组列出内容相同的文件。保留项已高亮，需手动选择的组请先指定保留项。';
      setText(els.resultsSubtitle, baseSubtitle + ' 视频视觉相似为独立分区，默认不勾选，支持组级/全局全选视频副本。');
    }
    if (similarMode) {
      setText(els.similarPartitionCount, '共 ' + fmtInt(results.similarTotal) + ' 组 · 已加载 ' + fmtInt(results.similarGroups.length) + ' 组');
      setText(els.similarLoadMoreStatus, '已加载 ' + fmtInt(results.similarGroups.length) + ' / ' + fmtInt(results.similarTotal) + ' 组');
      var similarHasMore = results.similarGroups.length < results.similarTotal;
      setHidden(els.similarLoadMoreBtn, !similarHasMore);
      setText(els.similarLoadMoreBtn, '加载更多相似组（剩余 ' + fmtInt(Math.max(0, results.similarTotal - results.similarGroups.length)) + ' 组）');
      setHidden(els.similarLoading, !results.similarLoading);
      var similarEmpty = results.similarLoaded && !results.similarLoading && results.similarTotal === 0;
      setHidden(els.similarEmpty, !similarEmpty);
      if (similarEmpty) {
        setText(els.similarEmpty, results.q ? '未找到匹配“' + results.q + '”的内容相似组。' : '没有内容相似的组。');
      }
    }
  }

  function renderGroups() {
    clear(els.groupsList);
    state.results.groups.forEach(function (group, index) {
      els.groupsList.appendChild(renderGroupCard(group, index));
    });
  }

  function renderSimilarGroups() {
    if (!els.similarGroupsList) { return; }
    clear(els.similarGroupsList);
    state.results.similarGroups.forEach(function (group, index) {
      els.similarGroupsList.appendChild(renderSimilarGroupCard(group, index));
    });
  }

  function renderResultsSummary(stats) {
    stats = stats || currentScanStats();
    var similarMode = state.results.scanMode === 'similar';
    var manualGroups = Number(stats.needs_manual_keep_groups);
    if (!isFinite(manualGroups)) {
      manualGroups = state.results.groups.filter(function (g) { return g.needs_manual_keep; }).length
        + state.results.similarGroups.filter(function (g) { return g.needs_manual_keep; }).length;
    }
    var unsafe = !!(state.scan.data && state.scan.data.creation_time_unsafe);
    var skipped = stats.skipped || {};
    var summaryCards;
    if (similarMode) {
      var exactGroups = Number(stats.exact_groups !== undefined ? stats.exact_groups : stats.duplicate_groups) || 0;
      var similarGroups = Number(stats.similar_groups) || 0;
      var exactFiles = Number(stats.exact_files !== undefined ? stats.exact_files : stats.selectable_duplicate_files) || 0;
      var similarFiles = Number(stats.similar_files) || 0;
      summaryCards = [
        summaryCard('folder', 'is-green', fmtInt(exactGroups) + ' 组', '哈希一致（精准重复）',
          '精准文件 ' + fmtInt(exactFiles) + ' 个'),
        summaryCard('warning', 'is-amber', fmtInt(similarGroups) + ' 组', '内容相似',
          '相似文件 ' + fmtInt(similarFiles) + ' 个 · 需人工复核'),
        summaryCard('trash', 'is-green', fmtBytes(stats.reclaimable_bytes), '精准可释放空间',
          '仅统计安全精准组'),
        summaryCard('info', 'is-amber', fmtBytes(stats.similar_reclaimable_bytes), '相似潜在可释放',
          '仅作参考，须人工复核'),
        summaryCard('search', '', fmtInt(stats.files_scanned), '已扫描文件',
          '已读取 ' + fmtInt(stats.files_seen) + ' 个文件'),
        summaryCard('warning', (manualGroups > 0 || unsafe) ? 'is-amber' : 'is-green',
          fmtInt(manualGroups) + ' 组', '需人工确认',
          (manualGroups > 0 || unsafe) ? '本组不参与自动删除' : '无需人工确认')
      ];
    } else {
      summaryCards = [
        summaryCard('folder', '',
          fmtInt(stats.exact_groups !== undefined ? stats.exact_groups : (stats.duplicate_groups || stats.groups || 0)), '重复组',
          '已扫描 ' + fmtInt(stats.files_scanned) + ' 个文件'),
        summaryCard('file', '',
          fmtInt(stats.exact_files !== undefined ? stats.exact_files : stats.duplicate_files), '重复文件',
          '按组核对后再删除'),
        summaryCard('trash', 'is-green', fmtBytes(stats.reclaimable_bytes), '可释放空间',
          '移动到回收站可恢复'),
        summaryCard('search', '', fmtInt(stats.files_seen), '已读取文件',
          '跳过：空 ' + fmtInt(skipped.empty) + ' · 隐藏 ' + fmtInt(skipped.hidden) + ' · 后缀 ' + fmtInt(skipped.extension)),
        summaryCard('warning', (manualGroups > 0 || unsafe) ? 'is-amber' : 'is-green',
          fmtInt(manualGroups) + ' 组', '需人工确认',
          (manualGroups > 0 || unsafe) ? '本组不参与自动删除' : '无需人工确认')
      ];
    }
    var fallbackGroups = mtimeFallbackGroupCount(stats);
    if (fallbackGroups > 0) {
      summaryCards.push(summaryCard('warning', 'is-amber', fmtInt(fallbackGroups) + ' 组', '修改时间降级',
        '无创建时间 · 按修改时间保留'));
    }
    // v0043：增量扫描统计（首次全量扫描时 new_files 接近全部，属正常）
    var newFiles = Number(stats.new_files) || 0;
    var changedFiles = Number(stats.changed_files) || 0;
    var cacheHits = Number(stats.hash_cache_hits) || 0;
    var reusedBytes = Number(stats.bytes_hashed_reused) || 0;
    if (cacheHits > 0 || newFiles > 0 || changedFiles > 0) {
      var incrementalBits = '本次新增 ' + fmtInt(newFiles) + ' · 变化 ' + fmtInt(changedFiles) +
        ' · 缓存命中 ' + fmtInt(cacheHits) + ' 个文件';
      summaryCards.push(summaryCard('refresh', cacheHits > 0 ? 'is-green' : '',
        fmtBytes(reusedBytes), '增量缓存复用',
        incrementalBits + '（未重复读取 ' + fmtBytes(reusedBytes) + '）'));
    }
    var excludedByRule = Number(skipped.rule) || 0;
    var sidecarFiles = Number(stats.sidecar_files !== undefined
      ? stats.sidecar_files : skipped.sidecar) || 0;
    if (excludedByRule > 0 || sidecarFiles > 0) {
      summaryCards.push(summaryCard('lock', '',
        fmtInt(excludedByRule) + ' / ' + fmtInt(sidecarFiles), '排除规则 / 附属文件',
        '按文件名规则与 .ignore 跳过 ' + fmtInt(excludedByRule) + ' 个，影视附属文件跳过 ' + fmtInt(sidecarFiles) + ' 个'));
    }
    // v0044：删除白名单命中数（命中的文件不可勾选，也不会进入删除计划）
    var protectedFiles = Number(stats.protected_files) || 0;
    if (protectedFiles > 0) {
      summaryCards.push(summaryCard('lock', 'is-amber',
        fmtInt(protectedFiles) + ' 个', '受删除白名单保护',
        '共 ' + fmtInt(stats.protected_groups || 0)
          + ' 组含受保护文件；这些文件不可勾选、删除计划也会跳过'));
    }
    var videoGroupCount = Number(stats.video_groups);
    if (!isFinite(videoGroupCount) || videoGroupCount <= 0) {
      videoGroupCount = state.results.videoGroups.length;
    }
    if (videoGroupCount > 0) {
      var videoBytes = Number(stats.video_reclaimable_bytes);
      if (!isFinite(videoBytes) || videoBytes <= 0) {
        videoBytes = 0;
        state.results.videoGroups.forEach(function (group) { videoBytes += Number(group.wasted_bytes) || 0; });
      }
      summaryCards.push(summaryCard('eye', '', fmtInt(videoGroupCount) + ' 组', '视频视觉相似',
        '抽样帧比对 · 默认不勾选' + (videoBytes > 0 ? ' · 潜在 ' + fmtBytes(videoBytes) : '')));
    }
    clear(els.resultsSummary);
    els.resultsSummary.classList.toggle('is-similar', similarMode);
    els.resultsSummary.classList.toggle('is-many', summaryCards.length > 5);
    summaryCards.forEach(function (card) { els.resultsSummary.appendChild(card); });
  }

  function summaryCard(iconName, tint, value, label, sub) {
    return el('div', { className: 'summary-card' }, [
      el('span', { className: 'summary-icon ' + (tint || '') }, [icon(iconName)]),
      el('div', { className: 'summary-body' }, [
        el('div', { className: 'summary-value', text: value }),
        el('div', { className: 'summary-label', text: label }),
        el('div', { className: 'summary-sub', text: sub })
      ])
    ]);
  }

  function truncHash(hash) {
    var text = String(hash || '');
    if (text.length <= 22) { return text || '—'; }
    return text.slice(0, 14) + '…' + text.slice(-8);
  }

  // ---------- 无创建时间 / mtime 降级 ----------
  function isMtimeFallbackGroup(group) {
    if (!group) { return false; }
    if (group.time_fallback === true) { return true; }
    var reason = String(group.keep_reason || '');
    if (MTIME_FALLBACK_REASONS.indexOf(reason) !== -1) { return true; }
    var source = String(group.keep_time_source || '').toLowerCase();
    return source === 'mtime' || source === 'modified' || source.indexOf('mtime') !== -1;
  }

  function keepTimeSourceLabel(source) {
    var value = String(source || '').toLowerCase();
    if (!value) { return ''; }
    if (value.indexOf('mtime') !== -1 || value === 'modified') { return '修改时间（mtime）'; }
    if (value.indexOf('ctime') !== -1) { return '状态变更时间（ctime，降级）'; }
    if (value.indexOf('statx') !== -1 || value.indexOf('birth') !== -1 ||
        value.indexOf('btime') !== -1 || value === 'created') {
      return '文件系统创建时间';
    }
    return '';
  }

  function mtimeFallbackGroupCount(stats) {
    stats = stats || currentScanStats();
    var count = Number(stats.mtime_fallback_groups);
    if (isFinite(count) && count >= 0) { return count; }
    if (state.scan.data && typeof state.scan.data.mtime_fallback_groups === 'number') {
      return state.scan.data.mtime_fallback_groups;
    }
    return state.results.groups.filter(isMtimeFallbackGroup).length
      + state.results.similarGroups.filter(isMtimeFallbackGroup).length;
  }

  function fallbackSelectionInfo() {
    var results = state.results;
    var override = state.deleteFlow.override;
    if (override && override.similarGroupId) {
      var scoped = null;
      results.similarGroups.forEach(function (group) {
        if (group.group_id === override.similarGroupId) { scoped = group; }
      });
      return { count: (scoped && isMtimeFallbackGroup(scoped)) ? 1 : 0, allMode: false, scoped: true };
    }
    if (results.mode === 'all') {
      return { count: mtimeFallbackGroupCount(), allMode: true, scoped: false };
    }
    var count = 0;
    results.groups.forEach(function (group) {
      if (!isMtimeFallbackGroup(group)) { return; }
      var hasSelected = (group.files || []).some(function (file) {
        return results.selected.has(file.file_id);
      });
      if (hasSelected) { count += 1; }
    });
    results.similarGroups.forEach(function (group) {
      if (!isMtimeFallbackGroup(group)) { return; }
      var hasSelected = (group.files || []).some(function (file) {
        return results.similarSelected.has(file.file_id);
      });
      if (hasSelected) { count += 1; }
    });
    results.videoGroups.forEach(function (group) {
      if (!isMtimeFallbackGroup(group)) { return; }
      var hasSelected = (group.files || []).some(function (file) {
        return results.videoSelected.has(file.file_id);
      });
      if (hasSelected) { count += 1; }
    });
    return { count: count, allMode: false, scoped: false };
  }

  function renderMtimeFallbackNotice(stats) {
    if (!els.mtimeFallbackNotice) { return; }
    var count = mtimeFallbackGroupCount(stats);
    if (count > 0) {
      setText(els.mtimeFallbackNoticeText,
        '本次扫描有 ' + fmtInt(count) + ' 组无法读取可靠创建时间（btime），已改用修改时间（mtime）选择保留项。' +
        '请人工核对这些组的保留文件后再删除；创建时间在结果中显示为“未知/降级”时不代表文件创建时间。');
      show(els.mtimeFallbackNotice);
    } else {
      hide(els.mtimeFallbackNotice);
    }
  }

  function incrementalGroupBadge(group) {
    if (!group || typeof group !== 'object') { return null; }
    var newCount = Number(group.new_files || 0);
    var changedCount = Number(group.changed_files || 0);
    if (!newCount && !changedCount) { return null; }
    var parts = [];
    if (newCount) { parts.push('新增 ' + fmtInt(newCount)); }
    if (changedCount) { parts.push('变化 ' + fmtInt(changedCount)); }
    return el('span', {
      className: 'badge badge-blue',
      attrs: { title: '相对上次扫描：本次新增/内容变化的文件数（增量扫描统计）' },
      text: '本次增量 · ' + parts.join(' / ')
    });
  }

  function renderGroupCard(group, index) {
    var manual = !!group.needs_manual_keep;
    var keeperId = manual ? null : (group.keep_file_id || null);
    var card = el('article', {
      className: 'group-card' + (manual ? ' needs-keep' : ''),
      dataset: { groupKey: encodePath(group.group_id), groupId: group.group_id }
    });

    var fallback = isMtimeFallbackGroup(group);
    var badges = [];
    if (manual) {
      badges.push(el('span', { className: 'badge badge-amber', text: '需手动选择 · 禁止删除' }));
    }
    if (fallback) {
      var fallbackTitle = '保留依据：修改时间（mtime）' +
        (group.keep_reason ? '；' + (KEEP_LABEL[group.keep_reason] || group.keep_reason) : '') +
        '。创建时间不可用，请人工核对保留文件。';
      badges.push(el('span', {
        className: 'badge badge-amber mtime-fallback-badge',
        attrs: { title: fallbackTitle },
        text: MTIME_FALLBACK_LABEL
      }));
    } else if (!manual) {
      var reasonLabel = KEEP_LABEL[group.keep_reason] || '保留项';
      badges.push(el('span', { className: 'badge badge-green', text: '保留：' + reasonLabel }));
    }
    if (group.creation_time_suspect && !fallback) {
      badges.push(el('span', { className: 'badge badge-amber', text: '创建时间存疑' }));
    }
    var exactIncrementalBadge = incrementalGroupBadge(group);
    if (exactIncrementalBadge) { badges.push(exactIncrementalBadge); }
    if (Number(group.protected_files) > 0) {
      badges.push(el('span', {
        className: 'badge badge-shield',
        attrs: { title: '组内受删除白名单保护的文件不会进入删除计划' },
        text: '受保护 ' + fmtInt(group.protected_files)
      }));
    }

    var groupSubItems = [
      el('span', { className: 'mono', attrs: { title: group.hash || '' }, text: truncHash(group.hash) }),
      el('span', { text: '单文件 ' + fmtBytes(group.size) }),
      el('span', { text: '可释放 ' + fmtBytes(group.wasted_bytes) }),
      el('span', { text: String(group.algo || 'sha256').toUpperCase() })
    ];
    if (fallback) {
      var keepSource = keepTimeSourceLabel(group.keep_time_source || 'mtime');
      if (keepSource) {
        groupSubItems.push(el('span', { className: 'keep-source-note', text: '保留依据：' + keepSource }));
      }
    }
    var headMain = el('div', { className: 'group-head-main' }, [
      el('div', { className: 'group-title', text: '组 #' + (index + 1) + ' · ' + fmtInt(group.count) + ' 个文件' }),
      el('div', { className: 'group-sub' }, groupSubItems)
    ]);

    var groupCheck = el('input', {
      attrs: {
        type: 'checkbox',
        disabled: manual,
        'aria-label': manual ? '本组需人工确认，禁止选择删除' : '选择本组全部待删除副本'
      }
    });
    groupCheck.disabled = manual;
    if (!manual) {
      groupCheck.addEventListener('change', function () {
        toggleGroupSelection(group, groupCheck.checked);
        syncGroupCheckbox(group);
        updateSelectionSummary();
      });
    }
    var groupCheckLabel = el('label', { className: 'group-check' }, [
      groupCheck,
      manual ? '本组不参与删除' : '选择本组副本'
    ]);

    var sideChildren = [
      el('div', { className: 'group-badges' }, badges),
      groupCheckLabel
    ];
    if (mergeEnabled()) {
      var mergeCandidates = (group.files || []).filter(function (item) {
        return !item.is_keep && !item.protected;
      });
      if (mergeCandidates.length) {
        var mergeBtn = el('button', {
          className: 'btn btn-ghost btn-sm group-merge-btn',
          attrs: {
            type: 'button',
            title: '把这些副本替换成指向保留项的硬链接：节省空间、不删除路径，合并后同一内容共享'
          },
          text: '合并为硬链接'
        });
        mergeBtn.addEventListener('click', function () {
          openMergeGroupDialog(group);
        });
        sideChildren.push(mergeBtn);
      }
    }
    var side = el('div', { className: 'group-head-side' }, sideChildren);
    var head = el('div', { className: 'group-head' }, [headMain, side]);
    if (manual) {
      head.appendChild(el('p', { className: 'group-hint' }, [
        icon('warning'),
        '该组创建时间无法可靠区分，已禁止自动删除；请先在 DSM 中人工确认保留项，本组不参与本次清理。'
      ]));
    } else if (fallback) {
      head.appendChild(el('p', { className: 'group-hint is-fallback' }, [
        icon('warning'),
        '该组无可靠创建时间（btime），保留项按修改时间（mtime）选择；请人工核对保留文件后再删除。'
      ]));
    }
    card.appendChild(head);

    var list = el('div', { className: 'file-list' });
    (group.files || []).forEach(function (file) {
      list.appendChild(renderFileRow(group, file, keeperId, manual));
    });
    card.appendChild(list);
    card.appendChild(el('div', { className: 'group-foot' }, [
      el('span', { text: manual ? '本组已排除在删除范围之外' : '保留项不可勾选；需删除的副本请逐项确认' })
    ]));

    syncGroupCheckbox(group, card);
    return card;
  }

  function renderFileRow(group, file, keeperId, manual) {
    var isKeeper = !manual && file.file_id === keeperId;
    var isProtected = !!file.protected && !isKeeper;
    var row = el('div', {
      className: 'file-row' + (isKeeper ? ' is-keeper' : '')
        + (isProtected ? ' is-protected' : ''),
      dataset: { fileId: file.file_id }
    });

    var checkbox = el('input', {
      attrs: {
        type: 'checkbox',
        disabled: manual || isKeeper || isProtected,
        'aria-label': isKeeper ? '保留项，不可选择删除'
          : (manual ? '本组禁止删除'
            : (isProtected ? '受保护文件，不可选择删除' : '选择删除 ' + file.path))
      }
    });
    checkbox.disabled = manual || isKeeper || isProtected;
    checkbox.checked = !manual && !isKeeper && !isProtected && isFileSelected(group, file);
    if (manual) { checkbox.title = '该组需人工确认，禁止删除'; }
    else if (isKeeper) { checkbox.title = '保留项不可删除'; }
    else if (isProtected) {
      checkbox.title = '删除白名单保护：'
        + (PROTECT_REASON_LABEL[file.protect_reason] || '受保护');
    }
    if (!manual && !isKeeper && !isProtected) {
      checkbox.addEventListener('change', function () {
        toggleFileSelection(group, file, checkbox.checked);
        checkbox.checked = isFileSelected(group, file);
        syncGroupCheckbox(group);
        updateSelectionSummary();
      });
    }
    var selectWrap = el('div', { className: 'file-select' }, [checkbox]);

    var main = el('div', { className: 'file-main' }, [
      el('div', { className: 'file-path', attrs: { title: file.path }, text: file.path }),
      buildFileSub(group, file)
    ]);

    var times = buildFileTimes(file, group);
    var actions = buildFileActions(group, file, isKeeper, manual);

    appendChildren(row, [selectWrap, main, times, actions]);
    return row;
  }

  function buildFileSub(group, file) {
    var copyBtn = el('button', {
      className: 'icon-btn',
      attrs: { type: 'button', title: '复制完整哈希', 'aria-label': '复制完整哈希' }
    }, [icon('copy')]);
    copyBtn.addEventListener('click', function () {
      if (file.hash) { copyWithFeedback(file.hash, copyBtn); }
      else { toast('该文件没有可复制的哈希值。', 'error'); }
    });
    var hashCode = el('code', {
      className: 'hash-code',
      attrs: { title: file.hash || '' },
      text: file.hash || '—'
    });
    var subItems = [
      el('span', { text: fmtBytes(file.size) }),
      el('span', { className: 'file-hash' }, [
        el('span', { className: 'muted', text: String(group.algo || 'sha256').toUpperCase() }),
        hashCode,
        copyBtn
      ])
    ];
    if (file.protected) {
      subItems.push(el('span', {
        className: 'badge badge-shield',
        attrs: { title: file.protect_message || '删除白名单保护，不会进入删除计划' },
        text: '受保护 · '
          + (PROTECT_REASON_LABEL[file.protect_reason] || '白名单')
      }));
    }
    return el('div', { className: 'file-sub' }, subItems);
  }

  function buildFileTimes(file, group) {
    var fallback = isMtimeFallbackGroup(group);
    var source = SOURCE_INFO[file.created_source] || SOURCE_INFO.unknown;
    var title = source.title;
    var shortLabel = source.short;
    var cls = source.cls;
    var createdText = fmtDateTime(file.created_at);
    var labelText = '创建';
    if (fallback) {
      title += '；本组无可靠创建时间，保留项依据修改时间（mtime），此处“创建”时间不作为保留依据';
      if (!file.created_at) {
        labelText = '创建（未知）';
        createdText = '—';
        if (file.created_source !== 'statx' && file.created_source !== 'birthtime') {
          shortLabel = 'mtime 降级';
          cls = 'is-fallback';
        }
      }
    }
    return el('div', { className: 'file-times' }, [
      el('div', { className: 'time-item' }, [
        el('span', { className: 'time-label', text: labelText }),
        el('span', { text: createdText }),
        el('span', { className: 'time-src ' + cls, attrs: { title: title }, text: shortLabel })
      ]),
      el('div', { className: 'time-item' }, [
        el('span', { className: 'time-label', text: '修改' }),
        el('span', { text: fmtDateTime(file.modified_at) })
      ])
    ]);
  }

  function buildFileActions(group, file, isKeeper, manual) {
    var actions = el('div', { className: 'file-actions' });
    if (manual) {
      actions.appendChild(el('span', { className: 'badge badge-amber', text: '禁止删除' }));
    } else if (isKeeper) {
      var fallbackKeeper = isMtimeFallbackGroup(group);
      actions.appendChild(el('span', {
        className: 'badge ' + (fallbackKeeper ? 'badge-amber' : 'badge-green'),
        attrs: fallbackKeeper ? { title: '保留项按修改时间（mtime）选择，创建时间不可用' } : {},
        text: fallbackKeeper ? '保留 · 按修改时间' : '保留'
      }));
    }
    var previewBtn = el('button', {
      className: 'icon-btn',
      attrs: { type: 'button', title: '预览文件', 'aria-label': '预览文件 ' + file.path }
    }, [icon('eye')]);
    previewBtn.addEventListener('click', function () { openPreview(file); });
    actions.appendChild(previewBtn);
    return actions;
  }

  function findGroupCard(groupId) {
    if (!els.groupsList) { return null; }
    return $('[data-group-key="' + encodePath(groupId) + '"]', els.groupsList);
  }

  function syncGroupCheckbox(group, card) {
    card = card || findGroupCard(group.group_id);
    if (!card) { return; }
    var input = $('.group-check input', card);
    if (!input) { return; }
    if (group.needs_manual_keep) {
      input.checked = false;
      input.indeterminate = false;
      input.disabled = true;
      return;
    }
    var selectable = (group.files || []).filter(function (file) {
      return isFileSelectable(group, file);
    });
    var selectedCount = selectable.filter(function (file) { return isFileSelected(group, file); }).length;
    if (!selectable.length) {
      input.checked = false;
      input.indeterminate = false;
      input.disabled = true;
      return;
    }
    input.disabled = false;
    input.checked = selectable.length > 0 && selectedCount === selectable.length;
    input.indeterminate = selectedCount > 0 && selectedCount < selectable.length;
  }

  // ---------- 内容相似组渲染（人工复核门控） ----------
  function similarityMethodLabel(method) {
    if (method === 'phash') { return '图像感知哈希'; }
    if (method === 'bytes') { return '字节 MinHash'; }
    if (method === 'minhash') { return '文本 MinHash'; }
    return method || '特征比对';
  }

  function groupKeeperFileId(group) {
    if (!group) { return null; }
    if (group.needs_manual_keep) { return null; }
    if (group.keep_file_id) { return group.keep_file_id; }
    var files = group.files || [];
    for (var i = 0; i < files.length; i += 1) {
      if (files[i] && files[i].is_keep) { return files[i].file_id; }
    }
    return null;
  }

  function similarGroupSelectedCount(group) {
    var count = 0;
    (group.files || []).forEach(function (file) {
      if (state.results.similarSelected.has(file.file_id)) { count += 1; }
    });
    return count;
  }

  function similarGroupSelectedBytes(group) {
    var bytes = 0;
    (group.files || []).forEach(function (file) {
      if (state.results.similarSelected.has(file.file_id)) { bytes += Number(file.size) || 0; }
    });
    return bytes;
  }

  function findSimilarGroupCard(groupId) {
    if (!els.similarGroupsList) { return null; }
    return $('[data-group-key="' + encodePath(groupId) + '"]', els.similarGroupsList);
  }

  function refreshSimilarActionButtons() {
    state.results.similarGroups.forEach(function (group) {
      var card = findSimilarGroupCard(group.group_id);
      if (card) { updateSimilarGroupAction(group, card); }
    });
  }

  function updateSimilarGroupAction(group, card) {
    if (!card) { card = findSimilarGroupCard(group.group_id); }
    if (!card) { return; }
    var btn = $('.similar-delete-btn', card);
    var info = $('.group-foot-info', card);
    var reviewed = state.results.similarReview.has(group.group_id);
    var selectedCount = similarGroupSelectedCount(group);
    if (info) {
      if (group.needs_manual_keep) { setText(info, '本组已排除在删除范围之外'); }
      else if (!reviewed) { setText(info, '未复核：勾选“人工复核”后才可逐条选择'); }
      else { setText(info, '已复核：已选 ' + fmtInt(selectedCount) + ' 个副本（保留项不可选）'); }
    }
    if (btn) {
      var disabled = !reviewed || !!group.needs_manual_keep || selectedCount === 0 || state.deleteFlow.running;
      btn.disabled = disabled;
      btn.title = disabled
        ? (group.needs_manual_keep ? '本组禁止删除' : (!reviewed ? '请先人工复核本组' : (selectedCount === 0 ? '请逐条勾选要删除的副本' : '删除任务进行中')))
        : '仅删除本组已勾选的副本（需两步确认）';
    }
  }

  function syncSimilarGroupUi(group) {
    var card = findSimilarGroupCard(group.group_id);
    if (!card) { return; }
    var reviewed = state.results.similarReview.has(group.group_id);
    var bar = $('.similar-review-bar', card);
    if (bar) {
      bar.classList.toggle('is-reviewed', reviewed);
    }
    $$('.file-row', card).forEach(function (row) {
      var cb = $('input[type="checkbox"]', row);
      if (!cb) { return; }
      var fileId = row.dataset.fileId;
      var file = null;
      (group.files || []).forEach(function (item) { if (item.file_id === fileId) { file = item; } });
      if (!file) { return; }
      var keeperId = groupKeeperFileId(group);
      var selectable = reviewed && !group.needs_manual_keep && file.file_id !== keeperId;
      cb.disabled = !selectable;
      cb.checked = selectable && state.results.similarSelected.has(file.file_id);
      row.classList.toggle('is-await-review', !group.needs_manual_keep && !reviewed && file.file_id !== keeperId);
    });
    updateSimilarGroupAction(group, card);
  }

  function focusSimilarReview(groupId) {
    if (!els.similarGroupsList) { return; }
    var input = $('input[data-review-key="' + encodePath(groupId) + '"]', els.similarGroupsList);
    if (input) { focusSoon(input); }
  }

  function toggleSimilarReview(group, checked) {
    var results = state.results;
    if (!group || group.needs_manual_keep) { return; }
    if (checked) {
      results.similarReview.add(group.group_id);
    } else {
      results.similarReview.delete(group.group_id);
      (group.files || []).forEach(function (file) { results.similarSelected.delete(file.file_id); });
    }
    renderSimilarGroups();
    focusSimilarReview(group.group_id);
    updateSelectionSummary();
  }

  function toggleSimilarFileSelection(group, file, checked) {
    if (!group || !file) { return; }
    var results = state.results;
    if (group.needs_manual_keep) { return; }
    if (!results.similarReview.has(group.group_id)) {
      toast('请先勾选该组的“人工复核”，再逐条选择要删除的副本。', 'error');
      return;
    }
    if (file.file_id === groupKeeperFileId(group)) { return; }
    if (checked) {
      if (results.mode === 'all') {
        results.mode = 'none';
        results.selected = new Map();
        results.excluded = new Map();
        renderGroups();
        toast('已取消“全选精准组”：相似文件需与手动选择的精准组文件混合删除。', 'info', 4600);
      }
      results.similarSelected.set(file.file_id, {
        size: file.size, groupId: group.group_id, path: file.path
      });
    } else {
      results.similarSelected.delete(file.file_id);
    }
    syncSimilarGroupUi(group);
    updateSelectionSummary();
  }

  function renderSimilarGroupCard(group, index) {
    var manual = !!group.needs_manual_keep;
    var keeperId = groupKeeperFileId(group);
    var score = Number(group.similarity_score);
    if (!isFinite(score)) { score = null; }
    var reviewed = state.results.similarReview.has(group.group_id);
    var card = el('article', {
      className: 'group-card similar-group-card' + (manual ? ' needs-keep' : '') + (reviewed ? ' is-reviewed' : ''),
      dataset: { groupKey: encodePath(group.group_id), groupId: group.group_id, matchType: 'similar' }
    });
    var fallback = isMtimeFallbackGroup(group);
    var badges = [
      el('span', { className: 'similar-score-badge', text: '内容相似' + (score !== null ? ' · ' + score + '%' : '') })
    ];
    if (manual) {
      badges.push(el('span', { className: 'badge badge-amber', text: '需手动选择 · 禁止删除' }));
    } else if (!fallback) {
      badges.push(el('span', { className: 'badge badge-green', text: '保留：' + (KEEP_LABEL[group.keep_reason] || '保留项') }));
    }
    if (fallback) {
      var fallbackTitle = '保留依据：修改时间（mtime）' +
        (group.keep_reason ? '；' + (KEEP_LABEL[group.keep_reason] || group.keep_reason) : '') +
        '。创建时间不可用，请人工核对保留文件。';
      badges.push(el('span', {
        className: 'badge badge-amber mtime-fallback-badge',
        attrs: { title: fallbackTitle },
        text: MTIME_FALLBACK_LABEL
      }));
    }
    if (group.low_confidence) { badges.push(el('span', { className: 'badge low-confidence-badge', text: '低置信度' })); }
    if (group.creation_time_suspect && !fallback) { badges.push(el('span', { className: 'badge badge-amber', text: '创建时间存疑' })); }
    var similarIncrementalBadge = incrementalGroupBadge(group);
    if (similarIncrementalBadge) { badges.push(similarIncrementalBadge); }
    if (Number(group.protected_files) > 0) {
      badges.push(el('span', {
        className: 'badge badge-shield',
        attrs: { title: '组内受删除白名单保护的文件不会进入删除计划' },
        text: '受保护 ' + fmtInt(group.protected_files)
      }));
    }

    var similarSubItems = [
      el('span', { text: similarityMethodLabel(group.similarity_method) }),
      el('span', { text: '单文件 ' + fmtBytes(group.size) }),
      el('span', { text: '潜在可释放 ' + fmtBytes(group.wasted_bytes) }),
      el('span', { className: 'muted', text: '哈希：—' })
    ];
    if (fallback) {
      var similarKeepSource = keepTimeSourceLabel(group.keep_time_source || 'mtime');
      if (similarKeepSource) {
        similarSubItems.push(el('span', { className: 'keep-source-note', text: '保留依据：' + similarKeepSource }));
      }
    }
    var headMain = el('div', { className: 'group-head-main' }, [
      el('div', { className: 'group-title', text: '组 #' + (index + 1) + ' · ' + fmtInt(group.count) + ' 个文件' }),
      el('div', { className: 'group-sub' }, similarSubItems)
    ]);
    var head = el('div', { className: 'group-head' }, [headMain, el('div', { className: 'group-badges' }, badges)]);
    if (manual) {
      head.appendChild(el('p', { className: 'group-hint' }, [
        icon('warning'),
        '该组创建时间无法可靠区分，已禁止自动删除；请先在 DSM 中人工确认保留项，本组不参与本次清理。'
      ]));
    } else if (fallback) {
      head.appendChild(el('p', { className: 'group-hint is-fallback' }, [
        icon('warning'),
        '该组无可靠创建时间（btime），保留项按修改时间（mtime）选择；请人工核对保留项后再复核删除。'
      ]));
    }
    card.appendChild(head);

    var reviewCheckbox = el('input', { attrs: { type: 'checkbox', 'data-review-key': encodePath(group.group_id), 'aria-label': '人工复核组 #' + (index + 1) } });
    reviewCheckbox.checked = reviewed;
    reviewCheckbox.disabled = manual;
    if (!manual) {
      reviewCheckbox.addEventListener('change', function () {
        toggleSimilarReview(group, reviewCheckbox.checked);
      });
    }
    var reviewBar = el('div', { className: 'similar-review-bar' + (reviewed ? ' is-reviewed' : '') }, [
      el('label', { className: 'check' }, [
        reviewCheckbox,
        manual ? '本组禁止删除，无需复核' : '人工复核本组（必须勾选后才能逐条选择副本）'
      ]),
      el('span', {
        className: 'similar-review-hint',
        text: reviewed ? '已复核：请逐条勾选要删除的副本，保留项不可选。' : '未复核：组内文件选择已禁用。'
      })
    ]);
    card.appendChild(reviewBar);

    var list = el('div', { className: 'file-list' });
    (group.files || []).forEach(function (file) {
      list.appendChild(renderSimilarFileRow(group, file, keeperId, manual, reviewed));
    });
    card.appendChild(list);

    var footInfo = el('span', { className: 'group-foot-info', text: '' });
    var groupDeleteBtn = el('button', {
      className: 'btn btn-danger btn-sm similar-delete-btn', attrs: { type: 'button' },
      text: '删除本组所选副本'
    });
    groupDeleteBtn.disabled = true;
    groupDeleteBtn.addEventListener('click', function () {
      beginDelete({ similarGroupId: group.group_id });
    });
    var foot = el('div', { className: 'group-foot' }, [footInfo, groupDeleteBtn]);
    card.appendChild(foot);
    updateSimilarGroupAction(group, card);
    return card;
  }

  function buildContentHashSub(file) {
    var hash = file.hash || '';
    var copyBtn = null;
    if (hash) {
      copyBtn = el('button', {
        className: 'icon-btn',
        attrs: { type: 'button', title: '复制内容哈希', 'aria-label': '复制内容哈希' }
      }, [icon('copy')]);
      copyBtn.addEventListener('click', function () { copyWithFeedback(hash, copyBtn); });
    }
    return el('span', { className: 'file-hash' }, [
      el('span', { className: 'content-hash-label', text: '内容哈希' }),
      el('code', {
        className: 'hash-code' + (hash ? '' : ' is-empty'),
        attrs: { title: hash || '' },
        text: hash ? truncHash(hash) : '—'
      }),
      copyBtn
    ]);
  }

  function renderSimilarFileRow(group, file, keeperId, manual, reviewed) {
    var isKeeper = !manual && file.file_id === keeperId;
    var selectable = !manual && !isKeeper && reviewed;
    var selected = state.results.similarSelected.has(file.file_id);
    var rowClass = 'file-row' + (isKeeper ? ' is-keeper' : '') + (!manual && !isKeeper && !reviewed ? ' is-await-review' : '');
    var row = el('div', { className: rowClass, dataset: { fileId: file.file_id, matchType: 'similar' } });

    var checkbox = el('input', {
      attrs: {
        type: 'checkbox',
        disabled: !selectable,
        'aria-label': isKeeper ? '保留项，不可选择删除' : (manual ? '本组禁止删除' : (!reviewed ? '请先人工复核本组' : '选择删除 ' + file.path))
      }
    });
    checkbox.checked = selectable && selected;
    if (!selectable) {
      checkbox.title = manual ? '该组需人工确认，禁止删除'
        : (isKeeper ? '保留项不可删除' : '请先勾选“人工复核本组”');
    }
    if (selectable) {
      checkbox.addEventListener('change', function () {
        toggleSimilarFileSelection(group, file, checkbox.checked);
        checkbox.checked = state.results.similarSelected.has(file.file_id);
      });
    }
    var selectWrap = el('div', { className: 'file-select' }, [checkbox]);

    var score = Number(file.similarity_score);
    if (!isFinite(score)) { score = isKeeper ? 100 : (Number(group.similarity_score) || null); }
    var subItems = [
      el('span', { text: fmtBytes(file.size) }),
      el('span', { className: 'similar-file-score' }, [
        el('span', { className: 'muted', text: '相似度' }),
        el('span', { className: 'similar-score-badge', text: score === null ? '—' : String(score) + '%' })
      ]),
      buildContentHashSub(file)
    ];
    if (file.low_confidence) {
      subItems.push(el('span', { className: 'badge low-confidence-badge', text: '低置信度' }));
    }
    var main = el('div', { className: 'file-main' }, [
      el('div', { className: 'file-path', attrs: { title: file.path }, text: file.path }),
      el('div', { className: 'file-sub' }, subItems)
    ]);

    var times = buildFileTimes(file, group);
    var actions = buildFileActions(group, file, isKeeper, manual);
    appendChildren(row, [selectWrap, main, times, actions]);
    return row;
  }

  // ---------- 视频视觉相似组渲染与批量选择 ----------
  function videoGroupKeeperId(group) {
    return groupKeeperFileId(group);
  }

  function videoGroupSelectedCount(group) {
    var count = 0;
    (group.files || []).forEach(function (file) {
      if (state.results.videoSelected.has(file.file_id)) { count += 1; }
    });
    return count;
  }

  function findVideoGroupCard(groupId) {
    if (!els.videoGroupsList) { return null; }
    return $('[data-group-key="' + encodePath(groupId) + '"]', els.videoGroupsList);
  }

  function videoFingerprintShort(fingerprint) {
    var text = String(fingerprint || '');
    if (!text) { return ''; }
    if (text.length <= 30) { return text; }
    return text.slice(0, 18) + '…' + text.slice(-8);
  }

  function renderVideoGroups() {
    if (!els.videoGroupsList) { return; }
    clear(els.videoGroupsList);
    state.results.videoGroups.forEach(function (group, index) {
      els.videoGroupsList.appendChild(renderVideoGroupCard(group, index));
    });
    updateVideoSelectAllButtonLabel();
  }

  function renderVideoGroupCard(group, index) {
    var manual = !!group.needs_manual_keep;
    var batchAllowed = group.allow_batch_delete !== false;
    var keeperId = videoGroupKeeperId(group);
    var fallback = isMtimeFallbackGroup(group);
    var score = Number(group.video_similarity_score !== undefined ? group.video_similarity_score : group.similarity_score);
    if (!isFinite(score)) { score = null; }
    var lowConfidence = group.video_low_confidence === true;
    var card = el('article', {
      className: 'group-card video-group-card' + (manual ? ' needs-keep' : ''),
      dataset: { groupKey: encodePath(group.group_id), groupId: group.group_id, matchType: 'video' }
    });

    var badges = [
      el('span', {
        className: 'video-score-badge',
        text: '视频视觉相似' + (score !== null ? ' · ' + score + '%' : '')
      })
    ];
    if (manual) {
      badges.push(el('span', { className: 'badge badge-amber', text: '需手动选择 · 禁止删除' }));
    } else if (!fallback) {
      badges.push(el('span', { className: 'badge badge-green', text: '保留：' + (KEEP_LABEL[group.keep_reason] || '保留项') }));
    }
    if (fallback) {
      badges.push(el('span', {
        className: 'badge badge-amber mtime-fallback-badge',
        attrs: { title: '保留依据：修改时间（mtime）；创建时间不可用，请人工核对保留文件。' },
        text: MTIME_FALLBACK_LABEL
      }));
    }
    if (lowConfidence) { badges.push(el('span', { className: 'badge low-confidence-badge', text: '低置信度' })); }
    // v0047：置信度档位 / 边缘裁剪 / 时间轴对齐 / 音频辅助
    var confidenceKey = group.video_confidence;
    if (confidenceKey) {
      var confidenceLabel = group.video_confidence_label
        || VIDEO_CONFIDENCE_LABEL[confidenceKey] || confidenceKey;
      badges.push(el('span', {
        className: 'badge ' + (VIDEO_CONFIDENCE_CLASS[confidenceKey] || 'badge-gray')
          + ' video-confidence-badge',
        attrs: { title: '置信度按组内最低配对分数分档：' + confidenceLabel },
        text: confidenceLabel
      }));
    }
    if (group.video_center_used) {
      badges.push(el('span', {
        className: 'badge badge-gray',
        attrs: { title: '已使用边缘裁剪指纹（自动裁掉黑边）参与比对' },
        text: '边缘裁剪'
      }));
    }
    if (group.video_match_method === 'offset') {
      badges.push(el('span', {
        className: 'badge badge-gray',
        attrs: { title: '使用时间轴偏移对齐（片头/片尾剪切导致整体错位）' },
        text: '时间轴对齐'
      }));
    }
    if (group.video_audio_used) {
      var audioScore = Number(group.video_audio_score);
      badges.push(el('span', {
        className: 'badge badge-gray',
        attrs: { title: '音频包络签名参与评分（只比音量包络，不还原内容）' },
        text: '音频辅助' + (isFinite(audioScore) && audioScore > 0
          ? ' · ' + Math.round(audioScore) + '%' : '')
      }));
    }
    if (!batchAllowed) { badges.push(el('span', { className: 'badge badge-gray', text: '禁止批量删除' })); }
    var asrUsed = group.video_asr_used === true || group.asr_used === true;
    if (asrUsed) {
      badges.push(el('span', {
        className: 'badge asr-result-badge',
        attrs: { title: 'ASR 文本签名参与评分；转录内容不展示、不回传' },
        text: 'ASR辅助'
      }));
    }
    var videoIncrementalBadge = incrementalGroupBadge(group);
    if (videoIncrementalBadge) { badges.push(videoIncrementalBadge); }
    if (Number(group.protected_files) > 0) {
      badges.push(el('span', {
        className: 'badge badge-shield',
        attrs: { title: '组内受删除白名单保护的文件不会进入删除计划' },
        text: '受保护 ' + fmtInt(group.protected_files)
      }));
    }

    var subItems = [
      el('span', { text: '时长 ' + fmtVideoDuration(group.video_duration) }),
      el('span', { text: '抽样 ' + fmtInt(group.video_sample_frames) + ' 帧' }),
      el('span', { text: '硬解 ' + videoHwaccelLabel(group.video_hwaccel) }),
      el('span', { text: '相似度 ' + (score === null ? '—' : score + '%') })
    ];
    if (asrUsed) {
      var asrTextScore = firstFiniteNumber([
        group.video_asr_text_score, group.asr_text_score, group.video_text_score, group.text_score
      ]);
      var asrWeight = firstFiniteNumber([group.video_asr_weight, group.asr_weight]);
      if (asrWeight === null) { asrWeight = asrCapability().weight; }
      var asrText = 'ASR 辅助';
      if (asrTextScore !== null) { asrText += ' · 文本分 ' + Math.round(asrTextScore) + '%'; }
      if (asrWeight !== null) { asrText += ' · 权重 ' + Math.round(asrWeight * 100) + '%'; }
      subItems.push(el('span', { className: 'asr-score-note', text: asrText }));
    }
    if (Number(group.video_duration_delta_sec) > 3) {
      subItems.push(el('span', {
        className: 'muted',
        text: '组内时长差 ' + fmtVideoDuration(group.video_duration_delta_sec)
      }));
    }
    if (fallback) {
      subItems.push(el('span', { className: 'keep-source-note', text: '保留依据：修改时间（mtime）' }));
    }
    var headMain = el('div', { className: 'group-head-main' }, [
      el('div', { className: 'group-title', text: '视频组 #' + (index + 1) + ' · ' + fmtInt(group.count) + ' 个视频' }),
      el('div', { className: 'group-sub' }, subItems)
    ]);

    var groupCheck = el('input', {
      attrs: {
        type: 'checkbox',
        disabled: manual || !batchAllowed,
        'aria-label': manual ? '本组禁止删除' : (batchAllowed ? '全选本组视频副本' : '本组禁止批量删除')
      }
    });
    groupCheck.disabled = manual || !batchAllowed;
    if (!manual && batchAllowed) {
      groupCheck.addEventListener('change', function () {
        toggleVideoGroupSelection(group, groupCheck.checked);
        syncVideoGroupCheckbox(group);
        updateSelectionSummary();
      });
    }
    var groupCheckLabel = el('label', { className: 'group-check' }, [
      groupCheck,
      manual ? '本组不参与删除' : (batchAllowed ? '全选本组视频副本' : '本组禁止批量删除')
    ]);
    var head = el('div', { className: 'group-head' }, [
      headMain,
      el('div', { className: 'group-head-side' }, [
        el('div', { className: 'group-badges' }, badges),
        groupCheckLabel
      ])
    ]);
    if (manual) {
      head.appendChild(el('p', { className: 'group-hint' }, [
        icon('warning'),
        '该组创建时间无法可靠区分，已禁止自动删除；请先在 DSM 中人工确认保留项，本组不参与本次清理。'
      ]));
    } else if (fallback) {
      head.appendChild(el('p', { className: 'group-hint is-fallback' }, [
        icon('warning'),
        '该组无可靠创建时间（btime），保留项按修改时间（mtime）选择；请人工核对保留项后再批量删除。'
      ]));
    }
    card.appendChild(head);

    var list = el('div', { className: 'file-list' });
    (group.files || []).forEach(function (file) {
      list.appendChild(renderVideoFileRow(group, file, keeperId, manual, batchAllowed));
    });
    card.appendChild(list);

    var footInfo = el('span', { className: 'group-foot-info', text: '' });
    card.appendChild(el('div', { className: 'group-foot' }, [
      footInfo,
      el('span', { className: 'hint', text: batchAllowed ? '默认不勾选；组选/全选后进入两步删除确认。' : '本组仅可人工处理，不支持批量删除。' })
    ]));
    updateVideoGroupUi(group, card);
    return card;
  }

  function buildVideoFingerprint(file, group) {
    var fingerprint = file.video_fingerprint || (group && group.video_fingerprint) || '';
    if (!fingerprint) { return null; }
    var copyBtn = el('button', {
      className: 'icon-btn',
      attrs: { type: 'button', title: '复制视频指纹', 'aria-label': '复制视频指纹' }
    }, [icon('copy')]);
    copyBtn.addEventListener('click', function () { copyWithFeedback(fingerprint, copyBtn); });
    return el('span', { className: 'video-file-meta' }, [
      el('span', { className: 'muted', text: '指纹' }),
      el('code', { className: 'video-fingerprint', attrs: { title: fingerprint }, text: videoFingerprintShort(fingerprint) }),
      copyBtn
    ]);
  }

  function renderVideoFileRow(group, file, keeperId, manual, batchAllowed) {
    var isKeeper = !manual && file.file_id === keeperId;
    var selectable = !manual && !isKeeper && batchAllowed;
    var selected = state.results.videoSelected.has(file.file_id);
    var row = el('div', {
      className: 'file-row' + (isKeeper ? ' is-keeper' : ''),
      dataset: { fileId: file.file_id, matchType: 'video' }
    });

    var checkbox = el('input', {
      attrs: {
        type: 'checkbox',
        disabled: !selectable,
        'aria-label': isKeeper ? '保留项，不可选择删除' : (manual ? '本组禁止删除' : '选择删除 ' + file.path)
      }
    });
    checkbox.checked = selectable && selected;
    if (!selectable) {
      checkbox.title = manual ? '该组需人工确认，禁止删除'
        : (isKeeper ? '保留项不可删除' : '本组禁止批量删除');
    }
    if (selectable) {
      checkbox.addEventListener('change', function () {
        toggleVideoFileSelection(group, file, checkbox.checked);
        checkbox.checked = state.results.videoSelected.has(file.file_id);
        syncVideoGroupCheckbox(group);
      });
    }
    var selectWrap = el('div', { className: 'file-select' }, [checkbox]);

    var score = Number(file.similarity_score);
    if (!isFinite(score)) { score = isKeeper ? 100 : (Number(group.video_similarity_score) || null); }
    var subItems = [
      el('span', { text: fmtBytes(file.size) }),
      el('span', { className: 'video-meta-badge', text: '相似度 ' + (score === null ? '—' : score + '%') }),
      el('span', { className: 'video-meta-badge', text: '时长 ' + fmtVideoDuration(file.video_duration !== undefined ? file.video_duration : group.video_duration) }),
      el('span', { className: 'video-meta-badge', text: '抽样 ' + fmtInt(file.video_sample_frames !== undefined ? file.video_sample_frames : group.video_sample_frames) + ' 帧' }),
      el('span', { className: 'video-meta-badge', text: '硬解 ' + videoHwaccelLabel(file.video_hwaccel !== undefined ? file.video_hwaccel : group.video_hwaccel) })
    ];
    var fingerprintEl = buildVideoFingerprint(file, group);
    if (fingerprintEl) { subItems.push(fingerprintEl); }
    if (file.low_confidence || file.video_low_confidence) {
      subItems.push(el('span', { className: 'badge low-confidence-badge', text: '低置信度' }));
    }
    var main = el('div', { className: 'file-main' }, [
      el('div', { className: 'file-path', attrs: { title: file.path }, text: file.path }),
      el('div', { className: 'file-sub' }, subItems)
    ]);

    var times = buildFileTimes(file, group);
    var actions = buildFileActions(group, file, isKeeper, manual);
    actions.appendChild(buildSubtitleGenerateButton(file));
    appendChildren(row, [selectWrap, main, times, actions]);
    return row;
  }

  // ---------- 云端字幕：生成入口、任务轮询、取消与下载 ----------
  function buildSubtitleGenerateButton(file) {
    var btn = el('button', {
      className: 'btn btn-ghost btn-sm subtitle-generate-btn',
      attrs: { type: 'button' },
      text: '生成字幕'
    });
    var ready = subtitleReady();
    btn.disabled = !ready;
    btn.title = ready ? '生成云端字幕（音频将上传到第三方服务）' : subtitleUnavailableReason();
    btn.addEventListener('click', function () {
      if (!subtitleReady()) { toast(subtitleUnavailableReason(), 'error'); return; }
      openSubtitleCloudModal(file);
    });
    return btn;
  }

  function subtitleStageLabel(stage) {
    return SUBTITLE_STAGE_LABEL[stage] || stage || '处理中';
  }

  var SUBTITLE_JOBS_POLL_MS = 1500;

  function subtitleJobStatusLabel(status) {
    return SUBTITLE_STATUS_LABEL[status] || status || '未知';
  }

  function subtitleJobStatusClass(status) {
    return SUBTITLE_STATUS_BADGE[status] || 'badge-blue';
  }

  function isTerminalSubtitleStatus(status) {
    return status === 'done' || status === 'failed' || status === 'cancelled';
  }

  function isSafeSubtitleFileName(name) {
    if (typeof name !== 'string' || !name || name.length > 160) { return false; }
    if (name.indexOf('/') !== -1 || name.indexOf('\\') !== -1) { return false; }
    return /\.(srt|vtt)$/i.test(name);
  }

  // ---------- v0028：写回结果（视频同目录 .srt）展示 ----------
  function subtitleWarningText(job) {
    if (!job) { return ''; }
    var text = '';
    if (job.warning) { text = SUBTITLE_WARNING_LABEL[job.warning] || job.warning; }
    if (job.translation_partial === true) {
      // v0030：翻译批次失败会回退原文，必须在任务详情里明确提示，避免误以为已翻译
      var note = '部分翻译批次失败，对应 cue 已回退原文；请检查 LLM 配置（Base URL/Key/模型，本地推理模型可尝试“禁用思考”）后重新生成。';
      text = text ? text + '；' + note : note;
    }
    return text;
  }

  function subtitleOutputKindLabel(kind) {
    return SUBTITLE_OUTPUT_KIND_LABEL[kind] || kind || '字幕';
  }

  function subtitleJobOutputs(job) {
    var outputs = job && Array.isArray(job.outputs) ? job.outputs : [];
    return outputs.filter(function (item) {
      return item && typeof item.path === 'string' && item.path;
    });
  }

  /** 已写回视频目录的字幕列表（compact 时最多 3 条）；无写回返回 null。 */
  function renderSubtitleOutputs(job, compact) {
    var outputs = subtitleJobOutputs(job);
    if (!outputs.length) { return null; }
    var limit = compact ? 3 : outputs.length;
    var wrap = el('div', { className: compact ? 'subtitle-outputs is-compact' : 'subtitle-outputs' });
    outputs.slice(0, limit).forEach(function (item) {
      wrap.appendChild(el('div', { className: 'subtitle-output-item' }, [
        el('span', { className: 'badge badge-green', text: subtitleOutputKindLabel(item.kind) }),
        el('span', { className: 'subtitle-output-path', attrs: { title: item.path }, text: item.path }),
        el('span', { className: 'subtitle-output-size', text: fmtBytes(item.size) })
      ]));
    });
    if (outputs.length > limit) {
      wrap.appendChild(el('p', { className: 'hint', text: '…共 ' + fmtInt(outputs.length) + ' 个写回文件（见任务详情）' }));
    }
    return wrap;
  }

  /** v0035：任务编号简写（前 6 位…后 8 位，沿用项目 head…tail 展示习惯）。 */
  function subtitleShortJobId(jobId) {
    var id = String(jobId || '');
    if (!id) { return '—'; }
    if (id.length <= 14) { return id; }
    return id.slice(0, 6) + '…' + id.slice(-8);
  }

  function subtitleSourceLabel(job) {
    if (!job) { return ''; }
    if (job.source_job_id) {
      return '补翻译（源任务 ' + subtitleShortJobId(job.source_job_id) + '）';
    }
    if (!job.source) { return ''; }
    return SUBTITLE_SOURCE_LABEL[job.source] || job.source;
  }

  // ---------- v0035：补翻译（只做了 ASR、没翻译的已完成任务） ----------
  /** 已完成且缺译文（或译文部分回退）的任务可补翻译。 */
  function subtitleJobTranslatable(job) {
    if (!job || job.status !== 'done') { return false; }
    return job.translate !== true || job.translation_partial === true;
  }

  /** 补翻译能力门控：总开关 + 翻译开关 + LLM Key 都就绪才可提交。 */
  function subtitleTranslateGate() {
    var sub = subtitleCapability();
    if (sub.supported !== true) { return { ok: false, reason: '当前后端未提供云端字幕能力' }; }
    if (sub.enabled !== true) { return { ok: false, reason: '请先在设置中启用云端字幕' }; }
    if (sub.translateEnabled !== true) { return { ok: false, reason: '字幕翻译未启用（请在设置中开启）' }; }
    if (sub.llmKeySet !== true) { return { ok: false, reason: '请先在设置中配置 LLM API Key' }; }
    return { ok: true, reason: '' };
  }

  function subtitleTargetLangLabel(value) {
    for (var i = 0; i < SUBTITLE_TARGET_LANGUAGES.length; i += 1) {
      if (SUBTITLE_TARGET_LANGUAGES[i].value === value) { return SUBTITLE_TARGET_LANGUAGES[i].label; }
    }
    return '中文';
  }

  /** 补翻译按钮（列表 action / 详情 footer 共用）：done 且缺译文时出现。 */
  function buildSubtitleTranslateButton(job, options) {
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

  async function subtitleTranslateJob(job, button) {
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

  function subtitleOutputModeLabel(mode) {
    if (mode === 'download_only') { return '仅任务目录下载'; }
    if (mode === 'alongside_video') { return '写入视频同目录'; }
    return '—';
  }

  // v0036：语言码校验与“未知但合法”的语言码保留（避免保存设置时把
  // es/fr/de 等后端支持的语言静默重置回 auto/zh）。
  var SUBTITLE_LANGUAGE_RE = /^(?:auto|[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?)$/;
  function ensureLanguageOption(select, value) {
    if (!select || !value) { return; }
    for (var i = 0; i < select.options.length; i += 1) {
      if (select.options[i].value === value) { return; }
    }
    select.appendChild(el('option', { attrs: { value: value }, text: value }));
  }
  function fillLanguageSelect(select, value, fallback) {
    if (!select) { return; }
    var normalized = (typeof value === 'string' && SUBTITLE_LANGUAGE_RE.test(value))
      ? value : (fallback || 'auto');
    ensureLanguageOption(select, normalized);
    select.value = normalized;
  }

  /** 语言下拉：SUBTITLE_LANGUAGES / SUBTITLE_TARGET_LANGUAGES 渲染。 */
  function buildSubtitleLanguageSelect(options) {
    options = options || {};
    var source = options.includeAuto ? SUBTITLE_LANGUAGES : SUBTITLE_TARGET_LANGUAGES;
    var attrs = { 'aria-label': options.ariaLabel || '语言' };
    if (options.id) { attrs.id = options.id; }
    var select = el('select', { attrs: attrs });
    source.forEach(function (item) {
      select.appendChild(el('option', { attrs: { value: item.value }, text: item.label }));
    });
    if (options.value) {
      ensureLanguageOption(select, options.value);
      select.value = options.value;
    }
    return select;
  }

  function openSubtitleCloudModal(file) {
    var sub = subtitleCapability();
    var cloud = {
      job: null, timer: null, abort: null, polling: false, pollErrors: 0,
      running: false, cancelling: false, format: 'srt', modal: null, progressUi: null,
      openedJobs: false
    };
    var languageSelect = buildSubtitleLanguageSelect({
      id: 'subtitleLanguage', includeAuto: true,
      ariaLabel: '识别语言', value: sub.asrLanguage || 'auto'
    });
    var translateCheck = el('input', { attrs: { type: 'checkbox' } });
    if (!sub.llmKeySet) { translateCheck.disabled = true; }
    if (sub.translateEnabled && sub.llmKeySet) { translateCheck.checked = true; }
    var targetSelect = buildSubtitleLanguageSelect({
      ariaLabel: '翻译目标语言', value: sub.targetLang || 'zh'
    });
    targetSelect.disabled = !translateCheck.checked;
    translateCheck.addEventListener('change', function () {
      targetSelect.disabled = !translateCheck.checked;
    });
    var formatOptions = [el('option', { attrs: { value: 'srt' }, text: 'SRT' })];
    if (sub.writeVtt) {
      formatOptions.push(el('option', { attrs: { value: 'vtt' }, text: 'VTT' }));
    }
    var formatSelect = el('select',
      { attrs: { 'aria-label': '首选下载格式' } }, formatOptions);
    formatSelect.value = 'srt';
    var confirmCheck = el('input', { attrs: { type: 'checkbox' } });
    var errorBox = el('div', { className: 'modal-error', attrs: { role: 'alert' }, hidden: true });
    var privacy = el('div', { className: 'privacy-warning', attrs: { role: 'alert' } }, [
      icon('warning'),
      el('p', { text: '音频将上传到所配置的第三方服务（ASR / 翻译）。请确认视频内容适合上传，并已获得授权；API Key 不会在界面回显。' })
    ]);
    var formBody = el('div', { className: 'subtitle-progress-card' }, [
      privacy,
      el('div', { className: 'form-grid' }, [
        el('div', { className: 'field' }, [
          el('label', { attrs: { for: 'subtitleLanguage' }, text: '识别语言' }),
          languageSelect,
          el('p', { className: 'hint', text: '默认自动检测；ASR 模型需支持 verbose_json/segments。' })
        ]),
        el('div', { className: 'field' }, [
          el('label', { className: 'check' }, [translateCheck, document.createTextNode('生成译文')]),
          targetSelect,
          el('p', {
            className: 'hint',
            text: sub.llmKeySet ? '仅翻译字幕文本，不改时间轴；部分失败会回退原文。' : '未配置 LLM API Key，暂不能生成译文。'
          })
        ]),
        el('div', { className: 'field' }, [
          el('label', { text: '首选下载格式' }),
          formatSelect,
          el('p', {
            className: 'hint',
            text: sub.writeVtt
              ? '任务会生成 SRT/VTT；这里决定完成后优先展示的格式。'
              : '默认只生成 SRT；如需 VTT 请在设置中开启「任务目录同时生成 VTT」。'
          })
        ])
      ]),
      el('label', { className: 'modal-check-row' }, [
        confirmCheck,
        el('span', { text: '我了解音频将上传到所配置的第三方服务，并确认有权处理该视频内容。' })
      ]),
      el('p', {
        className: 'subtitle-form-note',
        text: '生成后可查看进度、暂停/取消任务并下载字幕；任务进行中可关闭弹窗，任务会在“历史记录 → 字幕任务”中继续，不会丢失。'
      }),
      errorBox
    ]);
    var generateBtn = el('button', { className: 'btn btn-primary', attrs: { type: 'button' }, text: '生成字幕' });
    generateBtn.disabled = true;
    var cancelBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '取消',
      on: { click: function () { closeModal(true); } }
    });
    confirmCheck.addEventListener('change', function () {
      generateBtn.disabled = !confirmCheck.checked;
    });
    generateBtn.addEventListener('click', submitSubtitleJob);

    var modal = openModal({
      title: '生成云端字幕',
      subtitle: file.path,
      size: 'lg',
      body: formBody,
      footer: [cancelBtn, generateBtn],
      initialFocus: confirmCheck,
      onClose: function () {
        var wasActive = !!(cloud.job && SUBTITLE_ACTIVE_STATUS.indexOf(cloud.job.status) !== -1);
        cloud.running = false;
        stopSubtitlePolling(cloud);
        if (wasActive && !cloud.openedJobs) {
          toast('字幕任务仍在后台运行，可在“历史记录 → 字幕任务”中查看。', 'info', 6000);
        }
        if (state.view === 'history') { loadSubtitleJobs({ silent: true }); }
      }
    });
    cloud.modal = modal;

    function submitSubtitleJob() {
      if (cloud.running || !confirmCheck.checked) { return; }
      cloud.running = true;
      cloud.format = formatSelect.value === 'vtt' ? 'vtt' : 'srt';
      hide(errorBox);
      setBusy(generateBtn, true, '正在提交…');
      var payload = {
        path: file.path,
        language: languageSelect.value,
        translate: !!(translateCheck.checked && sub.llmKeySet),
        target_lang: targetSelect.value,
        confirm_cloud: true
      };
      api('/api/subtitles', { method: 'POST', body: payload, timeout: 30000 }).then(function (res) {
        var job = res && res.job ? res.job : res;
        if (!job || !job.job_id) { throw new ApiError(200, 'INVALID_RESPONSE', '服务未返回字幕任务编号，请稍后重试。'); }
        modal.setTitle('字幕任务');
        showSubtitleProgress(job);
        startSubtitlePolling(job.job_id);
      }).catch(function (err) {
        cloud.running = false;
        setBusy(generateBtn, false);
        setText(errorBox, errorMessage(err, '创建字幕任务失败，请稍后重试。'));
        show(errorBox);
      });
    }

    function makeProgressUi() {
      var statusBadge = el('span', { className: 'badge badge-gray', text: '排队中' });
      var stageEl = el('span', { className: 'subtitle-stage', text: '等待中' });
      var pctEl = el('span', { className: 'subtitle-pct', text: '0%' });
      var fill = el('div', { className: 'progress-fill' });
      var bar = el('div', { className: 'progress-bar', attrs: { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0' } }, [fill]);
      var messageEl = el('p', { className: 'modal-note', text: '正在排队…' });
      var errorEl = el('div', { className: 'modal-error', attrs: { role: 'alert' }, hidden: true });
      var metaEl = el('p', { className: 'subtitle-job-meta mono', text: file.path });
      var filesWrap = el('div', { className: 'subtitle-downloads', hidden: true });
      var cancelTaskBtn = el('button', { className: 'btn btn-danger-ghost', attrs: { type: 'button' }, text: '取消任务' });
      cancelTaskBtn.addEventListener('click', cancelSubtitleJob);
      var body = el('div', { className: 'subtitle-progress-card' }, [
        privacy,
        el('div', { className: 'subtitle-stage-row' }, [stageEl, pctEl]),
        bar,
        messageEl,
        el('div', { className: 'subtitle-stage-row' }, [
          el('span', { className: 'subtitle-job-meta', text: '任务状态' }),
          statusBadge
        ]),
        metaEl,
        errorEl,
        filesWrap,
        el('p', {
          className: 'hint',
          text: '音频只临时存在于服务端；任务完成/失败/取消后自动清理。字幕会优先写入视频同目录，任务目录也保留副本供下载。'
        })
      ]);
      cloud.progressUi = {
        body: body, statusBadge: statusBadge, stageEl: stageEl, pctEl: pctEl,
        fill: fill, bar: bar, messageEl: messageEl, errorEl: errorEl,
        metaEl: metaEl, filesWrap: filesWrap, cancelTaskBtn: cancelTaskBtn
      };
    }

    function showSubtitleProgress(job) {
      makeProgressUi();
      var ui = cloud.progressUi;
      ui.statusBadge.className = 'badge ' + subtitleJobStatusClass(job.status);
      var viewJobsBtn = el('button', {
        className: 'btn btn-ghost', attrs: { type: 'button' }, text: '在字幕任务中查看'
      });
      viewJobsBtn.addEventListener('click', function () {
        cloud.openedJobs = true;
        closeModal(true);
        goToSubtitleJobs();
      });
      cloud.viewJobsBtn = viewJobsBtn;
      modal.setBody(ui.body);
      modal.setFooter([
        el('span', { className: 'footer-left', text: '任务进行中；关闭弹窗不会取消任务，可在字幕任务面板继续操作。' }),
        ui.cancelTaskBtn,
        viewJobsBtn
      ]);
      updateSubtitleProgress(job);
    }

    function renderSubtitleFiles(job) {
      var ui = cloud.progressUi;
      var files = Array.isArray(job.files) ? job.files.filter(function (item) {
        return item && isSafeSubtitleFileName(item.name);
      }) : [];
      clear(ui.filesWrap);
      ui.filesWrap.hidden = false;
      var warningText = subtitleWarningText(job);
      if (warningText) {
        ui.filesWrap.appendChild(el('div', { className: 'subtitle-job-warning' }, [
          icon('warning'), el('span', { text: warningText })
        ]));
      }
      if (job.skipped) {
        ui.filesWrap.appendChild(el('div', { className: 'subtitle-job-note' }, [
          el('span', { className: 'badge badge-gray', text: '已跳过' }),
          el('span', { text: job.message || '视频旁已存在字幕，已跳过（不覆盖已有文件）。' })
        ]));
      }
      var outputsEl = renderSubtitleOutputs(job, false);
      if (outputsEl) {
        ui.filesWrap.appendChild(el('h3', { text: '已写入视频目录' }));
        ui.filesWrap.appendChild(outputsEl);
      }
      if (!files.length) {
        if (!job.skipped && !outputsEl) {
          ui.filesWrap.appendChild(el('p', { className: 'subtitle-empty', text: '任务已完成，但任务目录中没有可下载的字幕文件。' }));
        }
        return;
      }
      ui.filesWrap.appendChild(el('h3', { text: '任务目录文件（可下载）' }));
      var preferredExt = cloud.format === 'vtt' ? '.vtt' : '.srt';
      files.sort(function (a, b) {
        var aPref = a.name.toLowerCase().slice(-4) === preferredExt ? 0 : 1;
        var bPref = b.name.toLowerCase().slice(-4) === preferredExt ? 0 : 1;
        if (aPref !== bPref) { return aPref - bPref; }
        return String(a.name).localeCompare(String(b.name));
      });
      var basePath = '/api/subtitles/' + encodePath(job.job_id) + '/file/';
      var preferred = files.filter(function (item) {
        return String(item.name).toLowerCase().slice(-4) === preferredExt;
      })[0];
      if (preferred) {
        ui.filesWrap.appendChild(el('a', {
          className: 'btn btn-primary btn-sm',
          attrs: {
            href: basePath + encodeURIComponent(preferred.name),
            download: preferred.name
          }
        }, [icon('download'), '下载 ' + preferredExt.slice(1).toUpperCase() + '（' + preferred.name + '）']));
      }
      ui.filesWrap.appendChild(el('h3', { text: '全部字幕文件' }));
      var list = el('div', { className: 'subtitle-file-list' });
      files.forEach(function (item) {
        list.appendChild(el('div', { className: 'subtitle-file-item' }, [
          el('span', { className: 'subtitle-file-name', text: item.name }),
          el('span', { className: 'subtitle-file-size', text: fmtBytes(item.size) }),
          el('a', {
            className: 'btn btn-ghost btn-sm',
            attrs: { href: basePath + encodeURIComponent(item.name), download: item.name, title: '下载 ' + item.name }
          }, [icon('download'), '下载'])
        ]));
      });
      ui.filesWrap.appendChild(list);
      ui.filesWrap.appendChild(el('p', { className: 'hint', text: '文件列表以服务端实际生成为准；任务完成后自动清理临时音频。' }));
    }

    function updateSubtitleProgress(job) {
      var ui = cloud.progressUi;
      if (!ui || !job) { return; }
      var status = job.status || 'queued';
      var terminal = isTerminalSubtitleStatus(status);
      var progress = clamp(Number(job.progress) || 0, 0, 100);
      var pct = Math.round(progress);
      setText(ui.statusBadge, subtitleJobStatusLabel(status));
      ui.statusBadge.className = 'badge ' + subtitleJobStatusClass(status);
      setText(ui.stageEl, subtitleStageLabel(job.stage) + (status === 'queued' ? '（排队中）' : ''));
      setText(ui.pctEl, pct + '%');
      ui.fill.style.width = pct + '%';
      ui.bar.setAttribute('aria-valuenow', String(pct));
      setText(ui.messageEl, job.message || (terminal ? '任务已结束。' : '正在处理…'));
      if (job.path) { setText(ui.metaEl, job.path); }
      ui.metaEl.title = job.path || '';
      if (status === 'failed') {
        var errorText = '字幕任务失败。';
        if (job.error && typeof job.error === 'string') { errorText = job.error; }
        else if (job.error && job.error.message) { errorText = job.error.message; }
        else if (job.message) { errorText = job.message; }
        setText(ui.errorEl, errorText);
        show(ui.errorEl);
      } else if (status === 'cancelled') {
        setText(ui.errorEl, '任务已取消，未生成字幕文件。');
        show(ui.errorEl);
      } else {
        hide(ui.errorEl);
      }
      if (status === 'done') {
        renderSubtitleFiles(job);
      } else {
        ui.filesWrap.hidden = true;
      }
      if (terminal) {
        cloud.running = false;
        stopSubtitlePolling(cloud);
        if (cloud.modal) {
          cloud.modal.setTitle('字幕任务');
          cloud.modal.setBusy(false);
          var doneViewBtn = el('button', {
            className: 'btn btn-ghost', attrs: { type: 'button' }, text: '在字幕任务中查看'
          });
          doneViewBtn.addEventListener('click', function () {
            cloud.openedJobs = true;
            closeModal(true);
            goToSubtitleJobs();
          });
          cloud.modal.setFooter([
            el('span', { className: 'footer-left', text: status === 'done' ? '字幕生成完成。' : '任务已结束。' }),
            doneViewBtn,
            el('button', {
              className: 'btn btn-primary', attrs: { type: 'button' }, text: '关闭',
              on: { click: function () { closeModal(true); } }
            })
          ]);
        }
        setModalDismissible(true);
        if (status === 'done') {
          if (job.skipped) {
            toast('已存在字幕，已跳过；任务目录可查看。', 'info', 5000);
          } else if (subtitleJobOutputs(job).length) {
            toast('字幕生成完成，已写入视频同目录；任务目录副本也可下载。', 'success', 5000);
          } else if (subtitleWarningText(job)) {
            toast('字幕已生成，但未能写回视频目录；可在任务目录下载。', 'error', 7000);
          } else {
            toast('字幕生成完成，可下载 SRT/VTT 文件。', 'success', 5000);
          }
        } else if (status === 'failed') {
          toast('字幕任务失败，请查看弹窗中的错误信息。', 'error', 6000);
        } else {
          toast('字幕任务已取消。', 'info');
        }
      } else {
        ui.cancelTaskBtn.disabled = false;
        setText(ui.cancelTaskBtn, '取消任务');
        setModalDismissible(true);
      }
    }

    function startSubtitlePolling(jobId) {
      stopSubtitlePolling(cloud);
      cloud.polling = true;
      cloud.pollErrors = 0;
      pollSubtitleOnce(jobId);
      cloud.timer = setInterval(function () { pollSubtitleOnce(jobId); }, POLL_MS);
    }

    function stopSubtitlePolling(cloudState) {
      if (!cloudState) { return; }
      if (cloudState.timer) { clearInterval(cloudState.timer); cloudState.timer = null; }
      if (cloudState.abort) {
        try { cloudState.abort.abort(); } catch (e) { /* 忽略 */ }
        cloudState.abort = null;
      }
      cloudState.polling = false;
    }

    async function pollSubtitleOnce(jobId) {
      if (!cloud.polling || !cloud.modal) { return; }
      if (cloud.abort) { try { cloud.abort.abort(); } catch (e) { /* 忽略 */ } }
      var controller = new AbortController();
      cloud.abort = controller;
      try {
        var res = await api('/api/subtitles/' + encodePath(jobId), { signal: controller.signal, timeout: 15000 });
        if (!cloud.polling) { return; }
        var job = res && res.job ? res.job : res;
        if (!job || !job.job_id) { return; }
        cloud.pollErrors = 0;
        cloud.job = job;
        updateSubtitleProgress(job);
      } catch (err) {
        if (err && err.name === 'AbortError') { return; }
        cloud.pollErrors += 1;
        if (cloud.pollErrors >= 5) {
          stopSubtitlePolling(cloud);
          if (cloud.progressUi) {
            setText(cloud.progressUi.errorEl, '无法获取字幕任务进度：' + errorMessage(err, '请稍后重试。'));
            show(cloud.progressUi.errorEl);
            setText(cloud.progressUi.messageEl, '进度轮询已中断。');
          }
          if (cloud.modal) { cloud.modal.setBusy(false); }
          setModalDismissible(true);
          if (cloud.modal) {
            cloud.modal.setFooter([el('button', {
              className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
              on: { click: function () { closeModal(true); } }
            })]);
          }
        }
      }
    }

    async function cancelSubtitleJob() {
      if (!cloud.job || !cloud.job.job_id || cloud.cancelling) { return; }
      cloud.cancelling = true;
      var btn = cloud.progressUi ? cloud.progressUi.cancelTaskBtn : null;
      if (btn) { setBusy(btn, true, '正在取消…'); }
      try {
        var res = await api('/api/subtitles/' + encodePath(cloud.job.job_id) + '/cancel', {
          method: 'POST', body: {}, timeout: 15000
        });
        var job = res && res.job ? res.job : res;
        if (job && job.status) { cloud.job = job; updateSubtitleProgress(job); }
        toast('已请求取消字幕任务。', 'info');
      } catch (err) {
        toast(errorMessage(err, '取消字幕任务失败，请稍后重试。'), 'error');
      } finally {
        cloud.cancelling = false;
        if (btn && cloud.progressUi && cloud.progressUi.cancelTaskBtn === btn) {
          setBusy(btn, false);
        }
      }
    }
  }

  function videoGroupSelectableFiles(group) {
    if (!group || group.needs_manual_keep || group.allow_batch_delete === false) { return []; }
    var keeperId = videoGroupKeeperId(group);
    return (group.files || []).filter(function (file) {
      return file.file_id !== keeperId && !file.protected;
    });
  }

  function syncVideoGroupCheckbox(group, card) {
    card = card || findVideoGroupCard(group.group_id);
    if (!card) { return; }
    var input = $('.group-check input', card);
    if (!input) { return; }
    var selectable = videoGroupSelectableFiles(group);
    if (!selectable.length) {
      input.checked = false;
      input.indeterminate = false;
      input.disabled = true;
      return;
    }
    var selectedCount = selectable.filter(function (file) {
      return state.results.videoSelected.has(file.file_id);
    }).length;
    input.disabled = false;
    input.checked = selectedCount === selectable.length;
    input.indeterminate = selectedCount > 0 && selectedCount < selectable.length;
  }

  function updateVideoGroupUi(group, card) {
    card = card || findVideoGroupCard(group.group_id);
    if (!card) { return; }
    syncVideoGroupCheckbox(group, card);
    var info = $('.group-foot-info', card);
    if (info) {
      var selectedCount = videoGroupSelectedCount(group);
      setText(info, selectedCount > 0
        ? '本组已选 ' + fmtInt(selectedCount) + ' 个视频副本（共 ' + fmtInt(group.count) + ' 个）'
        : '本组默认未选择 · 共 ' + fmtInt(group.count) + ' 个视频');
    }
  }

  function isVideoFileSelectable(group, file) {
    if (!group || !file) { return false; }
    if (group.needs_manual_keep || group.allow_batch_delete === false) { return false; }
    return file.file_id !== videoGroupKeeperId(group);
  }

  function ensureVideoSelectionNotAllScope() {
    var results = state.results;
    if (results.mode !== 'all') { return; }
    results.mode = 'none';
    results.selected = new Map();
    results.excluded = new Map();
    renderGroups();
    toast('已取消“全选精准组”：视频相似文件需与手动选择的精准组文件混合删除。', 'info', 4600);
  }

  function toggleVideoFileSelection(group, file, checked) {
    if (!isVideoFileSelectable(group, file)) { return; }
    var results = state.results;
    ensureVideoSelectionNotAllScope();
    if (checked) {
      results.videoSelected.set(file.file_id, { size: file.size, groupId: group.group_id, path: file.path });
    } else {
      results.videoSelected.delete(file.file_id);
    }
    results.videoSelectAllMode = false;
    updateVideoGroupUi(group);
    updateSelectionSummary();
  }

  function toggleVideoGroupSelection(group, checked) {
    if (!group || group.needs_manual_keep || group.allow_batch_delete === false) { return; }
    ensureVideoSelectionNotAllScope();
    var selectable = videoGroupSelectableFiles(group);
    selectable.forEach(function (file) {
      if (checked) {
        state.results.videoSelected.set(file.file_id, { size: file.size, groupId: group.group_id, path: file.path });
      } else {
        state.results.videoSelected.delete(file.file_id);
      }
    });
    state.results.videoSelectAllMode = false;
    var card = findVideoGroupCard(group.group_id);
    if (card) {
      $$('.file-row', card).forEach(function (row) {
        var file = null;
        selectable.forEach(function (item) { if (item.file_id === row.dataset.fileId) { file = item; } });
        if (!file) { return; }
        var cb = $('input[type="checkbox"]', row);
        if (cb && !cb.disabled) { cb.checked = state.results.videoSelected.has(file.file_id); }
      });
    }
    updateVideoGroupUi(group, card);
    updateSelectionSummary();
  }

  function autoSelectVideoGroups(groups) {
    var results = state.results;
    (groups || []).forEach(function (group) {
      videoGroupSelectableFiles(group).forEach(function (file) {
        results.videoSelected.set(file.file_id, { size: file.size, groupId: group.group_id, path: file.path });
      });
    });
  }

  function selectAllVideoDuplicates() {
    if (!state.scan.id) { toast('请先完成一次扫描。', 'info'); return; }
    if (!state.results.videoTotal && !state.results.videoGroups.length) {
      toast('没有可选择的视频相似组。', 'info');
      return;
    }
    var anySelectable = false;
    state.results.videoGroups.forEach(function (group) {
      if (videoGroupSelectableFiles(group).length) { anySelectable = true; }
    });
    if (!anySelectable) { toast('当前加载的视频组均禁止批量删除。', 'info'); return; }
    ensureVideoSelectionNotAllScope();
    state.results.videoSelectAllMode = true;
    autoSelectVideoGroups(state.results.videoGroups);
    renderVideoGroups();
    updateSelectionSummary();
    toast('已全选已加载的视频相似组副本；继续“加载更多”会自动选中新加载的视频组。', 'info', 4600);
  }

  function updateVideoSelectAllButtonLabel() {
    if (!els.selectAllVideoBtn) { return; }
    var mode = state.results.videoSelectAllMode;
    setText(els.selectAllVideoBtn, mode ? '已全选视频相似组副本' : '全选视频相似组副本');
  }

  function bindResultsEvents() {
    els.searchGroups.addEventListener('input', debounce(function () {
      var value = els.searchGroups.value.trim();
      if (value === state.results.q) { return; }
      state.results.q = value;
      clearSelectionState();
      loadResults(true);
    }, 350));
    els.sortGroups.addEventListener('change', function () {
      state.results.sort = els.sortGroups.value;
      clearSelectionState();
      loadResults(true);
    });
    if (els.newOnlyToggle) {
      els.newOnlyToggle.addEventListener('change', function () {
        state.results.newOnly = !!els.newOnlyToggle.checked;
        clearSelectionState();
        loadResults(true);
        syncNewOnlyUi();
      });
    }
    els.refreshResultsBtn.addEventListener('click', function () {
      if (!state.scan.id) { toast('当前没有可刷新的扫描结果。', 'info'); return; }
      clearSelectionState();
      loadResults(true);
      toast('正在刷新扫描结果。', 'info');
    });
    els.loadMoreBtn.addEventListener('click', function () { loadExactPartition(false); });
    if (els.similarLoadMoreBtn) { els.similarLoadMoreBtn.addEventListener('click', function () { loadSimilarPartition(false); }); }
    if (els.videoLoadMoreBtn) { els.videoLoadMoreBtn.addEventListener('click', function () { loadVideoPartition(false); }); }
    if (els.selectAllVideoBtn) { els.selectAllVideoBtn.addEventListener('click', selectAllVideoDuplicates); }
    els.selectAllBtn.addEventListener('click', selectAllDuplicates);
    els.clearSelectionBtn.addEventListener('click', clearSelection);
    if (els.batchSubtitleBtn) {
      els.batchSubtitleBtn.addEventListener('click', function () {
        if (!subtitleReady()) { toast(subtitleUnavailableReason(), 'error'); return; }
        var paths = selectedVideoPaths();
        if (!paths.length) { toast('请先在视频结果中勾选要生成字幕的视频副本。', 'info'); return; }
        openSubtitleBatchModal(paths);
      });
    }
    els.deleteSelectedBtn.addEventListener('click', function () { beginDelete(); });
    els.csvLink.addEventListener('click', function (event) {
      if (els.csvLink.getAttribute('aria-disabled') === 'true' || !state.scan.id) {
        event.preventDefault();
        toast('暂无可导出的扫描结果。', 'info');
      }
    });
    if (els.xlsxLink) {
      els.xlsxLink.addEventListener('click', function (event) {
        if (els.xlsxLink.getAttribute('aria-disabled') === 'true' || !state.scan.id) {
          event.preventDefault();
          toast('暂无可导出的扫描结果。', 'info');
        }
      });
    }
  }

  // ==================== 12. 选择逻辑与批量操作 ====================
  function isFileSelectable(group, file) {
    if (!group || !file) { return false; }
    if (group.needs_manual_keep) { return false; }
    if (file.protected) { return false; }
    return file.file_id !== group.keep_file_id;
  }

  function isFileSelected(group, file) {
    if (!isFileSelectable(group, file)) { return false; }
    if (state.results.mode === 'all') { return !state.results.excluded.has(file.file_id); }
    if (state.results.mode === 'explicit') { return state.results.selected.has(file.file_id); }
    return false;
  }

  function selectionTotals() {
    var results = state.results;
    var exactCount = 0;
    var exactBytes = 0;
    var excludedCount = 0;
    if (results.mode === 'all') {
      var stats = currentScanStats();
      var excludedBytes = 0;
      results.excluded.forEach(function (item) { excludedBytes += Number(item && item.size) || 0; });
      var selectableFiles = typeof stats.selectable_duplicate_files === 'number'
        ? stats.selectable_duplicate_files : (Number(stats.duplicate_files) || 0);
      var selectableBytes = typeof stats.selectable_reclaimable_bytes === 'number'
        ? stats.selectable_reclaimable_bytes : (Number(stats.reclaimable_bytes) || 0);
      exactCount = Math.max(0, selectableFiles - results.excluded.size);
      exactBytes = Math.max(0, selectableBytes - excludedBytes);
      excludedCount = results.excluded.size;
    } else {
      results.selected.forEach(function (item) {
        exactCount += 1;
        exactBytes += Number(item && item.size) || 0;
      });
    }
    var similarCount = 0;
    var similarBytes = 0;
    var similarGroupIds = [];
    results.similarSelected.forEach(function (item) {
      similarCount += 1;
      similarBytes += Number(item && item.size) || 0;
      if (item && item.groupId && similarGroupIds.indexOf(item.groupId) === -1) { similarGroupIds.push(item.groupId); }
    });
    var allSimilarReviewed = similarGroupIds.every(function (groupId) {
      return results.similarReview.has(groupId);
    });
    var videoCount = 0;
    var videoBytes = 0;
    var videoGroupIds = [];
    results.videoSelected.forEach(function (item) {
      videoCount += 1;
      videoBytes += Number(item && item.size) || 0;
      if (item && item.groupId && videoGroupIds.indexOf(item.groupId) === -1) {
        videoGroupIds.push(item.groupId);
      }
    });
    return {
      mode: results.mode,
      exactCount: exactCount,
      exactBytes: exactBytes,
      excludedCount: excludedCount,
      similarCount: similarCount,
      similarBytes: similarBytes,
      similarGroupIds: similarGroupIds,
      allSimilarReviewed: allSimilarReviewed,
      videoCount: videoCount,
      videoBytes: videoBytes,
      videoGroupIds: videoGroupIds,
      count: exactCount + similarCount + videoCount,
      bytes: exactBytes + similarBytes + videoBytes
    };
  }

  function manualGroupCount() {
    var stats = currentScanStats();
    var count = Number(stats.needs_manual_keep_groups);
    if (isFinite(count) && count >= 0) { return count; }
    return state.results.groups.filter(function (group) { return group.needs_manual_keep; }).length
      + state.results.similarGroups.filter(function (group) { return group.needs_manual_keep; }).length
      + state.results.videoGroups.filter(function (group) { return group.needs_manual_keep; }).length;
  }

  function updateSelectionSummary() {
    if (!els.selectionSummary) { return; }
    var totals = selectionTotals();
    clear(els.selectionSummary);
    if (!totals.count) {
      els.selectionSummary.appendChild(document.createTextNode('未选择任何文件'));
    } else {
      if (totals.exactCount > 0) {
        appendChildren(els.selectionSummary, [
          totals.mode === 'all' ? '精准组：已全选 ' : '精准组：已选 ',
          el('strong', { text: fmtInt(totals.exactCount) }), ' 个 · ', fmtBytes(totals.exactBytes)
        ]);
        if (totals.excludedCount) {
          els.selectionSummary.appendChild(document.createTextNode('（已排除 ' + fmtInt(totals.excludedCount) + ' 个）'));
        }
      }
      if (totals.similarCount > 0) {
        if (totals.exactCount > 0) { els.selectionSummary.appendChild(document.createTextNode('；')); }
        appendChildren(els.selectionSummary, [
          '内容相似：已选 ', el('strong', { text: fmtInt(totals.similarCount) }), ' 个 · ',
          fmtBytes(totals.similarBytes),
          '（已复核 ' + fmtInt(totals.similarGroupIds.length) + ' 组）'
        ]);
      }
      if (totals.videoCount > 0) {
        if (totals.exactCount > 0 || totals.similarCount > 0) {
          els.selectionSummary.appendChild(document.createTextNode('；'));
        }
        appendChildren(els.selectionSummary, [
          '视频视觉：已选 ', el('strong', { text: fmtInt(totals.videoCount) }), ' 个 · ',
          fmtBytes(totals.videoBytes),
          '（' + fmtInt(totals.videoGroupIds.length) + ' 组）'
        ]);
      }
    }
    var blocked = false;
    var blockedReason = '';
    if (totals.mode === 'all' && (totals.similarCount > 0 || totals.videoCount > 0)) {
      blocked = true;
      blockedReason = '“全选精准组”不能与相似/视频文件混合删除，请手动选择精准组文件。';
    } else if (totals.similarCount > 0 && !totals.allSimilarReviewed) {
      blocked = true;
      blockedReason = '存在未人工复核的相似组，无法删除。';
    }
    if (els.deleteSelectedBtn) {
      els.deleteSelectedBtn.disabled = !(totals.count > 0) || state.deleteFlow.running || state.results.loading || blocked;
      els.deleteSelectedBtn.title = blocked ? blockedReason : '';
    }
    if (els.selectAllBtn) {
      setText(els.selectAllBtn, state.results.scanMode === 'similar'
        ? (totals.mode === 'all' ? '已全选精准组副本' : '全选待删除副本（仅精准组）')
        : (totals.mode === 'all' ? '已全选待删除副本' : '全选待删除副本'));
    }
    if (els.clearSelectionBtn) { els.clearSelectionBtn.disabled = totals.mode === 'none' && !totals.count; }
    if (state.results.scanMode === 'similar') { refreshSimilarActionButtons(); }
    updateVideoSelectAllButtonLabel();
    if (els.selectAllVideoBtn) {
      var anyVideoSelectable = false;
      state.results.videoGroups.forEach(function (group) {
        if (videoGroupSelectableFiles(group).length) { anyVideoSelectable = true; }
      });
      els.selectAllVideoBtn.disabled = !anyVideoSelectable;
    }
    updateBatchSubtitleButton();
  }

  function clearSelectionState() {
    state.results.mode = 'none';
    state.results.selected = new Map();
    state.results.excluded = new Map();
    state.results.similarSelected = new Map();
    state.results.similarReview = new Set();
    state.results.videoSelected = new Map();
    state.results.videoSelectAllMode = false;
  }

  function clearSelection() {
    clearSelectionState();
    renderGroups();
    if (state.results.scanMode === 'similar') { renderSimilarGroups(); }
    if (videoScanEnabledForResults()) { renderVideoGroups(); }
    updateSelectionSummary();
    toast('已清空选择（包含人工复核状态）。', 'info');
  }

  function selectAllDuplicates() {
    if (!state.scan.id) { toast('请先完成一次扫描。', 'info'); return; }
    if (state.results.similarSelected.size > 0 || state.results.videoSelected.size > 0) {
      toast('已选择相似/视频文件，不能再“全选”精准组；请手动勾选精准组文件，或先清空选择。', 'error', 5200);
      return;
    }
    if (!state.results.total) { toast('没有可选择的精准组。', 'info'); return; }
    var results = state.results;
    results.mode = 'all';
    results.selected = new Map();
    results.excluded = new Map();
    renderGroups();
    updateSelectionSummary();
    var manual = manualGroupCount();
    toast(state.results.scanMode === 'similar'
      ? '已全选精准组副本（相似组不参与全选，需人工复核后逐条选择）。'
      : ('已全选待删除副本' + (manual ? '；需人工确认的 ' + manual + ' 组已自动排除，不会删除。' : '。')),
      'info', 4800);
  }

  function toggleFileSelection(group, file, checked) {
    if (!isFileSelectable(group, file)) { return; }
    var results = state.results;
    if (results.mode === 'all') {
      if (checked) { results.excluded.delete(file.file_id); }
      else { results.excluded.set(file.file_id, { size: file.size, groupId: group.group_id }); }
      return;
    }
    if (checked) {
      if (results.mode !== 'explicit') { results.mode = 'explicit'; }
      results.selected.set(file.file_id, { size: file.size, groupId: group.group_id, path: file.path });
    } else {
      results.selected.delete(file.file_id);
      if (!results.selected.size && results.mode === 'explicit') { results.mode = 'none'; }
    }
  }

  function toggleGroupSelection(group, checked) {
    if (!group || group.needs_manual_keep) { return; }
    var results = state.results;
    var selectable = (group.files || []).filter(function (file) {
      return isFileSelectable(group, file);
    });
    if (results.mode === 'all') {
      selectable.forEach(function (file) {
        if (checked) { results.excluded.delete(file.file_id); }
        else { results.excluded.set(file.file_id, { size: file.size, groupId: group.group_id }); }
      });
    } else if (checked) {
      if (results.mode !== 'explicit') { results.mode = 'explicit'; }
      selectable.forEach(function (file) {
        results.selected.set(file.file_id, { size: file.size, groupId: group.group_id, path: file.path });
      });
    } else {
      selectable.forEach(function (file) { results.selected.delete(file.file_id); });
      if (!results.selected.size && results.mode === 'explicit') { results.mode = 'none'; }
    }
    var card = findGroupCard(group.group_id);
    if (card) {
      $$('.file-row', card).forEach(function (row) {
        var fileId = row.dataset.fileId;
        var file = null;
        for (var i = 0; i < selectable.length; i += 1) {
          if (selectable[i].file_id === fileId) { file = selectable[i]; break; }
        }
        if (!file) { return; }
        var cb = $('input[type="checkbox"]', row);
        if (cb && !cb.disabled) { cb.checked = isFileSelected(group, file); }
      });
    }
  }

  // ==================== 13. 文件预览 ====================
  async function openPreview(file) {
    if (!file || !file.path) { return; }
    var objectUrls = [];
    var abortController = new AbortController();
    var closeBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
      on: { click: function () { closeModal(true); } }
    });
    var openLink = el('a', {
      className: 'btn btn-ghost',
      attrs: { href: '/api/preview?path=' + encodePath(file.path), target: '_blank', rel: 'noopener noreferrer' },
      text: '在新窗口打开'
    }, [icon('file')]);
    var modal = openModal({
      title: '文件预览',
      subtitle: file.path,
      size: 'xl',
      body: el('div', { className: 'modal-loading' }, [el('span', { className: 'spinner spinner-lg' }), '正在加载预览…']),
      footer: [openLink, closeBtn],
      initialFocus: closeBtn,
      onClose: function () {
        try { abortController.abort(); } catch (e) { /* 忽略 */ }
        objectUrls.forEach(function (url) { try { URL.revokeObjectURL(url); } catch (e2) { /* 忽略 */ } });
        objectUrls = [];
      }
    });
    function ensureOpen() { return document.body.contains(modal.modal); }
    function showError(err) {
      if (!ensureOpen()) { return; }
      modal.setBody(el('div', { className: 'preview-error' }, [
        icon('warning'),
        el('p', { text: errorMessage(err, '预览失败，请稍后重试。') }),
        el('p', { className: 'preview-error-detail', text: file.path })
      ]));
    }
    try {
      // v0044：先用 Range 探测类型，避免把整个视频下载成 blob 才能播放；
      // 视频/音频改为直接 URL（浏览器按 Range 流式播放，支持拖动与片段定位）。
      async function fetchPreview(withRange) {
        var headers = { Accept: '*/*' };
        if (withRange) { headers.Range = 'bytes=0-0'; }
        var response = await fetch('/api/preview?path=' + encodePath(file.path), {
          credentials: 'same-origin',
          signal: abortController.signal,
          headers: headers
        });
        if (response.status === 401) {
          handleUnauthorized();
          throw new ApiError(401, 'AUTH_REQUIRED', ERROR_FALLBACK.AUTH_REQUIRED);
        }
        if (!response.ok && response.status !== 206) {
          var message = '预览失败（HTTP ' + response.status + '）。';
          try {
            var payload = await response.json();
            if (payload && payload.error && payload.error.message) { message = payload.error.message; }
          } catch (e2) { /* 非 JSON 错误体 */ }
          throw new ApiError(response.status, 'PREVIEW_FAILED', message);
        }
        return response;
      }
      var probe = await fetchPreview(true);
      var probeType = (probe.headers.get('Content-Type') || '').toLowerCase();
      var mediaLike = probeType.indexOf('video/') === 0
        || probeType.indexOf('audio/') === 0;
      var res = mediaLike ? probe : await fetchPreview(false);
      var contentType = (res.headers.get('Content-Type') || '').toLowerCase();
      var truncated = res.headers.get('X-Preview-Truncated') === '1';
      var stage;
      var extras = null;
      if (contentType.indexOf('image/') === 0) {
        var imageBlob = await res.blob();
        var imageUrl = URL.createObjectURL(imageBlob);
        objectUrls.push(imageUrl);
        stage = el('div', { className: 'preview-stage' }, [
          el('img', { attrs: { src: imageUrl, alt: '文件预览：' + (file.name || file.path) } })
        ]);
      } else if (contentType.indexOf('text/') === 0 || contentType.indexOf('json') !== -1 || contentType.indexOf('xml') !== -1) {
        var text = await res.text();
        stage = el('div', { className: 'preview-stage is-light' }, [
          el('pre', { className: 'preview-text', text: text })
        ]);
        if (truncated) {
          stage.appendChild(el('p', { className: 'preview-truncated', text: '文本超过 1 MB，已截断显示。' }));
        }
      } else if (contentType.indexOf('pdf') !== -1) {
        var pdfBlob = await res.blob();
        var pdfUrl = URL.createObjectURL(pdfBlob);
        objectUrls.push(pdfUrl);
        stage = el('div', { className: 'preview-stage is-light' }, [
          el('iframe', { attrs: { src: pdfUrl, title: 'PDF 文件预览' } })
        ]);
      } else if (contentType.indexOf('audio/') === 0) {
        var audioUrl = '/api/preview?path=' + encodePath(file.path);
        stage = el('div', { className: 'preview-stage' }, [
          el('audio', { attrs: { src: audioUrl, controls: true, preload: 'metadata' } })
        ]);
      } else if (contentType.indexOf('video/') === 0) {
        // 直接 URL：Range 流式播放，不预先下载整个视频（大文件也能立即拖动）。
        var videoUrl = '/api/preview?path=' + encodePath(file.path);
        var videoPlayer = el('video', {
          className: 'preview-video',
          attrs: { src: videoUrl, controls: true, preload: 'metadata' }
        });
        stage = el('div', { className: 'preview-stage is-video' }, [videoPlayer]);
        extras = await buildVideoExtras(file, videoPlayer, abortController.signal);
      } else {
        throw new ApiError(415, 'PREVIEW_UNSUPPORTED',
          '该文件类型暂不支持预览（' + (contentType || '未知类型') + '）。可点击“在新窗口打开”尝试。');
      }
      if (!ensureOpen()) { return; }
      var bodyChildren = [stage];
      if (extras) { bodyChildren.push(extras); }
      bodyChildren.push(el('p', { className: 'preview-path', text: file.path }));
      modal.setBody(el('div', { className: 'preview-body' }, bodyChildren));
      var focusable = $('img, video, audio, iframe', modal.modal);
      if (!focusable) { closeBtn.focus(); }
    } catch (err) {
      if (err && err.name === 'AbortError') { return; }
      showError(err);
    }
  }

  // ==================== 13.5 v0044 视频缩略图 / 媒体信息 / 并排对比 ====================
  var PREVIEW_SYNC_TOLERANCE = 0.5;

  function clockText(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    function pad(value) { return value < 10 ? '0' + value : String(value); }
    return (hours > 0 ? hours + ':' + pad(minutes) : String(minutes))
      + ':' + pad(secs);
  }

  function findGroupForFile(file) {
    if (!file) { return null; }
    var lists = [state.results.groups, state.results.similarGroups,
      state.results.videoGroups];
    for (var i = 0; i < lists.length; i += 1) {
      var list = lists[i] || [];
      for (var j = 0; j < list.length; j += 1) {
        var group = list[j] || {};
        var files = group.files || [];
        for (var k = 0; k < files.length; k += 1) {
          var item = files[k] || {};
          if ((file.file_id && item.file_id === file.file_id)
              || (file.path && item.path === file.path)) { return group; }
        }
      }
    }
    return null;
  }

  function videoPreviewUrl(path) {
    return '/api/preview?path=' + encodePath(path);
  }

  function mediaInfoText(media) {
    media = media || {};
    var parts = [];
    if (media.duration) { parts.push('时长 ' + clockText(media.duration)); }
    var video = media.video || {};
    if (video.width && video.height) { parts.push(video.width + '×' + video.height); }
    if (video.codec) { parts.push(String(video.codec).split(' ')[0]); }
    parts.push('音轨 ' + fmtInt(media.audio_stream_count || 0));
    parts.push('字幕轨 ' + fmtInt(media.subtitle_stream_count || 0));
    if (media.size) { parts.push(fmtBytes(media.size)); }
    if (!media.duration && !video.width) { parts.push('（无法读取媒体信息）'); }
    return parts.join(' · ');
  }

  function buildVideoExtras(file, player) {
    var wrap = el('div', { className: 'preview-extras' });
    var info = el('p', { className: 'preview-media-info', text: '正在读取媒体信息…' });
    var strip = el('div', { className: 'preview-thumb-strip' });
    var actions = el('div', { className: 'preview-extras-actions' });
    wrap.appendChild(info);
    wrap.appendChild(strip);
    wrap.appendChild(actions);
    var group = findGroupForFile(file);
    var siblings = ((group && group.files) || []).filter(function (item) {
      return item && item.path && item.path !== file.path;
    });
    if (siblings.length) {
      actions.appendChild(el('button', {
        className: 'btn btn-ghost btn-sm',
        attrs: { type: 'button' },
        on: { click: function () { openCompareView(file, group); } }
      }, [icon('eye'), '与同组文件并排对比']));
      actions.appendChild(el('span', {
        className: 'hint',
        text: '同组还有 ' + fmtInt(siblings.length)
          + ' 个文件可并排对比（时长 / 分辨率 / 音轨差异可帮助判断不同压制版本）'
      }));
    }
    api('/api/preview/thumbs?path=' + encodePath(file.path) + '&count=6&w=320',
      { timeout: 30000 }).then(function (plan) {
      setText(info, mediaInfoText(plan.media || {}));
      (plan.thumbs || []).forEach(function (item) {
        var thumb = el('img', {
          className: 'preview-thumb',
          attrs: {
            src: item.url, alt: '缩略图 ' + clockText(item.t),
            title: '跳到 ' + clockText(item.t), loading: 'lazy'
          }
        });
        thumb.addEventListener('click', function () {
          try {
            player.currentTime = Number(item.t) || 0;
            player.play();
          } catch (e) { /* 浏览器不支持时忽略 */ }
        });
        strip.appendChild(thumb);
      });
      if (!(plan.thumbs || []).length) {
        setText(info, '该视频无法生成缩略图（可用“在新窗口打开”查看）。');
      }
    }).catch(function (err) {
      setText(info, '媒体信息读取失败：' + errorMessage(err, '暂不可用'));
    });
    return wrap;
  }

  function openCompareView(file, group) {
    var files = ((group && group.files) || []).slice();
    if (!files.length) { files = [file]; }
    var found = files.filter(function (item) { return item.path === file.path; })[0];
    if (!found) { files.unshift(file); }
    var others = files.filter(function (item) { return item.path !== file.path; });
    var other = others[0] || file;
    var closeBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
      on: { click: function () { closeModal(true); } }
    });
    var syncInput = el('input', { attrs: { type: 'checkbox' } });
    var syncLabel = el('label', { className: 'check' }, [
      syncInput, '同步播放（勾选后两侧互相带动）'
    ]);
    var alignBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' }, text: '对齐到左侧时间点'
    });
    var modal = openModal({
      title: '并排对比（片段播放）',
      subtitle: '用于判断不同压制版本 / 剪辑版本；两侧视频可独立拖动或同步播放',
      size: 'xl',
      body: el('div', { className: 'modal-loading' }, [
        el('span', { className: 'spinner spinner-lg' }), '正在准备对比…'
      ]),
      footer: [syncLabel, alignBtn, closeBtn],
      initialFocus: closeBtn
    });
    var syncing = false;

    function loadPanelMeta(item, target) {
      setText(target, '正在读取媒体信息…');
      api('/api/preview/media?path=' + encodePath(item.path), { timeout: 20000 })
        .then(function (payload) {
          setText(target, mediaInfoText(payload.media || {}));
        })
        .catch(function (err) {
          setText(target, '媒体信息读取失败：' + errorMessage(err, '暂不可用'));
        });
    }

    function makePanel(initial, label) {
      var select = el('select', { attrs: { 'aria-label': label } });
      files.forEach(function (item) {
        var option = el('option', {
          attrs: { value: item.path },
          text: (item.name || item.path) + ' · ' + fmtBytes(item.size)
        });
        if (item.path === initial.path) { option.selected = true; }
        select.appendChild(option);
      });
      var video = el('video', {
        className: 'compare-video',
        attrs: {
          src: videoPreviewUrl(initial.path),
          controls: true, preload: 'metadata'
        }
      });
      var meta = el('p', { className: 'compare-meta', text: '' });
      var panel = { select: select, video: video, meta: meta, file: initial };
      select.addEventListener('change', function () {
        var next = files.filter(function (item) {
          return item.path === select.value;
        })[0];
        if (!next) { return; }
        panel.file = next;
        video.setAttribute('src', videoPreviewUrl(next.path));
        try { video.load(); } catch (e) { /* 忽略 */ }
        loadPanelMeta(next, meta);
      });
      loadPanelMeta(initial, meta);
      return panel;
    }

    var left = makePanel(file, '左侧文件');
    var right = makePanel(other, '右侧文件');
    var panels = [left, right];

    function maybeSync(source, target) {
      if (!syncInput.checked || syncing) { return; }
      var delta = Math.abs((target.video.currentTime || 0)
        - (source.video.currentTime || 0));
      if (delta < PREVIEW_SYNC_TOLERANCE) { return; }
      syncing = true;
      try { target.video.currentTime = source.video.currentTime || 0; } catch (e) { /* 忽略 */ }
      setTimeout(function () { syncing = false; }, 120);
    }

    panels.forEach(function (source) {
      var target = source === left ? right : left;
      source.video.addEventListener('play', function () {
        if (!syncInput.checked) { return; }
        try {
          if (target.video.paused) { target.video.play(); }
        } catch (e) { /* 忽略 */ }
        maybeSync(source, target);
      });
      source.video.addEventListener('pause', function () {
        if (!syncInput.checked) { return; }
        try {
          if (!target.video.paused) { target.video.pause(); }
        } catch (e) { /* 忽略 */ }
      });
      source.video.addEventListener('seeked', function () {
        maybeSync(source, target);
      });
    });

    alignBtn.addEventListener('click', function () {
      var time = Number(left.video.currentTime) || 0;
      try { right.video.currentTime = time; } catch (e) { /* 忽略 */ }
      toast('已把右侧对齐到 ' + clockText(time) + '。', 'info');
    });

    function panelNode(panel, label) {
      return el('div', { className: 'compare-panel' }, [
        el('div', { className: 'compare-panel-head' }, [
          el('span', { className: 'compare-panel-label', text: label }),
          panel.select
        ]),
        panel.video,
        panel.meta
      ]);
    }

    modal.setBody(el('div', { className: 'compare-body' }, [
      el('div', { className: 'compare-grid' }, [
        panelNode(left, '左侧'),
        panelNode(right, '右侧')
      ]),
      el('p', {
        className: 'hint',
        text: '提示：浏览器内置解码器不支持 HEVC / MKV / DTS 等编码时无法播放，但“在新窗口打开”或缩略图仍可用于核对文件内容。'
      })
    ]));
    return modal;
  }

  // ==================== 14. 两步删除流程 ====================
  function mapKeyList(map) {
    var keys = [];
    map.forEach(function (value, key) { keys.push(key); });
    return keys;
  }

  function beginDelete(override) {
    if (!state.scan.id) { toast('请先完成一次扫描。', 'info'); return; }
    if (state.deleteFlow.running) { toast('删除任务正在执行，请等待完成。', 'info'); return; }
    var totals = selectionTotals();
    var similarGroupId = override && override.similarGroupId ? override.similarGroupId : null;
    if (similarGroupId) {
      if (!state.results.similarReview.has(similarGroupId)) {
        toast('请先勾选该相似组的“人工复核”，再删除。', 'error');
        return;
      }
      var groupSelected = 0;
      state.results.similarSelected.forEach(function (item) {
        if (item && item.groupId === similarGroupId) { groupSelected += 1; }
      });
      if (!groupSelected) { toast('请先逐条勾选该组要删除的副本。', 'error'); return; }
    } else {
      if (!totals.count) { toast('请先选择要删除的副本。', 'error'); return; }
      if (totals.similarCount > 0 && !totals.allSimilarReviewed) {
        toast('存在未人工复核的相似组，无法删除。', 'error');
        return;
      }
      if (totals.mode === 'all' && (totals.similarCount > 0 || totals.videoCount > 0)) {
        toast('“全选精准组”不能与相似/视频文件混合删除，请手动选择精准组文件。', 'error', 5200);
        return;
      }
      if (state.results.mode === 'none' && totals.similarCount === 0 && totals.videoCount === 0) {
        toast('请先选择要删除的副本。', 'error');
        return;
      }
    }
    var flow = state.deleteFlow;
    flow.override = similarGroupId ? { similarGroupId: similarGroupId } : null;
    flow.plan = null;
    flow.job = null;
    flow.busy = false;
    flow.running = false;
    flow.recycle = !!state.recycleDefault;
    flow.step = 'plan';
    flow.permanentPhrase = '';
    flow.progressUi = null;
    flow.planUi = null;
    var bodyEl = el('div', { className: 'modal-loading' }, [el('span', { className: 'spinner spinner-lg' }), '正在生成删除计划…']);
    flow.modal = openModal({
      title: '删除计划确认（第 1 步 / 共 2 步）',
      size: 'lg',
      body: bodyEl,
      footer: [],
      dismissible: true,
      onClose: function () {
        if (flow.running) { return; }
        flow.plan = null;
        flow.step = null;
        flow.override = null;
      }
    });
    requestDeletePlan(flow.recycle);
  }

  function setDeleteFooter(nodes) {
    var flow = state.deleteFlow;
    if (flow.modal) { flow.modal.setFooter(nodes); }
  }

  function renderPlanLoading() {
    var flow = state.deleteFlow;
    if (!flow.modal) { return; }
    flow.modal.setTitle('删除计划确认（第 1 步 / 共 2 步）');
    flow.modal.setBody(el('div', { className: 'modal-loading' }, [
      el('span', { className: 'spinner spinner-lg' }), '正在由服务端校验文件并生成删除计划…'
    ]));
    setDeleteFooter([
      el('span', { className: 'footer-left', text: '本计划 5 分钟内有效，仅可执行一次。' }),
      el('button', {
        className: 'btn btn-ghost', attrs: { type: 'button' }, text: '取消',
        on: { click: function () { closeModal(true); } }
      })
    ]);
  }

  async function requestDeletePlan(recycle) {
    var flow = state.deleteFlow;
    if (!state.scan.id || flow.busy) { return; }
    flow.busy = true;
    flow.recycle = !!recycle;
    renderPlanLoading();
    try {
      var body;
      var override = flow.override || null;
      var similarIds = [];
      var reviewedGroupIds = [];
      var videoIds = [];
      if (override && override.similarGroupId) {
        state.results.similarSelected.forEach(function (item, fileId) {
          if (item && item.groupId === override.similarGroupId) { similarIds.push(fileId); }
        });
        reviewedGroupIds = [override.similarGroupId];
      } else {
        state.results.similarSelected.forEach(function (item, fileId) {
          similarIds.push(fileId);
          if (item && item.groupId && reviewedGroupIds.indexOf(item.groupId) === -1) {
            reviewedGroupIds.push(item.groupId);
          }
        });
        state.results.videoSelected.forEach(function (item, fileId) {
          videoIds.push(fileId);
        });
      }
      if (similarIds.length || videoIds.length) {
        var exactIds = state.results.mode === 'all' ? [] : mapKeyList(state.results.selected);
        if (state.results.mode === 'all' && !override) {
          throw new ApiError(400, 'SIMILAR_REVIEW_REQUIRED', '“全选精准组”不能与相似/视频文件混合删除，请先手动选择精准组文件。');
        }
        body = {
          file_ids: exactIds.concat(similarIds, videoIds),
          recycle: flow.recycle
        };
        if (similarIds.length) {
          body.confirm_similar = true;
          body.reviewed_similar_group_ids = reviewedGroupIds;
        }
      } else if (state.results.mode === 'all') {
        body = {
          scope: 'all_duplicates',
          exclude_file_ids: mapKeyList(state.results.excluded),
          include_manual_groups: false,
          recycle: flow.recycle
        };
      } else {
        body = { file_ids: mapKeyList(state.results.selected), recycle: flow.recycle };
      }
      if (flow.override) {
        flow.override.selectedFileIds = similarIds.slice();
        flow.override.reviewedGroupIds = reviewedGroupIds.slice();
      }
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/delete-plan', {
        method: 'POST', body: body, timeout: 30000
      });
      if (!res || !res.plan) { throw new ApiError(200, 'INVALID_RESPONSE', '服务未返回删除计划，请稍后重试。'); }
      flow.plan = res.plan;
      flow.recycle = res.plan.recycle !== false;
      renderPlanStep(res.plan);
    } catch (err) {
      var planMessage = errorMessage(err, '创建删除计划失败，请稍后重试。');
      if (err && err.code === 'SIMILAR_REVIEW_REQUIRED') {
        planMessage = '相似组必须人工复核并逐条选择后才能删除。' + (err.message ? ' ' + err.message : '');
      }
      renderPlanError(planMessage);
    } finally {
      flow.busy = false;
    }
  }

  function renderPlanError(message) {
    var flow = state.deleteFlow;
    if (!flow.modal) { return; }
    flow.modal.setTitle('删除计划确认（第 1 步 / 共 2 步）');
    flow.modal.setBody(el('div', {}, [
      el('div', { className: 'modal-error', attrs: { role: 'alert' }, text: message }),
      el('p', { className: 'modal-note', text: '尚未执行任何删除操作。可调整删除方式后重试，或关闭弹窗重新选择。' })
    ]));
    var closeBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '关闭',
      on: { click: function () { closeModal(true); } }
    });
    var retryBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '重试',
      on: { click: function () { requestDeletePlan(flow.recycle); } }
    });
    var footer = [el('span', { className: 'footer-left', text: '尚未删除任何文件' }), closeBtn];
    if (flow.recycle && /回收站|RECYCLE/i.test(message)) {
      footer.push(el('button', {
        className: 'btn btn-danger', attrs: { type: 'button' }, text: '改用永久删除重试',
        on: { click: function () { requestDeletePlan(false); } }
      }));
    }
    footer.push(retryBtn);
    setDeleteFooter(footer);
  }

  function renderPlanStep(plan) {
    var flow = state.deleteFlow;
    if (!flow.modal) { return; }
    flow.step = 'plan';
    var permanent = plan.recycle === false;
    var skipped = Array.isArray(plan.skipped) ? plan.skipped : [];
    var skippedTotal = typeof plan.skipped_total === 'number' ? plan.skipped_total : skipped.length;
    var unavailable = Array.isArray(plan.recycle_unavailable) ? plan.recycle_unavailable : [];
    var fallbackInfo = fallbackSelectionInfo();

    flow.modal.setTitle('删除计划确认（第 1 步 / 共 2 步）');
    var body = el('div', {});
    var revalidateNote = Number(plan.similar_count) > 0
      ? '服务端已重新校验：文件仍然存在、未被修改；精准组哈希与保留文件一致，相似组各自内容哈希与扫描时一致，且都不是保留项。校验未通过的文件会自动跳过，不会被删除。'
      : '服务端已重新校验：文件仍然存在、未被修改、哈希与保留文件一致，且不是保留项。校验未通过的文件会自动跳过，不会被删除。';
    body.appendChild(el('p', { className: 'modal-note', text: revalidateNote }));

    var kv = el('dl', { className: 'modal-kv' });
    function addKv(label, valueNodeOrText, cls) {
      var dd = el('dd', { className: cls || '' });
      appendChildren(dd, valueNodeOrText);
      kv.appendChild(el('dt', { text: label }));
      kv.appendChild(dd);
    }
    addKv('待删除文件', fmtInt(plan.count) + ' 个');
    addKv('可释放空间', fmtBytes(plan.bytes));
    var hasSplit = typeof plan.exact_count === 'number' || typeof plan.similar_count === 'number' ||
      typeof plan.video_count === 'number';
    if (hasSplit) {
      addKv('精准组文件', fmtInt(plan.exact_count) + ' 个 · ' + fmtBytes(plan.exact_bytes));
      addKv('内容相似文件', fmtInt(plan.similar_count) + ' 个 · ' + fmtBytes(plan.similar_bytes));
      var reviewedList = Array.isArray(plan.reviewed_similar_group_ids) ? plan.reviewed_similar_group_ids : [];
      if (reviewedList.length || plan.similar_count > 0) {
        addKv('相似组已复核', fmtInt(reviewedList.length) + ' 组');
      }
      if (typeof plan.video_count === 'number') {
        addKv('视频视觉相似文件', fmtInt(plan.video_count) + ' 个 · ' + fmtBytes(plan.video_bytes));
      }
      if (plan.manual_review) { addKv('人工复核', '已确认（相似组为逐条选择）'); }
    }
    if (fallbackInfo.count > 0) {
      addKv('修改时间降级', fmtInt(fallbackInfo.count) + ' 组');
    }
    addKv('删除方式', permanent ? '永久删除（不可恢复）' : ('移动到回收站（' + planRecycleModeLabel(plan) + '）'), permanent ? 'is-permanent' : '');
    addKv('涉及分组', fmtInt(plan.groups_affected) + ' 组');
    addKv('校验跳过', fmtInt(skippedTotal) + ' 个');
    if (plan.expires_at) { addKv('计划有效期', fmtDateTime(plan.expires_at)); }
    body.appendChild(el('div', { className: 'modal-section' }, [kv]));
    if (hasSplit && Number(plan.similar_count) > 0) {
      body.appendChild(el('div', { className: 'modal-section modal-warning' }, [
        icon('warning'),
        '本次计划包含内容相似文件：相似度是特征比对结果，存在误匹配概率。请确认已对相似组逐一人工复核，并核对保留项与文件清单。'
      ]));
    }
    if (Number(plan.video_count) > 0) {
      body.appendChild(el('div', { className: 'modal-section modal-warning' }, [
        icon('warning'),
        '本次计划包含视频视觉相似文件：视频相似度基于抽样帧比对，存在误匹配可能。请确认已核对保留视频，且仅对确认为重复副本的视频执行删除。'
      ]));
    }
    if (fallbackInfo.count > 0) {
      body.appendChild(el('div', { className: 'modal-section modal-warning' }, [
        icon('warning'),
        fallbackInfo.allMode
          ? '本次扫描共有 ' + fmtInt(fallbackInfo.count) + ' 组无可靠创建时间（按修改时间 mtime 保留）。全选删除可能包含这些组，请核对保留文件后再继续。'
          : '本次计划包含 ' + fmtInt(fallbackInfo.count) + ' 个“无创建时间 · 按修改时间保留”的组。请核对这些组的保留文件（保留依据为修改时间，创建时间不可用）。'
      ]));
    }

    // 回收站开关：切换后需要重新生成计划（计划中记录了删除方式）
    var recycleCheckbox = el('input', { attrs: { type: 'checkbox' } });
    recycleCheckbox.checked = !permanent;
    recycleCheckbox.disabled = false;
    recycleCheckbox.addEventListener('change', function () {
      if (flow.busy) { recycleCheckbox.checked = !permanent; return; }
      requestDeletePlan(recycleCheckbox.checked);
    });
    body.appendChild(el('div', { className: 'modal-section' }, [
      el('label', { className: 'modal-check-row' }, [
        recycleCheckbox,
        el('span', {}, [
          recycleMode === 'package'
            ? '移动到套件独立回收站（可在「任务中心 → 回收站与合并」整组恢复）'
            : '移动到回收站（推荐，可在 DSM 回收站中恢复）',
          el('span', { className: 'modal-note', text: '关闭后为永久删除，删除后不可恢复。' })
        ])
      ])
    ]));

    if (permanent) {
      body.appendChild(el('div', { className: 'modal-section modal-danger-box' }, [
        icon('warning'),
        '已选择永久删除：文件将从磁盘移除且无法恢复，请务必先备份重要数据。'
      ]));
    }

    if (unavailable.length) {
      var unavailableList = el('div', { className: 'modal-list' });
      unavailable.forEach(function (item) {
        unavailableList.appendChild(el('div', { className: 'modal-list-item' }, [
          el('span', { className: 'mono', text: (item.share || '') + ' ' + (item.path || '') })
        ]));
      });
      body.appendChild(el('div', { className: 'modal-section' }, [
        el('p', { className: 'modal-section-title', text: '回收站不可用的共享文件夹' }),
        el('div', { className: 'modal-warning' }, [
          icon('warning'),
          '以下共享文件夹未启用回收站，无法安全移动到回收站；如需清理请改用永久删除，或先在 DSM 中启用回收站。'
        ]),
        unavailableList
      ]));
    }

    if (skippedTotal > 0) {
      var skippedList = el('div', { className: 'modal-list' });
      skipped.slice(0, 200).forEach(function (item) {
        var reason = DELETE_REASON_LABEL[item.reason] || item.reason || '校验未通过';
        skippedList.appendChild(el('div', { className: 'modal-list-item' }, [
          el('span', { className: 'mono', text: item.path || '' }),
          el('span', { className: 'reason', text: reason + (item.message ? '：' + item.message : '') })
        ]));
      });
      body.appendChild(el('div', { className: 'modal-section' }, [
        el('p', { className: 'modal-section-title', text: '已跳过（不会删除）' }),
        skippedList,
        skippedTotal > 200 ? el('p', { className: 'modal-note', text: '仅显示前 200 条，共 ' + fmtInt(skippedTotal) + ' 条。' }) : null
      ]));
    }

    if (!plan.count || Number(plan.count) <= 0) {
      body.appendChild(el('div', { className: 'state-box state-empty', text: '没有可安全删除的文件。请关闭弹窗，检查扫描结果或稍后重试。' }));
    }

    flow.modal.setBody(body);
    var cancelBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '取消',
      on: { click: function () { closeModal(true); } }
    });
    var nextBtn = el('button', {
      className: 'btn btn-danger', attrs: { type: 'button' }, text: '下一步：最终确认',
      on: { click: function () { if (flow.plan) { renderConfirmStep(flow.plan); } } }
    });
    nextBtn.disabled = !(Number(plan.count) > 0);
    setDeleteFooter([
      el('span', { className: 'footer-left', text: '第 1 步：核对数量与删除方式' }),
      cancelBtn, nextBtn
    ]);
  }

  function renderConfirmStep(plan) {
    var flow = state.deleteFlow;
    if (!flow.modal) { return; }
    flow.step = 'confirm';
    var permanent = plan.recycle === false;
    var skippedTotal = typeof plan.skipped_total === 'number' ? plan.skipped_total : (Array.isArray(plan.skipped) ? plan.skipped.length : 0);
    var fallbackInfo = fallbackSelectionInfo();
    flow.modal.setTitle('最终确认（第 2 步 / 共 2 步）');
    var body = el('div', {});
    body.appendChild(el('div', { className: 'modal-danger-box' }, [
      icon('warning'),
      permanent
        ? '即将永久删除以下文件。删除后不可恢复，请确认已完成备份并核对保留项！'
        : ((plan && plan.recycle_mode) === 'package'
          ? '即将把以下文件移动到套件独立回收站，可在「任务中心 → 回收站与合并」整组恢复。'
          : '即将把以下文件移动到回收站，可在 DSM 回收站中恢复。')
    ]));
    var kv = el('dl', { className: 'modal-kv' });
    function addKv(label, value, cls) {
      kv.appendChild(el('dt', { text: label }));
      kv.appendChild(el('dd', { className: cls || '', text: value }));
    }
    addKv('待删除文件', fmtInt(plan.count) + ' 个');
    addKv('可释放空间', fmtBytes(plan.bytes));
    if (typeof plan.exact_count === 'number' || typeof plan.similar_count === 'number' ||
        typeof plan.video_count === 'number') {
      addKv('精准组文件', fmtInt(plan.exact_count) + ' 个 · ' + fmtBytes(plan.exact_bytes));
      addKv('内容相似文件', fmtInt(plan.similar_count) + ' 个 · ' + fmtBytes(plan.similar_bytes));
      var reviewedIds = Array.isArray(plan.reviewed_similar_group_ids) ? plan.reviewed_similar_group_ids : [];
      addKv('相似组已复核', fmtInt(reviewedIds.length) + ' 组');
      if (typeof plan.video_count === 'number') {
        addKv('视频视觉相似文件', fmtInt(plan.video_count) + ' 个 · ' + fmtBytes(plan.video_bytes));
      }
    }
    if (fallbackInfo.count > 0) {
      addKv('修改时间降级', fmtInt(fallbackInfo.count) + ' 组');
    }
    addKv('删除方式', permanent ? '永久删除（不可恢复）' : ('移动到回收站（' + planRecycleModeLabel(plan) + '）'), permanent ? 'is-permanent' : '');
    addKv('涉及分组', fmtInt(plan.groups_affected) + ' 组');
    addKv('校验跳过', fmtInt(skippedTotal) + ' 个');
    body.appendChild(el('div', { className: 'modal-section' }, [kv]));
    body.appendChild(el('p', { className: 'modal-note', text: '需人工确认的重复组不在删除范围内；保留项不会被删除。相似组仅包含已人工复核并逐条选择的文件。' }));
    if (Number(plan.video_count) > 0) {
      body.appendChild(el('div', { className: 'modal-section modal-warning' }, [
        icon('warning'),
        '本计划包含 ' + fmtInt(plan.video_count) + ' 个视频视觉相似文件：抽样帧比对存在误匹配可能，请再次核对保留视频后再确认删除。'
      ]));
    }
    if (fallbackInfo.count > 0) {
      body.appendChild(el('div', { className: 'modal-section modal-warning' }, [
        icon('warning'),
        '本计划包含 ' + fmtInt(fallbackInfo.count) + ' 个无可靠创建时间的组：保留项按修改时间（mtime）选择，请再次核对保留文件，避免误删仍需要保留的副本。'
      ]));
    }

    var confirmCheckbox = el('input', { attrs: { type: 'checkbox' } });
    var confirmRow = el('label', { className: 'modal-check-row' }, [
      confirmCheckbox,
      el('span', { text: '我已核对删除清单与保留项，确认执行删除操作。' })
    ]);
    body.appendChild(el('div', { className: 'modal-section' }, [confirmRow]));

    var phraseInput = null;
    var phraseError = null;
    if (permanent) {
      phraseInput = el('input', {
        className: 'phrase-input',
        attrs: { type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: '请输入“永久删除”', 'aria-label': '永久删除确认短语' }
      });
      phraseError = el('p', { className: 'field-error', attrs: { role: 'alert' }, hidden: true });
      body.appendChild(el('div', { className: 'modal-section' }, [
        el('p', { className: 'modal-section-title', text: '请输入“永久删除”以确认（不可恢复）' }),
        phraseInput,
        phraseError
      ]));
    }

    var errorBox = el('div', { className: 'modal-error', attrs: { role: 'alert' }, hidden: true });
    body.appendChild(errorBox);

    var deleteBtn = el('button', { className: 'btn btn-danger', attrs: { type: 'button' }, text: permanent ? '永久删除' : '确认删除' });
    deleteBtn.disabled = true;
    var backBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '上一步',
      on: { click: function () { renderPlanStep(plan); } }
    });
    var cancelBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: '取消',
      on: { click: function () { closeModal(true); } }
    });

    function validate() {
      var phraseOk = true;
      if (permanent) {
        var typed = phraseInput.value.trim();
        phraseOk = typed === '永久删除';
        if (phraseInput.value && !phraseOk) { setText(phraseError, '确认短语不正确，请输入“永久删除”。'); show(phraseError); }
        else { hide(phraseError); }
      }
      var ok = confirmCheckbox.checked && phraseOk;
      deleteBtn.disabled = !ok || flow.busy;
      return ok;
    }
    confirmCheckbox.addEventListener('change', validate);
    if (phraseInput) { phraseInput.addEventListener('input', validate); }
    deleteBtn.addEventListener('click', function () {
      if (!validate()) { return; }
      if (permanent && phraseInput.value.trim() !== '永久删除') { return; }
      flow.permanentPhrase = permanent ? '永久删除' : '';
      executeDelete(plan, permanent ? phraseInput.value.trim() : '', errorBox, deleteBtn);
    });

    flow.modal.setBody(body);
    setDeleteFooter([
      el('span', { className: 'footer-left', text: '第 2 步：最终确认（需勾选确认）' }),
      cancelBtn, backBtn, deleteBtn
    ]);
  }

  async function executeDelete(plan, permanentPhrase, errorBox, deleteBtn) {
    var flow = state.deleteFlow;
    if (flow.busy || !state.scan.id) { return; }
    flow.busy = true;
    if (deleteBtn) { setBusy(deleteBtn, true, '正在提交…'); }
    if (errorBox) { hide(errorBox); }
    try {
      var body = { plan_id: plan.plan_id, confirm: 'DELETE' };
      if (plan.recycle === false) { body.permanent_phrase = permanentPhrase; }
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/delete', {
        method: 'POST', body: body, timeout: 30000
      });
      if (!res || !res.job_id) { throw new ApiError(200, 'INVALID_RESPONSE', '服务未返回删除任务编号，请稍后重试。'); }
      flow.job = {
        job_id: res.job_id, status: 'running', progress: 0, total: plan.count, done: 0,
        failed: 0, bytes_total: plan.bytes, bytes_done: 0, results: []
      };
      flow.plan = plan;
      showDeleteProgress(flow.job);
      startDeletePolling(res.job_id);
      toast('删除任务已开始，正在逐文件处理。', 'info');
    } catch (err) {
      flow.busy = false;
      if (deleteBtn) { setBusy(deleteBtn, false); }
      var message = errorMessage(err, '删除请求失败，请稍后重试。');
      if (errorBox) {
        setText(errorBox, message);
        show(errorBox);
      } else {
        toast(message, 'error');
      }
    }
  }

  function startDeletePolling(jobId) {
    var flow = state.deleteFlow;
    stopDeletePolling();
    flow.running = true;
    flow.polling = true;
    flow.pollErrors = 0;
    setModalDismissible(false);
    if (flow.modal) { flow.modal.setBusy(true); }
    pollDeleteOnce();
    flow.timer = setInterval(pollDeleteOnce, POLL_MS);
  }

  function stopDeletePolling() {
    var flow = state.deleteFlow;
    if (flow.timer) { clearInterval(flow.timer); flow.timer = null; }
    if (flow.abort) {
      try { flow.abort.abort(); } catch (e) { /* 忽略 */ }
      flow.abort = null;
    }
    flow.polling = false;
  }

  async function pollDeleteOnce() {
    var flow = state.deleteFlow;
    if (!flow.polling || !flow.job || !state.scan.id) { return; }
    if (flow.abort) { try { flow.abort.abort(); } catch (e) { /* 忽略 */ } }
    var controller = new AbortController();
    flow.abort = controller;
    try {
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/delete/' + encodePath(flow.job.job_id), {
        signal: controller.signal, timeout: 15000
      });
      if (!flow.polling) { return; }
      if (!res || !res.job) { return; }
      flow.pollErrors = 0;
      flow.job = res.job;
      updateDeleteProgress(res.job);
      if (['completed', 'partial', 'failed', 'stopped'].indexOf(res.job.status) !== -1) {
        flow.polling = false;
        if (flow.timer) { clearInterval(flow.timer); flow.timer = null; }
        flow.running = false;
        flow.busy = false;
        setModalDismissible(true);
        if (flow.modal) { flow.modal.setBusy(false); }
        finishDelete(res.job);
      }
    } catch (err) {
      if (err && err.name === 'AbortError') { return; }
      flow.pollErrors = (flow.pollErrors || 0) + 1;
      if (flow.pollErrors >= 5) {
        flow.polling = false;
        if (flow.timer) { clearInterval(flow.timer); flow.timer = null; }
        flow.running = false;
        flow.busy = false;
        setModalDismissible(true);
        if (flow.modal) { flow.modal.setBusy(false); }
        toast('删除任务进度轮询中断，请稍后在操作日志中查看结果。', 'error', 6000);
      }
    }
  }

  function showDeleteProgress(job) {
    var flow = state.deleteFlow;
    if (!flow.modal) { return; }
    flow.step = 'progress';
    flow.modal.setTitle('正在删除文件');
    var bar = el('div', { className: 'progress-bar', attrs: { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0' } });
    var fill = el('div', { className: 'progress-fill' });
    bar.appendChild(fill);
    var pctEl = el('span', { className: 'progress-pct', text: '0%' });
    var head = el('div', { className: 'delete-progress-head' }, [
      el('span', { text: '正在删除文件…' }), pctEl
    ]);
    var counters = el('div', { className: 'job-summary-grid' });
    var doneEl = el('dd', { text: '0' });
    var failedEl = el('dd', { text: '0' });
    var totalEl = el('dd', { text: fmtInt(job.total) });
    counters.appendChild(el('div', {}, [el('dt', { text: '已完成' }), doneEl]));
    counters.appendChild(el('div', {}, [el('dt', { text: '失败' }), failedEl]));
    counters.appendChild(el('div', {}, [el('dt', { text: '总计' }), totalEl]));
    var statusEl = el('p', { className: 'modal-note', text: '正在逐文件校验并删除，请勿关闭页面。' });
    var resultList = el('div', { className: 'result-list' });
    var summary = el('div', { className: 'delete-progress-summary' });
    var body = el('div', {}, [head, bar, statusEl, counters, summary, resultList]);
    flow.progressUi = { bar: bar, fill: fill, pctEl: pctEl, doneEl: doneEl, failedEl: failedEl, totalEl: totalEl, statusEl: statusEl, resultList: resultList, summary: summary };
    flow.modal.setBody(body);
    setDeleteFooter([
      el('span', { className: 'footer-left', text: '删除任务执行中，页面会自动刷新结果。' })
    ]);
    updateDeleteProgress(job);
  }

  function updateDeleteProgress(job) {
    var flow = state.deleteFlow;
    if (!flow.progressUi) { return; }
    var ui = flow.progressUi;
    var total = Number(job.total) || 0;
    var done = Number(job.done) || 0;
    var failed = Number(job.failed) || 0;
    var pct = total > 0 ? clamp(Math.round(((done + failed) / total) * 100), 0, 100) : (Number(job.progress) ? Math.round(Number(job.progress) * 100) : 0);
    ui.pctEl.textContent = pct + '%';
    ui.fill.style.width = pct + '%';
    ui.bar.setAttribute('aria-valuenow', String(pct));
    ui.doneEl.textContent = fmtInt(done);
    ui.failedEl.textContent = fmtInt(failed);
    ui.totalEl.textContent = fmtInt(total);
    var statusText = JOB_STATUS_LABEL[job.status] || job.status || '删除中';
    var bytesInfo = fmtBytes(job.bytes_done) + ' / ' + fmtBytes(job.bytes_total);
    setText(ui.statusEl, (job.status === 'running' ? '正在逐文件校验并删除，请勿关闭页面。' : '删除任务' + statusText + '。') + ' 已处理字节：' + bytesInfo);
    var results = Array.isArray(job.results) ? job.results : [];
    var resultSig = results.length + '|' + done + '|' + failed + '|' + job.status;
    if (ui.lastResultSig === resultSig) { return; }
    ui.lastResultSig = resultSig;
    clear(ui.resultList);
    if (!results.length) {
      ui.resultList.appendChild(el('div', { className: 'tree-empty', text: job.status === 'running' ? '正在等待逐文件结果…' : '没有逐文件结果。' }));
    } else {
      results.slice(0, 300).forEach(function (result) {
        ui.resultList.appendChild(renderDeleteResultItem(result));
      });
      if (results.length > 300) {
        ui.resultList.appendChild(el('div', { className: 'tree-empty', text: '仅显示前 300 条结果，共 ' + fmtInt(results.length) + ' 条。' }));
      }
    }
    clear(ui.summary);
    var remaining = Math.max(0, total - done - failed);
    appendChildren(ui.summary, [
      el('p', { className: 'modal-note', text: '状态：' + statusText + ' · 剩余 ' + fmtInt(remaining) + ' 个' })
    ]);
  }

  function renderDeleteResultItem(result) {
    var label = DELETE_STATUS_LABEL[result.status] || result.status || '处理中';
    var item = el('div', { className: 'result-item' }, [
      el('span', { className: 'result-status is-' + (result.status || 'pending'), text: label }),
      el('div', { className: 'result-path' }, [el('span', { text: result.path || '' })])
    ]);
    var detail = '';
    if (result.reason) { detail = DELETE_REASON_LABEL[result.reason] || result.reason; }
    if (result.message) { detail += (detail ? '：' : '') + result.message; }
    if (detail) {
      item.lastChild.appendChild(el('span', { className: 'result-msg', text: detail }));
    }
    if (result.recycle_path) {
      item.lastChild.appendChild(el('span', { className: 'result-msg', text: '回收站路径：' + result.recycle_path }));
    }
    return item;
  }

  async function finishDelete(job) {
    var flow = state.deleteFlow;
    var status = job.status;
    var done = Number(job.done) || 0;
    var failed = Number(job.failed) || 0;
    var message;
    if (status === 'completed') {
      message = '删除完成：成功处理 ' + fmtInt(done) + ' 个文件' + (failed ? '，失败 ' + fmtInt(failed) + ' 个' : '') + '。';
      toast(withRemovedHint(message, done), 'success', 6000);
    } else if (status === 'partial') {
      message = '删除部分完成：成功 ' + fmtInt(done) + ' 个，失败 ' + fmtInt(failed) + ' 个。请查看逐文件结果。';
      toast(withRemovedHint(message, done), 'error', 7000);
    } else if (status === 'stopped') {
      message = '删除任务已停止：成功 ' + fmtInt(done) + ' 个，失败 ' + fmtInt(failed) + ' 个。';
      toast(withRemovedHint(message, done), 'info', 6000);
    } else {
      message = '删除任务失败：成功 ' + fmtInt(done) + ' 个，失败 ' + fmtInt(failed) + ' 个。请查看操作日志。';
      toast(message, 'error', 7000);
    }
    if (flow.modal) {
      flow.modal.setTitle('删除结果');
      var resultBox = el('div', { className: 'modal-danger-box' }, [icon('warning'), message]);
      if (flow.progressUi && flow.progressUi.summary) {
        clear(flow.progressUi.summary);
        appendChildren(flow.progressUi.summary, [
          el('p', { className: 'modal-note', text: message }),
          el('p', { className: 'modal-note', text: '需人工确认的组未参与删除；如需清理，请先在 DSM 中人工确认保留项。' })
        ]);
      }
      flow.modal.setFooter([el('button', {
        className: 'btn btn-primary', attrs: { type: 'button' }, text: '关闭',
        on: { click: function () { closeModal(true); } }
      })]);
    }
    state.historyCache = null;
    flow.plan = null;
    clearSelectionState();
    await refreshScanAfterDelete();
  }

  /** 删除成功的 toast 附加“已从结果列表移除”语义（保持原有完成提示为前提）。 */
  function withRemovedHint(message, done) {
    return done > 0 ? message + '成功删除的文件已从结果列表移除。' : message;
  }

  /**
   * 删除结束后刷新结果页（v0032）：后端在删除成功时会写入数据库标记，
   * groups / stats 接口会自动排除已删除文件；前端只在删除结束后强制刷新一次：
   * 1) 重置分页游标与已加载列表到第一页，清空全部选择状态与“删除选中”按钮状态；
   * 2) 重新拉取扫描详情（模式/进度）、分组列表、统计摘要与各分区计数；
   * 3) 当前页删空时回退到上一页（删除前 offset > 0 时），否则由空状态提示兜底，避免停留在空白页。
   */
  async function refreshScanAfterDelete() {
    if (!state.scan.id) { return; }
    var scanId = state.scan.id;
    var loadedBefore = {
      exactOffset: state.results.offset,
      similarOffset: state.results.similarOffset,
      videoOffset: state.results.videoOffset
    };
    resetResultsViewAfterDelete();
    try {
      var res = await api('/api/scans/' + encodePath(scanId), { timeout: 15000 });
      if (state.scan.id !== scanId) { return; }
      if (res && res.scan) {
        state.scan.data = res.scan;
        state.results.scanMode = detectScanMode(res.scan);
        renderScanProgress(res.scan);
      }
    } catch (e) {
      /* 扫描详情刷新失败不阻塞结果列表刷新（分组/统计仍会重新拉取）。 */
    }
    if (state.scan.id !== scanId) { return; }
    await loadResults(true);
    if (state.scan.id !== scanId) { return; }
    await recoverEmptyResultsPageAfterDelete(loadedBefore);
  }

  /** 删除后强制把结果视图重置到第一页：清空已加载分组、分页游标与全部选择状态。 */
  function resetResultsViewAfterDelete() {
    var results = state.results;
    results.groups = [];
    results.total = 0;
    results.offset = 0;
    results.loaded = false;
    results.loading = false;
    results.seq += 1;
    results.similarGroups = [];
    results.similarTotal = 0;
    results.similarOffset = 0;
    results.similarLoaded = false;
    results.similarLoading = false;
    results.similarSeq += 1;
    results.videoGroups = [];
    results.videoTotal = 0;
    results.videoOffset = 0;
    results.videoLoaded = false;
    results.videoLoading = false;
    results.videoSeq += 1;
    results.exactTotal = 0;
    results.videoSelectAllMode = false;
    clearSelectionState();
    updateSelectionSummary();
    if (els.groupsList) { clear(els.groupsList); }
    if (els.similarGroupsList) { clear(els.similarGroupsList); }
    if (els.videoGroupsList) { clear(els.videoGroupsList); }
    setHidden(els.resultsEmpty, true);
    setHidden(els.resultsError, true);
  }

  /**
   * 兜底：删除后已重置到第一页；若第一页仍为空但总数大于 0（游标/服务端异常），
   * 回退到删除前当前页的上一页（offset - limit，仅当 offset > 0）重新加载；
   * 仍无数据时显示空状态提示，避免停留在空白页。
   */
  async function recoverEmptyResultsPageAfterDelete(loadedBefore) {
    var results = state.results;
    if (!state.scan.id) { return; }
    // 搜索态下分区总数是未按搜索词过滤的全量值，不能据此判断“页面为空”，跳过兜底。
    if (results.q) { return; }
    var before = loadedBefore || {};
    if (results.loaded && !results.loading && !results.groups.length && results.total > 0) {
      var exactOffset = previousResultsPageOffset(before.exactOffset, results.limit);
      if (exactOffset > 0) { await loadExactPartition(false, exactOffset); }
    }
    if (results.similarLoaded && !results.similarLoading && !results.similarGroups.length &&
        results.similarTotal > 0) {
      var similarOffset = previousResultsPageOffset(before.similarOffset, results.limit);
      if (similarOffset > 0) { await loadSimilarPartition(false, similarOffset); }
    }
    if (results.videoLoaded && !results.videoLoading && !results.videoGroups.length &&
        results.videoTotal > 0) {
      var videoOffset = previousResultsPageOffset(before.videoOffset, results.limit);
      if (videoOffset > 0) { await loadVideoPartition(false, videoOffset); }
    }
    // 回退后仍为空：显式展示空状态，避免结果区只剩空白列表（已显示错误框时不覆盖）。
    if (results.loaded && !results.loading && !results.groups.length && results.total > 0 &&
        !(els.resultsError && els.resultsError.hidden === false)) {
      setText(els.resultsEmpty, '结果页已刷新，但未返回可展示的分组。请点击“刷新结果”重新加载。');
      setHidden(els.resultsEmpty, false);
    }
    if (results.similarLoaded && !results.similarLoading && !results.similarGroups.length &&
        results.similarTotal > 0 && !(els.similarError && els.similarError.hidden === false)) {
      setText(els.similarEmpty, '结果页已刷新，但未返回可展示的内容相似组。请点击“刷新结果”重新加载。');
      setHidden(els.similarEmpty, false);
    }
    if (results.videoLoaded && !results.videoLoading && !results.videoGroups.length &&
        results.videoTotal > 0 && !(els.videoError && els.videoError.hidden === false)) {
      setText(els.videoEmpty, '结果页已刷新，但未返回可展示的视频相似组。请点击“刷新结果”重新加载。');
      setHidden(els.videoEmpty, false);
    }
  }

  /** 删除前的当前页 offset 回退一页得到的 offset；offset 无效或已在第一页时返回 0。 */
  function previousResultsPageOffset(lastOffset, limit) {
    var offset = Number(lastOffset);
    var pageSize = Number(limit);
    if (!isFinite(offset) || offset <= 0) { return 0; }
    if (!isFinite(pageSize) || pageSize <= 0) { pageSize = 1; }
    return Math.max(0, Math.floor(offset) - Math.floor(pageSize));
  }

  // ==================== 15. 历史记录 ====================
  var LOG_KIND_LABEL = { scan: '扫描日志', delete: '删除日志', audit: '审计日志', deletion: '删除日志', other: '其他' };

  async function loadHistory() {
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
      on: { click: function () { openHistoryScan(scan); } }
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

  function openHistoryScan(scan) {
    if (!scan || !scan.scan_id) { return; }
    var scanId = scan.scan_id;
    stopScanPolling();
    closeModal(true);
    setView('scan');
    state.scan.id = scanId;
    state.scan.handled = true;
    state.scan.data = scan;
    resetResultsState();
    state.results.scanMode = detectScanMode(scan);
    show(els.progressPanel);
    renderScanProgress(scan);
    if (ACTIVE_STATUS.indexOf(scan.status) !== -1) {
      if (scan.status === 'paused') {
        toast('扫描已暂停；点击“继续扫描”从断点恢复。', 'info');
      } else {
        startScanPolling(scanId);
        toast('已打开正在运行的扫描任务进度。', 'info');
      }
    } else {
      hide(els.resultsSection);
      api('/api/scans/' + encodePath(scanId), { timeout: 15000 }).then(function (res) {
        if (state.scan.id !== scanId || !res || !res.scan) { return; }
        state.scan.data = res.scan;
        state.results.scanMode = detectScanMode(res.scan);
        renderScanProgress(res.scan);
        if (res.scan.status === 'completed' || res.scan.status === 'stopped') {
          show(els.resultsSection);
          loadResults(true);
        }
      }).catch(function (err) {
        toast(errorMessage(err, '读取扫描详情失败。'), 'error');
      });
    }
    scrollToNode(els.progressPanel);
  }

  // ---------- 字幕任务历史面板（GET /api/subtitles / pause / resume / cancel） ----------
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

  function stopSubtitleJobsPolling() {
    var jobsState = state.subtitleJobs;
    if (!jobsState) { return; }
    if (jobsState.timer) {
      clearTimeout(jobsState.timer);
      jobsState.timer = null;
    }
  }

  function scheduleSubtitleJobsPoll() {
    stopSubtitleJobsPolling();
    if (state.view !== 'history' || state.taskTab !== 'subtitles' || !state.subtitleJobs) { return; }
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

  // ==================== v0048：字幕失败清单 / 优先级 / 目录监控 ====================
  function subtitleFailureLabel(key, data) {
    var labels = (data && data.category_labels)
      || (state.meta && state.meta.subtitle
        && state.meta.subtitle.failure_category_labels) || {};
    return labels[key] || SUBTITLE_FAILURE_LABEL[key] || key || '未知错误';
  }

  async function loadSubtitleFailures() {
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

  async function subtitleFailuresRetry(force) {
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

  async function loadSubtitleWatchStatus() {
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

  async function subtitleWatchScan() {
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

  async function loadSubtitleJobs(options) {
    options = options || {};
    if (!els.subtitleJobsList || state.subtitleJobs.loading) { return; }
    state.subtitleJobs.loading = true;
    if (!options.silent) {
      setHidden(els.subtitleJobsLoading, false);
      setHidden(els.subtitleJobsError, true);
      setHidden(els.subtitleJobsEmpty, true);
    }
    try {
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

  // ---- v0038：步骤时间线 ----
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
      el('span', { className: 'badge ' + subtitleJobStatusClass(status), text: subtitleJobStatusLabel(status) }),
      el('span', { className: 'subtitle-job-path', attrs: { title: job.path || '' }, text: job.path || '（无路径）' })
    ]);
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

  function renderSubtitleJobs(jobs) {
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

  function goToSubtitleJobs() {
    state.taskTab = 'subtitles';
    setView('history');
    setTimeout(function () {
      if (els.subtitleJobsPanel) { scrollToNode(els.subtitleJobsPanel); }
    }, 350);
  }

  function selectedVideoPaths() {
    var selected = state.results.videoSelected;
    var paths = [];
    if (!selected || !selected.size) { return paths; }
    state.results.videoGroups.forEach(function (group) {
      (group.files || []).forEach(function (file) {
        if (file && file.path && selected.has(file.file_id) && paths.indexOf(file.path) === -1) {
          paths.push(file.path);
        }
      });
    });
    return paths;
  }

  function updateBatchSubtitleButton() {
    if (!els.batchSubtitleBtn) { return; }
    var paths = selectedVideoPaths();
    var ready = subtitleReady();
    var show = paths.length > 0;
    els.batchSubtitleBtn.hidden = !show;
    if (!show) { els.batchSubtitleBtn.disabled = false; return; }
    els.batchSubtitleBtn.disabled = !ready;
    els.batchSubtitleBtn.title = ready
      ? '为选中的 ' + paths.length + ' 个视频批量生成字幕（音频将上传第三方）'
      : subtitleUnavailableReason();
  }

  function openSubtitleBatchModal(paths) {
    if (!subtitleReady()) { toast(subtitleUnavailableReason(), 'error'); return; }
    if (!paths || !paths.length) { toast('请先在视频结果中勾选要生成字幕的视频副本。', 'info'); return; }
    var sub = subtitleCapability();
    var languageSelect = buildSubtitleLanguageSelect({
      includeAuto: true, ariaLabel: '批量识别语言',
      value: sub.asrLanguage || 'auto'
    });
    var translateCheck = el('input', { attrs: { type: 'checkbox' } });
    if (!sub.llmKeySet) { translateCheck.disabled = true; }
    var targetSelect = buildSubtitleLanguageSelect({
      ariaLabel: '批量翻译目标语言', value: sub.targetLang || 'zh'
    });
    targetSelect.disabled = true;
    translateCheck.addEventListener('change', function () { targetSelect.disabled = !translateCheck.checked; });
    var confirmCheck = el('input', { attrs: { type: 'checkbox' } });
    var errorBox = el('div', { className: 'modal-error', attrs: { role: 'alert' }, hidden: true });
    var statusEl = el('p', { className: 'subtitle-job-meta', text: '将按顺序提交 ' + paths.length + ' 个字幕任务。' });
    var pathList = el('div', { className: 'batch-subtitle-paths' });
    paths.slice(0, 8).forEach(function (path) {
      pathList.appendChild(el('div', { className: 'mono', text: path }));
    });
    if (paths.length > 8) { pathList.appendChild(el('div', { className: 'hint', text: '…等共 ' + paths.length + ' 个视频' })); }
    var body = el('div', { className: 'subtitle-progress-card' }, [
      el('div', { className: 'privacy-warning', attrs: { role: 'alert' } }, [
        icon('warning'),
        el('p', { text: '音频将上传到所配置的第三方服务（ASR / 翻译）。请确认这些视频内容适合上传，并已获得授权；API Key 不会在界面回显。' })
      ]),
      el('p', { className: 'modal-note', text: '已选择 ' + paths.length + ' 个视频。批量提交后可在“历史记录 → 字幕任务”中统一查看进度、暂停/继续、取消和下载。' }),
      el('div', { className: 'form-grid' }, [
        el('div', { className: 'field' }, [
          el('label', { text: '识别语言' }), languageSelect
        ]),
        el('div', { className: 'field' }, [
          el('label', { className: 'check' }, [translateCheck, document.createTextNode('生成译文')]),
          targetSelect,
          el('p', { className: 'hint', text: sub.llmKeySet ? '译文只处理字幕文本，不改时间轴。' : '未配置 LLM API Key，暂不能生成译文。' })
        ])
      ]),
      pathList,
      el('label', { className: 'modal-check-row' }, [
        confirmCheck,
        el('span', { text: '我了解音频将上传到所配置的第三方服务，并确认有权处理这些视频内容。' })
      ]),
      statusEl,
      errorBox
    ]);
    var cancelBtn = el('button', { className: 'btn btn-ghost', attrs: { type: 'button' }, text: '取消', on: { click: function () { closeModal(true); } } });
    var submitBtn = el('button', { className: 'btn btn-primary', attrs: { type: 'button' }, text: '批量生成字幕' });
    submitBtn.disabled = true;
    confirmCheck.addEventListener('change', function () { submitBtn.disabled = !confirmCheck.checked; });
    var modal = openModal({
      title: '批量生成云端字幕',
      subtitle: '共 ' + paths.length + ' 个视频',
      size: 'lg',
      body: body,
      footer: [cancelBtn, submitBtn],
      initialFocus: confirmCheck
    });
    var submitting = false;
    submitBtn.addEventListener('click', async function () {
      if (submitting || !confirmCheck.checked) { return; }
      submitting = true;
      hide(errorBox);
      setBusy(submitBtn, true, '正在提交…');
      var ok = 0;
      var fail = 0;
      var firstError = '';
      var translate = !!(translateCheck.checked && sub.llmKeySet);
      for (var i = 0; i < paths.length; i += 1) {
        setText(statusEl, '正在提交 ' + (i + 1) + ' / ' + paths.length + '…');
        try {
          await api('/api/subtitles', {
            method: 'POST',
            body: {
              path: paths[i],
              language: languageSelect.value,
              translate: translate,
              target_lang: targetSelect.value,
              confirm_cloud: true
            },
            timeout: 30000
          });
          ok += 1;
        } catch (err) {
          fail += 1;
          if (!firstError) { firstError = errorMessage(err, '提交字幕任务失败。'); }
        }
      }
      if (ok > 0) {
        closeModal(true);
        toast('已提交 ' + ok + ' 个字幕任务' + (fail ? '，失败 ' + fail + ' 个' : '') + '。可在字幕任务面板查看进度。',
          fail ? 'error' : 'success', 7000);
        goToSubtitleJobs();
      } else {
        submitting = false;
        setBusy(submitBtn, false);
        setText(statusEl, '批量提交失败。');
        setText(errorBox, firstError || '批量提交失败，请稍后重试。');
        show(errorBox);
      }
    });
  }

  // ---------- 删除任务历史（GET /api/deletions?limit=20） ----------
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

  async function loadDeletionJobs() {
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
    if (cached) { openHistoryScan(cached); return; }
    if (button) { setBusy(button, true, '正在打开…'); }
    try {
      var res = await api('/api/scans/' + encodePath(scanId), { timeout: 15000 });
      if (res && res.scan) { openHistoryScan(res.scan); }
      else { toast('未找到对应的扫描任务，可能已被清理。', 'error'); }
    } catch (err) {
      toast(errorMessage(err, '打开扫描任务失败，请稍后重试。'), 'error', 6000);
    } finally {
      if (button) { setBusy(button, false); }
    }
  }

  // ---------- 存储诊断（GET /api/diagnostics、备份、维护） ----------
  function diagLooseValue(data, paths) {
    for (var i = 0; i < paths.length; i += 1) {
      var parts = paths[i].split('.');
      var node = data;
      var ok = true;
      for (var j = 0; j < parts.length; j += 1) {
        if (!node || typeof node !== 'object' || node[parts[j]] === undefined) { ok = false; break; }
        node = node[parts[j]];
      }
      if (ok && node !== undefined && node !== null) { return node; }
    }
    return undefined;
  }

  function diagIsSensitivePath(pathText) {
    return /(?:^|[._-])(?:key|keys|secret|token|password|passwd|credential|authorization)(?:$|[._-])/i.test(String(pathText || '')) ||
      /api_key|api-key|access_key|private_key/i.test(String(pathText || ''));
  }

  function diagBoolText(value) {
    if (value === true) { return '是'; }
    if (value === false) { return '否'; }
    return '—';
  }

  function diagBoolClass(value) {
    return value === true ? 'diag-value-ok' : 'diag-value-warn';
  }

  function diagText(value, opts) {
    opts = opts || {};
    if (value === undefined || value === null || value === '') { return '—'; }
    if (typeof value === 'boolean') { return diagBoolText(value); }
    if (typeof value === 'number') { return opts.bytes ? fmtBytes(value) : fmtInt(value); }
    if (Array.isArray(value)) {
      var items = value.map(function (item) {
        if (item === null || item === undefined || typeof item === 'object') { return ''; }
        return String(item);
      }).filter(function (text) { return text; });
      return items.length ? items.join('、') : '—';
    }
    if (typeof value === 'object') { return '—'; }
    return String(value);
  }

  function diagRow(label, value, opts) {
    opts = opts || {};
    var text = diagText(value, opts);
    return {
      label: label,
      text: (text === undefined || text === null || text === '') ? '—' : String(text),
      mono: !!opts.mono,
      cls: opts.cls || ''
    };
  }

  function appendDiagRows(listEl, rows) {
    if (!listEl) { return; }
    clear(listEl);
    rows.forEach(function (row) {
      if (!row) { return; }
      listEl.appendChild(el('dt', { text: row.label }));
      var dd = el('dd');
      dd.appendChild(el('span', {
        className: (row.mono ? 'mono' : '') + (row.cls ? (row.mono ? ' ' : '') + row.cls : ''),
        attrs: row.mono ? { title: row.text } : null,
        text: row.text
      }));
      listEl.appendChild(dd);
    });
  }

  function diagSummaryCard(label, value, iconName, cls, sub) {
    return el('div', { className: 'summary-card' }, [
      el('span', { className: 'summary-icon' + (cls ? ' ' + cls : '') }, [icon(iconName || 'info')]),
      el('div', { className: 'summary-body' }, [
        el('div', { className: 'summary-value', text: value }),
        el('div', { className: 'summary-label', text: label }),
        sub ? el('div', { className: 'summary-sub', text: sub }) : null
      ])
    ]);
  }

  function diagKeyLabel(key) {
    var labels = {
      subtitle_asr_api_key_set: 'ASR API Key',
      subtitle_llm_api_key_set: 'LLM API Key',
      asr_api_key_set: 'ASR API Key',
      llm_api_key_set: 'LLM API Key'
    };
    if (labels[key]) { return labels[key]; }
    return '密钥配置（' + String(key) + '）';
  }

  function diagFlattenExtra(object, prefix, out, depth) {
    if (!object || typeof object !== 'object' || depth > 3) { return; }
    Object.keys(object).forEach(function (key) {
      var value = object[key];
      var pathText = prefix + '.' + key;
      if (diagIsSensitivePath(pathText)) { return; }
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        diagFlattenExtra(value, pathText, out, depth + 1);
      } else if (Array.isArray(value) && value.some(function (item) { return item && typeof item === 'object'; })) {
        return;
      } else {
        out.push({ path: pathText, value: value });
      }
    });
  }

  function renderDiagnosticsOthers(diag) {
    if (!els.storageOtherSection || !els.storageOtherList) { return; }
    var knownTop = ['app_version', 'version', 'data_dir', 'database', 'db', 'row_counts',
      'tables', 'feature_cache', 'video_cache', 'ffmpeg', 'asr', 'active', 'active_tasks',
      'keys', 'paths', 'ok'];
    var rows = [];
    Object.keys(diag).forEach(function (key) {
      if (knownTop.indexOf(key) !== -1 || diagIsSensitivePath(key)) { return; }
      var value = diag[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        var flat = [];
        diagFlattenExtra(value, key, flat, 0);
        flat.forEach(function (item) {
          if (!diagIsSensitivePath(item.path)) { rows.push(diagRow(item.path, item.value)); }
        });
      } else if (!(Array.isArray(value) && value.some(function (item) { return item && typeof item === 'object'; }))) {
        rows.push(diagRow(key, value));
      }
    });
    if (!rows.length) {
      setHidden(els.storageOtherSection, true);
      clear(els.storageOtherList);
      return;
    }
    setHidden(els.storageOtherSection, false);
    appendDiagRows(els.storageOtherList, rows.slice(0, 80));
  }

  function renderDiagnostics(payload) {
    if (!els.storageContent) { return; }
    var diag = payload && payload.diagnostics ? payload.diagnostics : payload;
    if (!diag || typeof diag !== 'object' || Array.isArray(diag)) {
      state.diagnostics.data = null;
      setHidden(els.storageContent, true);
      setText(els.storageError, '诊断接口未返回可显示的数据。');
      setHidden(els.storageError, false);
      return;
    }
    state.diagnostics.data = diag;
    setHidden(els.storageError, true);
    setHidden(els.storageContent, false);

    var db = diag.database || diag.db || {};
    var rowCounts = diag.row_counts || diag.tables || {};
    var featureCache = diag.feature_cache || {};
    var videoCache = diag.video_cache || {};
    var ffmpeg = diag.ffmpeg || {};
    var asr = diag.asr || (diag.subtitle && diag.subtitle.asr) || {};
    var active = diag.active || diag.active_tasks || {};
    var keys = diag.keys || {};
    var appVersion = diagLooseValue(diag, ['app_version', 'version', 'app.version']);
    var dataDir = diagLooseValue(diag, ['data_dir', 'data.path', 'paths.data_dir']);
    var dbSize = diagLooseValue(db, ['total_size', 'size', 'file_size']);
    var featureEntries = diagLooseValue(featureCache, ['entries', 'count']);
    if (featureEntries === undefined) {
      featureEntries = diagLooseValue(diag, ['feature_cache_entries', 'feature_cache_count']);
    }
    var videoEntries = diagLooseValue(videoCache, ['entries', 'count']);
    var tableTotal = 0;
    Object.keys(rowCounts).forEach(function (key) {
      var num = Number(rowCounts[key]);
      if (isFinite(num)) { tableTotal += num; }
    });

    clear(els.storageSummary);
    els.storageSummary.appendChild(diagSummaryCard(
      '应用版本', appVersion ? 'v' + appVersion : '—', 'info', '',
      dataDir ? '数据目录已配置' : ''));
    els.storageSummary.appendChild(diagSummaryCard(
      '数据库总大小', dbSize !== undefined ? fmtBytes(dbSize) : '—', 'file', 'is-green',
      'WAL ' + fmtBytes(diagLooseValue(db, ['wal_size', 'wal'])) + ' · SHM ' + fmtBytes(diagLooseValue(db, ['shm_size', 'shm']))));
    els.storageSummary.appendChild(diagSummaryCard(
      '表记录合计', fmtInt(tableTotal), 'search', 'is-amber',
      '扫描 ' + fmtInt(rowCounts.scans || 0) + ' · 分组 ' + fmtInt(rowCounts.scan_groups || 0)));
    els.storageSummary.appendChild(diagSummaryCard(
      '缓存条目', fmtInt((Number(featureEntries) || 0) + (Number(videoEntries) || 0)), 'copy', '',
      '特征 ' + fmtInt(featureEntries || 0) + ' · 视频 ' + fmtInt(videoEntries || 0)));

    appendDiagRows(els.storageDbList, [
      diagRow('数据库文件', diagLooseValue(db, ['path', 'file']), { mono: true }),
      diagRow('数据库大小', diagLooseValue(db, ['size', 'db_size']), { bytes: true }),
      diagRow('WAL 大小', diagLooseValue(db, ['wal_size', 'wal']), { bytes: true }),
      diagRow('SHM 大小', diagLooseValue(db, ['shm_size', 'shm']), { bytes: true }),
      diagRow('合计大小', diagLooseValue(db, ['total_size']), { bytes: true }),
      diagRow('Schema 版本', diagLooseValue(db, ['schema_version', 'schema'])),
      diagRow('数据目录', dataDir, { mono: true })
    ]);

    var tableLabel = {
      scans: '扫描任务', scan_groups: '结果分组', scan_files: '结果文件',
      feature_cache: '特征缓存', subtitle_jobs: '字幕任务', deletion_jobs: '删除任务',
      scan_checkpoints: '扫描断点', subtitle_chunks: '字幕分块',
      hash_cache: '增量哈希缓存', media_probe_cache: '媒体探测缓存'
    };
    var tableRows = [];
    Object.keys(rowCounts).forEach(function (key) {
      var value = rowCounts[key];
      if (value && typeof value === 'object') { return; }
      tableRows.push(diagRow(tableLabel[key] || key, value));
    });
    if (!tableRows.length) { tableRows.push(diagRow('表行数', '后端未返回')); }
    appendDiagRows(els.storageTablesList, tableRows);

    var hashCache = diag.hash_cache || {};
    var mediaProbeCache = diag.media_probe_cache || {};
    appendDiagRows(els.storageCachesList, [
      diagRow('特征缓存条目', featureEntries),
      diagRow('视频指纹缓存可用', diagLooseValue(videoCache, ['available'])),
      diagRow('视频指纹缓存条目', videoEntries),
      diagRow('视频指纹缓存大小', diagLooseValue(videoCache, ['size']), { bytes: true }),
      diagRow('视频缓存路径', diagLooseValue(videoCache, ['path']), { mono: true }),
      // v0043：增量扫描缓存
      diagRow('增量哈希缓存条目', diagLooseValue(hashCache, ['entries'])),
      diagRow('增量哈希缓存覆盖大小', diagLooseValue(hashCache, ['bytes']), { bytes: true }),
      diagRow('媒体探测缓存条目', diagLooseValue(mediaProbeCache, ['entries'])),
      // v0044：预览缩略图缓存
      diagRow('预览缩略图缓存条目', diagLooseValue(diag.thumbnail_cache || {}, ['entries'])),
      diagRow('预览缩略图缓存大小', diagLooseValue(diag.thumbnail_cache || {}, ['bytes']), { bytes: true })
    ]);

    appendDiagRows(els.storageMediaList, [
      diagRow('FFmpeg 可用', diagLooseValue(ffmpeg, ['available']),
        { cls: diagBoolClass(diagLooseValue(ffmpeg, ['available'])) }),
      diagRow('FFmpeg 版本', diagLooseValue(ffmpeg, ['version']), { mono: true }),
      diagRow('FFmpeg 路径', diagLooseValue(ffmpeg, ['path']), { mono: true }),
      diagRow('硬件加速', diagLooseValue(ffmpeg, ['hwaccels', 'hwaccel'])),
      diagRow('GPU 提示', diagLooseValue(ffmpeg, ['gpu_hint'])),
      diagRow('ASR 已启用', diagLooseValue(asr, ['enabled'])),
      diagRow('ASR 已配置', diagLooseValue(asr, ['configured'])),
      diagRow('ASR 可用', diagLooseValue(asr, ['available'])),
      diagRow('ASR 原因', diagLooseValue(asr, ['reason'])),
      diagRow('ASR 权重', diagLooseValue(asr, ['weight']))
    ]);

    appendDiagRows(els.storageRuntimeList, [
      diagRow('运行中扫描', diagLooseValue(active, ['scans', 'active_scans'])),
      diagRow('已暂停扫描', diagLooseValue(active, ['paused_scans', 'paused'])),
      diagRow('运行中字幕任务', diagLooseValue(active, ['subtitle_jobs', 'subtitles']))
    ]);

    var keyRows = [];
    Object.keys(keys).forEach(function (key) {
      var value = keys[key];
      var configured;
      if (typeof value === 'boolean') { configured = value; }
      else if (typeof value === 'number') { configured = value !== 0; }
      else if (typeof value === 'string') { configured = value.length > 0; }
      else { return; }
      keyRows.push(diagRow(diagKeyLabel(key), configured ? '已配置' : '未配置',
        { cls: configured ? 'diag-value-ok' : 'diag-value-warn' }));
    });
    if (!keyRows.length) {
      keyRows.push(diagRow('密钥状态', '未配置或后端未返回'));
    }
    appendDiagRows(els.storageKeysList, keyRows);

      renderDiagnosticsOthers(diag);
    setText(els.storageStatus, '已加载 ' + fmtDateTime(new Date().toISOString()));
    if (els.settingsDiagnosticsSummary) {
      var summaryBits = [];
      if (dbSize !== undefined) { summaryBits.push('数据库 ' + fmtBytes(dbSize)); }
      if (tableTotal) { summaryBits.push('表记录 ' + fmtInt(tableTotal)); }
      summaryBits.push('FFmpeg ' + (diagLooseValue(ffmpeg, ['available']) ? '可用' : '不可用'));
      setText(els.settingsDiagnosticsSummary, summaryBits.join(' · '));
    }
  }

  async function loadDiagnostics() {
    if (!els.storageContent || state.diagnostics.loading) { return; }
    state.diagnostics.loading = true;
    setHidden(els.storageLoading, false);
    setHidden(els.storageError, true);
    try {
      var res = await api('/api/diagnostics', { timeout: 15000 });
      state.diagnostics.loaded = true;
      renderDiagnostics(res);
    } catch (err) {
      var message = errorMessage(err, '读取存储诊断失败，请稍后重试。');
      if (err && err.status === 404) {
        message = '当前后端未提供存储诊断接口；升级后端后可用。';
      } else if (err && err.status === 403) {
        message = '仅管理员可查看存储诊断。';
      }
      state.diagnostics.data = null;
      setHidden(els.storageContent, true);
      setText(els.storageStatus, '');
      setText(els.storageError, message);
      setHidden(els.storageError, false);
    } finally {
      state.diagnostics.loading = false;
      setHidden(els.storageLoading, true);
    }
  }

  function setStorageActionBusy(busy) {
    state.diagnostics.busy = !!busy;
    if (els.storageBackupBtn) { setBusy(els.storageBackupBtn, !!busy); }
    if (els.storageMaintenanceBtn) { setBusy(els.storageMaintenanceBtn, !!busy); }
    if (els.storageRefreshBtn) { els.storageRefreshBtn.disabled = !!busy; }
    setHidden(els.storageActionLoading, !busy);
  }

  function showStorageActionResult(text, isError) {
    if (!els.storageActionResult) { return; }
    clear(els.storageActionResult);
    els.storageActionResult.className = 'state-box' + (isError ? ' state-error' : '');
    els.storageActionResult.appendChild(el('p', { text: text }));
    setHidden(els.storageActionResult, false);
  }

  async function startStorageBackup() {
    if (state.diagnostics.busy) { return; }
    var ok = await confirmDialog({
      title: '备份数据库？',
      message: '将在数据目录的 backups 子目录创建 SQLite 一致性备份；备份包含扫描元数据与缓存索引，不包含 API Key。备份完成前请勿重复操作。',
      confirmText: '开始备份',
      danger: false
    });
    if (!ok) { return; }
    setStorageActionBusy(true);
    try {
      var res = await api('/api/storage/backup', { method: 'POST', body: {}, timeout: 120000 });
      var backup = (res && (res.backup || res)) || {};
      var pathText = backup.path || backup.file || backup.filename || '备份文件';
      var parts = ['备份完成：' + pathText];
      if (backup.size !== undefined && backup.size !== null) { parts.push('大小 ' + fmtBytes(backup.size)); }
      if (backup.schema_version !== undefined && backup.schema_version !== null) {
        parts.push('Schema v' + backup.schema_version);
      }
      showStorageActionResult(parts.join(' · '), false);
      toast('数据库备份完成。', 'success');
      await loadDiagnostics();
    } catch (err) {
      var message = errorMessage(err, '数据库备份失败，请稍后重试。');
      if (err && err.status === 404) { message = '当前后端未提供数据库备份接口；升级后端后可用。'; }
      else if (err && err.status === 403) { message = '仅管理员可执行数据库备份。'; }
      showStorageActionResult(message, true);
      toast(message, 'error', 6000);
    } finally {
      setStorageActionBusy(false);
    }
  }

  async function startStorageMaintenance() {
    if (state.diagnostics.busy) { return; }
    var vacuum = !!(els.storageMaintenanceVacuum && els.storageMaintenanceVacuum.checked);
    var message = '将执行 WAL 检查点（TRUNCATE）与 PRAGMA optimize' +
      (vacuum ? '，并执行 VACUUM 压缩数据库（耗时更长，需要至少 2 倍数据库大小的可用空间）' : '') +
      '。维护不会删除扫描结果，确定继续？';
    var ok = await confirmDialog({
      title: '执行存储维护？',
      message: message,
      confirmText: '开始维护',
      danger: false
    });
    if (!ok) { return; }
    setStorageActionBusy(true);
    try {
      var res = await api('/api/storage/maintenance', {
        method: 'POST',
        body: { confirm: true, vacuum: vacuum },
        timeout: 180000
      });
      var m = (res && (res.maintenance || res)) || {};
      var before = m.before || {};
      var after = m.after || {};
      var parts = ['维护完成：检查点 ' + (m.checkpoint || 'truncate')];
      if (m.optimized) { parts.push('optimize 已执行'); }
      parts.push(m.vacuumed ? 'VACUUM 已执行' : '未执行 VACUUM');
      if (before.total_size !== undefined && after.total_size !== undefined) {
        parts.push('数据库总大小 ' + fmtBytes(before.total_size) + ' → ' + fmtBytes(after.total_size));
      }
      if (m.reclaimed_bytes !== undefined && m.reclaimed_bytes !== null) {
        parts.push('释放空间 ' + fmtBytes(m.reclaimed_bytes));
      }
      showStorageActionResult(parts.join('；'), false);
      toast('存储维护完成。', 'success');
      await loadDiagnostics();
    } catch (err) {
      var message = errorMessage(err, '存储维护失败，请稍后重试。');
      if (err && err.code === 'INSUFFICIENT_SPACE') {
        message = '磁盘可用空间不足，已跳过 VACUUM；可取消勾选 VACUUM 后重试。';
      } else if (err && err.status === 404) {
        message = '当前后端未提供存储维护接口；升级后端后可用。';
      } else if (err && err.status === 403) {
        message = '仅管理员可执行存储维护。';
      }
      showStorageActionResult(message, true);
      toast(message, 'error', 6000);
    } finally {
      setStorageActionBusy(false);
    }
  }

  async function startIncrementalCacheClear() {
    if (state.diagnostics.busy) { return; }
    var ok = await confirmDialog({
      title: '清理增量扫描缓存？',
      message: '将清空“跨扫描哈希缓存”和“媒体探测缓存”。清理后下一次扫描会重新读取文件计算哈希（更慢），' +
        '但不会删除任何扫描结果、字幕或文件。确定继续？',
      confirmText: '清理缓存',
      danger: false
    });
    if (!ok) { return; }
    setStorageActionBusy(true);
    try {
      await api('/api/cache/clear', {
        method: 'POST',
        body: { confirm: true, targets: ['hash', 'media'] },
        timeout: 30000
      });
      showStorageActionResult('增量缓存已清理；下一次扫描会重新计算哈希。', false);
      toast('增量缓存已清理。', 'success');
      await loadDiagnostics();
    } catch (err) {
      var message = errorMessage(err, '清理增量缓存失败，请稍后重试。');
      if (err && err.status === 404) {
        message = '当前后端未提供缓存清理接口；升级后端后可用。';
      } else if (err && err.status === 403) {
        message = '仅管理员可清理缓存。';
      }
      showStorageActionResult(message, true);
      toast(message, 'error', 6000);
    } finally {
      setStorageActionBusy(false);
    }
  }

  function bindHistoryEvents() {
    if (els.historyRefreshBtn) { els.historyRefreshBtn.addEventListener('click', refreshTaskCenter); }
    if (els.subtitleJobsRefreshBtn) {
      els.subtitleJobsRefreshBtn.addEventListener('click', function () { loadSubtitleJobs(); });
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
    if (els.deletionJobsRefreshBtn) {
      els.deletionJobsRefreshBtn.addEventListener('click', function () { loadDeletionJobs(); });
    }
    if (els.storageRefreshBtn) {
      els.storageRefreshBtn.addEventListener('click', function () { loadDiagnostics(); });
    }
    if (els.storageBackupBtn) { els.storageBackupBtn.addEventListener('click', startStorageBackup); }
    if (els.storageMaintenanceBtn) { els.storageMaintenanceBtn.addEventListener('click', startStorageMaintenance); }
    if (els.clearHashCacheBtn) { els.clearHashCacheBtn.addEventListener('click', startIncrementalCacheClear); }
    bindTaskTabs();
  }

  // ==================== 15.5 v0045 套件回收站 / 硬链接合并 ====================
  function mergeEnabled() {
    if (state.settings && typeof state.settings.merge_enabled === 'boolean') {
      return state.settings.merge_enabled === true;
    }
    var meta = state.meta && state.meta.merge;
    return !!(meta && meta.enabled === true);
  }

  function planRecycleModeLabel(plan) {
    var mode = (plan && plan.recycle_mode) === 'package' ? 'package' : 'share';
    return RECYCLE_MODE_LABEL[mode] || '回收站';
  }

  function reasonLabel(map, reason, fallback) {
    return (map && map[reason]) || fallback || reason || '—';
  }

  async function loadTrashPanel() {
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

  async function loadMergeHistory() {
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

  function openMergeGroupDialog(group) {
    var candidates = (group.files || []).filter(function (item) {
      return !item.is_keep && !item.protected;
    });
    if (!candidates.length) {
      toast('该组没有可合并的副本（保留项/受保护文件不可合并）。', 'info');
      return;
    }
    var bytes = candidates.reduce(function (total, item) {
      return total + (Number(item.size) || 0);
    }, 0);
    var paths = candidates.slice(0, 8).map(function (item) { return item.name || item.path; });
    confirmDialog({
      title: '合并为硬链接？',
      message: '将把本组 ' + fmtInt(candidates.length) + ' 个副本（' + fmtBytes(bytes)
        + '）替换成指向保留项的硬链接：路径都还在、内容共享、可释放空间。\n\n'
        + '注意：合并后任一路径的内容修改会影响所有路径；回收站无法恢复“原来的第二个副本”，'
        + '但可在「任务中心 → 回收站与合并」里恢复为独立副本。\n\n'
        + '示例：' + paths.join('、') + (candidates.length > paths.length ? ' …' : ''),
      confirmText: '继续'
    }).then(function (ok) {
      if (!ok) { return; }
      runMergePlan(candidates.map(function (item) { return item.file_id; }));
    });
  }

  async function runMergePlan(fileIds) {
    if (!state.scan.id) { toast('请先完成一次扫描。', 'info'); return; }
    try {
      var planRes = await api('/api/scans/' + encodePath(state.scan.id) + '/merge-plan', {
        method: 'POST', body: { file_ids: fileIds }, timeout: 30000
      });
      var plan = planRes.plan || {};
      if (Number(plan.count || 0) <= 0) {
        toast('没有可合并的文件。', 'info');
        return;
      }
      var skipped = plan.skipped || [];
      var skippedText = skipped.length
        ? ('\n\n跳过 ' + fmtInt(skipped.length) + ' 个：' + skipped.slice(0, 5).map(function (item) {
            return (item.path || '') + '（' + reasonLabel(MERGE_REASON_LABEL, item.reason) + '）';
          }).join('；'))
        : '';
      var ok = await confirmDialog({
        title: '最终确认：合并为硬链接（' + fmtInt(plan.count) + ' 个）',
        message: '计划已生成：' + fmtInt(plan.count) + ' 个副本，' + fmtBytes(plan.bytes || 0)
          + '，涉及 ' + fmtInt(plan.groups_affected || 0) + ' 组。\n\n'
          + (plan.warning || '') + skippedText,
        confirmText: '执行合并',
        danger: true
      });
      if (!ok) { return; }
      var startRes = await api('/api/scans/' + encodePath(state.scan.id) + '/merge', {
        method: 'POST',
        body: { plan_id: plan.plan_id, confirm: 'MERGE' },
        timeout: 30000
      });
      var jobId = startRes.job_id;
      var job = await pollMergeJob(jobId);
      var summary = '合并完成：' + fmtInt(job.done || 0) + ' 个路径';
      if (job.skipped) { summary += '，跳过 ' + fmtInt(job.skipped); }
      if (job.failed) { summary += '，失败 ' + fmtInt(job.failed); }
      toast(summary, job.failed ? 'error' : 'success', 7000);
      await loadResults(true);
      await loadMergeHistory();
    } catch (err) {
      toast(errorMessage(err, '合并失败，请稍后重试。'), 'error', 7000);
    }
  }

  async function pollMergeJob(jobId, timeoutMs) {
    var deadline = Date.now() + (timeoutMs || 180000);
    var last = {};
    while (Date.now() < deadline) {
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/merge/'
        + encodePath(jobId), { timeout: 15000 });
      last = res.job || {};
      if (['completed', 'partial', 'failed', 'stopped'].indexOf(last.status) !== -1) {
        return last;
      }
      await new Promise(function (resolve) { setTimeout(resolve, 700); });
    }
    return last;
  }

  // ==================== 16. 操作日志 ====================
  async function loadLogs() {
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

  function bindLogsEvents() {
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
  }

  // ==================== 17. 设置页 ====================
  var SETTINGS_ERROR_FIELDS = {
    minSizeMb: { input: 'setMinSizeMb', error: 'setMinSizeError' },
    maxSizeMb: { input: 'setMaxSizeMb', error: 'setMaxSizeError' },
    includeExt: { input: 'setIncludeExt', error: 'setIncludeExtError' },
    excludeExt: { input: 'setExcludeExt', error: 'setExcludeExtError' },
    quickSample: { input: 'setQuickSampleKb', error: 'setQuickSampleError' },
    concurrency: { input: 'setConcurrency', error: 'setConcurrencyError' },
    hashRate: { input: 'setHashRate', error: 'setHashRateError' },
    maxFiles: { input: 'setMaxFiles', error: 'setMaxFilesError' },
    similarityThreshold: { input: 'setSimilarityThreshold', error: 'setSimilarityThresholdError' },
    similarityConcurrency: { input: 'setSimilarityConcurrency', error: 'setSimilarityConcurrencyError' },
    similarityMaxFile: { input: 'setSimilarityMaxMb', error: 'setSimilarityMaxFileError' },
    similarityExt: { input: 'setSimExtList', error: 'setSimilarityExtError' },
    similarityExcludeExt: { input: 'setSimilarityExcludeExt', error: 'setSimilarityExcludeExtError' },
    videoThreshold: { input: 'setVideoThreshold', error: 'setVideoThresholdError' },
    videoFrames: { input: 'setVideoSampleFrames', error: 'setVideoFramesError' },
    videoInterval: { input: 'setVideoMaxInterval', error: 'setVideoIntervalError' },
    videoConcurrency: { input: 'setVideoConcurrency', error: 'setVideoConcurrencyError' },
    videoMaxFile: { input: 'setVideoMaxMb', error: 'setVideoMaxFileError' },
    videoTimeout: { input: 'setVideoTimeout', error: 'setVideoTimeoutError' },
    videoExt: { input: 'setVideoExtList', error: 'setVideoExtError' },
    videoFfmpegPath: { input: 'setVideoFfmpegPath', error: 'setVideoFfmpegPathError' },
    videoHwaccel: { input: 'setVideoHwaccel', error: 'setVideoHwaccelError' },
    videoAsrCommand: { input: 'setVideoAsrCommand', error: 'setVideoAsrCommandError' },
    videoAsrWeight: { input: 'setVideoAsrWeight', error: 'setVideoAsrWeightError' },
    videoAsrTimeout: { input: 'setVideoAsrTimeout', error: 'setVideoAsrTimeoutError' },
    videoAsrMaxSec: { input: 'setVideoAsrMaxSec', error: 'setVideoAsrMaxSecError' },
    videoAudioWeight: { input: 'setVideoAudioWeight', error: 'setVideoAudioWeightError' },
    subtitleAsrBaseUrl: { input: 'subtitleAsrBaseUrl', error: 'subtitleAsrBaseUrlError' },
    subtitleAsrModel: { input: 'subtitleAsrModel', error: 'subtitleAsrModelError' },
    subtitleAsrTimeout: { input: 'subtitleAsrTimeout', error: 'subtitleAsrTimeoutError' },
    subtitleAsrChunk: { input: 'subtitleAsrChunk', error: 'subtitleAsrChunkError' },
    subtitleAsrMaxAudioMb: { input: 'subtitleAsrMaxAudioMb', error: 'subtitleAsrMaxAudioMbError' },
    subtitleLlmBaseUrl: { input: 'subtitleLlmBaseUrl', error: 'subtitleLlmBaseUrlError' },
    subtitleLlmModel: { input: 'subtitleLlmModel', error: 'subtitleLlmModelError' },
    subtitleTranslateTimeout: { input: 'subtitleTranslateTimeout', error: 'subtitleTranslateTimeoutError' },
    subtitleTranslateBatchSize: { input: 'subtitleTranslateBatchSize', error: 'subtitleTranslateBatchSizeError' },
    setSubtitleJobLimit: { input: 'setSubtitleJobLimit', error: 'setSubtitleJobLimitError' },
    setSubtitleConcurrency: { input: 'setSubtitleConcurrency', error: 'setSubtitleConcurrencyError' },
    // v0048：字幕自动重试 / 负载 / 目录优先级 / 音轨 / 监控
    setSubtitleAutoRetryMax: { input: 'setSubtitleAutoRetryMax', error: 'setSubtitleAutoRetryMaxError' },
    subtitleMaxLoad: { input: 'setSubtitleMaxLoad', error: 'setSubtitleMaxLoadError' },
    subtitleDirPriorities: { input: 'setSubtitleDirPriorities', error: 'setSubtitleDirPrioritiesError' },
    subtitleAudioTrack: { input: 'setSubtitleAudioTrack', error: 'setSubtitleAudioTrackError' },
    subtitleAudioTrackLanguage: { input: 'setSubtitleAudioTrackLanguage', error: 'setSubtitleAudioTrackLanguageError' },
    subtitleWatchDirs: { input: 'setSubtitleWatchDirs', error: 'setSubtitleWatchDirsError' },
    setSubtitleWatchInterval: { input: 'setSubtitleWatchInterval', error: 'setSubtitleWatchIntervalError' },
    // v0043：排除规则（增量扫描无独立校验错误）
    excludeNameRegex: { input: 'setExcludeNameRegex', error: 'setExcludeNameRegexError' },
    excludeNameGlob: { input: 'setExcludeNameGlob', error: 'setExcludeNameGlobError' },
    // v0044：删除白名单
    protectExtensions: { input: 'setProtectExtensions', error: 'setProtectExtensionsError' },
    protectDirs: { input: 'setProtectDirs', error: 'setProtectDirsError' },
    protectNameRegex: { input: 'setProtectNameRegex', error: 'setProtectNameRegexError' },
    // v0046：计划扫描 / 通知
    scheduledScanRules: { input: 'setScheduledScanRules', error: 'setScheduledScanRulesError' },
    scheduledScanRoots: { input: 'setScheduledScanRoots', error: 'setScheduledScanRootsError' },
    notifyWebhookUrl: { input: 'setNotifyWebhookUrl', error: 'setNotifyWebhookUrlError' }
  };

  function bytesToMbValue(bytes) {
    var n = Number(bytes) || 0;
    if (n <= 0) { return '0'; }
    var mb = n / 1048576;
    return String(Math.round(mb * 100) / 100);
  }

  function settingsOptionText(select, fallback) {
    if (!select || !select.options || select.selectedIndex < 0) { return fallback || '—'; }
    return select.options[select.selectedIndex].textContent || fallback || '—';
  }

  function updateSettingsSummaries() {
    if (els.settingsScanSummary) {
      var scanBits = [els.setHashAlgo ? settingsOptionText(els.setHashAlgo, 'SHA-256') : 'SHA-256'];
      if (els.setKeepMode) { scanBits.push(KEEP_MODE_LABEL[els.setKeepMode.value] || els.setKeepMode.value || '—'); }
      var rate = els.setHashRate ? Number(els.setHashRate.value) || 0 : 0;
      scanBits.push(rate > 0 ? '限速 ' + rate + ' MB/s' : '不限速');
      if (els.setIncrementalScan) {
        scanBits.push(els.setIncrementalScan.checked ? '增量缓存开' : '增量缓存关');
      }
      var extraSettingsRules = composeKeepRules('setKeep');
      if (extraSettingsRules.length) { scanBits.push('高级保留规则 ' + extraSettingsRules.length + ' 条'); }
      if (els.setProtectEnabled && els.setProtectEnabled.checked) {
        var protectCount = textToLines(els.setProtectExtensions
          ? els.setProtectExtensions.value : '').length
          + textToLines(els.setProtectDirs ? els.setProtectDirs.value : '').length
          + textToLines(els.setProtectNameRegex
            ? els.setProtectNameRegex.value : '').length;
        scanBits.push('删除白名单 ' + protectCount + ' 项配置'
          + (els.setProtectSidecarFiles && els.setProtectSidecarFiles.checked
            ? '（含视频附属文件）' : ''));
      } else {
        scanBits.push('删除白名单关闭');
      }
      scanBits.push(els.setDeleteRecycleMode
        ? (RECYCLE_MODE_LABEL[els.setDeleteRecycleMode.value] || '回收站')
        : '回收站');
      scanBits.push(els.setMergeEnabled && els.setMergeEnabled.checked
        ? '硬链接合并开' : '硬链接合并关');
      setText(els.settingsScanSummary, scanBits.join(' · '));
    }
    if (els.settingsSimilaritySummary) {
      var simBits = [els.setScanMode && els.setScanMode.value === 'similar' ? '默认相似模式' : '默认精准模式'];
      if (els.setSimilarityThreshold) { simBits.push('阈值 ' + els.setSimilarityThreshold.value + '%'); }
      if (els.setSimilarityConcurrency) { simBits.push('并发 ' + els.setSimilarityConcurrency.value); }
      setText(els.settingsSimilaritySummary, simBits.join(' · '));
    }
    if (els.settingsVideoSummary) {
      var vcap = state.video || videoCapability();
      var videoBits = [els.setVideoEnabled && els.setVideoEnabled.checked ? '默认开启' : '默认关闭'];
      videoBits.push(vcap.available ? 'FFmpeg ' + (vcap.ffmpegVersion || '可用') : 'FFmpeg 不可用');
      if (vcap.available) {
        if (vcap.configuredPath) { videoBits.push('外部 FFmpeg'); }
        var videoEffective = videoHwaccelEffectiveFor(vcap.hwaccelMode || 'auto', vcap);
        videoBits.push('硬解 ' + videoHwaccelLabel(videoEffective.label));
        if (vcap.hwaccelFallback) { videoBits.push('显式模式不支持，回退软件'); }
      }
      var v2Bits = [];
      if (els.setVideoCenterCrop && els.setVideoCenterCrop.checked) { v2Bits.push('边缘裁剪'); }
      if (els.setVideoOffsetAlign && els.setVideoOffsetAlign.checked) { v2Bits.push('时间轴对齐'); }
      if (els.setVideoAudioSignature && els.setVideoAudioSignature.checked) {
        var audioWeightValue = Number(els.setVideoAudioWeight
          ? els.setVideoAudioWeight.value : 0);
        v2Bits.push('音频辅助 ' + (isFinite(audioWeightValue)
          ? Math.round(audioWeightValue * 100) : 0) + '%');
      }
      if (v2Bits.length) { videoBits.push(v2Bits.join(' / ')); }
      setText(els.settingsVideoSummary, videoBits.join(' · '));
    }
    if (els.settingsSubtitleSummary) {
      var sub = state.subtitle || subtitleCapability();
      var subBits = [];
      if (!sub.supported) { subBits.push('后端未提供'); }
      else {
        subBits.push(sub.enabled ? '已启用' : '默认关闭');
        if (sub.enabled) { subBits.push(sub.asrKeySet ? 'ASR Key 已配置' : 'ASR Key 未配置'); }
      }
      // v0048：自动重试 / 负载阈值 / 目录监控
      subBits.push(els.setSubtitleAutoRetry && els.setSubtitleAutoRetry.checked
        ? '自动重试 ≤' + (els.setSubtitleAutoRetryMax
          ? els.setSubtitleAutoRetryMax.value : '0') + ' 次'
        : '自动重试关');
      var subLoad = Number(els.setSubtitleMaxLoad ? els.setSubtitleMaxLoad.value : 0);
      if (isFinite(subLoad) && subLoad > 0) {
        subBits.push('负载阈值 ' + subLoad);
      }
      if (els.setSubtitleWatchEnabled && els.setSubtitleWatchEnabled.checked) {
        subBits.push('目录监控开（' + textToLines(els.setSubtitleWatchDirs
          ? els.setSubtitleWatchDirs.value : '').length + ' 个目录）');
      }
      setText(els.settingsSubtitleSummary, subBits.join(' · '));
    }
    if (els.settingsAutomationSummary) {
      var autoBits = [];
      autoBits.push(els.setScheduledScanEnabled && els.setScheduledScanEnabled.checked
        ? '计划扫描开（' + textToLines(els.setScheduledScanRules
          ? els.setScheduledScanRules.value : '').length + ' 条规则）'
        : '计划扫描关');
      if (els.setNotifyEnabled && els.setNotifyEnabled.checked) {
        var channels = [];
        if (els.setNotifyChannelDsm && els.setNotifyChannelDsm.checked) { channels.push('DSM'); }
        if (els.setNotifyChannelWebhook && els.setNotifyChannelWebhook.checked) { channels.push('Webhook'); }
        if (els.setNotifyChannelTelegram && els.setNotifyChannelTelegram.checked) { channels.push('Telegram'); }
        autoBits.push('通知开（' + (channels.length ? channels.join('/') : '未选渠道') + '）');
      } else {
        autoBits.push('通知关');
      }
      setText(els.settingsAutomationSummary, autoBits.join(' · '));
    }
    if (els.settingsStorageSummary) {
      var storageBits = [els.setRecycleDefault && els.setRecycleDefault.checked ? '默认回收站' : '默认永久删除'];
      storageBits.push(els.setAllowCtime && els.setAllowCtime.checked ? '允许 ctime 降级' : 'ctime 降级关闭');
      if (state.meta && state.meta.app_version) { storageBits.push('v' + state.meta.app_version); }
      setText(els.settingsStorageSummary, storageBits.join(' · '));
    }
    if (els.settingsOverview) {
      setText(els.settingsOverview,
        '共 6 组设置：扫描 / 自动化 / 相似 / 视频 / 云端字幕 / 存储 · 修改后点击底部“保存设置”生效');
    }
  }

  // ==================== v0046 计划扫描 / 通知 ====================
  function fillAutomationSettings(settings) {
    settings = settings || {};
    if (els.setScheduledScanEnabled) {
      els.setScheduledScanEnabled.checked = settings.scheduled_scan_enabled === true;
    }
    if (els.setScheduledScanRules) {
      els.setScheduledScanRules.value = linesToText(settings.scheduled_scan_rules);
    }
    if (els.setScheduledScanRoots) {
      els.setScheduledScanRoots.value = linesToText(settings.scheduled_scan_roots);
    }
    if (els.setScheduledScanMaxLoad) {
      var load = Number(settings.scheduled_scan_max_load);
      els.setScheduledScanMaxLoad.value =
        String(isFinite(load) && load > 0 ? load : 0);
    }
    if (els.setNotifyEnabled) { els.setNotifyEnabled.checked = settings.notify_enabled === true; }
    var channels = Array.isArray(settings.notify_channels)
      ? settings.notify_channels : DEFAULT_SETTINGS.notify_channels;
    if (els.setNotifyChannelDsm) { els.setNotifyChannelDsm.checked = channels.indexOf('dsm') !== -1; }
    if (els.setNotifyChannelWebhook) { els.setNotifyChannelWebhook.checked = channels.indexOf('webhook') !== -1; }
    if (els.setNotifyChannelTelegram) { els.setNotifyChannelTelegram.checked = channels.indexOf('telegram') !== -1; }
    var events = Array.isArray(settings.notify_events)
      ? settings.notify_events : DEFAULT_SETTINGS.notify_events;
    if (els.setNotifyEventScanComplete) { els.setNotifyEventScanComplete.checked = events.indexOf('scan_complete') !== -1; }
    if (els.setNotifyEventScanFailed) { els.setNotifyEventScanFailed.checked = events.indexOf('scan_failed') !== -1; }
    if (els.setNotifyEventSubtitleBatchDone) { els.setNotifyEventSubtitleBatchDone.checked = events.indexOf('subtitle_batch_done') !== -1; }
    if (els.setNotifyEventFailureBurst) { els.setNotifyEventFailureBurst.checked = events.indexOf('failure_burst') !== -1; }
    if (els.setNotifyEventFreedThreshold) { els.setNotifyEventFreedThreshold.checked = events.indexOf('freed_threshold') !== -1; }
    if (els.setNotifyWebhookUrl) { els.setNotifyWebhookUrl.value = settings.notify_webhook_url || ''; }
    // 安全：Bot Token 永不回填明文
    if (els.setNotifyTelegramToken) { els.setNotifyTelegramToken.value = ''; }
    if (els.setNotifyTelegramChatId) { els.setNotifyTelegramChatId.value = settings.notify_telegram_chat_id || ''; }
    if (els.setNotifyFailThreshold) {
      els.setNotifyFailThreshold.value = String(parseInt(settings.notify_fail_threshold, 10) || 20);
    }
    if (els.setNotifyFreedGb) {
      var bytes = Number(settings.notify_freed_bytes_threshold) || 0;
      els.setNotifyFreedGb.value = String(Math.round(bytes / 1073741824 * 100) / 100);
    }
    if (els.setNotifyTimeout) {
      els.setNotifyTimeout.value = String(parseInt(settings.notify_timeout_sec, 10) || 5);
    }
    updateSettingsSummaries();
  }

  function automationRuleValid(rule) {
    return /^daily\s+\d{1,2}:\d{2}$/i.test(rule)
      || /^weekly\s+[a-z0-9,\s]+\s+\d{1,2}:\d{2}$/i.test(rule)
      || /^interval\s+\d{1,5}$/i.test(rule);
  }

  function readAutomationSettings(settings, errors) {
    settings.scheduled_scan_enabled = !!(els.setScheduledScanEnabled
      && els.setScheduledScanEnabled.checked);
    var rules = textToLines(els.setScheduledScanRules ? els.setScheduledScanRules.value : '');
    if (rules.length > 10) {
      errors.scheduledScanRules = '计划扫描规则最多 10 条。';
    } else {
      var badRule = '';
      rules.forEach(function (rule) {
        if (!badRule && !automationRuleValid(rule)) { badRule = rule; }
      });
      if (badRule) {
        errors.scheduledScanRules = '无法解析规则：' + badRule
          + '（示例：daily 03:30 / weekly mon,wed 04:00 / interval 360）';
      } else {
        settings.scheduled_scan_rules = rules;
      }
    }
    var roots = textToLines(els.setScheduledScanRoots ? els.setScheduledScanRoots.value : '');
    var badRoot = '';
    roots.forEach(function (item) {
      if (!badRoot && !isAbsolutePath(item)) { badRoot = item; }
    });
    if (badRoot) {
      errors.scheduledScanRoots = '计划扫描目录必须是绝对路径：' + badRoot;
    } else {
      settings.scheduled_scan_roots = roots;
    }
    var maxLoad = parseFloat(els.setScheduledScanMaxLoad
      ? els.setScheduledScanMaxLoad.value : '0');
    if (!isFinite(maxLoad) || maxLoad < 0 || maxLoad > 256) {
      errors.scheduledScanMaxLoad = '负载阈值需在 0..256 之间（0 = 不检查）。';
    } else {
      settings.scheduled_scan_max_load = Math.round(maxLoad * 100) / 100;
    }
    settings.notify_enabled = !!(els.setNotifyEnabled && els.setNotifyEnabled.checked);
    var channels = [];
    if (els.setNotifyChannelDsm && els.setNotifyChannelDsm.checked) { channels.push('dsm'); }
    if (els.setNotifyChannelWebhook && els.setNotifyChannelWebhook.checked) { channels.push('webhook'); }
    if (els.setNotifyChannelTelegram && els.setNotifyChannelTelegram.checked) { channels.push('telegram'); }
    settings.notify_channels = channels;
    var events = [];
    if (els.setNotifyEventScanComplete && els.setNotifyEventScanComplete.checked) { events.push('scan_complete'); }
    if (els.setNotifyEventScanFailed && els.setNotifyEventScanFailed.checked) { events.push('scan_failed'); }
    if (els.setNotifyEventSubtitleBatchDone && els.setNotifyEventSubtitleBatchDone.checked) { events.push('subtitle_batch_done'); }
    if (els.setNotifyEventFailureBurst && els.setNotifyEventFailureBurst.checked) { events.push('failure_burst'); }
    if (els.setNotifyEventFreedThreshold && els.setNotifyEventFreedThreshold.checked) { events.push('freed_threshold'); }
    settings.notify_events = events;
    var webhook = els.setNotifyWebhookUrl ? els.setNotifyWebhookUrl.value.trim() : '';
    if (webhook && !/^https?:\/\/[^\/\s]+/i.test(webhook)) {
      errors.notifyWebhookUrl = 'Webhook 必须是 http(s) 地址。';
    } else {
      settings.notify_webhook_url = webhook;
    }
    if (els.setNotifyTelegramToken) {
      settings.notify_telegram_bot_token = els.setNotifyTelegramToken.value.trim();
    }
    if (els.setNotifyTelegramChatId) {
      settings.notify_telegram_chat_id = els.setNotifyTelegramChatId.value.trim();
    }
    var failThreshold = parseInt(els.setNotifyFailThreshold
      ? els.setNotifyFailThreshold.value : '', 10);
    if (!isFinite(failThreshold) || failThreshold < 1 || failThreshold > 100000) {
      errors.notifyFailThreshold = '批量失败阈值需在 1..100000 之间。';
    } else {
      settings.notify_fail_threshold = failThreshold;
    }
    var freedGb = parseFloat(els.setNotifyFreedGb ? els.setNotifyFreedGb.value : '0');
    if (!isFinite(freedGb) || freedGb < 0) {
      errors.notifyFreedGb = '释放空间阈值必须是非负数字（GB）。';
    } else {
      settings.notify_freed_bytes_threshold = Math.round(freedGb * 1073741824);
    }
    var timeout = parseInt(els.setNotifyTimeout ? els.setNotifyTimeout.value : '', 10);
    if (!isFinite(timeout) || timeout < 2 || timeout > 30) {
      errors.notifyTimeoutSec = '通知发送超时需在 2..30 秒之间。';
    } else {
      settings.notify_timeout_sec = timeout;
    }
  }

  async function loadAutomationStatus() {
    if (els.schedulerStatusText) {
      try {
        var res = await api('/api/scheduler', { timeout: 10000 });
        var sched = (res && res.scheduler) || {};
        var bits = [];
        if (!sched.enabled) {
          bits.push('计划扫描未启用');
        } else if (Array.isArray(sched.rules) && sched.rules.length) {
          bits.push('下次：' + sched.rules.map(function (item) {
            return item.rule + ' → ' + (item.next_run || '—');
          }).join('；'));
        } else {
          bits.push('计划扫描已启用但未配置规则');
        }
        if (sched.last_run) {
          bits.push('上次 ' + sched.last_run + '（' + (sched.last_status || '—') + '）');
        }
        if (sched.current_load !== null && sched.current_load !== undefined) {
          bits.push('当前 1 分钟负载 ' + sched.current_load);
        }
        setText(els.schedulerStatusText, bits.join(' · '));
      } catch (err) {
        setText(els.schedulerStatusText, '计划扫描状态不可用：' + errorMessage(err, '加载失败'));
      }
    }
    if (els.notifyChannelsHint) {
      try {
        var res2 = await api('/api/notifications', { timeout: 10000 });
        var notify = (res2 && res2.notify) || {};
        var labels = (notify.channels || []).map(function (item) {
          var stateText = item.available ? (item.configured ? '可用' : '未配置') : '不可用';
          return item.label + '：' + stateText;
        });
        setText(els.notifyChannelsHint, labels.join('；') || '通知渠道状态未知');
      } catch (err2) {
        setText(els.notifyChannelsHint, '通知渠道状态不可用：' + errorMessage(err2, '加载失败'));
      }
    }
  }

  async function testNotifyChannel(channel) {
    if (els.notifyTestResult) {
      setText(els.notifyTestResult, '正在测试 ' + (NOTIFY_CHANNEL_LABEL[channel] || channel) + '…');
    }
    try {
      var res = await api('/api/notifications/test', {
        method: 'POST', body: { channel: channel }, timeout: 20000
      });
      var result = (res && res.result) || {};
      setText(els.notifyTestResult,
        (NOTIFY_CHANNEL_LABEL[channel] || channel) + '：'
        + (result.ok ? '发送成功' : ('失败 - ' + (result.detail || '未知错误'))));
    } catch (err) {
      setText(els.notifyTestResult,
        (NOTIFY_CHANNEL_LABEL[channel] || channel) + '：测试失败 - '
        + errorMessage(err, '请求失败'));
    }
  }

  function settingsStatusItem(label, value, cls) {
    return el('div', { className: 'settings-status-item' }, [
      el('span', { className: 'settings-status-label', text: label }),
      el('span', { className: 'settings-status-value' + (cls ? ' ' + cls : ''), text: value })
    ]);
  }

  function renderSettingsStatusSummary() {
    if (!els.settingsStatusSummary) { return; }
    clear(els.settingsStatusSummary);
    var meta = state.meta || {};
    var vcap = state.video || videoCapability();
    var platformBits = [];
    if (meta.platform) { platformBits.push(meta.platform); }
    if (meta.dsm_version) { platformBits.push('DSM ' + meta.dsm_version); }
    if (meta.python_version) { platformBits.push('Python ' + meta.python_version); }
    var timeBits = [];
    timeBits.push(meta.creation_time_supported === true ? '创建时间可用' : '创建时间不可用');
    timeBits.push(meta.statx_supported === true ? 'statx 可用' : 'statx 不可用');
    var ffmpegText = '不可用';
    if (vcap.available) {
      var ffmpegInfo = videoFfmpegInfo(vcap);
      var supportedHw = videoHwaccelSupported(vcap);
      ffmpegText = '可用' + (ffmpegInfo.version ? ' · ' + ffmpegInfo.version : '');
      if (ffmpegInfo.source) { ffmpegText += ' · 来源 ' + videoFfmpegSourceLabel(ffmpegInfo.source); }
      if (supportedHw.length) {
        ffmpegText += ' · 硬解 ' + supportedHw.map(function (name) { return videoHwaccelLabel(name); }).join('/');
      }
    }
    var asrText = '未启用';
    var metaAsr = meta.video && meta.video.asr;
    if (metaAsr) {
      if (metaAsr.enabled === false) { asrText = '未启用（默认关闭）'; }
      else if (metaAsr.available === true) { asrText = '可用（本地命令）'; }
      else if (metaAsr.configured === true) { asrText = '已配置但不可用'; }
      else { asrText = '未配置'; }
    } else {
      asrText = '后端未提供';
    }
    appendChildren(els.settingsStatusSummary, [
      settingsStatusItem('应用版本', meta.app_version ? 'v' + meta.app_version : '—'),
      settingsStatusItem('平台 / DSM / Python', platformBits.length ? platformBits.join(' · ') : '—'),
      settingsStatusItem('创建时间支持', timeBits.join(' · ')),
      settingsStatusItem('FFmpeg', ffmpegText, vcap.available ? 'is-ok' : 'is-warn'),
      settingsStatusItem('ASR 语音辅助', asrText, metaAsr && metaAsr.available === true ? 'is-ok' : ''),
      settingsStatusItem('元数据存储', 'SQLite 元数据 + 视频指纹缓存（由后端管理）')
    ]);
  }

  function fillSettingsForm(settings) {
    if (!settings) { return; }
    els.setHashAlgo.value = settings.hash_algo === 'md5' ? 'md5' : 'sha256';
    els.setKeepMode.value = ['latest', 'earliest', 'pinned'].indexOf(settings.keep_mode) !== -1 ? settings.keep_mode : 'latest';
    els.setPinnedPath.value = settings.pinned_path || '';
    els.setMinSizeMb.value = bytesToMbValue(settings.min_size_bytes);
    els.setMaxSizeMb.value = bytesToMbValue(settings.max_size_bytes);
    els.setIncludeExt.value = extListToText(settings.include_extensions);
    els.setExcludeExt.value = extListToText(settings.exclude_extensions);
    els.setIgnoreCase.checked = settings.ignore_case_ext !== false;
    els.setSkipEmpty.checked = settings.skip_empty !== false;
    els.setSkipHidden.checked = settings.skip_hidden !== false;
    els.setSkipSymlinks.checked = true;
    els.setQuickSampleKb.value = settings.quick_sample_kb === undefined || settings.quick_sample_kb === null ? 64 : settings.quick_sample_kb;
    els.setConcurrency.value = settings.concurrency === undefined || settings.concurrency === null ? 2 : settings.concurrency;
    els.setHashRate.value = settings.hash_rate_mbps === undefined || settings.hash_rate_mbps === null ? 0 : settings.hash_rate_mbps;
    els.setMaxFiles.value = settings.max_files === undefined || settings.max_files === null ? 500000 : settings.max_files;
    els.setRecycleDefault.checked = settings.recycle_default !== false;
    els.setCreateRecycleDir.checked = settings.create_recycle_dir === true;
    els.setAllowCtime.checked = settings.allow_ctime_fallback === true;
    // v0043：增量扫描 / 保留规则链 / 排除规则
    if (els.setIncrementalScan) { els.setIncrementalScan.checked = settings.incremental_scan !== false; }
    if (els.setExcludeNameRegex) { els.setExcludeNameRegex.value = linesToText(settings.exclude_name_regex); }
    if (els.setExcludeNameGlob) { els.setExcludeNameGlob.value = linesToText(settings.exclude_name_glob); }
    if (els.setIgnoreFileEnabled) { els.setIgnoreFileEnabled.checked = settings.ignore_file_enabled !== false; }
    if (els.setMediaSidecarMode) {
      els.setMediaSidecarMode.value = settings.media_sidecar_mode === 'include' ? 'include' : 'skip';
    }
    fillKeepRuleControls('setKeep', settings.keep_rules);
    // v0044：删除白名单
    if (els.setProtectEnabled) { els.setProtectEnabled.checked = settings.protect_enabled !== false; }
    if (els.setProtectExtensions) {
      els.setProtectExtensions.value = extListToText(settings.protect_extensions);
    }
    if (els.setProtectDirs) { els.setProtectDirs.value = linesToText(settings.protect_dirs); }
    if (els.setProtectNameRegex) {
      els.setProtectNameRegex.value = linesToText(settings.protect_name_regex);
    }
    if (els.setProtectSidecarFiles) {
      els.setProtectSidecarFiles.checked = settings.protect_sidecar_files !== false;
    }
    // v0045：硬链接合并 / 删除去向
    if (els.setMergeEnabled) { els.setMergeEnabled.checked = settings.merge_enabled === true; }
    if (els.setDeleteRecycleMode) {
      els.setDeleteRecycleMode.value =
        settings.delete_recycle_mode === 'package' ? 'package' : 'share';
    }
    // v0046：计划扫描 / 通知
    fillAutomationSettings(settings);
    if (els.setScanMode) { els.setScanMode.value = settings.scan_mode === 'similar' ? 'similar' : 'exact'; }
    if (els.setSimilarityThreshold) {
      var threshold = parseInt(settings.similarity_threshold, 10);
      els.setSimilarityThreshold.value = String(clamp(isFinite(threshold) ? threshold : 85, 0, 100));
    }
    if (els.setSimilarityConcurrency) {
      var simConc = parseInt(settings.similarity_concurrency, 10);
      els.setSimilarityConcurrency.value = String(clamp(isFinite(simConc) ? simConc : 2, 1, 4));
    }
    if (els.setSimilarityMaxMb) {
      var simMaxMb = parseInt(settings.similarity_max_file_mb, 10);
      els.setSimilarityMaxMb.value = String(clamp(isFinite(simMaxMb) ? simMaxMb : 32, 1, 1024));
    }
    if (els.setSimilarityCache) { els.setSimilarityCache.checked = settings.similarity_cache !== false; }
    if (els.setSimilarityAllowBinary) { els.setSimilarityAllowBinary.checked = settings.similarity_allow_binary === true; }
    if (els.setSimExtList) {
      var simMeta = state.similarity || similarityCapability();
      var selectedExt = (Array.isArray(settings.similarity_extensions) && settings.similarity_extensions.length)
        ? settings.similarity_extensions : simMeta.extensions;
      renderExtChips(els.setSimExtList, simMeta.extensions, selectedExt, null);
    }
    if (els.setSimilarityExcludeExt) { els.setSimilarityExcludeExt.value = extListToText(settings.similarity_exclude_extensions); }
    fillVideoSettingsForm(settings);
    fillSubtitleSettingsForm(settings);
    clearSettingsErrors();
    updateSettingsSummaries();
    renderSettingsStatusSummary();
  }

  function clearSettingsErrors() {
    Object.keys(SETTINGS_ERROR_FIELDS).forEach(function (key) {
      var map = SETTINGS_ERROR_FIELDS[key];
      var input = byId(map.input);
      var errEl = byId(map.error);
      if (input) {
        input.removeAttribute('aria-invalid');
        var field = input.closest ? input.closest('.field') : null;
        if (field) { field.classList.remove('has-error'); }
      }
      if (errEl) { setText(errEl, ''); hide(errEl); }
    });
    if (els.settingsError) { setText(els.settingsError, ''); hide(els.settingsError); }
  }

  async function loadSettingsPage() {
    setHidden(els.settingsLoading, false);
    setHidden(els.settingsLoadError, true);
    try {
      var res = await api('/api/settings', { timeout: 15000 });
      var rawSettings = res && res.settings ? res.settings : null;
      if (rawSettings) {
        state.settings = Object.assign({}, DEFAULT_SETTINGS, rawSettings);
        if (res.defaults) { state.defaults = Object.assign({}, DEFAULT_SETTINGS, res.defaults); }
      }
      state.settingsLoaded = true;
      state.similarity = similarityCapability();
      state.video = videoCapability();
      updateModeAvailability();
      updateVideoAvailabilityUi();
      updateSubtitleFromRaw(rawSettings);
      var simSupported = !(state.meta && state.similarity && state.similarity.supported === false);
      if (els.settingsSimilarityDetails) { setHidden(els.settingsSimilarityDetails, !simSupported); }
      fillSettingsForm(state.settings || DEFAULT_SETTINGS);
      setHidden(els.settingsLoading, true);
      loadAutomationStatus();
      loadSubtitleWatchStatus();
    } catch (err) {
      setHidden(els.settingsLoading, true);
      setText(els.settingsLoadError, errorMessage(err, '加载设置失败，请稍后重试。'));
      setHidden(els.settingsLoadError, false);
      if (state.settings) { fillSettingsForm(state.settings); }
    }
    // v0038：存储诊断已从任务中心移到设置页，首次进入时懒加载
    if (!state.diagnostics.loaded && !state.diagnostics.loading) { loadDiagnostics(); }
  }

  function readSettingsForm() {
    var errors = {};
    var settings = {};
    settings.hash_algo = els.setHashAlgo.value === 'md5' ? 'md5' : 'sha256';
    settings.keep_mode = els.setKeepMode.value === 'earliest' ? 'earliest' : (els.setKeepMode.value === 'pinned' ? 'pinned' : 'latest');
    settings.pinned_path = els.setPinnedPath.value.trim();
    settings.skip_empty = !!els.setSkipEmpty.checked;
    settings.skip_hidden = !!els.setSkipHidden.checked;
    settings.skip_symlinks = true;
    settings.ignore_case_ext = !!els.setIgnoreCase.checked;
    settings.recycle_default = !!els.setRecycleDefault.checked;
    settings.create_recycle_dir = !!els.setCreateRecycleDir.checked;
    settings.allow_ctime_fallback = !!els.setAllowCtime.checked;

    var include = normalizeExtList(els.setIncludeExt.value);
    if (include.error) { errors.includeExt = include.error; } else { settings.include_extensions = include.list; }
    var exclude = normalizeExtList(els.setExcludeExt.value);
    if (exclude.error) { errors.excludeExt = exclude.error; } else { settings.exclude_extensions = exclude.list; }

    function readMb(input, key, label) {
      var raw = String(input.value === null || input.value === undefined ? '' : input.value).trim();
      if (!raw) { return 0; }
      if (!/^\d+(\.\d+)?$/.test(raw)) { errors[key] = label + '请输入有效的非负数字，可带小数。'; return null; }
      var num = parseFloat(raw);
      if (!isFinite(num) || num < 0) { errors[key] = label + '请输入有效的非负数字。'; return null; }
      return Math.round(num * 1048576);
    }
    var minBytes = readMb(els.setMinSizeMb, 'minSizeMb', '最小文件大小');
    var maxBytes = readMb(els.setMaxSizeMb, 'maxSizeMb', '最大文件大小');
    if (minBytes !== null) { settings.min_size_bytes = minBytes; }
    if (maxBytes !== null) { settings.max_size_bytes = maxBytes; }
    if (minBytes !== null && maxBytes !== null && maxBytes > 0 && minBytes > maxBytes) {
      errors.maxSizeMb = '最大值必须大于或等于最小值。';
    }

    var quick = readIntOption(els.setQuickSampleKb, 4, 1024, false, 0, '快速指纹采样（KB）');
    if (quick.error) { errors.quickSample = quick.error; } else { settings.quick_sample_kb = quick.value; }
    var concurrency = readIntOption(els.setConcurrency, 1, 8, false, 0, '哈希并发数');
    if (concurrency.error) { errors.concurrency = concurrency.error; } else { settings.concurrency = concurrency.value; }
    var rate = readIntOption(els.setHashRate, 0, 2000, true, 0, '全量哈希限速（MB/s）');
    if (rate.error) { errors.hashRate = rate.error; } else { settings.hash_rate_mbps = rate.value; }
    var maxFiles = readIntOption(els.setMaxFiles, 1, 10000000, false, 0, '最大扫描文件数');
    if (maxFiles.error) { errors.maxFiles = maxFiles.error; } else { settings.max_files = maxFiles.value; }

    if (settings.keep_mode === 'pinned' && !settings.pinned_path) {
      errors.pinned = '选择“指定路径”策略时请填写保留路径。';
    }

    var simSupported = !(state.meta && state.similarity && state.similarity.supported === false);
    if (simSupported) {
      settings.scan_mode = els.setScanMode && els.setScanMode.value === 'similar' ? 'similar' : 'exact';
      var simThreshold = readIntOption(els.setSimilarityThreshold, 0, 100, false, 0, '相似度阈值');
      if (simThreshold.error) { errors.similarityThreshold = simThreshold.error; } else { settings.similarity_threshold = simThreshold.value; }
      var simConc = readIntOption(els.setSimilarityConcurrency, 1, 4, false, 0, '相似特征并发数');
      if (simConc.error) { errors.similarityConcurrency = simConc.error; } else { settings.similarity_concurrency = simConc.value; }
      var simMax = readIntOption(els.setSimilarityMaxMb, 1, 1024, false, 0, '单文件相似上限（MB）');
      if (simMax.error) { errors.similarityMaxFile = simMax.error; } else { settings.similarity_max_file_mb = simMax.value; }
      var simExt = readExtChips(els.setSimExtList);
      if (!simExt.length) { errors.similarityExt = '请至少选择一种允许相似计算的默认文件类型。'; }
      else { settings.similarity_extensions = simExt; }
      var simExclude = normalizeExtList(els.setSimilarityExcludeExt.value);
      if (simExclude.error) { errors.similarityExcludeExt = simExclude.error; }
      else { settings.similarity_exclude_extensions = simExclude.list; }
      settings.similarity_cache = !!(els.setSimilarityCache && els.setSimilarityCache.checked);
      settings.similarity_allow_binary = !!(els.setSimilarityAllowBinary && els.setSimilarityAllowBinary.checked);
    }
    readVideoSettings(settings, errors);
    readSubtitleSettings(settings, errors);
    // v0043：增量扫描 / 保留规则链 / 排除规则
    settings.incremental_scan = els.setIncrementalScan ? !!els.setIncrementalScan.checked : true;
    settings.keep_rules = composeKeepRules('setKeep');
    var setRegexList = textToLines(els.setExcludeNameRegex ? els.setExcludeNameRegex.value : '');
    var setGlobList = textToLines(els.setExcludeNameGlob ? els.setExcludeNameGlob.value : '');
    if (setRegexList.length > 50) {
      errors.excludeNameRegex = '文件名正则最多 50 条。';
    } else {
      settings.exclude_name_regex = setRegexList;
    }
    if (setGlobList.length > 50) {
      errors.excludeNameGlob = '文件名通配符最多 50 条。';
    } else {
      settings.exclude_name_glob = setGlobList;
    }
    settings.ignore_file_enabled = els.setIgnoreFileEnabled ? !!els.setIgnoreFileEnabled.checked : true;
    settings.media_sidecar_mode = els.setMediaSidecarMode && els.setMediaSidecarMode.value === 'include'
      ? 'include' : 'skip';
    // v0044：删除白名单
    settings.protect_enabled = els.setProtectEnabled ? !!els.setProtectEnabled.checked : true;
    var protectExt = normalizeExtList(els.setProtectExtensions ? els.setProtectExtensions.value : '');
    if (protectExt.error) {
      errors.protectExtensions = protectExt.error;
    } else {
      settings.protect_extensions = protectExt.list;
    }
    var protectDirs = textToLines(els.setProtectDirs ? els.setProtectDirs.value : '');
    var badDir = '';
    protectDirs.forEach(function (item) {
      if (!badDir && !isAbsolutePath(item)) { badDir = item; }
    });
    if (badDir) {
      errors.protectDirs = '受保护目录必须是绝对路径：' + badDir;
    } else {
      settings.protect_dirs = protectDirs;
    }
    var protectRegex = textToLines(
      els.setProtectNameRegex ? els.setProtectNameRegex.value : '');
    if (protectRegex.length > 50) {
      errors.protectNameRegex = '受保护文件名正则最多 50 条。';
    } else {
      settings.protect_name_regex = protectRegex;
    }
    settings.protect_sidecar_files = els.setProtectSidecarFiles
      ? !!els.setProtectSidecarFiles.checked : true;
    // v0045：硬链接合并 / 删除去向
    settings.merge_enabled = els.setMergeEnabled
      ? !!els.setMergeEnabled.checked : false;
    settings.delete_recycle_mode = (els.setDeleteRecycleMode
      && els.setDeleteRecycleMode.value === 'package') ? 'package' : 'share';
    // v0046：计划扫描 / 通知
    readAutomationSettings(settings, errors);
    return { settings: settings, errors: errors, valid: Object.keys(errors).length === 0 };
  }

  function showSettingsErrors(errors) {
    clearSettingsErrors();
    var keys = Object.keys(errors || {});
    if (!keys.length) { return true; }
    keys.forEach(function (key) {
      var map = SETTINGS_ERROR_FIELDS[key];
      if (!map) { return; }
      var input = byId(map.input);
      var errEl = byId(map.error);
      if (input) {
        input.setAttribute('aria-invalid', 'true');
        var field = input.closest ? input.closest('.field') : null;
        if (field) { field.classList.add('has-error'); }
      }
      if (errEl) { setText(errEl, errors[key]); show(errEl); }
    });
    setText(els.settingsError, '请检查设置项：' + keys.map(function (key) { return errors[key]; }).join('；'));
    show(els.settingsError);
    return false;
  }

  /** 保存设置后重新拉取 /api/meta，刷新 FFmpeg 解析结果与硬解能力。 */
  async function refreshMetaCapabilities() {
    try {
      var meta = await api('/api/meta', { timeout: 10000 });
      if (meta) {
        state.meta = meta;
        applyMeta(meta);
      }
    } catch (e) {
      /* 能力刷新失败不影响已成功保存的设置；下次进入设置页会再次加载。 */
    }
  }

  async function saveSettings() {
    if (state.settingsSaving) { return; }
    var parsed = readSettingsForm();
    if (!showSettingsErrors(parsed.errors)) {
      toast('设置项有误，请检查红色提示。', 'error');
      return;
    }
    state.settingsSaving = true;
    setBusy(els.saveSettingsBtn, true, '正在保存…');
    try {
      var res = await api('/api/settings', { method: 'PUT', body: { settings: parsed.settings }, timeout: 20000 });
      if (res && res.settings) {
        state.settings = Object.assign({}, DEFAULT_SETTINGS, res.settings);
        // 外部 FFmpeg 路径/硬解模式变化会影响后端解析结果，保存成功后刷新能力。
        await refreshMetaCapabilities();
        state.similarity = similarityCapability();
        state.video = videoCapability();
        updateModeAvailability();
        updateVideoAvailabilityUi();
        updateSubtitleFromRaw(res.settings || null);
        fillSettingsForm(state.settings);
      }
      state.settingsLoaded = true;
      state.recycleDefault = state.settings ? state.settings.recycle_default !== false : state.recycleDefault;
      toast('设置已保存。', 'success');
      loadAutomationStatus();
    } catch (err) {
      var fieldErrors = videoFieldErrorsFromApi(err);
      if (fieldErrors) {
        showSettingsErrors(fieldErrors);
      } else {
        setText(els.settingsError, errorMessage(err, '保存设置失败，请稍后重试。'));
        show(els.settingsError);
      }
      toast('设置保存失败，请查看错误提示。', 'error');
    } finally {
      state.settingsSaving = false;
      setBusy(els.saveSettingsBtn, false);
    }
  }

  function resetSettingsToDefaults() {
    var defaults = state.defaults || clonePlain(DEFAULT_SETTINGS);
    fillSettingsForm(defaults);
    toast('已填入默认值，点击“保存设置”后生效。', 'info');
  }

  function bindSettingsEvents() {
    els.saveSettingsBtn.addEventListener('click', saveSettings);
    els.resetSettingsBtn.addEventListener('click', resetSettingsToDefaults);
    els.settingsForm.addEventListener('input', function () { clearSettingsErrors(); updateSettingsSummaries(); });
    els.settingsForm.addEventListener('change', function () { clearSettingsErrors(); updateSettingsSummaries(); });
    if (els.setSimExtList) { els.setSimExtList.addEventListener('change', clearSettingsErrors); }
    if (els.setVideoEnabled) {
      els.setVideoEnabled.addEventListener('change', function () {
        updateSettingsVideoFieldsState();
        updateAsrFieldsState();
        clearSettingsErrors();
      });
    }
    if (els.setVideoExtList) { els.setVideoExtList.addEventListener('change', clearSettingsErrors); }
    if (els.setVideoAudioSignature) {
      els.setVideoAudioSignature.addEventListener('change', function () {
        updateSettingsVideoFieldsState();
        clearSettingsErrors();
      });
    }
    if (els.setVideoAsrEnabled) {
      els.setVideoAsrEnabled.addEventListener('change', function () {
        updateAsrFieldsState();
        clearSettingsErrors();
      });
    }
    if (els.setVideoAsrSection) {
      els.setVideoAsrSection.addEventListener('change', clearSettingsErrors);
    }
    [els.videoHwaccel, els.setVideoHwaccel].forEach(function (select) {
      if (select) { select.addEventListener('change', updateVideoHwaccelHints); }
    });
    if (els.subtitleEnabled) {
      els.subtitleEnabled.addEventListener('change', function () { clearSettingsErrors(); updateSubtitleAvailabilityUi(); });
    }
    if (els.subtitleTranslateEnabled) {
      els.subtitleTranslateEnabled.addEventListener('change', function () {
        if (els.subtitleTranslateEnabled.checked && !subtitleCapability().llmKeySet) {
          toast('尚未配置 LLM API Key：可保存设置后再启用译文生成。', 'info', 4200);
        }
        clearSettingsErrors();
      });
    }
    if (els.subtitleClearAsrKey) {
      els.subtitleClearAsrKey.addEventListener('change', function () {
        if (els.subtitleAsrApiKey) {
          els.subtitleAsrApiKey.value = '';
          els.subtitleAsrApiKey.disabled = els.subtitleClearAsrKey.checked;
        }
        clearSettingsErrors();
      });
    }
    if (els.subtitleClearLlmKey) {
      els.subtitleClearLlmKey.addEventListener('change', function () {
        if (els.subtitleLlmApiKey) {
          els.subtitleLlmApiKey.value = '';
          els.subtitleLlmApiKey.disabled = els.subtitleClearLlmKey.checked;
        }
        clearSettingsErrors();
      });
    }
    bindSubtitleProbeEvents();
    // v0046：通知渠道测试按钮
    [['notifyTestDsm', 'dsm'], ['notifyTestWebhook', 'webhook'],
     ['notifyTestTelegram', 'telegram']].forEach(function (pair) {
      var button = byId(pair[0]);
      if (button) {
        button.addEventListener('click', function () { testNotifyChannel(pair[1]); });
      }
    });
    if (els.setScheduledScanRules) {
      els.setScheduledScanRules.addEventListener('change', function () {
        if (els.setScheduledScanEnabled) { els.setScheduledScanEnabled.checked = true; }
      });
    }
  }

  // ==================== 18. 启动 ====================
  function cacheDom() {
    IDS.forEach(function (id) {
      var node = document.getElementById(id);
      if (node) { els[id] = node; }
    });
  }

  function bindGlobalEvents() {
    window.addEventListener('resize', syncTopStackHeight, { passive: true });
    document.addEventListener('keydown', trapModalKeys);
    document.addEventListener('focusin', function (event) {
      if (!isModalOpen()) { return; }
      var modalEntry = getActiveModal();
      if (!modalEntry) { return; }
      if (modalEntry.root.contains(event.target)) { return; }
      if (els.toastRoot && els.toastRoot.contains(event.target)) { return; }
      var first = getFocusable(modalEntry.modal)[0];
      if (first) { first.focus(); }
    });
  }

  function bindLoginEvents() {
    els.loginForm.addEventListener('submit', handleLogin);
    els.logoutBtn.addEventListener('click', handleLogout);
  }

  function init() {
    cacheDom();
    bindLoginEvents();
    bindNavigation();
    bindGlobalEvents();
    bindScanEvents();
    bindResultsEvents();
    bindHistoryEvents();
    bindLogsEvents();
    bindSettingsEvents();
    initSubtitlePage();
    // v0039：字幕页批次总览“去任务中心”入口
    state.openSubtitleJobs = goToSubtitleJobs;
    state.view = initialViewFromHash();
    initSession();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
