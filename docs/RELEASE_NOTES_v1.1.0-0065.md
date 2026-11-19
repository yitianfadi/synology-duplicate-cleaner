# 发布说明 1.1.0-0065（转换硬件编解码：VAAPI 自包含）

- 版本：`1.1.0-0065`
- 日期：2026-11-19
- 定位：**把转换从纯 CPU 换成核显硬件编解码**（NAS 本地，不需要外部服务器）。
- 升级：覆盖安装即可（schema 仍 v11，无设置迁移；新增一个设置键 `convert_hwaccel`）。
- 兼容：`/api/convert/*` 既有字段不变，`capabilities` 新增 `hardware`，预设新增 3 个（共 19 个）。

## 1. 为什么做

转换此前 16 个预设全是 `libx264` / `libx265` 软件编码。DS920+（Celeron J4125）实测：
`libx264 medium` 1080p 只有 **约 1.0×** 实时，2 小时片子要 2.5–3 小时。

NAS 上其实有 Intel UHD 600（Gemini Lake），但有三道门槛：
1. `/dev/dri/renderD128` 属于 `root:videodriver 660`，套件用户默认不在组里；
2. libva 驱动不在 `/usr/lib/dri`（System 目录为空），必须显式 `LIBVA_DRIVERS_PATH`；
3. 旧内置 FFmpeg 4.1 的 libva 太老（2.1.0），加载不了新版 iHD 驱动。

## 2. 实测能力（DS920+，合成源）

| 路径 | 倍率 |
|---|---|
| CPU `libx264 medium` 1080p | 1.0× |
| CPU `libx264 veryfast` 1080p | 2.0× |
| i965 硬编 H.264 1080p | 2.02× |
| iHD 硬编 H.264 1080p | 3.49× |
| iHD 硬编 HEVC 720p | 2.24× |
| **iHD 硬解 HEVC + 硬编 H.264（全 GPU）** | **9.07×（273 fps）** |

结论：统一用 iHD；源编码可硬解时走全 GPU（`-hwaccel_output_format vaapi`），
否则软解 + `hwupload` 硬编。GLK 上 HEVC **编码**可用，但 HEVC **硬解**只有 iHD 提供。

## 3. 实现

### 3.1 自包含运行时（不再依赖 Emby/CodecPack）

- 内置 x86_64 FFmpeg 从 4.1 换成 **BtbN 静态构建 n8.1**（`--enable-vaapi --enable-libvpl`，
  `hwaccels: cuda vaapi qsv drm opencl vulkan amf`，7 个 vaapi 编码器）。
- 随包放入 `target/lib/vaapi/`：
  - `libva.so.2` + `libva-drm.so.2`（BtbN 对 libva 用 implib 运行时 dlopen，必须提供）
  - `libigdgmm.so.12`（iHD 依赖）
  - `dri/iHD_drv_video.so`、`dri/i965_drv_video.so`
- `spk/conf/privilege` 增加 `"join-groupname": "videodriver"`（Emby/Jellyfin 同款做法），
  安装后套件用户即可打开 `/dev/dri/renderD128`。
- aarch64 仍用原 4.1（ARM 机型没有这些设备，不做无谓膨胀）。

> 注意：**不能**把 `/volume1/@appstore/EmbyServer/lib` 整个放进 `LD_LIBRARY_PATH`——
> 那里有旧 `libc.so.6`，会让 FFmpeg 报 `GLIBC_2.28 not found`。只放 libva 相关库。

### 3.2 探测与回退

- 新增 `dupcleaner/hwaccel.py`：探测设备 → 驱动（iHD 优先，缺 gmm 则退 i965）→ 拼
  `LIBVA_DRIVER_NAME` / `LIBVA_DRIVERS_PATH` / `LD_LIBRARY_PATH`；带缓存与 `reset_cache()`。
- `FFmpegTool.probe_vaapi()`：跑一次最小 `-init_hw_device vaapi`，失败即判定不可用（结果缓存）。
- `capabilities()` 新增 `hardware: {available, device, driver, reason, encoders, codecs, mode}`；
  硬件预设在不可用时置灰并给原因。
- 新增设置键 `convert_hwaccel`（`auto` 默认 / `off` 只用 CPU）。
- 任务执行：硬件预设失败（驱动/像素格式不支持等）**自动回退同容器 CPU 预设**重跑一次，
  写审计 `convert_hw_fallback`，任务快照新增 `hw_fallback=true`。

### 3.3 预设（3 个新硬件预设，共 19 个）

| 预设 | 说明 |
|---|---|
| `mp4_h264_hw` · MP4 · H.264 + AAC（硬件，快） | 硬编 `h264_vaapi`，`-rc_mode CQP -qp 20/24/28` |
| `mp4_h265_hw` · MP4 · H.265 + AAC（硬件，快） | 硬编 `hevc_vaapi`，`-qp 22/26/30`，`-tag:v hvc1` |
| `mkv_h264_hw` · MKV · H.264 + AAC（硬件，快） | 硬编 + 复制字幕轨 |

- 解码策略：源编码在驱动能力表里（iHD：h264/hevc/vp9）→ 全 GPU（`-hwaccel vaapi
  -hwaccel_output_format vaapi`，缩放用 `scale_vaapi`）；否则软解 + `format=nv12,hwupload`。
- 分辨率上限：全 GPU 用 `scale_vaapi=w=-2:h=N:format=nv12`；软解用
  `scale=-2:min(N\,ih),format=nv12,hwupload`。

## 4. 验证

- 单元：`tests/test_v0065_convert_hw.py`（15 例，参数金标 / 探测 / 环境变量 / 回退）；
  转换相关合计 103 例通过。
- 静态：`BE-CONVERT-HWACCEL`、`BE-CONVERT-HW-PRESETS`、`SPK-PRIVILEGE-VIDEODRIVER`、
  `SPK-FFMPEG-VAAPI`、`SPK-VAAPI-BUNDLE`、`FE-CONVERT-HW`。
- e2e：`CV-01` 改为 19 个预设，新增 `CV-21`（硬件状态与置灰原因）。
- 真机：装包后按 §5 复测。

## 5. 真机复测步骤（DS920+）

1. 装 `dist/DuplicateCleaner-1.1.0-0065.spk`；
2. 转换页选一个 H.264 源 → 预设「MP4 · H.264（硬件，快）」，对比 CPU 预设的耗时；
3. 选一个 HEVC 源（H.265）→ 同一硬件预设，观察速度和 `hw_fallback`；
4. 设置里把 `convert_hwaccel` 改 `off`（需在 API/设置文件）可强制 CPU，用于对照。

## 6. 限制

- 需要带 Intel iGPU 的机型（DS920+/DS720+/DS420+ 等）；其他架构/机型硬件预设自动置灰。
- HEVC 硬解依赖 iHD；GLK 上 HEVC 硬编可用但不保证所有分辨率/档次。
- VAAPI 质量用 CQP，同码率下画质略逊 x264/x265 的 CRF 模式。
- SPK 体积从 39 MB 涨到约 85 MB（FFmpeg 8.1 175 MB + VAAPI 运行库/驱动 ~56 MB，仅 x86_64）。
