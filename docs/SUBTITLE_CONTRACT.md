# 云端字幕契约（OpenAI 兼容 ASR + LLM，v1.3 binding）

用户已确认：只做 **OpenAI 兼容接口 + 云端 ASR**；不做本地 Whisper 模型、不做其他厂商专有 SDK。默认关闭；必须由管理员显式开启并勾选“音频将上传第三方”后才能生成。

## 1. 配置

| key | 默认 | 约束 |
|---|---|---|
| subtitle_enabled | false | 总开关 |
| subtitle_asr_base_url | https://api.openai.com/v1 | http/https，去尾部 /；拒绝 URL userinfo（user:pass@host） |
| subtitle_asr_api_key | "" | 密钥；GET 不回传明文 |
| subtitle_asr_model | whisper-1 | 必须支持 verbose_json/segments 或 srt；字符集 [A-Za-z0-9._/:@+-]{1,100} |
| subtitle_asr_language | auto | auto/zh/en/ja/ko/... |
| subtitle_asr_timeout_sec | 300 | 30..1800 |
| subtitle_asr_chunk_sec | 600 | 60..1800 |
| subtitle_asr_max_audio_mb | 24 | 1..100；超过则强制分块 |
| subtitle_translate_enabled | false | 是否生成译文 |
| subtitle_llm_base_url | https://api.openai.com/v1 | http/https |
| subtitle_llm_api_key | "" | GET 不回传明文 |
| subtitle_llm_model | gpt-4.1-mini | 任意 chat completions 模型 |
| subtitle_translate_target | zh | zh/en/ja/ko/... |
| subtitle_translate_timeout_sec | 120 | 30..900 |
| subtitle_translate_batch_size | 20 | 1..40 |

密钥存储：只在 `var/settings.json`（DSM 权限保护）。GET /api/settings 返回 `subtitle_asr_api_key_set/subtitle_llm_api_key_set`，绝不返回明文；PUT 空字符串表示保持原值，`clear_*_api_key=true` 才删除。不得写日志/错误信息/审计。

## 2. 任务 API

- `POST /api/subtitles`（管理员）：
```json
{"path":"/volume2/video/a.mp4","language":"auto","translate":true,
 "target_lang":"zh","confirm_cloud":true}
```
  - 校验：文件存在、常规文件、非 symlink、位于共享文件夹、视频后缀、`confirm_cloud=true`、总开关开启。
  - 返回 202 `{"ok":true,"job":{...}}`。
- `GET /api/subtitles/{job_id}`：status/stage/progress/message/error/files。
- `GET /api/subtitles?limit=`：字幕任务历史列表。
- `POST /api/subtitles/{job_id}/pause`：在 chunk 边界暂停；
- `POST /api/subtitles/{job_id}/resume`：从已完成 chunk 后继续，不重复调用 ASR。
- `POST /api/subtitles/{job_id}/cancel`：取消；运行中的 HTTP/ffmpeg 子进程应尽快终止。
- `GET /api/subtitles/{job_id}/file/{name}`：下载 `original.srt`、`original.vtt`、`<lang>.srt`、`<lang>.vtt`、`bilingual.<lang>.srt`。
- job 持久化到 `<var>/subtitles/jobs/<job_id>.json`；文件在 `<var>/subtitles/<job_id>/`；最多保留 20 个任务，LRU 清理。
- v0019 起 ASR chunk 结果持久化到 `subtitle_chunks`（schema v4）；重启后 queued/running 任务转为 paused，可手动继续，不自动续跑，不重复上传已完成 chunk。
- 同一时间只跑 1 个云端字幕任务；不同任务排队。

job schema：
```json
{"job_id":"sub-...","status":"queued|running|translating|done|failed|cancelled",
 "stage":"audio|asr|translate|write","progress":0..100,"message":"...",
 "path":"...","language":"zh","translate":true,"target_lang":"zh",
 "created_at":...,"updated_at":...,"error":null,
 "files":[{"name":"original.srt","size":1234}]}
```

## 3. 音频提取

