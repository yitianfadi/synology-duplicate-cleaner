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
| subtitle_audio_source | nas | v0032：`nas`=本机解码抽 16k mp3（只上传音频）；`server`=本机仅流复制抽音轨（不解码），由 ASR 服务器解码（仍只上传音频） |
| subtitle_write_vtt | false | v0033：默认只生成 SRT；`true` 时任务目录额外生成 `.vtt`（写回视频目录始终只写 `.srt`） |
| subtitle_concurrency | 1 | v0034：同时执行的字幕任务数 1..4，默认 1（串行，最稳）；服务启动时读取，**修改后需重启套件生效** |
| subtitle_translate_enabled | false | 是否生成译文 |
| subtitle_llm_base_url | https://api.openai.com/v1 | http/https |
| subtitle_llm_api_key | "" | GET 不回传明文 |
| subtitle_llm_model | gpt-4.1-mini | 任意 chat completions 模型 |
| subtitle_translate_target | zh | zh/en/ja/ko/... |
| subtitle_translate_timeout_sec | 120 | 30..900 |
| subtitle_translate_batch_size | 20 | 1..40 |
| subtitle_llm_disable_thinking | false | 布尔；true 时翻译/连接测试请求附带 `chat_template_kwargs.enable_thinking=false`（本地 Qwen3/Strata 类推理模型建议开启；OpenAI 官方等不支持该参数的服务保持关闭） |

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
- `GET /api/subtitles?limit=&offset=&batch_id=&group_id=&status=`：字幕任务历史列表；v0034 新增 `status` 过滤：`all`（默认）/`active`（= running+translating）/`queued`/`running`/`translating`/`paused`/`done`/`failed`/`cancelled`，多个状态可用逗号组合（如 `failed,cancelled`；`active` 也可参与组合），非法值 400 `INVALID_ARGUMENT`；带 `offset`/`group_id` 时返回分页对象 `{jobs,total,offset,limit}`（`total` 为过滤后数量）。
- `POST /api/subtitles/{job_id}/pause`：在 chunk 边界暂停；
- `POST /api/subtitles/{job_id}/resume`：从已完成 chunk 后继续，不重复调用 ASR。
- `POST /api/subtitles/{job_id}/cancel`：取消；运行中的 HTTP/ffmpeg 子进程应尽快终止。
- `GET /api/subtitles/{job_id}/file/{name}`：下载 `original.srt`、`original.vtt`、`<lang>.srt`、`<lang>.vtt`、`bilingual.<lang>.srt`。
- job 持久化到 `<var>/subtitles/jobs/<job_id>.json`；文件在 `<var>/subtitles/<job_id>/`；最多保留 20 个任务，LRU 清理。
- v0019 起 ASR chunk 结果持久化到 `subtitle_chunks`（schema v4）。v0034 重启语义：`queued` 任务保留并**自动重新入队继续**（从未执行过，不会重复上传）；`running/translating` 有分片 → 转 `paused` 可手动继续，无分片 → `SERVICE_RESTART` failed；`paused` 保持不变；已完成分片不重复上传。
- 默认并发 1（`subtitle_concurrency` 可配置 1..4）；不同任务排队。

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
- v0032 音频来源 `subtitle_audio_source`：
  - `nas`（默认）：上述本机解码抽出 16k mp3 后上传（只上传音频）。
  - `server`：本机只用 FFmpeg **流复制**抽音轨（`-map 0:a:0 -c copy`，不解码，速度快），按块上传音频容器（.mka），由 ASR 服务器解码转音频；仍只上传音频、不上传视频。单块超过 `SUBTITLE_SERVER_UPLOAD_MAX_MB`（780MB）继续对半切。
  - 流复制失败（容器/编码不支持、无音轨）→ 该分块回退 NAS 解码（mp3），任务仍 `done` 并置 `warning=SERVER_AUDIO_FALLBACK`。
  - 音频来源在提交时快照到 job（`audio_source`），运行期不随设置变化。
- 音频/音轨片段只存在临时目录，完成/失败/取消后删除；不得保存在结果目录。
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
  - `subtitle_llm_disable_thinking=true` 时附加 `chat_template_kwargs={"enable_thinking":false}`
    （llama.cpp / vLLM / Strata 等支持；v0030 实测本地 Qwen3.6 翻译 20 条 cue 由约 48s
    降到约 3s。OpenAI 官方等不接受该参数的服务保持默认关闭；不兼容时按失败回退原文）。
  - 逐批 cue（batch_size），system 要求“只翻译，不合并/拆分/改时间，返回 JSON 字符串数组，长度必须等于 N”
  - user 内容为 `{"target_language":"zh","cues":["...","..."]}`
