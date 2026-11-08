# -*- coding: utf-8 -*-
"""运行期配置、安全常量与路径黑名单。"""

from __future__ import absolute_import

import os
import re

APP_NAME = "DuplicateCleaner"
APP_VERSION = "1.1.0-0045"
DISPLAY_NAME = "重复文件清理与字幕"
SPK_VERSION = "1.1.0-0045"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# 敏感文件（settings.json / TLS 证书与私钥）权限：仅属主可读写。
SECURE_FILE_MODE = 0o600
SECURE_DIR_MODE = 0o700

SESSION_COOKIE = "dc_session"
SESSION_TTL_SECONDS = 28800
PLAN_TTL_SECONDS = 300
REQUIRED_PERMANENT_PHRASE = u"永久删除"

HASH_CHUNK_SIZE = 1024 * 1024
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_ROOTS = 20
MAX_EXCLUDES = 200
MAX_FILES_LIMIT = 1000000
MAX_FILES_DEFAULT = 500000

# 单次计划响应里 files 明细上限（过大响应会拖垮前端；计划本身按文件流式落盘）。
PLAN_FILES_RESPONSE_LIMIT = 5000
# 删除任务在内存里保留的结果条数上限，超出部分只在 JSONL 日志里。
JOB_RESULTS_MEMORY_LIMIT = 5000

# 计划阶段做“候选+保留文件全量哈希复核”的总字节预算；超出则把哈希复核
# 推迟到后台删除任务中逐文件执行（删除任务始终会复核全量哈希）。
PLAN_HASH_VERIFY_BUDGET_BYTES = 512 * 1024 * 1024

# ---------------------------------------------------------------------------
# 增量扫描 / 跨扫描哈希缓存（v0043，docs/INCREMENTAL_SCAN_CONTRACT.md）
# ---------------------------------------------------------------------------
# 缓存键 = (st_dev, st_ino)；用 size + mtime_ns + 算法 + 采样配置校验有效性。
# 文件内容未变（size/mtime_ns 未变）时直接复用 quick_hash/full_hash，不再读盘。
INCREMENTAL_SCAN_ENABLED_DEFAULT = True
HASH_CACHE_MAX_ENTRIES = 300000
# 快速指纹算法版本：采样方式变化必须递增；旧版本缓存只复用全量哈希，
# quick_hash 重算（避免新旧指纹混用）。
QUICK_SAMPLE_VERSION = 2
# 大文件（>= 阈值）快速指纹改为“头 + 中 + 尾”三段采样，降低“中段被替换”漏检。
LARGE_FILE_SAMPLE_THRESHOLD_BYTES = 256 * 1024 * 1024
# 媒体探测缓存（保留策略“优先带字幕/内嵌音轨”用）；按 (dev,inode)+身份校验。
MEDIA_PROBE_CACHE_MAX_ENTRIES = 50000
# 影视库附属文件（外挂字幕/海报/nfo 等）：media_sidecar_mode=skip（默认）时
# 只跳过“与同目录视频同名”的附属文件，不按后缀无差别跳过（避免照片误伤）。
SIDECAR_EXTENSIONS = frozenset([
    ".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx", ".sup", ".nfo", ".sfv",
    ".jpg", ".jpeg", ".png", ".webp", ".bif", ".tbn",
])
SIDECAR_MODES = ("skip", "include")
KNOWN_SIDECAR_NAMES = frozenset([
    "poster.jpg", "poster.png", "folder.jpg", "folder.png", "fanart.jpg",
    "fanart.png", "backdrop.jpg", "backdrop.png", "banner.jpg", "banner.png",
    "logo.png", "clearlogo.png", "clearart.png", "landscape.jpg",
    "thumb.jpg", "thumb.png", "movie.nfo", "tvshow.nfo", "season.nfo",
    "albumart.jpg", "albumartsmall.jpg",
])