- 使用 SPK 内置 FFmpeg（`DUP_FFMPEG_PATH`）。
- 读视频时长；按 `subtitle_asr_chunk_sec` 切块，块起点/终点准确；输出 16kHz 单声道 MP3，32kbps，临时文件。
- 每块必须小于 `subtitle_asr_max_audio_mb`；超出时继续按时长对半切。
- 音频文件只存在临时目录，完成/失败/取消后删除；不得保存在结果目录。
- 每块 ASR 返回的时间戳加块偏移；块边界重叠/重复 cue 去重（相同 start/end/text 或首尾重叠）。
- 不处理无音轨视频：报 `no_audio`，不生成空字幕。

## 4. OpenAI 兼容 ASR

- `POST {base_url}/audio/transcriptions`，multipart/form-data：
  - file=mp3, model, language（auto 时省略）, response_format=verbose_json, temperature=0
- 优先接受 `segments`（start/end/text）；若返回 `srt`/文本，解析 SRT；只有纯 text 无时间轴则报 `no_timestamps`，提示改用支持 verbose_json 的模型（如 whisper-1）。
- 401/403 不重试；429/5xx/网络错误最多重试 2 次，指数退避；超时终止。
- 不记录 Authorization；错误消息不含密钥和完整音频内容。
- 超长任务可取消。

## 5. LLM 翻译

- `POST {base_url}/chat/completions`：
  - model, temperature=0, messages
  - 逐批 cue（batch_size），system 要求“只翻译，不合并/拆分/改时间，返回 JSON 字符串数组，长度必须等于 N”
  - user 内容为 `{"target_language":"zh","cues":["...","..."]}`
- 解析 JSON（容忍 ```json 包围）；长度不等/空项/失败：重试一次，仍失败则整批回退原文并标记 `translation_partial=true`，不中断字幕生成。
- 翻译绝不改 start/end；不把时间轴交给 LLM。
- 目标语言码 zh/en/ja/ko 等；UI 提供中文/英文/日文等。
- 不记录原文/译文到日志；结果文件仅保存在任务目录，供管理员下载。

## 6. 字幕输出

- SRT 时间格式 `HH:MM:SS,mmm`；VTT 为 `HH:MM:SS.mmm`。
- 去空 cue、end>start、按 start 排序；单行过长按字符宽度换行（CJK 42/行，拉丁 80/行），不改变时间。
- 文件：
  - `original.srt` / `original.vtt`
  - 翻译开启时：`<target>.srt` / `<target>.vtt` / `bilingual.<target>.srt`
- 字幕文本必须做 SRT/VTT 安全转义；VTT 使用 WEBVTT 头。
- 不覆盖同名；文件名固定白名单，下载时防目录穿越。

## 7. 前端

- 设置页“云端字幕（实验，默认关闭）”：ASR base URL、模型、API Key（password，已配置占位）、语言；翻译开关、LLM base URL/model/API Key、目标语言、超时/chunk/max；隐私警告“音频将上传到所配置的第三方服务”。
- 视频结果每个视频行有“生成字幕”按钮；弹窗选择语言、是否翻译/目标语言、格式（SRT/VTT），必须勾选“我了解音频将上传到第三方”才能提交。
- 任务进度/取消/下载；错误显示后端消息（不含密钥）。
- 未配置/未启用时按钮禁用或提示；不阻塞视频查重。
- 所有 API Key 输入不回填明文。

## 8. 模型列表与连接测试 API（v0026）

供设置页“获取模型列表 / 测试连接”使用。两个接口都要求**管理员 + CSRF**（POST），只用于探测：
**不上传真实用户数据**（连接测试使用 1s 静音音频与固定短句），不重试，单次请求。

### 8.1 模型列表

`POST /api/subtitles/models`

```json
{"kind":"asr","base_url":"https://api.openai.com/v1","api_key":"sk-..."}
```

- `kind` 必填，`asr` | `llm`；`base_url`/`api_key` 缺省（或空串）时从 `subtitle_asr_*` / `subtitle_llm_*` settings 回退。
- 后端 GET `{base_url}/models`，超时 20s（受 client timeout_cap 限制），不重试。
- 响应兼容 `{"data":[...]}`、顶层数组、`{"models":[...]}`；元素可为字符串或含 `id` 的对象。
- 去重（id 不区分大小写）、按 id 不区分大小写排序；跳过空/非字符串/id 长度 >200；最多返回 500 条（`truncated=true`）。
- 空列表视为成功（`count=0` + `warning`）。

```json
{"ok":true,"kind":"asr","base_url":"https://api.openai.com/v1","count":2,
 "truncated":false,"latency_ms":120,
 "models":[{"id":"whisper-1","owned_by":"openai"},{"id":"gpt-4o-transcribe","owned_by":""}],
 "warning":""}
