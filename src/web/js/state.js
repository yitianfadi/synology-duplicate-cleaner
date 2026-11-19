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
  // v0053：`#/view?k=v` 里的查询参数（下载页书签脚本用）
  hashQuery: {},
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
  subtitleJobs: { list: [], timer: null, loading: false, lastError: '', modal: null, snapshot: null },
  // v0051：下载中心（列表/队列快照/轮询）
  download: {
    jobs: [], snapshot: null, timer: null, loading: false, loaded: false,
    lastError: '', submitting: false,
    // v0052：网页解析（候选地址 + 服务端暂存的登录态 token）
    parseCandidates: [], parseSession: '', parseBusy: false, parseSeq: 0,
    // v0053：空间看板
    storage: null,
    // v0054：订阅列表
    subs: []
  },
  // v0056：格式转换（选择/预设/队列/轮询）
  convert: {
    caps: null, capsLoading: false, candidates: [], selected: new Map(),
    snapshot: null, timer: null, loading: false, loaded: false,
    lastError: '', submitting: false, enumerating: false,
    // v0061：源文件后缀多选（exts 为当前勾选集合，savingExt 防抖持久化标记）
    exts: new Set(), extsTouched: false, extSaving: null
  },
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
    videoSeq: 0, videoSelected: new Map(), videoSelectAllMode: false,
    // v0043：只看本次新增/内容变化的重复组
    newOnly: false, newTotal: 0
  },
  // 删除流程
  deleteFlow: { plan: null, job: null, timer: null, abort: null, running: false, busy: false, modal: null, recycle: true, override: null },
  picker: null,
  // app.js 注入的目录选择器（供独立模块如字幕页复用同一个扫描范围选择器）
  // 签名：openSubtitleDirPicker({initial, onConfirm})
  openSubtitleDirPicker: null,
  // v0039：字幕页批次总览里的“去字幕任务”入口（由 app.js 注入）
  openSubtitleJobs: null,
  // v0057：历史 / 删除任务面板打开某次扫描（由 app.js 注入，签名 openScan(scan)）
  openScan: null,
  // v0051：下载页目录选择器（签名 openDownloadDirPicker({initial, onConfirm})）
  openDownloadDirPicker: null,
  // v0056：转换页目录选择器（源目录 / 输出目录共用）
  openConvertDirPicker: null,
  preview: { urls: [], abort: null },
  historyCache: null,
  // v0057：各视图的内嵌标签页（扫描：scan/history/deletion；字幕：submit/jobs；设置：general/logs）
  viewTab: { scan: 'scan', subtitles: 'submit', settings: 'general' },
  deletionJobs: { list: [], loading: false, loaded: false, lastError: '' },
  diagnostics: { data: null, loading: false, loaded: false, busy: false },
  logsLoaded: false,
  settingsLoaded: false,
  logsLimit: 50,
  logsKeepDays: 30,
  auditSelection: new Set()
};

