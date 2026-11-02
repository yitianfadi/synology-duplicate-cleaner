/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 前端应用
   --------------------------------------------------------------------------
   · 纯静态单文件脚本（无框架、无构建、无 CDN），ES2019 语法。
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
   · 组织方式：常量与状态 → 工具 → API → Toast/弹窗 → 登录 → 导航 →
     文件夹选择器 → 扫描选项 → 进度 → 结果 → 选择 → 预览 → 删除 →
     历史 / 日志 / 设置 → 启动。
   ========================================================================== */
(function () {
  'use strict';

  // ==================== 0. 常量 ====================
  var CSRF_KEY = 'dc_csrf';
  var POLL_MS = 800;                 // 扫描 / 删除任务轮询间隔（毫秒）
  var GROUPS_LIMIT = 50;             // 结果分页大小
  var TERMINAL_STATUS = ['completed', 'failed', 'stopped', 'interrupted'];
  var ACTIVE_STATUS = ['queued', 'running', 'paused', 'stopping'];
  var VIEWS = ['scan', 'history', 'logs', 'settings'];
  var VIEW_HEADINGS = { scan: 'scanHeading', history: 'historyHeading', logs: 'logsHeading', settings: 'settingsHeading' };

  var STATUS_LABEL = {
    queued: '排队中', running: '扫描中', paused: '已暂停', stopping: '正在停止',
    stopped: '已停止', completed: '已完成', failed: '失败', interrupted: '已中断'
  };
  var STATUS_BADGE = {
    queued: 'badge-gray', running: 'badge-blue', paused: 'badge-amber',
    stopping: 'badge-amber', stopped: 'badge-gray', completed: 'badge-green', failed: 'badge-red', interrupted: 'badge-amber'
  };
  var STAGE_LABEL = {
    enumerate: '枚举文件', size: '按大小分组', sample: '快速指纹采样',
    hash: '计算哈希', similarity: '内容相似度分析', video: '视频视觉分析',
    finalize: '归并结果', done: '完成'
  };
  var KEEP_LABEL = {
    latest_created: '最晚创建', earliest_created: '最早创建',
    pinned_path: '指定路径', manual: '需手动选择', tie: '创建时间相同',
    latest_modified_fallback: '按修改时间保留（无创建时间，最晚修改）',
    earliest_modified_fallback: '按修改时间保留（无创建时间，最早修改）',
    mtime_tie: '修改时间相同（按修改时间保留）'
  };
  var MTIME_FALLBACK_LABEL = '无创建时间 · 按修改时间保留';
  var MTIME_FALLBACK_REASONS = ['latest_modified_fallback', 'earliest_modified_fallback', 'mtime_tie'];
  var KEEP_MODE_LABEL = { latest: '最晚创建', earliest: '最早创建', pinned: '指定路径' };
  var SCAN_MODE_LABEL = { exact: '精准哈希', similar: '内容相似' };
  var SCAN_MODE_BADGE = { exact: 'badge-gray', similar: 'badge-purple' };
  var DEFAULT_SIMILARITY_EXTENSIONS = [
    '.txt', '.md', '.markdown', '.csv', '.json', '.xml', '.html', '.htm', '.log',
    '.pdf', '.docx', '.odt', '.png', '.jpg', '.jpeg', '.webp'
  ];
  var SIMILARITY_OPTION_KEYS = ['scan_mode', 'similarity_threshold', 'similarity_extensions',
    'similarity_exclude_extensions', 'similarity_concurrency', 'similarity_cache',
    'similarity_max_file_mb', 'similarity_allow_binary'];
  var DEFAULT_VIDEO_EXTENSIONS = ['.mp4', '.mkv', '.mov', '.avi', '.flv', '.wmv', '.webm', '.m4v', '.ts', '.mpg', '.mpeg'];
  var DEFAULT_VIDEO_SETTINGS = {
    video_similarity_enabled: false,
    video_extensions: DEFAULT_VIDEO_EXTENSIONS.slice(),
    video_sample_frames: 20,
    video_max_sample_interval_sec: 300,
    video_similarity_threshold: 90,
    video_concurrency: 1,
    video_hwaccel: 'auto',
    video_max_file_mb: 8192,
    video_timeout_sec: 180,
    video_cache: true,
    video_asr_enabled: false,
    video_asr_command: '',
    video_asr_weight: 0.15,
    video_asr_timeout_sec: 300,
    video_asr_max_sec: 600
  };
  var VIDEO_HWACCEL_LABEL = { none: '软件解码', vaapi: 'VA-API', qsv: 'Intel QSV', nvdec: 'NVDEC', auto: '自动' };
  var SOURCE_INFO = {
    statx: { short: 'statx', title: 'statx：文件系统创建时间（可靠）', cls: 'is-ok' },
    birthtime: { short: 'birthtime', title: 'birthtime：系统提供的创建时间', cls: 'is-ok' },
    ctime: { short: 'ctime', title: 'ctime：文件状态变更时间（降级值，不能当作创建时间，存在风险）', cls: 'is-fallback' },
    unknown: { short: '未知', title: '未知：无法读取文件系统创建时间（不会用 ctime 冒充创建时间）', cls: 'is-unknown' }
  };
  var DELETE_STATUS_LABEL = { recycled: '已移入回收站', deleted: '已删除', skipped: '已跳过', failed: '失败' };
  var DELETE_REASON_LABEL = {
    FILE_CHANGED: '文件已被修改', HASH_MISMATCH: '哈希不一致', KEEP_FILE_MISSING: '保留文件不存在',
    PATH_NOT_ALLOWED: '路径不在允许范围', NOT_FOUND: '文件不存在', PERMISSION_DENIED: '没有权限',
    RECYCLE_UNAVAILABLE: '回收站不可用', CROSS_DEVICE: '跨文件系统无法移动', PLAN_EXPIRED: '计划已过期'
  };
  var JOB_STATUS_LABEL = {
    running: '删除中', completed: '删除完成', partial: '部分成功',
    failed: '删除失败', stopped: '已停止'
  };
  var AUDIT_LABEL = {
    login: '登录', logout: '退出登录', scan_start: '开始扫描', scan_finish: '扫描完成',
    scan_stop: '停止扫描', scan_pause: '暂停扫描', scan_resume: '继续扫描',
    delete_plan: '创建删除计划', delete_start: '开始删除', delete_file: '删除文件',
    delete_finish: '删除完成', settings_update: '更新设置', preview: '预览文件'
  };
  var ERROR_FALLBACK = {
    AUTH_REQUIRED: '登录已过期，请重新登录。',
    AUTH_FAILED: '账号或密码错误。',
    FORBIDDEN: '没有权限执行该操作。',
    CSRF_FAILED: '安全校验失败，请刷新页面后重试。',
    NOT_FOUND: '请求的资源不存在。',
    INVALID_ARGUMENT: '请求参数不正确。',
    INVALID_PATH: '路径不合法。',
    PATH_NOT_ALLOWED: '该路径不在允许的共享文件夹范围内。',
    SCAN_BUSY: '已有扫描任务正在运行，请等待其结束。',
    SCAN_NOT_DONE: '扫描尚未完成，暂时无法查看结果。',
    PLAN_EXPIRED: '删除计划已过期，请重新确认。',
    PLAN_INVALID: '删除计划无效，请重新确认。',
    HASH_MISMATCH: '文件哈希与扫描时不一致。',
    FILE_CHANGED: '文件在扫描后已被修改。',
    RECYCLE_UNAVAILABLE: '回收站不可用，请改用永久删除。',
    SIMILAR_REVIEW_REQUIRED: '相似组需要人工复核：请先逐组勾选“人工复核”，并逐条选择要删除的副本。',
    INTERNAL_ERROR: '服务内部错误，请稍后重试。'
  };
  var DEFAULT_SETTINGS = {
    hash_algo: 'sha256', keep_mode: 'latest', pinned_path: '',
    skip_empty: true, skip_hidden: true, skip_symlinks: true,
    include_extensions: [], exclude_extensions: [], ignore_case_ext: true,
    min_size_bytes: 0, max_size_bytes: 0, quick_sample_kb: 64,
    concurrency: 2, hash_rate_mbps: 0, recycle_default: true,
    create_recycle_dir: false, max_files: 500000, allow_ctime_fallback: false,
    scan_mode: 'exact', similarity_threshold: 85, similarity_extensions: [],
    similarity_exclude_extensions: [], similarity_concurrency: 2, similarity_cache: true,
    similarity_max_file_mb: 32, similarity_allow_binary: false,
    video_similarity_enabled: false, video_extensions: DEFAULT_VIDEO_EXTENSIONS.slice(),
    video_sample_frames: 20, video_max_sample_interval_sec: 300, video_similarity_threshold: 90,
    video_concurrency: 1, video_hwaccel: 'auto', video_max_file_mb: 8192,
    video_timeout_sec: 180, video_cache: true,
    video_asr_enabled: false, video_asr_command: '', video_asr_weight: 0.15,
    video_asr_timeout_sec: 300, video_asr_max_sec: 600,
    subtitle_enabled: false,
    subtitle_asr_base_url: 'https://api.openai.com/v1',
    subtitle_asr_api_key: '',
    subtitle_asr_model: 'whisper-1',
    subtitle_asr_language: 'auto',
    subtitle_asr_timeout_sec: 300,
    subtitle_asr_chunk_sec: 600,
    subtitle_asr_max_audio_mb: 24,
    subtitle_translate_enabled: false,
    subtitle_llm_base_url: 'https://api.openai.com/v1',
    subtitle_llm_api_key: '',
    subtitle_llm_model: 'gpt-4.1-mini',
    subtitle_translate_target: 'zh',
    subtitle_translate_timeout_sec: 120,
    subtitle_translate_batch_size: 20
  };
  var SIZE_UNITS = { kb: 1024, mb: 1048576, gb: 1073741824 };

  // ==================== 1. 应用状态 ====================
  var state = {
    csrf: '',
    user: null,
    meta: null,
    settings: null,
    defaults: null,
    view: 'scan',
    booted: false,
    loginSubmitting: false,
    // 扫描范围
    roots: [],
    excludes: [],
    pinnedPath: '',
    recycleDefault: true,
    scanMode: 'exact',
    similarity: { supported: true, extensions: DEFAULT_SIMILARITY_EXTENSIONS.slice(), defaultThreshold: 85, maxConcurrency: 4, binaryExtensions: [] },
    video: { supported: true, available: true, extensions: DEFAULT_VIDEO_EXTENSIONS.slice(), ffmpegPath: '', ffmpegVersion: '', hwaccels: [], gpuHint: '', cache: '' },
    subtitle: { supported: false, enabled: false, asrKeySet: false, llmKeySet: false, asrLanguage: 'auto', translateEnabled: false, targetLang: 'zh' },
    // 当前扫描
    scan: { id: null, data: null, stats: null, timer: null, abort: null, polling: false, errors: 0, handled: false },
    // 结果
    results: {
      groups: [], total: 0, offset: 0, limit: GROUPS_LIMIT, q: '', sort: 'wasted_desc',
      mode: 'none', selected: new Map(), excluded: new Map(), manualKeep: new Map(),
      loading: false, seq: 0, loaded: false,
      scanMode: 'exact', exactTotal: 0, similarTotal: 0,
      similarGroups: [], similarOffset: 0, similarLoading: false, similarLoaded: false,
      similarSeq: 0, similarReview: new Set(), similarSelected: new Map(),
      videoGroups: [], videoTotal: 0, videoOffset: 0, videoLoading: false, videoLoaded: false,
      videoSeq: 0, videoSelected: new Map(), videoSelectAllMode: false
    },
    // 删除流程
    deleteFlow: { plan: null, job: null, timer: null, abort: null, running: false, busy: false, modal: null, recycle: true, override: null },
    picker: null,
    preview: { urls: [], abort: null },
    historyCache: null,
    logsLoaded: false,
    settingsLoaded: false,
    logsLimit: 50
  };

  var els = {};
  var IDS = ('loginView loginForm loginUsername loginPassword loginOtp loginSubmit loginError app riskBanner envBadge userChip userName ' +
    'logoutBtn main view-scan view-history view-logs view-settings scanHeading historyHeading logsHeading settingsHeading ' +
    'rootsList rootsCount rootsError pickRootsBtn clearRootsBtn excludeChips excludeCount pickExcludesBtn optionsForm resetOptionsBtn ' +
    'hashAlgo keepMode keepModeHint pinnedField pinnedValue pinnedError pickPinnedBtn clearPinnedBtn minSize minUnit minSizeError maxSize maxUnit ' +
    'maxSizeError includeExt includeExtError excludeExt excludeExtError ignoreCaseExt skipEmpty skipHidden recycleDefault ' +
    'quickSampleKb quickSampleError concurrency concurrencyError hashRate hashRateError maxFiles maxFilesError allowCtimeFallback ' +
    'createRecycleDir skipSymlinks optionsError scanSummary startScanBtn progressPanel progressStatus progressScanId progressStage ' +
    'progressPct progressBar progressFill progressMessage counterFilesSeen counterFilesScanned counterCandidates counterGroups ' +
    'counterDuplicateFiles counterReclaimable counterRate counterErrors progressPath progressElapsed pauseBtn resumeBtn stopBtn ' +
    'viewResultsBtn resultsSection resultsSubtitle refreshResultsBtn resultsSummary searchGroups sortGroups resultsCountLabel ' +
    'selectAllBtn clearSelectionBtn csvLink deleteSelectedBtn selectionSummary resultsLoading resultsError resultsEmpty groupsList ' +
    'loadMoreBtn loadMoreStatus historyRefreshBtn historyLoading historyError historyEmpty historyList logLimitSelect logsRefreshBtn ' +
    'logsLoading logsError auditCount auditEmpty auditList logFilesCount logFilesEmpty logFilesList settingsForm settingsLoading ' +
    'settingsLoadError setHashAlgo setKeepMode setPinnedPath setMinSizeMb setMinSizeError setMaxSizeMb setMaxSizeError ' +
    'setIncludeExt setIncludeExtError setExcludeExt setExcludeExtError setIgnoreCase setSkipEmpty setSkipHidden setSkipSymlinks ' +
    'setQuickSampleKb setQuickSampleError setConcurrency setConcurrencyError setHashRate setHashRateError setMaxFiles ' +
    'setMaxFilesError setRecycleDefault setCreateRecycleDir setAllowCtime settingsError saveSettingsBtn resetSettingsBtn ' +
    'modeExactBtn modeSimilarBtn modeDesc modeCapabilityBadge similarityOptions simThreshold simThresholdRange simThresholdError ' +
    'simConcurrency simConcurrencyError simMaxFileMb simMaxFileError simCache simAllowBinary simExtList simExtError ' +
    'similarityCounters simFeatures simCacheHits simCandidates simSkipped resultsModeBadge mtimeFallbackNotice mtimeFallbackNoticeText exactPartition exactPartitionTitle ' +
    'exactPartitionBadge exactPartitionCount similarPartition similarPartitionCount similarLoading similarError similarEmpty ' +
    'similarGroupsList similarLoadMoreBtn similarLoadMoreStatus settingsSimilarityDetails setScanMode setSimilarityThreshold ' +
    'setSimilarityThresholdError setSimilarityConcurrency setSimilarityConcurrencyError setSimilarityMaxMb setSimilarityMaxFileError ' +
    'setSimilarityCache setSimilarityAllowBinary setSimExtList setSimilarityExtError setSimilarityExcludeExt ' +
    'setSimilarityExcludeExtError videoOptionsDetails videoStatusBadge videoStatusHint videoEnabled videoFields ' +
    'videoThreshold videoThresholdError videoSampleFrames videoFramesError videoMaxInterval videoIntervalError ' +
    'videoConcurrency videoConcurrencyError videoHwaccel videoMaxFileMb videoMaxFileError videoTimeout videoTimeoutError ' +
    'videoCache videoExtList videoExtError videoCounters videoCandidates videoCacheHits videoFfmpegUsed videoSkipped ' +
    'xlsxLink videoPartition videoPartitionCount selectAllVideoBtn videoLoading videoError videoEmpty videoGroupsList ' +
    'videoLoadMoreBtn videoLoadMoreStatus settingsVideoDetails setVideoStatusBadge setVideoStatusHint setVideoEnabled ' +
    'setVideoFields setVideoThreshold setVideoThresholdError setVideoSampleFrames setVideoFramesError setVideoMaxInterval ' +
    'setVideoIntervalError setVideoConcurrency setVideoConcurrencyError setVideoHwaccel setVideoMaxMb setVideoMaxFileError ' +
    'setVideoTimeout setVideoTimeoutError setVideoCache setVideoExtList setVideoExtError ' +
    'videoAsrSection videoAsrStatusBadge videoAsrStatusHint videoAsrEnabled videoAsrFields videoAsrCommand ' +
    'videoAsrCommandError videoAsrWeight videoAsrWeightError videoAsrTimeout videoAsrTimeoutError ' +
    'videoAsrMaxSec videoAsrMaxSecError setVideoAsrStatusBadge setVideoAsrStatusHint ' +
    'setVideoAsrEnabled setVideoAsrFields setVideoAsrCommand setVideoAsrCommandError setVideoAsrWeight ' +
    'setVideoAsrWeightError setVideoAsrTimeout setVideoAsrTimeoutError setVideoAsrMaxSec setVideoAsrMaxSecError ' +
    'settingsSubtitleDetails subtitleStatusBadge subtitleStatusHint subtitleFields subtitleEnabled subtitleAsrBaseUrl ' +
    'subtitleAsrBaseUrlError subtitleAsrModel subtitleAsrModelError subtitleAsrApiKey subtitleAsrApiKeyError ' +
    'subtitleAsrKeySetBadge subtitleClearAsrKey subtitleAsrLanguage subtitleAsrTimeout subtitleAsrTimeoutError ' +
    'subtitleAsrChunk subtitleAsrChunkError subtitleAsrMaxAudioMb subtitleAsrMaxAudioMbError subtitleTranslateEnabled ' +
    'subtitleLlmBaseUrl subtitleLlmBaseUrlError subtitleLlmModel subtitleLlmModelError subtitleLlmApiKey ' +
    'subtitleLlmApiKeyError subtitleLlmKeySetBadge subtitleClearLlmKey subtitleTranslateTarget subtitleTranslateTimeout ' +
    'subtitleTranslateTimeoutError subtitleTranslateBatchSize subtitleTranslateBatchSizeError ' +
    'footerMeta toastRoot modalRoot').split(' ');

  var activeModal = null;
  var modalSeq = 0;

  // ==================== 2. 通用工具 ====================
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function byId(id) { return document.getElementById(id); }
  function clear(node) { if (!node) return; while (node.firstChild) { node.removeChild(node.firstChild); } }
  function show(node) { if (node) { node.hidden = false; } }
  function hide(node) { if (node) { node.hidden = true; } }
  function setHidden(node, hidden) { if (node) { node.hidden = !!hidden; } }
  function setText(node, text) { if (node) { node.textContent = (text === null || text === undefined) ? '' : String(text); } }

  /** 安全创建 DOM 节点。opts: {className, text, attrs, dataset, on} */
  function el(tag, opts, children) {
    var node = document.createElement(tag);
    if (opts) {
      if (opts.className) { node.className = opts.className; }
      if (opts.text !== undefined && opts.text !== null) { node.textContent = String(opts.text); }
      if (opts.attrs) {
        Object.keys(opts.attrs).forEach(function (k) {
          var v = opts.attrs[k];
          if (v === null || v === undefined || v === false) { return; }
          if (k === 'checked' || k === 'disabled' || k === 'selected') { node[k] = !!v; return; }
          if (k === 'value') { node.value = v; return; }
          node.setAttribute(k, v === true ? '' : String(v));
        });
      }
      if (opts.dataset) {
        Object.keys(opts.dataset).forEach(function (k) { node.dataset[k] = String(opts.dataset[k]); });
      }
      if (opts.on) {
        Object.keys(opts.on).forEach(function (ev) { node.addEventListener(ev, opts.on[ev]); });
      }
    }
    appendChildren(node, children);
    return node;
  }

  function appendChildren(node, children) {
    if (children === null || children === undefined) { return; }
    if (Array.isArray(children)) {
      children.forEach(function (child) { appendChildren(node, child); });
      return;
    }
    if (children instanceof Node) { node.appendChild(children); }
    else { node.appendChild(document.createTextNode(String(children))); }
  }

  function icon(name, extraClass) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon' + (extraClass ? ' ' + extraClass : ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-' + name);
    use.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }

  function fmtBytes(bytes, digits) {
    var n = Number(bytes);
    if (!isFinite(n) || n <= 0) { return '0 B'; }
    var units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    var i = 0;
    var v = n;
    while (v >= 1024 && i < units.length - 1) { v = v / 1024; i += 1; }
    var d = digits;
    if (d === undefined || d === null) {
      d = i === 0 ? 0 : (v >= 100 ? 0 : (v >= 10 ? 1 : 2));
    }
    var s = v.toFixed(d);
    if (s.indexOf('.') !== -1) { s = s.replace(/0+$/, '').replace(/\.$/, ''); }
    return s + ' ' + units[i];
  }

  function fmtInt(value) {
    var n = Number(value);
    if (!isFinite(n)) { return '0'; }
    try { return n.toLocaleString('zh-CN'); } catch (e) { return String(n); }
  }

  function fmtDateTime(iso) {
    if (!iso) { return '—'; }
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return String(iso); }
    function p(n) { return n < 10 ? '0' + n : String(n); }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  function fmtElapsed(seconds) {
    var sec = Number(seconds);
    if (!isFinite(sec) || sec < 0) { return '—'; }
    sec = Math.round(sec);
    if (sec < 60) { return sec + ' 秒'; }
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    if (m < 60) { return m + ' 分 ' + s + ' 秒'; }
    var h = Math.floor(m / 60);
    return h + ' 小时 ' + (m % 60) + ' 分';
  }

  function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments;
      var self = this;
      if (timer) { clearTimeout(timer); }
      timer = setTimeout(function () { timer = null; fn.apply(self, args); }, wait);
    };
  }

  function clonePlain(value) {
    try { return JSON.parse(JSON.stringify(value)); } catch (e) { return null; }
  }

  /** 解析扩展名输入：逗号/空格分隔，自动补点号，最长 16 字符（含点）。 */
  function normalizeExtList(raw) {
    var text = String(raw === null || raw === undefined ? '' : raw).trim();
    if (!text) { return { list: [], error: '' }; }
    var parts = text.split(/[\s,，;；]+/).filter(function (s) { return s.length > 0; });
    var list = [];
    for (var i = 0; i < parts.length; i += 1) {
      var v = parts[i].trim();
      if (!v) { continue; }
      if (v.charAt(0) !== '.') { v = '.' + v; }
      if (v === '.' || /[\\/]/.test(v)) {
        return { list: [], error: '后缀“' + parts[i] + '”格式不正确，只能包含字母、数字、点、下划线和短横线' };
      }
      if (v.length > 16) {
        return { list: [], error: '后缀“' + parts[i] + '”过长（含点号最多 16 个字符）' };
      }
      if (list.indexOf(v) === -1) { list.push(v); }
    }
    return { list: list, error: '' };
  }

  function extListToText(list) {
    if (!Array.isArray(list)) { return ''; }
    return list.join(',');
  }

  function normalizePathList(list) {
    if (!Array.isArray(list)) { return []; }
    var out = [];
    list.forEach(function (item) {
      var p = typeof item === 'string' ? item : (item && item.path);
      if (typeof p === 'string' && p && out.indexOf(p) === -1) { out.push(p); }
    });
    return out;
  }

  function isUnderRoots(path, roots) {
    if (!path || !roots || !roots.length) { return false; }
    return roots.some(function (root) {
      var rp = typeof root === 'string' ? root : root.path;
      if (!rp) { return false; }
      var normalized = String(rp).replace(/[\\/]+$/, '');
      return path === normalized ||
        path.indexOf(normalized + '/') === 0 ||
        path.indexOf(normalized + '\\') === 0;
    });
  }

  function encodePath(path) { return encodeURIComponent(String(path === null || path === undefined ? '' : path)); }

  function setBusy(btn, busy, label) {
    if (!btn) { return; }
    if (busy) {
      if (!btn.__originalChildren) {
        btn.__originalChildren = Array.prototype.slice.call(btn.childNodes).map(function (n) { return n.cloneNode(true); });
      }
      btn.disabled = true;
      clear(btn);
      btn.appendChild(el('span', { className: 'spinner' }));
      btn.appendChild(document.createTextNode(label || '处理中…'));
    } else {
      btn.disabled = false;
      if (btn.__originalChildren) {
        clear(btn);
        btn.__originalChildren.forEach(function (n) { btn.appendChild(n.cloneNode(true)); });
      }
    }
  }

  function legacyCopy(text) {
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        ta.style.top = '0';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        if (ok) { resolve(); } else { reject(new Error('当前浏览器不支持自动复制')); }
      } catch (e) { reject(e); }
    });
  }

  function copyText(text) {
    var value = String(text === null || text === undefined ? '' : text);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value).catch(function () { return legacyCopy(value); });
    }
    return legacyCopy(value);
  }

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

  function getFocusable(root) {
    return $$('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', root)
      .filter(function (node) { return node.offsetParent !== null || node === document.activeElement; });
  }

  function focusSoon(node) {
    if (!node) { return; }
    setTimeout(function () {
      try { node.focus({ preventScroll: true }); } catch (e) { node.focus(); }
    }, 0);
  }

  function scrollToNode(node) {
    if (!node) { return; }
    try { node.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) { node.scrollIntoView(); }
  }

  function statusBadge(status) {
    var label = STATUS_LABEL[status] || status || '未知';
    return el('span', { className: 'badge ' + (STATUS_BADGE[status] || 'badge-gray'), text: label });
  }

  // ==================== 3. API 客户端 ====================
  class ApiError extends Error {
    constructor(status, code, message, details) {
      super(message || '请求失败');
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.details = details;
    }
  }

  function errorMessage(err, fallback) {
    if (!err) { return fallback || '操作失败，请稍后重试。'; }
    if (err.name === 'AbortError') { return '请求已取消。'; }
    if (err instanceof ApiError) {
      if (err.message) { return err.message; }
      return ERROR_FALLBACK[err.code] || fallback || '操作失败，请稍后重试。';
    }
    if (err.message) { return err.message; }
    return fallback || '网络异常，请检查连接后重试。';
  }

  function saveCsrf(token) {
    state.csrf = token || '';
    try {
      if (state.csrf) { sessionStorage.setItem(CSRF_KEY, state.csrf); }
      else { sessionStorage.removeItem(CSRF_KEY); }
    } catch (e) { /* 隐私模式下忽略 */ }
  }

  /**
   * 同源 JSON 请求：自动携带 Cookie 与 X-CSRF-Token，统一错误对象与 401 处理。
   * options: {method, body, timeout, signal, skipAuthHandler, headers}
   */
  async function api(path, options) {
    options = options || {};
    var method = (options.method || 'GET').toUpperCase();
    var controller = new AbortController();
    var headers = Object.assign({ Accept: 'application/json' }, options.headers || {});
    var init = { method: method, credentials: 'same-origin', headers: headers, signal: controller.signal };
    var timer = null;
    var timedOut = false;
    var timeout = options.timeout === undefined ? 30000 : options.timeout;
    if (timeout > 0) {
      timer = setTimeout(function () { timedOut = true; controller.abort(); }, timeout);
    }
    var externalAbort = null;
    if (options.signal) {
      if (options.signal.aborted) { controller.abort(); }
      else {
        externalAbort = function () { controller.abort(); };
        options.signal.addEventListener('abort', externalAbort);
      }
    }
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body);
    }
    if (method !== 'GET' && method !== 'HEAD' && state.csrf) {
      headers['X-CSRF-Token'] = state.csrf;
    }
    var response;
    try {
      response = await fetch(path, init);
    } catch (err) {
      if (timedOut) { throw new ApiError(0, 'TIMEOUT', '请求超时，请稍后重试。'); }
      if (err && err.name === 'AbortError') { throw err; }
      throw new ApiError(0, 'NETWORK', '无法连接服务，请确认后端服务已启动。');
    } finally {
      if (timer) { clearTimeout(timer); }
      if (externalAbort && options.signal) { options.signal.removeEventListener('abort', externalAbort); }
    }
    var contentType = response.headers.get('Content-Type') || '';
    var payload = null;
    if (contentType.indexOf('application/json') !== -1) {
      try { payload = await response.json(); } catch (e) { payload = null; }
    }
    if (response.status === 401 && !options.skipAuthHandler) {
      handleUnauthorized();
      throw new ApiError(401, 'AUTH_REQUIRED', ERROR_FALLBACK.AUTH_REQUIRED);
    }
    if (!response.ok) {
      var errorObj = payload && payload.error ? payload.error : null;
      var code = errorObj && errorObj.code ? errorObj.code : 'HTTP_' + response.status;
      var message = errorObj && errorObj.message ? errorObj.message
        : (ERROR_FALLBACK[code] || '请求失败（HTTP ' + response.status + '）。');
      throw new ApiError(response.status, code, message, errorObj ? errorObj.details : null);
    }
    return payload || {};
  }

  // ==================== 4. Toast ====================
  function toast(message, type, duration) {
    type = type || 'info';
    if (!els.toastRoot) { return; }
    var current = $$('.toast', els.toastRoot);
    if (current.length >= 4) { removeToast(current[0]); }
    var iconName = type === 'error' ? 'warning' : (type === 'success' ? 'check' : 'info');
    var node = el('div', {
      className: 'toast toast-' + type,
      attrs: { role: type === 'error' ? 'alert' : 'status' }
    }, [icon(iconName), el('span', { className: 'toast-msg', text: message })]);
    els.toastRoot.appendChild(node);
    var ms = duration || (type === 'error' ? 5600 : 3200);
    node.__timer = setTimeout(function () { removeToast(node); }, ms);
  }

  function removeToast(node) {
    if (!node || node.__removed) { return; }
    node.__removed = true;
    if (node.__timer) { clearTimeout(node.__timer); }
    node.classList.add('is-leaving');
    setTimeout(function () {
      if (node.parentNode) { node.parentNode.removeChild(node); }
    }, 220);
  }

  // ==================== 5. 弹窗系统（焦点陷阱 / ESC / 还原焦点） ====================
  function openModal(options) {
    options = options || {};
    if (activeModal) { closeModal(true); }
    var root = els.modalRoot;
    clear(root);
    var previousFocus = document.activeElement;
    var titleId = 'modal-title-' + (++modalSeq);
    var dismissible = options.dismissible !== false;
    var titleEl = el('h2', { className: 'modal-title', attrs: { id: titleId, tabindex: '-1' }, text: options.title || '对话框' });
    var headMain = el('div', { className: 'modal-head-main' }, [
      titleEl,
      options.subtitle ? el('p', { className: 'modal-subtitle', text: options.subtitle }) : null
    ]);
    var header = el('div', { className: 'modal-header' }, [headMain]);
    var closeBtn = null;
    if (dismissible) {
      closeBtn = el('button', {
        className: 'modal-close', attrs: { type: 'button', 'aria-label': '关闭对话框' },
        on: { click: function () { closeModal(); } }
      }, [icon('close')]);
      header.appendChild(closeBtn);
    }
    var bodyEl = el('div', { className: 'modal-body' }, options.body || null);
    var footerEl = el('div', { className: 'modal-footer' });
    renderFooter(footerEl, options.footer);
    var sizeClass = options.size === 'lg' ? ' modal-lg' : (options.size === 'xl' ? ' modal-xl' : '');
    var modal = el('div', {
      className: 'modal' + sizeClass,
      attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId }
    }, [header, bodyEl, footerEl]);
    var backdrop = el('div', {
      className: 'modal-backdrop',
      on: { click: function () { if (activeModal && activeModal.dismissible) { closeModal(); } } }
    });
    root.appendChild(backdrop);
    root.appendChild(modal);
    hide(root);
    show(root);
    document.body.style.overflow = 'hidden';

    var entry = {
      root: root, modal: modal, bodyEl: bodyEl, footerEl: footerEl, titleEl: titleEl,
      previousFocus: previousFocus, dismissible: dismissible, onClose: options.onClose || null
    };
    activeModal = entry;

    var controller = {
      modal: modal,
      bodyEl: bodyEl,
      setTitle: function (text) { setText(titleEl, text); },
      setSubtitle: function (text) {
        var sub = $('.modal-subtitle', header);
        if (!sub && text) { sub = el('p', { className: 'modal-subtitle' }); headMain.appendChild(sub); }
        if (sub) { setText(sub, text || ''); sub.hidden = !text; }
      },
      setBody: function (node) { clear(bodyEl); if (node) { bodyEl.appendChild(node); } },
      setFooter: function (footer) { clear(footerEl); renderFooter(footerEl, footer); },
      setBusy: function (busy) { closeBtn.disabled = !!busy; },
      close: function () { closeModal(true); }
    };

    var focusTarget = options.initialFocus;
    if (typeof focusTarget === 'string') { focusTarget = $(focusTarget, modal); }
    var first = focusTarget || getFocusable(modal)[0] || titleEl;
    focusSoon(first);
    return controller;
  }

  function renderFooter(footerEl, footer) {
    if (!footer) { return; }
    if (Array.isArray(footer)) {
      footer.forEach(function (node) { if (node) { footerEl.appendChild(node); } });
      return;
    }
    if (footer.left) {
      footerEl.appendChild(el('div', { className: 'footer-left' }, footer.left));
    }
    if (footer.right) {
      (Array.isArray(footer.right) ? footer.right : [footer.right]).forEach(function (node) {
        if (node) { footerEl.appendChild(node); }
      });
    }
  }

  function closeModal(force) {
    var entry = activeModal;
    if (!entry) { return; }
    if (!entry.dismissible && force !== true) { return; }
    activeModal = null;
    if (entry.onClose) {
      try { entry.onClose(); } catch (e) { /* 忽略清理异常 */ }
    }
    clear(entry.root);
    hide(entry.root);
    document.body.style.overflow = '';
    if (entry.previousFocus && entry.previousFocus.isConnected) {
      try { entry.previousFocus.focus({ preventScroll: true }); } catch (e2) { /* 忽略 */ }
    }
  }

  function trapModalKeys(event) {
    if (!activeModal) { return; }
    if (event.key === 'Escape') {
      if (activeModal.dismissible) {
        event.preventDefault();
        closeModal();
      }
      return;
    }
    if (event.key !== 'Tab') { return; }
    var modal = activeModal.modal;
    var focusables = getFocusable(modal);
    if (!focusables.length) { event.preventDefault(); return; }
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    var current = document.activeElement;
    if (!modal.contains(current)) { event.preventDefault(); first.focus(); return; }
    if (event.shiftKey && current === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && current === last) { event.preventDefault(); first.focus(); }
  }

  function confirmDialog(options) {
    return new Promise(function (resolve) {
      var settled = false;
      var cancelBtn = el('button', {
        className: 'btn btn-ghost', attrs: { type: 'button' }, text: options.cancelText || '取消'
      });
      var okBtn = el('button', {
        className: 'btn ' + (options.danger === false ? 'btn-primary' : 'btn-danger'),
        attrs: { type: 'button' }, text: options.confirmText || '确认'
      });
      function finish(value) {
        if (settled) { return; }
        settled = true;
        closeModal(true);
        resolve(value);
      }
      cancelBtn.addEventListener('click', function () { finish(false); });
      okBtn.addEventListener('click', function () { finish(true); });
      openModal({
        title: options.title || '请确认',
        body: el('div', {}, options.body || el('p', { className: 'modal-note', text: options.message || '' })),
        size: 'md',
        footer: [cancelBtn, okBtn],
        initialFocus: options.danger === false ? okBtn : cancelBtn,
        onClose: function () { if (!settled) { settled = true; resolve(false); } }
      });
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
    state.pinnedPath = '';
    state.recycleDefault = true;
    state.scanMode = 'exact';
    state.similarity = { supported: true, extensions: DEFAULT_SIMILARITY_EXTENSIONS.slice(), defaultThreshold: 85, maxConcurrency: 4, binaryExtensions: [] };
    state.video = { supported: true, available: true, extensions: DEFAULT_VIDEO_EXTENSIONS.slice(), ffmpegPath: '', ffmpegVersion: '', hwaccels: [], gpuHint: '', cache: '' };
    state.subtitle = { supported: false, enabled: false, asrKeySet: false, llmKeySet: false, asrLanguage: 'auto', translateEnabled: false, targetLang: 'zh' };
    state.historyCache = null;
    state.logsLoaded = false;
    state.settingsLoaded = false;
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
  }

  // ==================== 7. 导航 ====================
  function bindNavigation() {
    $$('.nav-item').forEach(function (btn) {
      btn.addEventListener('click', function () { setView(btn.dataset.nav); });
    });
    window.addEventListener('hashchange', function () {
      var view = String(window.location.hash || '').replace(/^#\/?/, '');
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
    if (view === 'history') { loadHistory(); }
    else if (view === 'logs') { loadLogs(); }
    else if (view === 'settings') { loadSettingsPage(); }
  }

  function initialViewFromHash() {
    var view = String(window.location.hash || '').replace(/^#\/?/, '');
    return VIEWS.indexOf(view) !== -1 ? view : 'scan';
  }

  // ==================== 8. 文件夹选择器（共享文件夹 / 懒加载目录树） ====================
  var PICKER_TITLES = { roots: '选择扫描范围', excludes: '选择排除目录', pinned: '选择指定保留路径' };
  var PICKER_HINTS = {
    roots: '勾选一个或多个目录作为扫描范围；点击文件夹名称进入下一级，点击左侧箭头可展开子目录。',
    excludes: '勾选需要排除的目录；排除目录必须位于扫描范围内，其中的文件不会被扫描或删除。',
    pinned: '选择目录或文件作为“指定路径”保留策略的匹配前缀。'
  };

  function openFolderPicker(mode) {
    if (VIEWS.indexOf(state.view) === -1) { return; }
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
    return picker.pinned === path;
  }

  function pickerPathState(picker, path, entry) {
    var result = { disabled: false, note: '', warn: false };
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
      if (pickerIsSelected(picker, path)) { row.classList.add('is-selected'); }
      else { row.classList.remove('is-selected'); }
    });
    $$('.tree-check', picker.listEl).forEach(function (input) {
      input.checked = pickerIsSelected(picker, input.dataset.path);
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
      var disabled = !picker.currentPath || pickerPathState(picker, picker.currentPath, null).disabled;
      if (picker.mode === 'excludes') { label = pickerIsSelected(picker, picker.currentPath) ? '取消排除当前目录' : '排除当前目录'; }
      else if (picker.mode === 'pinned') { label = pickerIsSelected(picker, picker.currentPath) ? '当前目录已指定' : '使用当前目录'; }
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
    var rowClass = 'tree-row' + (selected ? ' is-selected' : '');
    if (selectionState.disabled) { rowClass += entry.excluded ? ' is-excluded' : ' is-disabled'; }
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
        disabled: selectionState.disabled,
        'aria-label': (selected ? '取消选择 ' : '选择 ') + (entry.name || entry.path),
        'data-key': encodePath(entry.path)
      }
    });
    input.checked = selected;
    input.dataset.path = entry.path;
    if (selectionState.disabled) { input.title = selectionState.note || '不可选择'; }
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

  function applyPickerSelection(picker) {
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
    updateScanSummary();
  }

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
    videoAsrCommand: { input: 'videoAsrCommand', error: 'videoAsrCommandError' },
    videoAsrWeight: { input: 'videoAsrWeight', error: 'videoAsrWeightError' },
    videoAsrTimeout: { input: 'videoAsrTimeout', error: 'videoAsrTimeoutError' },
    videoAsrMaxSec: { input: 'videoAsrMaxSec', error: 'videoAsrMaxSecError' }
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
    var ffmpegPath = (video && video.ffmpeg_path) || '';
    var ffmpegVersion = (video && video.ffmpeg_version) || '';
    var ffmpegAvailable = false;
    if (video) {
      if (typeof video.available === 'boolean') {
        ffmpegAvailable = video.available;
      } else {
        ffmpegAvailable = !!ffmpegPath && !!ffmpegVersion && ffmpegVersion !== 'unavailable';
      }
    }
    return {
      supported: supported,
      available: supported && ffmpegAvailable,
      extensions: extensions,
      ffmpegPath: ffmpegPath,
      ffmpegVersion: ffmpegVersion,
      hwaccels: video && Array.isArray(video.hwaccels) ? video.hwaccels : [],
      gpuHint: (video && video.gpu_hint) || '',
      cache: (video && video.cache) || ''
    };
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
    if (els.videoHwaccel) { els.videoHwaccel.value = settings.video_hwaccel === 'off' ? 'off' : 'auto'; }
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
    if (els.setVideoHwaccel) { els.setVideoHwaccel.value = settings.video_hwaccel === 'off' ? 'off' : 'auto'; }
    if (els.setVideoCache) { els.setVideoCache.checked = settings.video_cache !== false; }
    var selected = (Array.isArray(settings.video_extensions) && settings.video_extensions.length)
      ? settings.video_extensions : cap.extensions;
    renderExtChips(els.setVideoExtList, cap.extensions, selected, null);
    fillAsrSettingsForm(settings);
    updateVideoAvailabilityUi();
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
    options.video_hwaccel = els.videoHwaccel && els.videoHwaccel.value === 'off' ? 'off' : 'auto';
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
    settings.video_hwaccel = els.setVideoHwaccel && els.setVideoHwaccel.value === 'off' ? 'off' : 'auto';
    settings.video_cache = !!(els.setVideoCache && els.setVideoCache.checked);
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
      asrLanguage: 'auto', translateEnabled: false, targetLang: 'zh'
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
      targetLang: (raw && raw.subtitle_translate_target) || 'zh'
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
        hint = '云端字幕默认关闭；启用并配置 ASR API Key 后，视频结果行会出现“生成字幕”按钮。未启用不影响视频查重。';
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
  }

  function fillSubtitleSettingsForm(settings) {
    var s = settings || {};
    if (els.subtitleEnabled) { els.subtitleEnabled.checked = s.subtitle_enabled === true; }
    if (els.subtitleAsrBaseUrl) { els.subtitleAsrBaseUrl.value = s.subtitle_asr_base_url || ''; }
    if (els.subtitleAsrModel) { els.subtitleAsrModel.value = s.subtitle_asr_model || ''; }
    if (els.subtitleAsrLanguage) { els.subtitleAsrLanguage.value = s.subtitle_asr_language || 'auto'; }
    setVideoNumberValue(els.subtitleAsrTimeout, s.subtitle_asr_timeout_sec, 300, 30, 1800);
    setVideoNumberValue(els.subtitleAsrChunk, s.subtitle_asr_chunk_sec, 600, 60, 1800);
    setVideoNumberValue(els.subtitleAsrMaxAudioMb, s.subtitle_asr_max_audio_mb, 24, 1, 100);
    if (els.subtitleTranslateEnabled) { els.subtitleTranslateEnabled.checked = s.subtitle_translate_enabled === true; }
    if (els.subtitleLlmBaseUrl) { els.subtitleLlmBaseUrl.value = s.subtitle_llm_base_url || ''; }
    if (els.subtitleLlmModel) { els.subtitleLlmModel.value = s.subtitle_llm_model || ''; }
    if (els.subtitleTranslateTarget) { els.subtitleTranslateTarget.value = s.subtitle_translate_target || 'zh'; }
    setVideoNumberValue(els.subtitleTranslateTimeout, s.subtitle_translate_timeout_sec, 120, 30, 900);
    setVideoNumberValue(els.subtitleTranslateBatchSize, s.subtitle_translate_batch_size, 20, 1, 40);
    if (els.subtitleAsrApiKey) { els.subtitleAsrApiKey.value = ''; }
    if (els.subtitleLlmApiKey) { els.subtitleLlmApiKey.value = ''; }
    if (els.subtitleClearAsrKey) { els.subtitleClearAsrKey.checked = false; }
    if (els.subtitleClearLlmKey) { els.subtitleClearLlmKey.checked = false; }
    if (els.subtitleAsrApiKey) { els.subtitleAsrApiKey.disabled = false; }
    if (els.subtitleLlmApiKey) { els.subtitleLlmApiKey.disabled = false; }
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
      settings.subtitle_asr_language = ['auto', 'zh', 'en', 'ja', 'ko'].indexOf(els.subtitleAsrLanguage.value) !== -1
        ? els.subtitleAsrLanguage.value : 'auto';
    }
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
      settings.subtitle_translate_target = ['zh', 'en', 'ja', 'ko'].indexOf(els.subtitleTranslateTarget.value) !== -1
        ? els.subtitleTranslateTarget.value : 'zh';
    }
    var translateTimeout = readSubtitleInt(els.subtitleTranslateTimeout, 30, 900, '翻译超时', errors, 'subtitleTranslateTimeout');
    if (translateTimeout !== null) { settings.subtitle_translate_timeout_sec = translateTimeout; }
    var batchSize = readSubtitleInt(els.subtitleTranslateBatchSize, 1, 40, '翻译批大小', errors, 'subtitleTranslateBatchSize');
    if (batchSize !== null) { settings.subtitle_translate_batch_size = batchSize; }
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
    updateKeepModeUi();
    clearOptionsErrors();
    updateScanSummary();
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
      setText(els.optionsError, msg);
      show(els.optionsError);
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
    if (status === 'paused') { stageText += '（已暂停）'; }
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

  async function loadExactPartition(reset) {
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
    var query = '?offset=' + offset + '&limit=' + results.limit +
      '&q=' + encodeURIComponent(results.q) + '&sort=' + encodeURIComponent(results.sort);
    if (results.scanMode === 'similar' || videoScanEnabledForResults()) { query += '&match_type=exact'; }
    try {
      var res = await api('/api/scans/' + encodePath(state.scan.id) + '/groups' + query, { timeout: 20000 });
      if (seq !== results.seq) { return; }
      var groups = Array.isArray(res.groups) ? res.groups : [];
      results.total = Number(res.exact_total !== undefined ? res.exact_total : res.total) || 0;
      results.exactTotal = results.total;
      if (typeof res.similar_total === 'number') { results.similarTotal = res.similar_total; }
      results.offset = res.offset === undefined ? offset : Number(res.offset);
      results.groups = reset ? groups : results.groups.concat(groups);
      results.loaded = true;
      results.loading = false;
      renderResults();
      if (reset) { loadScanStats(); }
    } catch (err) {
      if (seq !== results.seq) { return; }
      results.loading = false;
      setHidden(els.resultsLoading, true);
      setHidden(els.resultsError, false);
      setText(els.resultsError, errorMessage(err, '加载精准组失败，请稍后重试。'));
      updateSelectionSummary();
    }
  }

  async function loadSimilarPartition(reset) {
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
    var query = '?offset=' + offset + '&limit=' + results.limit + '&match_type=similar&sort=similarity_desc' +
      '&q=' + encodeURIComponent(results.q);
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

  async function loadVideoPartition(reset) {
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
    var query = '?offset=' + offset + '&limit=' + results.limit + '&match_type=video&sort=similarity_desc' +
      '&q=' + encodeURIComponent(results.q);
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

    var side = el('div', { className: 'group-head-side' }, [
      el('div', { className: 'group-badges' }, badges),
      groupCheckLabel
    ]);
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
    var row = el('div', {
      className: 'file-row' + (isKeeper ? ' is-keeper' : ''),
      dataset: { fileId: file.file_id }
    });

    var checkbox = el('input', {
      attrs: {
        type: 'checkbox',
        disabled: manual || isKeeper,
        'aria-label': isKeeper ? '保留项，不可选择删除' : (manual ? '本组禁止删除' : '选择删除 ' + file.path)
      }
    });
    checkbox.disabled = manual || isKeeper;
    checkbox.checked = !manual && !isKeeper && isFileSelected(group, file);
    if (manual) { checkbox.title = '该组需人工确认，禁止删除'; }
    else if (isKeeper) { checkbox.title = '保留项不可删除'; }
    if (!manual && !isKeeper) {
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
    return el('div', { className: 'file-sub' }, [
      el('span', { text: fmtBytes(file.size) }),
      el('span', { className: 'file-hash' }, [
        el('span', { className: 'muted', text: String(group.algo || 'sha256').toUpperCase() }),
        hashCode,
        copyBtn
      ])
    ]);
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
    var selectable = (group.files || []).filter(function (file) { return file.file_id !== group.keep_file_id; });
    var selectedCount = selectable.filter(function (file) { return isFileSelected(group, file); }).length;
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
    if (!batchAllowed) { badges.push(el('span', { className: 'badge badge-gray', text: '禁止批量删除' })); }
    var asrUsed = group.video_asr_used === true || group.asr_used === true;
    if (asrUsed) {
      badges.push(el('span', {
        className: 'badge asr-result-badge',
        attrs: { title: 'ASR 文本签名参与评分；转录内容不展示、不回传' },
        text: 'ASR辅助'
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
    var labels = { audio: '提取音频', asr: '云端语音识别', translate: '翻译字幕', write: '写入字幕文件' };
    return labels[stage] || stage || '处理中';
  }

  function subtitleJobStatusLabel(status) {
    var labels = {
      queued: '排队中', running: '识别中', translating: '翻译中',
      done: '已完成', failed: '失败', cancelled: '已取消'
    };
    return labels[status] || status || '未知';
  }

  function subtitleJobStatusClass(status) {
    if (status === 'done') { return 'badge-green'; }
    if (status === 'failed') { return 'badge-red'; }
    if (status === 'cancelled') { return 'badge-gray'; }
    if (status === 'queued') { return 'badge-gray'; }
    return 'badge-blue';
  }

  function isTerminalSubtitleStatus(status) {
    return status === 'done' || status === 'failed' || status === 'cancelled';
  }

  function isSafeSubtitleFileName(name) {
    if (typeof name !== 'string' || !name || name.length > 160) { return false; }
    if (name.indexOf('/') !== -1 || name.indexOf('\\') !== -1) { return false; }
    return /\.(srt|vtt)$/i.test(name);
  }

  function openSubtitleCloudModal(file) {
    var sub = subtitleCapability();
    var cloud = {
      job: null, timer: null, abort: null, polling: false, pollErrors: 0,
      running: false, cancelling: false, format: 'srt', modal: null, progressUi: null
    };
    var languageSelect = el('select', { attrs: { id: 'subtitleLanguage', 'aria-label': '识别语言' } }, [
      el('option', { attrs: { value: 'auto' }, text: '自动检测' }),
      el('option', { attrs: { value: 'zh' }, text: '中文' }),
      el('option', { attrs: { value: 'en' }, text: '英文' }),
      el('option', { attrs: { value: 'ja' }, text: '日文' }),
      el('option', { attrs: { value: 'ko' }, text: '韩文' })
    ]);
    languageSelect.value = sub.asrLanguage || 'auto';
    var translateCheck = el('input', { attrs: { type: 'checkbox' } });
    if (!sub.llmKeySet) { translateCheck.disabled = true; }
    if (sub.translateEnabled && sub.llmKeySet) { translateCheck.checked = true; }
    var targetSelect = el('select', { attrs: { 'aria-label': '翻译目标语言' } }, [
      el('option', { attrs: { value: 'zh' }, text: '中文' }),
      el('option', { attrs: { value: 'en' }, text: '英文' }),
      el('option', { attrs: { value: 'ja' }, text: '日文' }),
      el('option', { attrs: { value: 'ko' }, text: '韩文' })
    ]);
    targetSelect.value = sub.targetLang || 'zh';
    targetSelect.disabled = !translateCheck.checked;
    translateCheck.addEventListener('change', function () {
      targetSelect.disabled = !translateCheck.checked;
    });
    var formatSelect = el('select', { attrs: { 'aria-label': '首选下载格式' } }, [
      el('option', { attrs: { value: 'srt' }, text: 'SRT' }),
      el('option', { attrs: { value: 'vtt' }, text: 'VTT' })
    ]);
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
          el('p', { className: 'hint', text: '任务会生成 SRT/VTT；这里决定完成后优先展示的格式。' })
        ])
      ]),
      el('label', { className: 'modal-check-row' }, [
        confirmCheck,
        el('span', { text: '我了解音频将上传到所配置的第三方服务，并确认有权处理该视频内容。' })
      ]),
      el('p', {
        className: 'subtitle-form-note',
        text: '生成后可查看进度、取消任务并下载字幕；任务执行期间弹窗不可关闭，请使用“取消任务”终止。'
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
        cloud.running = false;
        stopSubtitlePolling(cloud);
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
          text: '音频只临时存在于服务端；任务完成/失败/取消后自动清理。字幕文件仅保存在 NAS 任务目录，下载需要当前登录会话。'
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
      modal.setBody(ui.body);
      modal.setFooter([
        el('span', { className: 'footer-left', text: '任务进行中，关闭按钮已禁用；可随时取消。' }),
        ui.cancelTaskBtn
      ]);
      if (activeModal) { activeModal.dismissible = false; }
      modal.setBusy(true);
      updateSubtitleProgress(job);
    }

    function renderSubtitleFiles(job) {
      var ui = cloud.progressUi;
      var files = Array.isArray(job.files) ? job.files.filter(function (item) {
        return item && isSafeSubtitleFileName(item.name);
      }) : [];
      clear(ui.filesWrap);
      ui.filesWrap.hidden = false;
      if (!files.length) {
        ui.filesWrap.appendChild(el('p', { className: 'subtitle-empty', text: '任务已完成，但没有可下载的字幕文件。' }));
        return;
      }
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
          cloud.modal.setFooter([
            el('span', { className: 'footer-left', text: status === 'done' ? '字幕生成完成。' : '任务已结束。' }),
            el('button', {
              className: 'btn btn-primary', attrs: { type: 'button' }, text: '关闭',
              on: { click: function () { closeModal(true); } }
            })
          ]);
        }
        if (activeModal) { activeModal.dismissible = true; }
        if (status === 'done') {
          toast('字幕生成完成，可下载 SRT/VTT 文件。', 'success', 5000);
        } else if (status === 'failed') {
          toast('字幕任务失败，请查看弹窗中的错误信息。', 'error', 6000);
        } else {
          toast('字幕任务已取消。', 'info');
        }
      } else {
        ui.cancelTaskBtn.disabled = false;
        setText(ui.cancelTaskBtn, '取消任务');
        if (activeModal) { activeModal.dismissible = false; }
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
          if (activeModal) { activeModal.dismissible = true; }
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
    return (group.files || []).filter(function (file) { return file.file_id !== keeperId; });
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
    var selectable = (group.files || []).filter(function (file) { return file.file_id !== group.keep_file_id; });
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
      var res = await fetch('/api/preview?path=' + encodePath(file.path), {
        credentials: 'same-origin',
        signal: abortController.signal,
        headers: { Accept: '*/*' }
      });
      if (res.status === 401) {
        handleUnauthorized();
        throw new ApiError(401, 'AUTH_REQUIRED', ERROR_FALLBACK.AUTH_REQUIRED);
      }
      if (!res.ok) {
        var message = '预览失败（HTTP ' + res.status + '）。';
        try {
          var payload = await res.json();
          if (payload && payload.error && payload.error.message) { message = payload.error.message; }
        } catch (e2) { /* 非 JSON 错误体 */ }
        throw new ApiError(res.status, 'PREVIEW_FAILED', message);
      }
      var contentType = (res.headers.get('Content-Type') || '').toLowerCase();
      var truncated = res.headers.get('X-Preview-Truncated') === '1';
      var stage;
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
        var audioBlob = await res.blob();
        var audioUrl = URL.createObjectURL(audioBlob);
        objectUrls.push(audioUrl);
        stage = el('div', { className: 'preview-stage' }, [
          el('audio', { attrs: { src: audioUrl, controls: true, preload: 'metadata' } })
        ]);
      } else if (contentType.indexOf('video/') === 0) {
        var videoBlob = await res.blob();
        var videoUrl = URL.createObjectURL(videoBlob);
        objectUrls.push(videoUrl);
        stage = el('div', { className: 'preview-stage' }, [
          el('video', { attrs: { src: videoUrl, controls: true, preload: 'metadata' } })
        ]);
      } else {
        throw new ApiError(415, 'PREVIEW_UNSUPPORTED',
          '该文件类型暂不支持预览（' + (contentType || '未知类型') + '）。可点击“在新窗口打开”尝试。');
      }
      if (!ensureOpen()) { return; }
      modal.setBody(el('div', { className: 'preview-body' }, [
        stage,
        el('p', { className: 'preview-path', text: file.path })
      ]));
      var focusable = $('img, video, audio, iframe', modal.modal);
      if (!focusable) { closeBtn.focus(); }
    } catch (err) {
      if (err && err.name === 'AbortError') { return; }
      showError(err);
    }
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
    addKv('删除方式', permanent ? '永久删除（不可恢复）' : '移动到回收站', permanent ? 'is-permanent' : '');
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
          '移动到回收站（推荐，可在 DSM 回收站中恢复）',
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
        : '即将把以下文件移动到回收站，可在 DSM 回收站中恢复。'
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
    addKv('删除方式', permanent ? '永久删除（不可恢复）' : '移动到回收站', permanent ? 'is-permanent' : '');
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
    if (activeModal) { activeModal.dismissible = false; }
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
        if (activeModal) { activeModal.dismissible = true; }
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
        if (activeModal) { activeModal.dismissible = true; }
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
      toast(message, 'success', 6000);
    } else if (status === 'partial') {
      message = '删除部分完成：成功 ' + fmtInt(done) + ' 个，失败 ' + fmtInt(failed) + ' 个。请查看逐文件结果。';
      toast(message, 'error', 7000);
    } else if (status === 'stopped') {
      message = '删除任务已停止：成功 ' + fmtInt(done) + ' 个，失败 ' + fmtInt(failed) + ' 个。';
      toast(message, 'info', 6000);
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

  async function refreshScanAfterDelete() {
    if (!state.scan.id) { return; }
    var scanId = state.scan.id;
    try {
      var res = await api('/api/scans/' + encodePath(scanId), { timeout: 15000 });
      if (state.scan.id !== scanId) { return; }
      if (res && res.scan) {
        state.scan.data = res.scan;
        state.results.scanMode = detectScanMode(res.scan);
        renderScanProgress(res.scan);
      }
      loadResults(true);
    } catch (e) {
      loadResults(true);
    }
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
    var viewBtn = el('button', {
      className: 'btn btn-ghost btn-sm', attrs: { type: 'button' },
      text: canViewProgress ? '查看进度' : '查看结果',
      on: { click: function () { openHistoryScan(scan); } }
    });
    viewBtn.disabled = !(canViewProgress || canViewResults);
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
      startScanPolling(scanId);
      toast('已打开正在运行的扫描任务进度。', 'info');
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

  function bindHistoryEvents() {
    els.historyRefreshBtn.addEventListener('click', loadHistory);
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

  function renderLogs(data) {
    setHidden(els.logsLoading, true);
    var logs = Array.isArray(data.logs) ? data.logs : [];
    var audit = Array.isArray(data.audit) ? data.audit : [];

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
      els.auditList.appendChild(el('tr', {}, [
        el('td', { text: fmtDateTime(entry.ts) }),
        el('td', {}, [actionCell]),
        el('td', { text: entry.user || '—' }),
        el('td', {}, [el('span', { className: 'audit-detail', attrs: { title: detailText || '' }, text: detailText || '—' })])
      ]));
    });

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
      var metaText = '类型 ' + (LOG_KIND_LABEL[log.kind] || log.kind || '未知') +
        ' · ' + fmtBytes(log.size) + ' · ' + fmtDateTime(log.modified_at);
      if (log.scan_id) { metaText += ' · ' + log.scan_id; }
      els.logFilesList.appendChild(el('li', { className: 'log-file-item' }, [
        el('span', { className: 'log-file-icon' }, [icon('file')]),
        el('div', { className: 'log-file-main' }, [
          el('div', { className: 'log-name', text: name || '—' }),
          el('div', { className: 'log-meta', text: metaText })
        ]),
        downloadLink
      ]));
    });
  }

  function bindLogsEvents() {
    els.logsRefreshBtn.addEventListener('click', loadLogs);
    els.logLimitSelect.addEventListener('change', function () {
      var value = parseInt(els.logLimitSelect.value, 10);
      state.logsLimit = value > 0 ? value : 50;
      loadLogs();
    });
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
    videoAsrCommand: { input: 'setVideoAsrCommand', error: 'setVideoAsrCommandError' },
    videoAsrWeight: { input: 'setVideoAsrWeight', error: 'setVideoAsrWeightError' },
    videoAsrTimeout: { input: 'setVideoAsrTimeout', error: 'setVideoAsrTimeoutError' },
    videoAsrMaxSec: { input: 'setVideoAsrMaxSec', error: 'setVideoAsrMaxSecError' },
    subtitleAsrBaseUrl: { input: 'subtitleAsrBaseUrl', error: 'subtitleAsrBaseUrlError' },
    subtitleAsrModel: { input: 'subtitleAsrModel', error: 'subtitleAsrModelError' },
    subtitleAsrTimeout: { input: 'subtitleAsrTimeout', error: 'subtitleAsrTimeoutError' },
    subtitleAsrChunk: { input: 'subtitleAsrChunk', error: 'subtitleAsrChunkError' },
    subtitleAsrMaxAudioMb: { input: 'subtitleAsrMaxAudioMb', error: 'subtitleAsrMaxAudioMbError' },
    subtitleLlmBaseUrl: { input: 'subtitleLlmBaseUrl', error: 'subtitleLlmBaseUrlError' },
    subtitleLlmModel: { input: 'subtitleLlmModel', error: 'subtitleLlmModelError' },
    subtitleTranslateTimeout: { input: 'subtitleTranslateTimeout', error: 'subtitleTranslateTimeoutError' },
    subtitleTranslateBatchSize: { input: 'subtitleTranslateBatchSize', error: 'subtitleTranslateBatchSizeError' }
  };

  function bytesToMbValue(bytes) {
    var n = Number(bytes) || 0;
    if (n <= 0) { return '0'; }
    var mb = n / 1048576;
    return String(Math.round(mb * 100) / 100);
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
    } catch (err) {
      setHidden(els.settingsLoading, true);
      setText(els.settingsLoadError, errorMessage(err, '加载设置失败，请稍后重试。'));
      setHidden(els.settingsLoadError, false);
      if (state.settings) { fillSettingsForm(state.settings); }
    }
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
    } catch (err) {
      setText(els.settingsError, errorMessage(err, '保存设置失败，请稍后重试。'));
      show(els.settingsError);
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
    els.settingsForm.addEventListener('input', clearSettingsErrors);
    els.settingsForm.addEventListener('change', clearSettingsErrors);
    if (els.setSimExtList) { els.setSimExtList.addEventListener('change', clearSettingsErrors); }
    if (els.setVideoEnabled) {
      els.setVideoEnabled.addEventListener('change', function () {
        updateSettingsVideoFieldsState();
        updateAsrFieldsState();
        clearSettingsErrors();
      });
    }
    if (els.setVideoExtList) { els.setVideoExtList.addEventListener('change', clearSettingsErrors); }
    if (els.setVideoAsrEnabled) {
      els.setVideoAsrEnabled.addEventListener('change', function () {
        updateAsrFieldsState();
        clearSettingsErrors();
      });
    }
    if (els.setVideoAsrSection) {
      els.setVideoAsrSection.addEventListener('change', clearSettingsErrors);
    }
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
  }

  // ==================== 18. 启动 ====================
  function cacheDom() {
    IDS.forEach(function (id) {
      var node = document.getElementById(id);
      if (node) { els[id] = node; }
    });
  }

  function bindGlobalEvents() {
    document.addEventListener('keydown', trapModalKeys);
    document.addEventListener('focusin', function (event) {
      if (!activeModal) { return; }
      if (activeModal.root.contains(event.target)) { return; }
      if (els.toastRoot && els.toastRoot.contains(event.target)) { return; }
      var first = getFocusable(activeModal.modal)[0];
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
    state.view = initialViewFromHash();
    initSession();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
