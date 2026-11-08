/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 常量定义（ES module）
   由 app.js 第 0 节拆出；数值、文案、语义保持完全一致。ES2019。
   ========================================================================== */

export var CSRF_KEY = 'dc_csrf';
export var POLL_MS = 800;                 // 扫描 / 删除任务轮询间隔（毫秒）
export var GROUPS_LIMIT = 50;             // 结果分页大小
export var TERMINAL_STATUS = ['completed', 'failed', 'stopped', 'interrupted'];
export var ACTIVE_STATUS = ['queued', 'running', 'paused', 'stopping'];
export var VIEWS = ['scan', 'subtitles', 'history', 'settings'];
export var VIEW_HEADINGS = { scan: 'scanHeading', subtitles: 'subtitlesHeading', history: 'historyHeading', settings: 'settingsHeading' };
// v0038：旧书签 #/logs 兼容 → 任务中心「操作日志」标签页
export var LEGACY_VIEW_ALIASES = { logs: 'history' };

export var STATUS_LABEL = {
  queued: '排队中', running: '扫描中', paused: '已暂停', stopping: '正在停止',
  stopped: '已停止', completed: '已完成', failed: '失败', interrupted: '已中断'
};
export var STATUS_BADGE = {
  queued: 'badge-gray', running: 'badge-blue', paused: 'badge-amber',
  stopping: 'badge-amber', stopped: 'badge-gray', completed: 'badge-green', failed: 'badge-red', interrupted: 'badge-amber'
};
export var STAGE_LABEL = {
  enumerate: '枚举文件', size: '按大小分组', sample: '快速指纹采样',
  hash: '计算哈希', similarity: '内容相似度分析', video: '视频视觉分析',
  finalize: '归并结果', done: '完成'
};
export var KEEP_LABEL = {
  latest_created: '最晚创建', earliest_created: '最早创建',
  pinned_path: '指定路径', manual: '需手动选择', tie: '创建时间相同',
  latest_modified_fallback: '按修改时间保留（无创建时间，最晚修改）',
  earliest_modified_fallback: '按修改时间保留（无创建时间，最早修改）',
  mtime_tie: '修改时间相同（按修改时间保留）',
  // v0043：保留规则链（keep_rules）结果
  latest_modified: '修改时间最晚', earliest_modified: '修改时间最早',
  size_largest: '体积最大', size_smallest: '体积最小',
  path_priority: '路径优先级', filename_priority: '文件名优先级',
  has_subtitle_stream: '带内嵌字幕', has_audio_streams: '内嵌音轨更多',
  atime_latest: '访问时间最晚（不可靠）', atime_earliest: '访问时间最早（不可靠）'
};
// v0043：保留规则链（与后端 config.KEEP_RULE_NAMES 对齐）
export var KEEP_RULE_LABEL = {
  created_latest: '创建时间最晚', created_earliest: '创建时间最早',
  mtime_latest: '修改时间最晚', mtime_earliest: '修改时间最早',
  atime_latest: '访问时间最晚（不可靠）', atime_earliest: '访问时间最早（不可靠）',
  size_largest: '体积最大', size_smallest: '体积最小',
  path_priority: '路径优先级', filename_priority: '文件名优先级',
  has_subtitle_stream: '带内嵌字幕优先', has_audio_streams: '内嵌音轨更多优先'
};
export var SIDECAR_MODE_LABEL = { skip: '跳过附属文件', include: '参与判重' };
export var MTIME_FALLBACK_LABEL = '无创建时间 · 按修改时间保留';
export var MTIME_FALLBACK_REASONS = ['latest_modified_fallback', 'earliest_modified_fallback', 'mtime_tie'];
export var KEEP_MODE_LABEL = { latest: '最晚创建', earliest: '最早创建', pinned: '指定路径' };
export var SCAN_MODE_LABEL = { exact: '精准哈希', similar: '内容相似' };
export var SCAN_MODE_BADGE = { exact: 'badge-gray', similar: 'badge-purple' };
export var DEFAULT_SIMILARITY_EXTENSIONS = [
  '.txt', '.md', '.markdown', '.csv', '.json', '.xml', '.html', '.htm', '.log',
  '.pdf', '.docx', '.odt', '.png', '.jpg', '.jpeg', '.webp'
];
export var SIMILARITY_OPTION_KEYS = ['scan_mode', 'similarity_threshold', 'similarity_extensions',
  'similarity_exclude_extensions', 'similarity_concurrency', 'similarity_cache',
  'similarity_max_file_mb', 'similarity_allow_binary'];