# ---------------------------------------------------------------------------
# 保留策略规则链（v0043）
# ---------------------------------------------------------------------------
# keep_rules 为空时沿用 keep_mode/pinned_path 的历史语义；非空时按顺序逐条比较，
# 前一条分出胜负就停止（平局继续下一条）。规则格式："rule" 或 "rule:参数"。
KEEP_RULE_NAMES = (
    "created_latest", "created_earliest",
    "mtime_latest", "mtime_earliest",
    "atime_latest", "atime_earliest",
    "size_largest", "size_smallest",
    "path_priority",      # 参数 = 目录/路径前缀；命中者优先保留
    "filename_priority",  # 参数 = 通配符，或 /正则/ 形式；命中者优先保留
    "has_subtitle_stream",  # 需要媒体探测（仅视频/音频文件有意义）
    "has_audio_streams",
)
KEEP_RULES_MAX = 40
KEEP_RULE_MAX_LEN = 300
KEEP_RULE_LABELS = {
    "created_latest": u"创建时间最晚",
    "created_earliest": u"创建时间最早",
    "mtime_latest": u"修改时间最晚",
    "mtime_earliest": u"修改时间最早",
    "atime_latest": u"访问时间最晚（不可靠）",
    "atime_earliest": u"访问时间最早（不可靠）",
    "size_largest": u"体积最大",
    "size_smallest": u"体积最小",
    "path_priority": u"路径优先级",
    "filename_priority": u"文件名优先级",
    "has_subtitle_stream": u"带内嵌字幕优先",
    "has_audio_streams": u"带内嵌音轨优先",
}
# 排除规则（v0043）：文件名正则/通配符、.ignore 文件
EXCLUDE_PATTERN_MAX_COUNT = 50
EXCLUDE_PATTERN_MAX_LEN = 200
IGNORE_FILE_NAME = ".ignore"
IGNORE_FILE_MAX_LINES = 1000

# ---------------------------------------------------------------------------
# 预览缩略图 / 媒体信息（v0044，docs/PREVIEW_PROTECTION_CONTRACT.md）
# ---------------------------------------------------------------------------
# 缩略图缓存放 <var>/cache/thumbs/<sha1(path|size|mtime|t|w)>.jpg，按 LRU 裁剪。
THUMB_CACHE_SUBDIR = "cache/thumbs"
THUMB_DEFAULT_WIDTH = 320
THUMB_MAX_WIDTH = 1280
THUMB_COUNT_DEFAULT = 6
THUMB_COUNT_MAX = 12
THUMB_TIMEOUT_SEC = 60
THUMB_MAX_ENTRIES = 3000
THUMB_MAX_BYTES = 300 * 1024 * 1024
THUMB_JPEG_QUALITY = 4
# 预览/媒体探测的内存缓存条数（按 path+size+mtime_ns 区分）
MEDIA_INFO_CACHE_MAX = 200
# 「片段播放」：对比弹窗同步播放的判定容差（秒）
PREVIEW_SYNC_TOLERANCE_SEC = 0.5

# ---------------------------------------------------------------------------
# 删除白名单 / 附属文件保护（v0044）
# ---------------------------------------------------------------------------
# 保护是**删除侧**的终局防线：扫描期标记（UI 不可选）+ 计划/执行期按当前设置重算。
PROTECT_ENABLED_DEFAULT = True
# 默认保护“外挂字幕 / 元数据”后缀（不含图片：照片库仍需判重；海报由
# “同目录同源视频的附属文件”规则保护，见 PROTECT_SIDECAR_DEFAULT）。
PROTECT_DEFAULT_EXTENSIONS = (
    ".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx", ".sup", ".smi",
    ".nfo", ".sfv", ".md5", ".sha256",
)
PROTECT_SIDECAR_DEFAULT = True
PROTECT_REASON_LABELS = {
    "extension": u"后缀受保护",
    "directory": u"目录受保护",
    "name": u"文件名命中保护规则",
    "sidecar": u"视频附属文件（同目录同源视频）",
}

# ---------------------------------------------------------------------------
# 硬链接合并 / 套件独立回收站（v0045，docs/MERGE_TRASH_CONTRACT.md）
# ---------------------------------------------------------------------------
# 硬链接合并：默认关闭（不可逆操作），开启后仍需逐组勾选 + 二次确认。
MERGE_ENABLED_DEFAULT = False
MERGE_PLAN_TTL_SECONDS = 300
MERGE_ALREADY_LINKED = "ALREADY_LINKED"
MERGE_RESTORE_COPY = "copy"
# Btrfs 上的 FICLONE ioctl（fs/ioctl.h: _IOW(0x94, 9, int)）；不可用时回退普通复制。
FICLONE_IOCTL = 0x40049409
# 套件独立回收站：放在共享文件夹根下的 # 前缀目录（扫描器会跳过 # 前缀目录）。
TRASH_DIR_NAME = "#duplicate-cleaner-trash"
TRASH_MANIFEST_SUBDIR = "trash"
MERGE_MANIFEST_SUBDIR = "merges"
RECYCLE_MODES = ("share", "package")
RECYCLE_MODE_DEFAULT = "share"
RECYCLE_MODE_LABELS = {
    "share": u"共享文件夹回收站（#recycle）",
    "package": u"套件独立回收站（可整组恢复）",
}