- 解析 JSON（容忍 ```json 包围）；长度不等/空项/失败：重试一次，仍失败则整批回退原文并标记 `translation_partial=true`，不中断字幕生成。v0030 起字幕任务列表/详情会显示“部分译文回退原文”，不静默。
- 翻译绝不改 start/end；不把时间轴交给 LLM。
- 目标语言码 zh/en/ja/ko 等；UI 提供中文/英文/日文等。
- 不记录原文/译文到日志；结果文件仅保存在任务目录，供管理员下载。

## 6. 字幕输出

- SRT 时间格式 `HH:MM:SS,mmm`；VTT 为 `HH:MM:SS.mmm`。
- 去空 cue、end>start、按 start 排序；单行过长按字符宽度换行（CJK 42/行，拉丁 80/行），不改变时间。
- 文件：
  - `original.srt`（始终）
  - `original.vtt`（仅 `subtitle_write_vtt=true`）
  - 翻译开启时：`<target>.srt` / `bilingual.<target>.srt`，以及 `<target>.vtt`（仅 `subtitle_write_vtt=true`）
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

## 9. 自建本地 ASR：WhisperLiveKit（可选，v0027）

参考项目：[QuentinFuxa/WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit)（Apache-2.0，Python 3.11+，FastAPI + faster-whisper/torch）。
它提供 **OpenAI 兼容 REST**，可直接作为本套件的“云端字幕 → ASR”后端，把音频留在局域网/本机：

| 能力 | WhisperLiveKit | 本套件用法 |
|---|---|---|
| 转写 | `POST /v1/audio/transcriptions`（multipart，支持 `verbose_json`/`srt`/`vtt`/`diarized_json`） | 现有 ASR 客户端直接兼容（请求 `response_format=verbose_json`；`model` 字段被 WLK 忽略） |
| 模型列表 | `GET /v1/models`（返回当前加载的单个模型） | 设置页「获取模型列表」可直接解析 |
| 鉴权 | 可选 `--api-token` / `WLK_API_TOKEN`，REST 用 `Authorization: Bearer` | 本套件“ASR API Key”填同一个 token；未设置 token 时留空 |
| 翻译 | WLK 提供服务内翻译，但**不提供** `/chat/completions` | 本套件 LLM 翻译仍指向 OpenAI 兼容 LLM（如本地 Ollama/LM Studio/vLLM） |

### 9.1 启动服务（在 NAS 的 Container Manager 或局域网服务器上）

Docker（上游 CPU 镜像构建，见其 docs/deployment.md；需先初始化 submodule）：

```bash
git clone --recurse-submodules https://github.com/QuentinFuxa/WhisperLiveKit.git
cd WhisperLiveKit
docker build -f Dockerfile.cpu -t wlk-cpu --build-arg EXTRAS=cpu .
# 默认 tiny；CPU 推荐 base/small，按 NAS 性能选择
docker run -d --name wlk -p 8000:8000 -v hf-cache:/root/.cache/huggingface/hub -e WLK_API_TOKEN=“<自定义令牌>” wlk-cpu --model base --language zh
```

或直接 pip 安装（Python 3.11–3.13）：

```bash
pip install whisperlivekit
wlk --host 0.0.0.0 --port 8000 --model base --language zh --api-token “<自定义令牌>”
```

> 模型首次使用自动下载（tiny≈75MB、base≈150MB、small≈500MB、large≈3GB）；CPU 转写速度约为实时速度的 0.1–0.5×（RTF），建议先用 base/small 评估。
> WLK 所在主机需要 `ffmpeg`（其 Docker 镜像已包含）；REST 单请求音频上限 512MB，处理预算默认 `max(120s, 2.5×音频时长)`。

### 9.2 本套件设置映射

| 设置项 | 建议值 |
|---|---|
| 启用云端字幕 | 打开 |
| ASR Base URL | `http://<WLK主机>:8000/v1` |
| ASR 模型 | 任意非空（WLK 忽略该字段）；可点「获取模型列表」选 `whisper-base` / `faster-whisper/base` 等以保持一致 |
| ASR API Key | 与 WLK 的 `--api-token` 相同；WLK 未启用 token 时留空 |
| 识别语言 | auto（不发送 language）/ zh / en / ja / ko |
| ASR 超时 | CPU 慢时建议 900–1800s |
| 音频分块 | CPU 建议 120–300s（否则单个 10 分钟分块可能超过超时） |
| 单块音频上限 | 保持 24MB 左右；分块过大会增加单请求时长 |
| LLM Base URL / 模型 | 翻译才需要：指向本地 OpenAI 兼容 LLM（如 Ollama `http://<主机>:11434/v1` + `qwen2.5:7b`），或继续用云端 |

