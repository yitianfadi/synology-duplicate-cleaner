/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 应用状态与 DOM 缓存（ES module）
   由 app.js 第 1 节拆出；state/els/IDS 为共享可变引用，语义与原来一致。
   ========================================================================== */

import { DEFAULT_SIMILARITY_EXTENSIONS, DEFAULT_VIDEO_EXTENSIONS, GROUPS_LIMIT } from './constants.js';

export var state = {
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
  subtitleJobs: { list: [], timer: null, loading: false, lastError: '', modal: null },
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
  // app.js 注入的目录选择器（供独立模块如字幕页复用同一个扫描范围选择器）
  // 签名：openSubtitleDirPicker({initial, onConfirm})
  openSubtitleDirPicker: null,
  preview: { urls: [], abort: null },
  historyCache: null,
  // 任务中心标签页：history / subtitles / deletions / storage
  taskTab: 'history',
  deletionJobs: { list: [], loading: false, loaded: false, lastError: '' },
  diagnostics: { data: null, loading: false, loaded: false, busy: false },
  logsLoaded: false,
  settingsLoaded: false,
  logsLimit: 50
};

export var els = {};
export var IDS = ('loginView loginForm loginUsername loginPassword loginOtp loginSubmit loginError app riskBanner envBadge userChip userName ' +
  'logoutBtn main view-scan view-subtitles view-history view-logs view-settings scanHeading subtitlesHeading historyHeading logsHeading settingsHeading ' +
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
  'videoConcurrency videoConcurrencyError videoHwaccel videoHwaccelError videoHwaccelHint ' +
  'videoFfmpegPath videoFfmpegPathError videoFfmpegHint videoMaxFileMb videoMaxFileError videoTimeout videoTimeoutError ' +
  'videoCache videoExtList videoExtError videoCounters videoCandidates videoCacheHits videoFfmpegUsed videoSkipped ' +
  'xlsxLink videoPartition videoPartitionCount selectAllVideoBtn videoLoading videoError videoEmpty videoGroupsList ' +
  'videoLoadMoreBtn videoLoadMoreStatus settingsVideoDetails setVideoStatusBadge setVideoStatusHint setVideoEnabled ' +
  'setVideoFields setVideoThreshold setVideoThresholdError setVideoSampleFrames setVideoFramesError setVideoMaxInterval ' +
  'setVideoIntervalError setVideoConcurrency setVideoConcurrencyError setVideoHwaccel setVideoHwaccelError setVideoHwaccelHint ' +
  'setVideoFfmpegPath setVideoFfmpegPathError setVideoFfmpegHint setVideoMaxMb setVideoMaxFileError ' +
  'setVideoTimeout setVideoTimeoutError setVideoCache setVideoExtList setVideoExtError ' +
  'videoAsrSection videoAsrStatusBadge videoAsrStatusHint videoAsrEnabled videoAsrFields videoAsrCommand ' +
  'videoAsrCommandError videoAsrWeight videoAsrWeightError videoAsrTimeout videoAsrTimeoutError ' +
  'videoAsrMaxSec videoAsrMaxSecError setVideoAsrStatusBadge setVideoAsrStatusHint ' +
  'setVideoAsrEnabled setVideoAsrFields setVideoAsrCommand setVideoAsrCommandError setVideoAsrWeight ' +
  'setVideoAsrWeightError setVideoAsrTimeout setVideoAsrTimeoutError setVideoAsrMaxSec setVideoAsrMaxSecError ' +
  'settingsSubtitleDetails subtitleStatusBadge subtitleStatusHint subtitleFields subtitleEnabled subtitleAsrBaseUrl ' +
  'settingsOverview settingsScanGroup settingsScanSummary settingsSimilaritySummary settingsVideoSummary ' +
  'settingsSubtitleSummary settingsStorageGroup settingsStorageSummary settingsStatusSummary ' +
  'batchSubtitleBtn subtitleJobsPanel subtitleJobsHeading subtitleJobsStatus subtitleJobsRefreshBtn ' +
  'subtitleJobsLoading subtitleJobsError subtitleJobsEmpty subtitleJobsList ' +
  'subtitleAsrBaseUrlError subtitleAsrModel subtitleAsrModelError subtitleAsrApiKey subtitleAsrApiKeyError ' +
  'subtitleAsrKeySetBadge subtitleClearAsrKey subtitleAsrLanguage subtitleAsrTimeout subtitleAsrTimeoutError ' +
  'subtitleAsrChunk subtitleAsrChunkError subtitleAsrMaxAudioMb subtitleAsrMaxAudioMbError subtitleTranslateEnabled ' +
  'subtitleAudioSource subtitleWriteVtt ' +
  'subtitleLlmBaseUrl subtitleLlmBaseUrlError subtitleLlmModel subtitleLlmModelError subtitleLlmApiKey ' +
  'subtitleLlmApiKeyError subtitleLlmKeySetBadge subtitleClearLlmKey subtitleTranslateTarget subtitleTranslateTimeout ' +
  'subtitleTranslateTimeoutError subtitleTranslateBatchSize subtitleTranslateBatchSizeError ' +
  'subtitleLlmDisableThinking ' +
  'subtitleAsrModelsBtn subtitleAsrModelPicker subtitleAsrModelsHint subtitleAsrTestBtn subtitleAsrTestResult ' +
  'subtitleLlmModelsBtn subtitleLlmModelPicker subtitleLlmModelsHint subtitleLlmTestBtn subtitleLlmTestResult ' +
  'setSubtitleOutputMode setSubtitleSkipExisting setSubtitleJobLimit setSubtitleJobLimitError ' +
  'taskTabHistory taskTabSubtitles taskTabDeletions taskTabStorage ' +
  'taskPanelHistory taskPanelSubtitles taskPanelDeletions taskPanelStorage ' +
  'deletionJobsPanel deletionJobsHeading deletionJobsStatus deletionJobsRefreshBtn ' +
  'deletionJobsLoading deletionJobsError deletionJobsEmpty deletionJobsList ' +
  'deletionJobsTable deletionJobsTableWrap ' +
  'storagePanel storagePanelHeading storageStatus storageRefreshBtn storageLoading storageError ' +
  'storageContent storageSummary storageDbList storageTablesList storageCachesList ' +
  'storageMediaList storageRuntimeList storageKeysList storageOtherSection storageOtherList ' +
  'storageBackupBtn storageMaintenanceBtn storageMaintenanceVacuum ' +
  'storageActionLoading storageActionResult ' +
  'footerMeta toastRoot modalRoot').split(' ');