# ---------------------------------------------------------------------------
# 内容相似度模式常量（docs/SIMILARITY_CONTRACT.md）
# ---------------------------------------------------------------------------
DEFAULT_SIMILARITY_EXTENSIONS = (
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".html", ".htm", ".log",
    ".pdf", ".docx", ".odt",
    ".png", ".jpg", ".jpeg", ".webp",
)

# 无论 allow_binary 如何，这些压缩包/镜像/可执行/媒体类别永远只做精准哈希。
SIMILARITY_BINARY_EXTENSIONS = frozenset([
    ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".zst", ".tgz",
    ".iso", ".img", ".dmg", ".msi", ".pkg", ".spk", ".deb", ".rpm", ".cab",
    ".exe", ".dll", ".so", ".dylib", ".com", ".scr", ".apk", ".jar", ".war",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
    ".mpeg", ".3gp", ".ts", ".rmvb",
    ".mp3", ".flac", ".aac", ".m4a", ".wav", ".ogg", ".wma", ".ape", ".opus",
    ".ttf", ".otf", ".woff", ".woff2", ".psd", ".ai",
])

SIMILARITY_CACHE_FILENAME = "feature_cache.json"
SIMILARITY_CACHE_MAX_ENTRIES = 50000
SIMILARITY_SIGNATURE_SIZE = 64
SIMILARITY_MINHASH_BANDS = 16
SIMILARITY_MINHASH_ROWS = 4
SIMILARITY_MAX_SHINGLES = 50000
SIMILARITY_CHUNK_BYTES = 1024
SIMILARITY_TEXT_EXTENSIONS = frozenset([
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html", ".htm",
    ".log", ".yaml", ".yml", ".ini", ".conf", ".cfg", ".py", ".js", ".css",
    ".sql", ".sh", ".bat", ".ps1", ".c", ".h", ".cpp", ".java", ".go", ".rs",
    ".rb", ".php", ".lua", ".toml", ".properties", ".srt", ".nfo", ".env",
])
SIMILARITY_DOCUMENT_EXTENSIONS = frozenset([".pdf", ".docx", ".odt"])
SIMILARITY_IMAGE_EXTENSIONS = frozenset([".png", ".jpg", ".jpeg", ".webp"])

# ---------------------------------------------------------------------------
# 视频视觉相似度常量（docs/VIDEO_CONTRACT.md）
# ---------------------------------------------------------------------------
DEFAULT_VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v",
    ".ts", ".mpg", ".mpeg",
)
VIDEO_ALGO_VERSION = "video-v1"
VIDEO_CACHE_FILENAME = "video_cache.sqlite3"
VIDEO_CACHE_MAX_ENTRIES = 20000
VIDEO_FRAME_SIZE = 32
VIDEO_FRAME_BYTES = 1024
# 删除安全：视频内容快速校验哈希（size + head/tail 64KB，SHA256）
VIDEO_CONTENT_QUICK_HASH_ALGO = "sha256"
VIDEO_CONTENT_QUICK_HASH_SAMPLE_KB = 64
# 抽帧批处理：每个 ffmpeg 进程最多顺序抽取 2-4 帧（失败回退逐帧）
VIDEO_BATCH_FRAMES = 4
VIDEO_MIN_SAMPLE_FRAMES = 5
VIDEO_MAX_SAMPLE_FRAMES = 100
VIDEO_DEFAULT_SAMPLE_FRAMES = 20
VIDEO_DEFAULT_MAX_INTERVAL = 300
VIDEO_DEFAULT_THRESHOLD = 90
# 硬解模式：auto=按检测能力自动；off=纯软件；其余为显式 -hwaccel 名称。
# 显式模式若不在 ffmpeg -hwaccels 列表，运行时回退 none（软件解码）。
VIDEO_HWACCEL_MODES = ("auto", "off", "vaapi", "qsv", "nvdec", "videotoolbox")
VIDEO_HWACCEL_EXPLICIT = ("vaapi", "qsv", "nvdec", "videotoolbox")
VIDEO_MIN_COVERAGE = 0.6
VIDEO_DURATION_RATIO_MIN = 0.7
VIDEO_DURATION_RATIO_MAX = 1.4
VIDEO_DEFAULT_MAX_FILE_MB = 8192
VIDEO_DEFAULT_TIMEOUT_SEC = 180

# 可选 ASR 语音辅助（外部命令，默认关闭；不内置模型、不联网）
ASR_ALGO_VERSION = "asr-v1"
ASR_AUDIO_RATE = 16000
ASR_DEFAULT_WEIGHT = 0.15
ASR_DEFAULT_TIMEOUT_SEC = 300
ASR_DEFAULT_MAX_SEC = 600
ASR_MIN_TEXT_CHARS = 8