配置后建议先点「获取模型列表」与「测试连接」：
- 测试 ASR 时本套件会发送 **1 秒静音 MP3**；WLK 对静音通常返回空 segments，但响应仍是 `verbose_json` 结构，本套件会显示“静音测试未产生转写（接口兼容）”的黄字提示，属正常。
- 真正生成字幕后，音频会按分块上传到 `subtitle_asr_base_url`；不再发往公网。

### 9.3 安全与限制

- WLK 是**独立服务**，不打包进 SPK：其 Python 3.11+、torch/faster-whisper 与模型体积远超 SPK（本套件后端保持 Python 3.8 标准库）。
- 建议部署在 NAS 的 Container Manager（x86_64 机型更稳妥；aarch64 的 torch/ctranslate2 wheel 可用性依 DSM 版本而定）或局域网 PC，仅开放给本套件主机访问，不要暴露到公网。
- 本套件只向配置的 base URL 发送音频；API Key/Token 仍遵循“只写不读、响应/日志/审计不出现密钥”的约束。WLK 自身的日志/数据保留策略需单独评估。
- 本套件的批量字幕流程是**离线文件**处理，不使用 WLK 的实时 WebSocket 流式接口；WLK 的说话人分离（`diarized_json`）暂未写入 SRT（后续可选：说话人前缀/轨道）。
- 文字翻译依赖 `/chat/completions`，WLK 不提供；若不配置 LLM，可只生成原文字幕。

### 9.4 兼容性测试

- 回归用例（`tests/test_subtitles.py::WhisperLiveKitCompatTest`）模拟 WLK 的 `/v1/models` 与 `/v1/audio/transcriptions(verbose_json)` 响应，验证：模型列表解析、Bearer token 透传、verbose_json segments 生成字幕、静音探测告警文案。
- 实际联调步骤见 9.2：启动 WLK → 设置 base URL/token → 获取模型列表/测试连接 → 选一个视频生成字幕。

## 10. 测试

- 后端 fake HTTP server：ASR verbose_json/SRT/无时间轴/401/429重试/超时；LLM 正常/长度不符/JSON 围栏/部分失败；chunk 偏移合并、no_audio、取消、密钥不回传/不日志、目录穿越、任务清理。
- v0026：fake server 增加 GET /models（排序/去重/500 上限/401/404/timeout/空列表/Authorization 记录），fake ffmpeg 增加 lavfi 静音分支；覆盖错误码映射、探测锁、密钥不泄漏/不落盘。
- v0027：模拟 WhisperLiveKit OpenAI 兼容响应（/v1/models 单模型、verbose_json 含 task/duration/text、静音空 segments）验证自建本地 ASR 可用；静音探测告警改为“接口兼容”文案。
- 前端 node --check + E2E（mock API）。
- 现有测试必须继续全部通过。

## 11. 目录批量字幕（v0028，另见独立契约）

v0028 在 v1.3 单文件任务之上新增**目录批量生成 / 翻译**：独立「字幕」页、`POST /api/subtitles/enumerate`、`POST /api/subtitles/batch`、批次控制与 `GET /api/subtitles?batch_id=`；字幕以 `.srt` 写回视频同目录（已有文件跳过、不覆盖），并支持 `source=existing_srt` 只翻译已有字幕（不跑 ASR、不上传音频）。

本节只是索引；**字段、错误码、落盘规则、测试与验收以 [SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md) 为准**。v1.3 的 `POST /api/subtitles` 保持兼容：新增字段均为可选，缺省行为不变。

## 12. v0032 批次组与服务器解码

- 一次提交最多 20000 个视频，自动拆成每批 ≤500 的批次组（详见 `SUBTITLE_BATCH_CONTRACT.md` §13）。
- `subtitle_audio_source=server`：NAS 只流复制抽音轨，ASR 服务器解码（详见 §3）；失败回退 NAS 解码并置 `warning=SERVER_AUDIO_FALLBACK`。
- 删除任务成功后，扫描分组/统计/CSV 读取自动排除已删除文件（`scan_file_deletions`，schema v7；详见 `docs/STORAGE_CONTRACT.md`）。

## 13. v0033 VTT 可选生成