```

### 8.2 连接测试

`POST /api/subtitles/test`

```json
{"kind":"asr","base_url":"...","api_key":"...","model":"whisper-1","language":"auto","target_lang":"zh"}
```

- ASR：FFmpeg `-f lavfi -i anullsrc=r=16000:cl=mono -t 1` 生成 1s 静音 MP3（临时文件，完成后删除），POST `/audio/transcriptions`（verbose_json、temperature=0）；按响应结构判定：
  - `format` = `verbose_json` | `srt` | `text` | `unknown`；`segments`、`has_timestamps`、`text_chars`、`warning`。
- LLM：POST `/chat/completions` 翻译固定短句 `["Hello world"]`，必须返回长度 1 的字符串数组，否则 `PROVIDER_REJECTED`。
- 超时 60s（受 client timeout_cap 限制），不重试。

```json
{"ok":true,"kind":"asr","mode":"asr","model":"whisper-1",
 "base_url":"https://api.openai.com/v1","latency_ms":430,
 "warning":"",
 "detail":{"format":"verbose_json","segments":2,"has_timestamps":true,
           "text_chars":42,"warning":""}}
```

- 顶层 `warning` 与 `detail.warning` 一致：非空表示“连接成功但有告警”
  （如响应只有纯文本、无时间轴），前端用黄色提示展示。
- 模型列表接口只使用 `base_url`/`api_key`；`model`/`language` 等字段即使传入也会被忽略，避免模型输入框暂态非法时无法获取列表。

### 8.3 错误码与安全

| code | HTTP | 场景 |
|---|---|---|
| INVALID_ARGUMENT | 400 | kind/base_url/model 等参数非法（含 URL userinfo） |
| PROVIDER_AUTH_FAILED | 400 | Provider 返回 401/403（**不使用 401，避免前端误判登录失效**） |
| PROVIDER_REJECTED | 400 | Provider 拒绝请求/LLM 返回格式不符 |
| MODELS_UNSUPPORTED | 400 | 不支持 /models、404/405、响应非 JSON/缺 data/models |
| PROVIDER_HTTP_ERROR | 502 | 其他 Provider HTTP 错误（不重试） |
| PROVIDER_NETWORK_ERROR | 502 | 网络不可达 |
| PROVIDER_TIMEOUT | 504 | 请求超时 |
| AUDIO_FAILED | 400 | 静音测试音频生成失败 |
| FFMPEG_UNAVAILABLE | 400 | 无可用 FFmpeg |
| PROBE_BUSY | 409 | 已有探测请求进行中（服务内探测锁，同一时间仅 1 个） |

- 错误 message 统一脱敏：api_key 替换为 `***`、去控制字符、截断 300；响应/日志/审计/任务 JSON 绝不出现 api_key。
- base_url 拒绝 userinfo（`user:pass@host`），避免凭据进入 URL/日志；模型名字符集放宽允许 `@` 与 `+`。

## 9. 测试

- 后端 fake HTTP server：ASR verbose_json/SRT/无时间轴/401/429重试/超时；LLM 正常/长度不符/JSON 围栏/部分失败；chunk 偏移合并、no_audio、取消、密钥不回传/不日志、目录穿越、任务清理。
- v0026：fake server 增加 GET /models（排序/去重/500 上限/401/404/timeout/空列表/Authorization 记录），fake ffmpeg 增加 lavfi 静音分支；覆盖错误码映射、探测锁、密钥不泄漏/不落盘。
- 前端 node --check + E2E（mock API）。
- 现有测试必须继续全部通过。
