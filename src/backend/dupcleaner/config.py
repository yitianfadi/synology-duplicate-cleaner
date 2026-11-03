# -*- coding: utf-8 -*-
"""运行期配置、安全常量与路径黑名单。"""

from __future__ import absolute_import

import os
import re

APP_NAME = "DuplicateCleaner"
APP_VERSION = "1.1.0-0026"
DISPLAY_NAME = "重复文件清理与字幕"
SPK_VERSION = "1.1.0-0026"

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