- 默认**只生成 SRT**（任务目录不再产生 `.vtt`，避免无谓的重复文件/写盘）。
- 设置 `subtitle_write_vtt=true` 后，任务目录额外生成 `original.vtt` 与（翻译时）`<target>.vtt`；视频同目录仍只写 `.srt`。
- 单个视频弹窗的“首选下载格式”仅在开启 VTT 时提供 VTT 选项；历史任务（已含 VTT）仍可下载。

## 14. v0034 队列恢复、并发与任务可见性

- **重启/升级恢复**：`queued` 任务在服务启动时自动重新入队并开始执行，不再判 `SERVICE_RESTART` 失败——数千任务的批次组升级套件后不会丢队列；`running/translating` 有分片的转 `paused` 可继续，无分片的仍失败并给出 `SERVICE_RESTART`。
- **并发**：`subtitle_concurrency`（1..4，默认 1）控制同时执行的任务数；服务启动时读取，修改后需重启套件生效；`/api/meta.subtitle.concurrency_max` 给出上限。
- **可见性**：`GET /api/subtitles` 支持 `status` 过滤（见 §2）；批次组快照新增 `active_jobs`（执行中任务明细，≤10 条）、`eta_seconds`（按已完成任务平均耗时估算的剩余秒数，样本不足为 `null`）与 `concurrency`（当前生效并发）。
- **前端**：批次组卡片显示“当前执行：<文件名> · <阶段> · <进度>%”与预计剩余；任务明细顶部固定“执行中”区块并支持状态筛选（全部/执行中/排队/已完成/失败）；任务中心「字幕任务」把执行中任务置顶（先取 `status=active`，再取最近任务）。

## 15. v0035 补翻译（只做了 ASR、没翻译的历史任务）

提交时未勾选“同时生成译文”的任务（`translate=false`）完成后，可以**单独补翻译**，不需要重新跑 ASR：

- **派生任务，不改原任务**：补翻译新建一条仅翻译任务（`source=existing_srt` + `source_job_id` 指回源任务）；源任务的 `status/files/outputs/审计/警告` 完全不变。原任务与派生任务都出现在任务中心/批次组，可按来源标签区分。
- **文本来源优先级**：源任务目录 `original.srt` → 视频同目录 `<stem>.srt` → 都不存在则 `SOURCE_SRT_MISSING`（“源字幕已不存在，请重新生成”）。
  - 因此即使视频文件已删除、或输出方式是 `download_only`、或任务目录是唯一副本，也能补翻译（任务目录可下载；写回视频旁失败只记 `warning`，不判失败）。
- **不跑 FFmpeg、不调用 ASR、不上传音频**：只把字幕文本发给 LLM，成本远低于重新生成。
- **资格**：源任务 `status=done` 且（`translate=false` 或 `translation_partial=true`）。
  - 已有完整译文 → 400 `TRANSLATION_EXISTS`；源任务未完成 → 400 `JOB_NOT_DONE`。
  - 部分译文回退（`translation_partial=true`）允许“重新翻译”：派生任务 `skip_existing=false`，任务目录重新生成译文；视频旁已有译文按“从不覆盖”规则保留。
- **开关与凭据**：`subtitle_enabled` 必须开启；`subtitle_translate_enabled` 必须开启；`subtitle_llm_api_key` 必须已配置。否则分别返回 `SUBTITLE_DISABLED` / `TRANSLATE_DISABLED` / `TRANSLATE_NOT_CONFIGURED`。
- **幂等**：同一源任务已有 `queued/running/translating` 的翻译任务时，重复请求返回 `created=false` 与已有任务（HTTP 200），不会重复入队。
- **目标语言**：默认 `subtitle_translate_target`；接口 `target_lang` 可覆盖（不得为 `auto`）。
- **队列与并发**：派生任务进同一 worker 队列，受 `subtitle_concurrency` 约束；`translating` 状态计入“执行中”。
- **重启恢复**：翻译任务运行中遇服务重启 → `paused` + 可继续（没有 ASR 分片损失，重启后从源 srt 重新翻译）；`queued` 沿用 v0034 自动重新入队。
- **接口**：`POST /api/subtitles/{job_id}/translate`、`POST /api/subtitles/batch-groups/{group_id}/translate`（字段与错误码见 [SUBTITLE_BATCH_CONTRACT.md](SUBTITLE_BATCH_CONTRACT.md) §15）。
- **审计**：`subtitle_start` 带 `operation=translate` 与 `source_job_id`；组级补翻译记录 `subtitle_translate_group`（源组、创建数）。