# 短视频：双方时长都 <= 180s 时放宽时长比例 0.5..2.0
SHORT_VIDEO_MAX_SEC = 180.0
SHORT_VIDEO_RATIO_MIN = 0.5
SHORT_VIDEO_RATIO_MAX = 2.0

# ---------------------------------------------------------------------------
# 云端字幕目录批量（docs/SUBTITLE_BATCH_CONTRACT.md）
# ---------------------------------------------------------------------------
# 已完成任务保留上限（设置 subtitle_job_limit 的缺省值）；LRU 只删任务目录，
# 绝不删除写回视频旁的字幕文件。
SUBTITLE_JOB_LIMIT_DEFAULT = 200
# v0034/v0037：字幕任务并发上限/缺省值。默认 1 = 保持历史串行行为；设置
# subtitle_concurrency 可在 1..4 调整，保存设置后立即生效（动态调整 worker，无需重启）。
SUBTITLE_CONCURRENCY_MAX = 4
SUBTITLE_DEFAULT_CONCURRENCY = 1
# 单次批量提交路径上限；超出拒绝并提示分批（组内每批仍按此上限拆分）。
SUBTITLE_BATCH_MAX_ITEMS = 500
# 单次“批次组”提交路径上限（v0032：一次提交自动拆成多个 <=500 的批次）。
SUBTITLE_BATCH_GROUP_MAX_ITEMS = 20000
# 单次目录枚举返回上限；超出截断并置 truncated=true。
SUBTITLE_ENUMERATE_MAX_ITEMS = 20000
# 已完成批次组保留数量（v0032：组内任务不参与单任务 LRU，按组整体清理）。
SUBTITLE_BATCH_GROUP_LIMIT = 20
# 允许写回视频同目录的字幕后缀（只写 srt，vtt 仅留在任务目录供下载）。
SUBTITLE_ALONGSIDE_EXT = ".srt"
SUBTITLE_OUTPUT_MODES = ("alongside_video", "download_only")
SUBTITLE_DEFAULT_OUTPUT_MODE = "alongside_video"
# v0036：译文输出组合（both=译文+双语，默认；target_only=只写译文；
# bilingual_only=只写双语）。任务目录始终保留 original.srt（补翻译源文本），
# 视频同目录已有文件从不覆盖；修改设置不删除已写出的旧文件。
SUBTITLE_TRANSLATE_OUTPUTS = ("both", "target_only", "bilingual_only")
SUBTITLE_DEFAULT_TRANSLATE_OUTPUTS = "both"
# 音频来源（v0032）：nas=本机解码抽 16k 单声道 mp3（默认，只上传音频）；
# server=NAS 只做流复制抽音轨（不解码，速度快），由 ASR 服务器解码转音频，
# 仍然只上传音频（不上传视频），体积取决于原音轨码率。
SUBTITLE_AUDIO_SOURCES = ("nas", "server")
SUBTITLE_DEFAULT_AUDIO_SOURCE = "nas"
# server 模式单段上传上限（ASR 服务 800MB 限制留余量）；超限继续对半切。
SUBTITLE_SERVER_UPLOAD_MAX_MB = 780

# 系统 / 隐藏目录名：任何一层路径出现都必须拒绝（回收站目标除外）。
SYSTEM_DIR_NAMES = frozenset([
    "@eaDir", "@tmp", "@appstore", "@docker", "@synologydrive", "@database",
    "@surveillance", "@guest", "@log", "@download", "@ups", "@mailplus", "@S2S",
    "@autoupdate", "@iSCSI", "@smallfiles", "@sharebin", "@appdata", "@webapi",
    "@spool", "@sync", "@cloudstation", "@server", "@pgsql", "@mariadb", "@mysql",
    "@synocore", "@user", "@sys", "@web", "@nfs", "@samba", "@recycle",
    "@thumbnails", "@photos", "@tmpfiles", "@index", "@search", "@convert",
    "#recycle", "#snapshot", "#surveillance", "#usb", "#video", "#music",
    "#photo", "#download", "#home", "#homes", "#docker", "#recyclebin",
    ".SynoFSFolder", ".synofolder", "lost+found",
    # Windows/常见系统文件（扫描时同样视为系统文件，永久跳过）
    "Thumbs.db", "thumbs.db", "ehthumbs.db", "desktop.ini", "Desktop.ini",
    "iconcache.db", "IconCache.db", "$RECYCLE.BIN", "System Volume Information",
])
SYSTEM_NAME_PREFIXES = ("@", "#")

