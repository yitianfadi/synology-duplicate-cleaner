/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 同源 API 客户端（ES module）
   由 app.js 第 3 节拆出；CSRF / 超时 / 401 行为与原来一致，
   401 通过 setUnauthorizedHandler 回调交给 app.js 处理。
   ========================================================================== */

import { CSRF_KEY, ERROR_FALLBACK } from './constants.js';
import { state } from './state.js';

var unauthorizedHandler = null;

/** 注册 401 未授权处理器（由 app.js 注入，避免循环依赖）。 */
export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = typeof handler === 'function' ? handler : null;
}

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message || '请求失败');
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function errorMessage(err, fallback) {
  if (!err) { return fallback || '操作失败，请稍后重试。'; }
  if (err.name === 'AbortError') { return '请求已取消。'; }
  if (err instanceof ApiError) {
    if (err.message) { return err.message; }
    return ERROR_FALLBACK[err.code] || fallback || '操作失败，请稍后重试。';
  }
  if (err.message) { return err.message; }
  return fallback || '网络异常，请检查连接后重试。';
}

export function saveCsrf(token) {
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
export async function api(path, options) {
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
    if (unauthorizedHandler) { unauthorizedHandler(); }
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