export var DEFAULT_VIDEO_EXTENSIONS = ['.mp4', '.mkv', '.mov', '.avi', '.flv', '.wmv', '.webm', '.m4v', '.ts', '.mpg', '.mpeg'];
export var DEFAULT_VIDEO_SETTINGS = {
  ffmpeg_path: '',
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
export var VIDEO_HWACCEL_LABEL = {
  none: '软件解码', auto: '自动', off: '关闭',
  vaapi: 'VA-API', qsv: 'Intel QSV', nvdec: 'NVDEC', videotoolbox: 'VideoToolbox'
};
export var VIDEO_HWACCEL_MODES = ['auto', 'off', 'vaapi', 'qsv', 'nvdec', 'videotoolbox'];
export var SOURCE_INFO = {
  statx: { short: 'statx', title: 'statx：文件系统创建时间（可靠）', cls: 'is-ok' },
  birthtime: { short: 'birthtime', title: 'birthtime：系统提供的创建时间', cls: 'is-ok' },
  ctime: { short: 'ctime', title: 'ctime：文件状态变更时间（降级值，不能当作创建时间，存在风险）', cls: 'is-fallback' },
  unknown: { short: '未知', title: '未知：无法读取文件系统创建时间（不会用 ctime 冒充创建时间）', cls: 'is-unknown' }
};
export var DELETE_STATUS_LABEL = { recycled: '已移入回收站', deleted: '已删除', skipped: '已跳过', failed: '失败' };
export var DELETE_REASON_LABEL = {
  FILE_CHANGED: '文件已被修改', HASH_MISMATCH: '哈希不一致', KEEP_FILE_MISSING: '保留文件不存在',
  PATH_NOT_ALLOWED: '路径不在允许范围', NOT_FOUND: '文件不存在', PERMISSION_DENIED: '没有权限',
  RECYCLE_UNAVAILABLE: '回收站不可用', CROSS_DEVICE: '跨文件系统无法移动', PLAN_EXPIRED: '计划已过期'
};
export var JOB_STATUS_LABEL = {
  running: '删除中', completed: '删除完成', partial: '部分成功',
  failed: '删除失败', stopped: '已停止'
};
export var AUDIT_LABEL = {
  login: '登录', logout: '退出登录', scan_start: '开始扫描', scan_finish: '扫描完成',
  scan_stop: '停止扫描', scan_pause: '暂停扫描', scan_resume: '继续扫描',
  delete_plan: '创建删除计划', delete_start: '开始删除', delete_file: '删除文件',
  delete_finish: '删除完成', settings_update: '更新设置', preview: '预览文件',
  // v0038：日志清理自身也记审计
  audit_delete: '删除审计事件', log_delete: '删除日志文件'
};
export var ERROR_FALLBACK = {
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
  // v0028 目录批量字幕
  INVALID_DIRECTORY: '目录不存在或不是目录。',
  ENUMERATE_LIMIT: '枚举数量超出允许范围。',
  BATCH_EMPTY: '没有可提交的字幕任务。',
  BATCH_TOO_LARGE: '单批视频数超过上限，请分批提交。',
  SOURCE_SRT_MISSING: '视频同目录未找到可翻译的字幕文件。',
  OUTPUT_NOT_WRITABLE: '视频目录不可写，字幕未写回。',
  INTERNAL_ERROR: '服务内部错误，请稍后重试。'
};

// ---------------------------------------------------------------------------
// v0028 字幕任务：状态 / 阶段 / 来源 / 写回警告 / 语言（前端共用一份，避免多套映射漂移）
// ---------------------------------------------------------------------------
export var SUBTITLE_ACTIVE_STATUS = ['queued', 'running', 'translating'];
// v0034：真正的“执行中”（不含排队）。任务中心置顶分区/组卡片当前执行只用这个；
// SUBTITLE_ACTIVE_STATUS 仍用于轮询判断（排队中的任务也要继续轮询）。
export var SUBTITLE_RUNNING_STATUS = ['running', 'translating'];
export var SUBTITLE_STATUS_LABEL = {
  queued: '排队中', running: '识别中', translating: '翻译中',
  paused: '已暂停', done: '已完成', failed: '失败', cancelled: '已取消'
};
export var SUBTITLE_STATUS_BADGE = {
  queued: 'badge-gray', running: 'badge-blue', translating: 'badge-blue',
  paused: 'badge-amber', done: 'badge-green', failed: 'badge-red',
  cancelled: 'badge-gray'
};
export var SUBTITLE_STAGE_LABEL = {
  audio: '提取音频', asr: '云端语音识别', translate: '翻译字幕', write: '写入字幕文件'
};
// v0038：任务步骤时间线（阶段 + 分片/批次明细）
export var SUBTITLE_STEP_LABEL = {
  queue: '排队等待', probe: '媒体探测', asr: '音频提取 + 语音识别',
  translate: '字幕翻译', write: '写入字幕文件'
};
export var SUBTITLE_STEP_STATUS_LABEL = {
  running: '进行中', done: '完成', failed: '失败', paused: '已暂停',
  cancelled: '已取消', skipped: '跳过', unknown: '未知'
};
export var SUBTITLE_SOURCE_LABEL = {
  asr: '云端 ASR 生成', existing_srt: '只翻译已有字幕'
};
export var SUBTITLE_WARNING_LABEL = {
  OUTPUT_NOT_WRITABLE: '视频目录不可写，字幕未写回（任务目录可下载）',
  OUTPUT_WRITE_PARTIAL: '部分字幕文件写回失败（任务目录可下载）',
  SERVER_AUDIO_FALLBACK: '服务器解码失败，已回退 NAS 抽取音频'
};
export var SUBTITLE_OUTPUT_KIND_LABEL = {
  original: '原文', translation: '译文', bilingual: '双语'
};
export var SUBTITLE_LANGUAGES = [
  { value: 'auto', label: '自动检测' },
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英文' },
  { value: 'ja', label: '日文' },
  { value: 'ko', label: '韩文' },
  { value: 'es', label: '西班牙文' },
  { value: 'fr', label: '法文' },
  { value: 'de', label: '德文' },
  { value: 'it', label: '意大利文' },
  { value: 'pt', label: '葡萄牙文' },
  { value: 'ru', label: '俄文' },
  { value: 'ar', label: '阿拉伯文' },
  { value: 'hi', label: '印地文' },
  { value: 'th', label: '泰文' },
  { value: 'vi', label: '越南文' },
  { value: 'id', label: '印尼文' },
  { value: 'ms', label: '马来文' },
  { value: 'tr', label: '土耳其文' },
  { value: 'nl', label: '荷兰文' },
  { value: 'pl', label: '波兰文' },
  { value: 'uk', label: '乌克兰文' },
  { value: 'sv', label: '瑞典文' }
];
export var SUBTITLE_TARGET_LANGUAGES = SUBTITLE_LANGUAGES.slice(1);
// v0036：译文输出组合（与后端 config.SUBTITLE_TRANSLATE_OUTPUTS 一致）
export var SUBTITLE_TRANSLATE_OUTPUTS = [
  { value: 'both', label: '译文 + 双语（默认，两个都写）' },
  { value: 'target_only', label: '只写译文（<lang>.srt）' },
  { value: 'bilingual_only', label: '只写双语（bilingual.<lang>.srt）' }
];
export var DEFAULT_SETTINGS = {
  hash_algo: 'sha256', keep_mode: 'latest', pinned_path: '',
  skip_empty: true, skip_hidden: true, skip_symlinks: true,
  include_extensions: [], exclude_extensions: [], ignore_case_ext: true,
  min_size_bytes: 0, max_size_bytes: 0, quick_sample_kb: 64,
  concurrency: 2, hash_rate_mbps: 0, recycle_default: true,
  create_recycle_dir: false, max_files: 500000, allow_ctime_fallback: false,
  scan_mode: 'exact', similarity_threshold: 85, similarity_extensions: [],
  similarity_exclude_extensions: [], similarity_concurrency: 2, similarity_cache: true,
  similarity_max_file_mb: 32, similarity_allow_binary: false,
  ffmpeg_path: '',
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
  // v0032：音频来源 nas=本机抽音（默认）/ server=上传视频段由 ASR 服务器抽音
  subtitle_audio_source: 'nas',
  // v0040：分片音频预取（默认开）：识别当前片时先抽下一片，FFmpeg 与 ASR 重叠
  subtitle_audio_prefetch: true,
  // v0033：默认只生成 SRT；开启后任务目录额外生成 VTT
  subtitle_write_vtt: false,
  subtitle_translate_enabled: false,
  subtitle_llm_base_url: 'https://api.openai.com/v1',
  subtitle_llm_api_key: '',
  subtitle_llm_model: 'gpt-4.1-mini',
  subtitle_translate_target: 'zh',
  subtitle_translate_timeout_sec: 120,
  subtitle_translate_batch_size: 20,
  // v0030：本地 Qwen3/llama.cpp 类服务关闭“思考”（默认关）
  subtitle_llm_disable_thinking: false,
  // v0028 目录批量字幕默认值（与后端 settings.DEFAULT_SETTINGS 对齐）
  subtitle_output_mode: 'alongside_video',
  subtitle_skip_existing: true,
  // v0036：译文输出组合 both/target_only/bilingual_only
  subtitle_translate_outputs: 'both',
  subtitle_job_limit: 200,
  // v0034/v0037：同时执行的字幕任务数（1..4，默认 1 串行；保存后立即生效，无需重启）
  subtitle_concurrency: 1,
  // v0043：增量扫描 / 保留规则链 / 排除规则（与后端 settings.DEFAULT_SETTINGS 对齐）
  incremental_scan: true,
  keep_rules: [],
  exclude_name_regex: [],
  exclude_name_glob: [],
  ignore_file_enabled: true,
  media_sidecar_mode: 'skip'
};
export var SIZE_UNITS = { kb: 1024, mb: 1048576, gb: 1073741824 };