# POSIX 系统目录（绝对前缀），任何情况下都不允许浏览/扫描/删除。
POSIX_BLOCKED_PREFIXES = (
    "/etc", "/usr", "/var", "/root", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/proc", "/sys", "/dev", "/opt", "/tmp", "/run", "/srv",
    "/mnt", "/media", "/home", "/snap", "/lost+found",
)

# Windows 系统目录（小写、规范化后比较），仅用于开发/测试环境的额外防线。
WINDOWS_BLOCKED_PREFIXES = (
    "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\$recycle.bin", "c:\\system volume information",
    "c:\\recovery", "c:\\boot", "c:\\perflogs",
)

VOLUME_ROOT_RE = re.compile(r"^/volume(?:usb)?\d+$", re.IGNORECASE)
VOLUME_RE = re.compile(r"^/volume(?:usb)?\d+(?:/|$)", re.IGNORECASE)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SAFE_LOG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:log|jsonl|csv|txt)$")
SCAN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
PLAN_ID_RE = re.compile(r"^plan-[A-Za-z0-9._-]{6,80}$")
JOB_ID_RE = re.compile(r"^del-[A-Za-z0-9._-]{6,80}$")
SUBTITLE_BATCH_ID_RE = re.compile(r"^bat-[A-Za-z0-9._-]{6,80}$")
SUBTITLE_BATCH_GROUP_ID_RE = re.compile(r"^grp-[A-Za-z0-9._-]{6,80}$")


def secure_permissions(path, mode=SECURE_FILE_MODE):
    """把敏感文件权限收紧为 600（失败不阻断；Windows 只支持只读位）。"""
    if not path:
        return False
    try:
        if os.path.exists(path):
            os.chmod(path, mode)
            return True
    except OSError:
        pass
    return False


def ensure_private_dir(path):
    """创建仅属主可访问的私有目录（备份目录等）。"""
    if not path:
        return False
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, SECURE_DIR_MODE)
        return True
    except OSError:
        return False


def default_web_dir():
    """默认静态资源目录：<repo>/src/web（app.py 位于 <repo>/src/backend）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, os.pardir, os.pardir, "web"))


class RuntimeConfig(object):
    """进程级运行配置。dev 模式只能由环境变量 / 显式参数开启。"""

    def __init__(self, data_dir=None, web_dir=None, dev_mode=None, dev_roots=None,
                 dev_password=None, tls_cert=None, tls_key=None, host=None,
                 port=None, environ=None, admin_users=None):
        env = os.environ if environ is None else environ
        self.data_dir = os.path.abspath(
            data_dir or env.get("DUP_DATA_DIR") or os.path.join(os.getcwd(), "data"))
        self.web_dir = os.path.abspath(web_dir or default_web_dir())

        if dev_mode is None:
            dev_mode = str(env.get("DUP_CLEANER_DEV", "")).strip().lower() in (
                "1", "true", "yes", "on")
        self.dev_mode = bool(dev_mode)

        if dev_roots is None:
            raw = env.get("DUP_DEV_ROOTS", "") or ""
            dev_roots = [p for p in raw.split(";") if p.strip()]
        roots = []
        for item in dev_roots or []:
            item = str(item).strip()
            if not item:
                continue
            roots.append(os.path.abspath(os.path.expanduser(item)))
        self.dev_roots = roots

        if dev_password is None:
            dev_password = env.get("DUP_DEV_PASSWORD") or "admin"
        self.dev_password = dev_password

        if admin_users is None:
            raw_admin = env.get("DUP_ADMIN_USERS", "") or ""
            admin_users = [item.strip() for item in raw_admin.split(",") if item.strip()]
        self.admin_users = [str(item) for item in admin_users]

        self.ffmpeg_path = env.get("DUP_FFMPEG_PATH") or None

        self.tls_cert = tls_cert or None
        self.tls_key = tls_key or None
        # TLS 证书/私钥在加载时收紧权限（私钥绝不允许组/其他用户可读）。
        secure_permissions(self.tls_cert)
        secure_permissions(self.tls_key)
        self.host = host or DEFAULT_HOST
        self.port = int(port if port is not None else DEFAULT_PORT)

    @property
    def tls_enabled(self):
        return bool(self.tls_cert and self.tls_key)

    @property
    def scheme(self):
        return "https" if self.tls_enabled else "http"

    def __repr__(self):
        return "RuntimeConfig(data_dir=%r, web_dir=%r, dev_mode=%r, dev_roots=%r)" % (
            self.data_dir, self.web_dir, self.dev_mode, self.dev_roots)


def is_dev_mode(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get("DUP_CLEANER_DEV", "")).strip().lower() in (
        "1", "true", "yes", "on")