export var els = {};
export var IDS = ('loginView loginForm loginUsername loginPassword loginOtp loginSubmit loginError app riskBanner envBadge userChip userName ' +
  'logoutBtn main view-scan view-subtitles view-settings navSettingsBtn scanHeading subtitlesHeading settingsHeading ' +
  'rootsList rootsCount rootsError pickRootsBtn clearRootsBtn excludeChips excludeCount pickExcludesBtn optionsForm resetOptionsBtn ' +
  'hashAlgo keepMode keepModeHint pinnedField pinnedValue pinnedError pickPinnedBtn clearPinnedBtn minSize minUnit minSizeError maxSize maxUnit ' +
  'maxSizeError includeExt includeExtError excludeExt excludeExtError ignoreCaseExt skipEmpty skipHidden recycleDefault ' +
  'quickSampleKb quickSampleError concurrency concurrencyError hashRate hashRateError maxFiles maxFilesError allowCtimeFallback ' +
  'createRecycleDir skipSymlinks optionsError scanSummary startScanBtn progressPanel progressStatus progressScanId progressStage ' +
  'progressPct progressBar progressFill progressMessage counterFilesSeen counterFilesScanned counterCandidates counterGroups ' +
  'counterDuplicateFiles counterReclaimable counterRate counterErrors progressPath progressElapsed pauseBtn resumeBtn stopBtn ' +
  'viewResultsBtn resultsSection resultsSubtitle refreshResultsBtn resultsSummary searchGroups sortGroups resultsCountLabel ' +
  'selectAllBtn clearSelectionBtn csvLink deleteSelectedBtn selectionSummary resultsLoading resultsError resultsEmpty groupsList ' +
  'loadMoreBtn loadMoreStatus historyRefreshBtn historyLoading historyError historyEmpty historyList logLimitSelect logsKeepDays auditSelectAll auditPruneBtn logFilesPruneBtn auditDeleteSelectedBtn logsRefreshBtn ' +
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
  'setVideoCenterCrop setVideoOffsetAlign setVideoAudioSignature setVideoAudioWeight ' +
  'setVideoAudioWeightError ' +
  'videoAsrSection videoAsrStatusBadge videoAsrStatusHint videoAsrEnabled videoAsrFields videoAsrCommand ' +
  'videoAsrCommandError videoAsrWeight videoAsrWeightError videoAsrTimeout videoAsrTimeoutError ' +
  'videoAsrMaxSec videoAsrMaxSecError setVideoAsrStatusBadge setVideoAsrStatusHint ' +
  'setVideoAsrEnabled setVideoAsrFields setVideoAsrCommand setVideoAsrCommandError setVideoAsrWeight ' +
  'setVideoAsrWeightError setVideoAsrTimeout setVideoAsrTimeoutError setVideoAsrMaxSec setVideoAsrMaxSecError ' +
  'settingsSubtitleDetails subtitleStatusBadge subtitleStatusHint subtitleFields subtitleEnabled subtitleAsrBaseUrl ' +
  'settingsOverview settingsMediaGroup settingsMediaSummary settingsSecurityGroup settingsSecuritySummary ' +
  'settingsScanGroup settingsScanSummary settingsSimilaritySummary settingsVideoSummary ' +
  'settingsSubtitleSummary settingsStorageGroup settingsStorageSummary settingsStatusSummary ' +
  'batchSubtitleBtn subtitleJobsPanel subtitleJobsHeading subtitleJobsStatus subtitleJobsRefreshBtn ' +
  'subtitleJobsLoading subtitleJobsError subtitleJobsEmpty subtitleJobsList ' +
  'subtitleAsrBaseUrlError subtitleAsrModel subtitleAsrModelError subtitleAsrApiKey subtitleAsrApiKeyError ' +
  'subtitleAsrKeySetBadge subtitleClearAsrKey subtitleAsrLanguage subtitleAsrTimeout subtitleAsrTimeoutError ' +
  'subtitleAsrChunk subtitleAsrChunkError subtitleAsrMaxAudioMb subtitleAsrMaxAudioMbError ' +
  'subtitleAudioSource subtitleWriteVtt subtitleAudioPrefetch ' +
  'subtitleLlmBaseUrl subtitleLlmBaseUrlError subtitleLlmModel subtitleLlmModelError subtitleLlmApiKey ' +
  'subtitleLlmApiKeyError subtitleLlmKeySetBadge subtitleClearLlmKey subtitleTranslateTarget subtitleTranslateTimeout ' +
  'subtitleTranslateTimeoutError subtitleTranslateBatchSize subtitleTranslateBatchSizeError ' +
  'subtitleLlmDisableThinking ' +
  'subtitleAsrModelsBtn subtitleAsrModelPicker subtitleAsrModelsHint subtitleAsrTestBtn subtitleAsrTestResult ' +
  'subtitleLlmModelsBtn subtitleLlmModelPicker subtitleLlmModelsHint subtitleLlmTestBtn subtitleLlmTestResult ' +
  'setSubtitleOutputMode setSubtitleSkipExisting setSubtitleJobLimit setSubtitleJobLimitError ' +
  'setSubtitleAutoRetry setSubtitleAutoRetryMax setSubtitleAutoRetryMaxError ' +
  'setSubtitleNoSpeechSkip setSubtitleMaxLoad setSubtitleMaxLoadError ' +
  'setSubtitleDirPriorities setSubtitleDirPrioritiesError ' +
  'setSubtitleAudioTrack setSubtitleAudioTrackLanguage ' +
  'setSubtitleWatchEnabled setSubtitleWatchDirs setSubtitleWatchDirsError ' +
  'setSubtitleWatchInterval setSubtitleWatchIntervalError ' +
  'subtitleWatchStatus subtitleWatchScanBtn ' +
  'subtitleFailuresPanel subtitleFailuresHeading subtitleFailuresCount ' +
  'subtitleFailuresRefreshBtn subtitleFailureFilter ' +
  'subtitleFailureIncludeSkipped subtitleFailuresExportBtn ' +
  'subtitleFailuresRetryBtn subtitleFailuresRetryForceBtn ' +
  'subtitleFailuresHint subtitleFailuresLoading subtitleFailuresEmpty ' +
  'subtitleFailuresList ' +
  'subtitleFilterBar subtitleJobSearch subtitleJobStatusFilter ' +
  'subtitleJobSuffix subtitleJobDir subtitleJobCategory ' +
  'subtitleJobFilterBtn subtitleJobFilterResetBtn subtitleSelectAll ' +
  'subtitleBulkCount subtitleBulkCancelBtn subtitleBulkRetryBtn ' +
  'subtitleBulkPriorityBtn ' +
  'logSearchPanel logSearchHeading logSearchStatus logSearchQuery ' +
  'logSearchJobId logSearchBtn logSearchClearBtn logSearchExportBtn ' +
  'logSearchLoading logSearchEmpty logSearchResults ' +
  'settingsPermissionGroup settingsPermissionSummary permissionCheckBtn ' +
  'permissionReportStatus permissionLoading permissionGuidance ' +
  'permissionReportList ' +
  'setSubtitleConcurrency setSubtitleConcurrencyError ' +
  'setSubtitleTranslateOutputs setSubtitleTranslateOutputsError ' +
  'subtitleGroupCurrent subtitleJobsFilter subtitleJobsActive ' +
  'tabScanMain tabScanHistory tabScanDeletion ' +
  'tabPanelScan tabPanelHistory tabPanelDeletion ' +
  'tabSubtitleSubmit tabSubtitleJobs ' +
  'tabPanelSubtitleSubmit tabPanelSubtitleJobs ' +
  'tabSettingsGeneral tabSettingsLogs ' +
  'tabPanelSettingsGeneral tabPanelSettingsLogs ' +
  'settingsDiagnosticsGroup settingsDiagnosticsSummary ' +
  'deletionJobsPanel deletionJobsHeading deletionJobsStatus deletionJobsRefreshBtn ' +
  'deletionJobsLoading deletionJobsError deletionJobsEmpty deletionJobsList ' +
  'deletionJobsTable deletionJobsTableWrap ' +
  'storagePanel storagePanelHeading storageStatus storageRefreshBtn storageLoading storageError ' +
  'storageContent storageSummary storageDbList storageTablesList storageCachesList ' +
  'storageMediaList storageRuntimeList storageKeysList storageOtherSection storageOtherList ' +
  'storageBackupBtn storageMaintenanceBtn storageMaintenanceVacuum ' +
  'storageActionLoading storageActionResult ' +
  'keepRulesField keepPrimaryRule keepPathPriority keepNamePriority keepPreferSubtitle ' +
  'keepPreferMultiAudio keepRulesHint keepRulesError ' +
  'excludeNameRegex excludeNameRegexError excludeNameGlob excludeNameGlobError ' +
  'ignoreFileEnabled incrementalScan mediaSidecarMode ' +
  'newOnlyLabel newOnlyToggle ' +
  'setKeepPrimaryRule setKeepPathPriority setKeepNamePriority setKeepPreferSubtitle ' +
  'setKeepPreferMultiAudio setIncrementalScan setExcludeNameRegex setExcludeNameRegexError ' +
  'setExcludeNameGlob setExcludeNameGlobError setIgnoreFileEnabled setMediaSidecarMode ' +
  'cacheStatsText clearHashCacheBtn cacheClearResult ' +
  'setProtectEnabled setProtectExtensions setProtectExtensionsError ' +
  'setProtectDirs setProtectDirsError setProtectNameRegex setProtectNameRegexError ' +
  'setProtectSidecarFiles ' +
  'setMergeEnabled setDeleteRecycleMode ' +
  'settingsAutomationSummary schedulerStatusText ' +
  'setScheduledScanEnabled setScheduledScanRules setScheduledScanRoots ' +
  'setScheduledScanMaxLoad setScheduledScanRulesError ' +
  'setScheduledScanRootsError ' +
  'setNotifyEnabled setNotifyChannelWebhook ' +
  'setNotifyChannelTelegram setNotifyEventScanComplete ' +
  'setNotifyEventScanFailed setNotifyEventSubtitleBatchDone ' +
  'setNotifyEventFailureBurst setNotifyEventFreedThreshold ' +
  'setNotifyWebhookUrl setNotifyTelegramToken setNotifyTelegramChatId ' +
  'setNotifyFailThreshold setNotifyFreedGb setNotifyTimeout ' +
  'notifyChannelsHint notifyTestWebhook notifyTestTelegram ' +
  'notifyTestResult ' +
  'trashList trashLoading trashError trashEmpty ' +
  'trashRefreshBtn trashRecycleMode trashMergeList trashMergeLoading ' +
  'trashMergeEmpty trashMergeCount ' +
  'view-download downloadHeading downloadUrls ' +
  'downloadDirPath downloadDirPickBtn downloadMaxHeight downloadSubdirMode ' +
  'downloadOverwrite downloadKeepTemp downloadSubmitBtn downloadRefreshBtn ' +
  'downloadSubmitHint downloadSubmitError downloadQueueHint downloadEmpty ' +
  'downloadJobsList downloadRecentEmpty downloadListError ' +
  'downloadConcurrencyHint downloadAdvancedDetails ' +
  'settingsDownloadDetails settingsDownloadSummary ' +
  'setDownloadOutputDir setDownloadOutputDirError ' +
  'setDownloadDirPickBtn setDownloadSubdirMode setDownloadSubdirTemplate ' +
  'setDownloadSubdirTemplateError setDownloadConcurrency ' +
  'setDownloadConcurrencyError setDownloadSegmentConcurrency ' +
  'setDownloadSegmentConcurrencyError setDownloadRetry setDownloadRetryError ' +
  'setDownloadTimeout setDownloadTimeoutError setDownloadRateLimit ' +
  'setDownloadRateLimitError setDownloadMaxHeight setDownloadUserAgent ' +
  'setDownloadReferer setDownloadRefererError setDownloadCookie ' +
  'setDownloadCookieError setDownloadCookieSetBadge setDownloadClearCookie ' +
  'setDownloadProxy setDownloadProxyError setDownloadAllowPrivate ' +
  'setDownloadKeepTemp setDownloadMinFreeGb setDownloadMinFreeGbError ' +
  'setDownloadVerifyTolerance setDownloadJobLimit setDownloadJobLimitError ' +
  'setDownloadAutoScan setDownloadAutoSubtitle ' +
  'downloadParseUrl downloadParseBtn downloadParseManual downloadParseHtml ' +
  'downloadParseCurl downloadParseManualBtn downloadParseManualHint ' +
  'downloadParseError downloadParseLoading downloadParseResult ' +
  'downloadParseResultHeading downloadParseSummary downloadParseEmpty ' +
  'downloadCandidatesList downloadApplyCandidatesBtn ' +
  'downloadSelectAllCandidatesBtn downloadClearCandidatesBtn ' +
  'downloadApplyHint downloadStorageHint downloadPauseAllBtn ' +
  'downloadResumeAllBtn downloadRetryFailedBtn downloadExportLink ' +
  'downloadBookmarkletDetails downloadBookmarkletCode ' +
  'downloadBookmarkletCopyBtn downloadBookmarkletHint ' +
  'downloadSubscribeStatus downloadSubscribeUrl downloadSubscribeInterval ' +
  'downloadSubscribeMode downloadSubscribeAddBtn downloadSubscribeError ' +
  'downloadSubscribeHint downloadSubscribeEmpty downloadSubscribeList ' +
  'setDownloadSubscribeEnabled ' +
  'setDownloadSkipExisting setDownloadAutoRetry setDownloadAutoRetryMax ' +
  'setDownloadAutoRetryMaxError setDownloadOffpeakEnabled ' +
  'setDownloadOffpeakStart setDownloadOffpeakStartError ' +
  'setDownloadOffpeakEnd setDownloadOffpeakEndError ' +
  'setDownloadOffpeakRate setDownloadOffpeakRateError ' +
  'setDownloadNameTemplate setDownloadNameTemplateError ' +
  'view-convert convertHeading convertPageStatusBadge convertDirPath ' +
  'convertDirPickBtn convertEnumerateBtn convertRecursive ' +
  'convertExtPicker convertExtHint convertExtAllBtn ' +
  'convertExtNoneBtn convertExtVideoBtn convertExtAudioBtn ' +
  'convertEnumerateError convertEnumerateLoading convertCandidatesPanel ' +
  'convertCandidatesSummary convertCandidatesList convertSelectAllBtn ' +
  'convertSelectNoneBtn convertManualDetails convertPaths ' +
  'convertAddManualBtn convertPresetGroup convertPreset ' +
  'convertPresetDesc convertPresetHint convertQuality convertSpeed ' +
  'convertMaxResolution convertChannels convertOutputMode ' +
  'convertOutputDir convertOutputPickBtn convertOutputDirField ' +
  'convertOutputSubdir convertOutputSubdirField convertNameTemplate ' +
  'convertOverwrite convertCopySubtitles ' +
  'convertDeleteSource convertSubmitBtn convertSubmitHint ' +
  'convertSubmitError convertCancelAllBtn convertRetryFailedBtn ' +
  'convertClearFinishedBtn convertExportLink convertQueueHint ' +
  'convertStorageHint convertListError convertEmpty convertJobsList ' +
  'settingsConvertDetails settingsConvertSummary ' +
  'setConvertOutputDir setConvertOutputDirError ' +
  'setConvertDirPickBtn setConvertOutputMode setConvertOutputSubdir ' +
  'setConvertOutputSubdirError setConvertConcurrency ' +
  'setConvertConcurrencyError setConvertDefaultPreset setConvertQuality ' +
  'setConvertSpeed setConvertMaxResolution setConvertOverwrite ' +
  'setConvertKeepSource setConvertCopySubtitles setConvertNameTemplate ' +
  'setConvertNameTemplateError setConvertJobLimit ' +
  'setConvertJobLimitError setConvertMinFreeGb setConvertMinFreeGbError ' +
  'setConvertKeepTemp setConvertOffpeakEnabled setConvertOffpeakStart ' +
  'setConvertOffpeakStartError setConvertOffpeakEnd ' +
  'setConvertOffpeakEndError setConvertAutoScan setConvertAutoSubtitle ' +
  'view-merge mergeHeading mergePageStatusBadge mergeDirPath mergeDirPickBtn ' +
  'mergeRecursive mergeEnumerateBtn mergeEnumerateSummary mergeEnumerateError ' +
  'mergeCandidatesCard mergeCandidates mergeCandidatesEmpty ' +
  'mergeVideoPaths mergeAudioPaths mergeVideoHint mergeAudioHint ' +
  'mergeOutputMode mergeOutputDir mergeOutputDirPickBtn mergeOutputSubdir ' +
  'mergeContainer mergeMode mergeQuality mergeMaxResolution mergeNameTemplate ' +
  'mergeDeleteSource mergeSubmitBtn mergeSubmitError mergeSubmitHint ' +
  'mergeJobsStatus mergeRefreshBtn mergeListError mergeJobsEmpty mergeJobsList ' +
  'settingsMergeDetails settingsMergeSummary setMergeOutputMode ' +
  'setMergeOutputDir setMergeOutputDirError setMergeDirPickBtn ' +
  'setMergeOutputSubdir setMergeContainer setMergeMode setMergeQuality ' +
  'setMergeMaxResolution setMergeNameTemplate setMergeConcurrency ' +
  'setMergeMinFreeGb setMergeJobLimit setMergeKeepSource ' +
  'footerMeta toastRoot modalRoot').split(' ');
