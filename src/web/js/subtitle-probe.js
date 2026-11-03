/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 云端字幕服务探测（ES module）
   --------------------------------------------------------------------------
   v0026：字幕设置页“获取模型列表 / 测试连接”的纯逻辑与 API 调用。
   - 后端接口（固定契约）：
       POST /api/subtitles/models  {kind, base_url?, api_key?, model?, language?, target_lang?}
       POST /api/subtitles/test    （同上）
   - API Key 只写入请求体，绝不拼接进任何提示/结果文本；所有格式化函数只
     接收服务端响应或错误对象。
   - ES2019；无 DOM 依赖，便于 Node 单元测试。
   ========================================================================== */

import { api } from './api.js';

var PROBE_ERROR_FALLBACK = {
  PROVIDER_AUTH_FAILED: '服务认证失败，请检查 API Key。',
  MODELS_UNSUPPORTED: '该服务未提供模型列表接口。',
  PROBE_BUSY: '已有测试请求进行中，请稍后重试。',
  TIMEOUT: '请求超时，请检查服务地址与网络。',
  NETWORK: '无法连接服务，请检查 Base URL 与网络。',
  INVALID_ARGUMENT: '请求参数不正确，请检查配置。'
};

function trimText(value) {
  return String(value === null || value === undefined ? '' : value).trim();
}

/** 去除文本中可能出现的密钥形态（防御性：结果/提示永不展示 API Key）。 */
function redactSecrets(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/sk-[A-Za-z0-9_-]{4,}/g, 'sk-***')
    .replace(/(api[_-]?key\s*[=:]\s*)[^\s,;]+/gi, '$1***');
}

function kindLabel(kind) {
  return normalizeProbeKind(kind) === 'llm' ? 'LLM' : 'ASR';
}

/** 归一化探测类型：未知值一律按 asr 处理。 */
export function normalizeProbeKind(kind) {
  var value = trimText(kind).toLowerCase();
  return value === 'llm' ? 'llm' : 'asr';
}

/**
 * 构造探测请求体：kind 必带，其余字段仅在非空时提交。
 * provider: {baseUrl, apiKey, model, language, targetLang}
 */
export function readProbePayload(kind, provider) {
  var source = provider || {};
  var payload = { kind: normalizeProbeKind(kind) };
  var baseUrl = trimText(source.baseUrl);
  if (baseUrl) { payload.base_url = baseUrl; }
  var apiKey = trimText(source.apiKey);
  if (apiKey) { payload.api_key = apiKey; }
  var model = trimText(source.model);
  if (model) { payload.model = model; }
  var language = trimText(source.language);
  if (language) { payload.language = language; }
  var targetLang = trimText(source.targetLang);
  if (targetLang) { payload.target_lang = targetLang; }
  return payload;
}

/** 获取服务端模型列表（POST /api/subtitles/models）。 */
export function fetchModelList(kind, provider) {
  return api('/api/subtitles/models', {
    method: 'POST',
    body: readProbePayload(kind, provider),
    timeout: 30000
  });
}

/** 测试 ASR/LLM 连接（POST /api/subtitles/test）。 */
export function testProvider(kind, provider) {
  return api('/api/subtitles/test', {
    method: 'POST',
    body: readProbePayload(kind, provider),
    timeout: 90000
  });
}

/** 归一化模型列表：去空、去重、按 id 排序，保留 count/truncated/warning。 */
export function normalizeModelList(res) {
  var source = res || {};
  var raw = Array.isArray(source) ? source : (Array.isArray(source.models) ? source.models : []);
  var models = [];
  var seen = {};
  raw.forEach(function (item) {
    var id = typeof item === 'string' ? trimText(item) : trimText(item && item.id);
    if (!id || seen[id]) { return; }
    seen[id] = true;
    models.push({
      id: id,
      owned_by: (item && typeof item === 'object') ? trimText(item.owned_by) : ''
    });
  });
  models.sort(function (a, b) {
    if (a.id < b.id) { return -1; }
    if (a.id > b.id) { return 1; }
    return 0;
  });
  var count = Number(source.count);
  return {
    models: models,
    count: isFinite(count) && count >= 0 ? count : models.length,
    truncated: source.truncated === true,
    warning: trimText(source.warning)
  };
}

/** 模型列表提示文案（不包含 API Key / Base URL 凭据）。 */
export function formatModelListHint(kind, list) {
  var label = kindLabel(kind);
  var models = Array.isArray(list) ? list : (list && Array.isArray(list.models) ? list.models : []);
  var text = '获取到 ' + models.length + ' 个' + label + '模型（来自服务端 /models，未按能力过滤）';
  if (list && !Array.isArray(list)) {
    if (list.truncated) { text += '；列表已截断'; }
    if (list.warning) { text += '；' + trimText(list.warning); }
  }
  return redactSecrets(text);
}

/** 连接测试成功/警告结果：{level:'ok'|'warn', text}。 */
export function formatProbeResult(kind, res) {
  var source = res || {};
  var label = kindLabel(kind);
  var parts = [label + ' 连接测试成功'];
  var latency = Number(source.latency_ms);
  if (isFinite(latency) && latency >= 0) { parts.push('延迟 ' + Math.round(latency) + ' ms'); }
  var model = trimText(source.model);
  if (model) { parts.push('模型 ' + model); }
  var mode = trimText(source.mode);
  if (mode) { parts.push('模式 ' + mode); }
  var detail = source.detail;
  if (detail && typeof detail === 'object') {
    var formatText = trimText(detail.format);
    if (formatText) { parts.push('格式 ' + formatText); }
    var segments = Number(detail.segments);
    if (isFinite(segments) && segments >= 0) { parts.push('段落 ' + Math.round(segments) + ' 个'); }
    var preview = trimText(detail.text_preview || detail.preview || detail.text);
    if (preview) { parts.push('预览：' + preview.slice(0, 80)); }
  } else if (trimText(detail)) {
    parts.push(trimText(detail).slice(0, 160));
  }
  // 兼容顶层 warning 与 detail.warning（后端两种都保留）。
  var warning = trimText(source.warning)
    || (detail && typeof detail === 'object' ? trimText(detail.warning) : '');
  if (warning) { parts.push(warning); }
  return { level: warning ? 'warn' : 'ok', text: redactSecrets(parts.join(' · ')) };
}

/** 连接测试失败：{level:'error', text}；永不包含 API Key。 */
export function formatProbeError(err) {
  var source = err || {};
  var code = trimText(source.code);
  var message = trimText(source.message);
  if (!message) {
    message = PROBE_ERROR_FALLBACK[code] || '连接测试失败，请检查配置后重试。';
  }
  return { level: 'error', text: redactSecrets(message) };
}
