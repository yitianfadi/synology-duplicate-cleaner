#!/usr/bin/env node
/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 前端 ES module 图自检
   --------------------------------------------------------------------------
   在 Node 中 stub 最小 document/window/navigator/sessionStorage/localStorage，
   然后动态 import 每个 src/web/js/*.js 模块与 src/web/app.js，验证：
     1) 模块 import/export 图可被解析（无循环死锁、无缺失导出）；
     2) 各模块导出名称符合预期；
     3) 顶层代码（含 app.js 的 IIFE 注册代码）不抛异常。
   注意：document.readyState 固定为 'loading'，init() 不会执行，仅验证顶层。
   纯标准库，无第三方依赖；ES2019 语法。
   ========================================================================== */

import { pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import process from 'node:process';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const REPO = path.resolve(HERE, '..');
const WEB = path.join(REPO, 'src', 'web');
const JS_DIR = path.join(WEB, 'js');

function classListStub() {
  return {
    add: function () {},
    remove: function () {},
    toggle: function () {},
    contains: function () { return false; }
  };
}

function nodeStub(tag) {
  var node = {
    tagName: String(tag || 'div').toUpperCase(),
    hidden: false,
    disabled: false,
    checked: false,
    value: '',
    textContent: '',
    className: '',
    dataset: {},
    style: { setProperty: function () {}, removeProperty: function () {} },
    classList: classListStub(),
    childNodes: [],
    firstChild: null,
    parentNode: null,
    offsetParent: null,
    offsetHeight: 0,
    isConnected: true,
    appendChild: function (child) {
      node.childNodes.push(child);
      node.firstChild = node.childNodes[0];
      return child;
    },
    removeChild: function (child) {
      var index = node.childNodes.indexOf(child);
      if (index !== -1) { node.childNodes.splice(index, 1); }
      node.firstChild = node.childNodes[0] || null;
      return child;
    },
    setAttribute: function () {},
    getAttribute: function () { return null; },
    removeAttribute: function () {},
    addEventListener: function () {},
    removeEventListener: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    focus: function () {},
    select: function () {},
    cloneNode: function () { return nodeStub(tag); },
    contains: function () { return false; },
    scrollIntoView: function () {}
  };
  return node;
}

function storageStub() {
  var store = Object.create(null);
  return {
    getItem: function (key) { return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null; },
    setItem: function (key, value) { store[key] = String(value); },
    removeItem: function (key) { delete store[key]; },
    clear: function () { store = Object.create(null); }
  };
}

function installDomStubs() {
  var body = nodeStub('body');
  body.style = { overflow: '' };
  var documentElement = nodeStub('html');
  documentElement.style = { setProperty: function () {}, removeProperty: function () {} };
  var doc = {
    readyState: 'loading',
    activeElement: null,
    body: body,
    documentElement: documentElement,
    addEventListener: function () {},
    removeEventListener: function () {},
    getElementById: function () { return null; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    createElement: function (tag) { return nodeStub(tag); },
    createElementNS: function (_ns, tag) { return nodeStub(tag); },
    createTextNode: function (text) {
      var node = nodeStub('#text');
      node.textContent = String(text);
      return node;
    },
    execCommand: function () { return true; }
  };
  var win = {
    addEventListener: function () {},
    removeEventListener: function () {},
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    setInterval: setInterval,
    clearInterval: clearInterval,
    requestAnimationFrame: function () { return 0; },
    location: { hash: '', pathname: '/', href: 'http://127.0.0.1/' }
  };
  function defineGlobal(name, value) {
    try {
      Object.defineProperty(globalThis, name, { value: value, writable: true, configurable: true });
    } catch (err) {
      globalThis[name] = value;
    }
  }
  defineGlobal('document', doc);
  defineGlobal('window', win);
  defineGlobal('navigator', { clipboard: null, userAgent: 'dsh-web-modules-check' });
  defineGlobal('sessionStorage', storageStub());
  defineGlobal('localStorage', storageStub());
  defineGlobal('Node', function NodeStub() {});
  defineGlobal('requestAnimationFrame', win.requestAnimationFrame);
  defineGlobal('cancelAnimationFrame', function () {});
}

const EXPECTED = {
  'constants.js': [
    'CSRF_KEY', 'POLL_MS', 'GROUPS_LIMIT', 'TERMINAL_STATUS', 'ACTIVE_STATUS',
    'VIEWS', 'VIEW_HEADINGS', 'STATUS_LABEL', 'STATUS_BADGE', 'STAGE_LABEL',
    'KEEP_LABEL', 'MTIME_FALLBACK_LABEL', 'MTIME_FALLBACK_REASONS', 'KEEP_MODE_LABEL',
    'SCAN_MODE_LABEL', 'SCAN_MODE_BADGE', 'DEFAULT_SIMILARITY_EXTENSIONS',
    'SIMILARITY_OPTION_KEYS', 'DEFAULT_VIDEO_EXTENSIONS', 'DEFAULT_VIDEO_SETTINGS',
    'VIDEO_HWACCEL_LABEL', 'SOURCE_INFO', 'DELETE_STATUS_LABEL', 'DELETE_REASON_LABEL',
    'JOB_STATUS_LABEL', 'AUDIT_LABEL', 'ERROR_FALLBACK', 'DEFAULT_SETTINGS', 'SIZE_UNITS'
  ],
  'format.js': ['fmtBytes', 'fmtInt', 'fmtDateTime', 'fmtElapsed'],
  'util.js': ['clamp', 'debounce', 'clonePlain', 'normalizeExtList', 'extListToText',
    'normalizePathList', 'isUnderRoots', 'encodePath', 'legacyCopy', 'copyText'],
  'dom.js': ['$', '$$', 'byId', 'clear', 'show', 'hide', 'setHidden', 'setText', 'el',
    'appendChildren', 'icon', 'setBusy', 'getFocusable', 'focusSoon', 'scrollToNode',
    'syncTopStackHeight', 'statusBadge'],
  'state.js': ['state', 'els', 'IDS'],
  'api.js': ['ApiError', 'errorMessage', 'saveCsrf', 'api', 'setUnauthorizedHandler'],
  'toast.js': ['toast', 'removeToast'],
  'modal.js': ['openModal', 'renderFooter', 'closeModal', 'trapModalKeys', 'confirmDialog',
    'isModalOpen', 'getActiveModal', 'setModalDismissible'],
  'subtitle-probe.js': ['normalizeProbeKind', 'readProbePayload', 'fetchModelList', 'testProvider',
    'normalizeModelList', 'formatModelListHint', 'formatProbeResult', 'formatProbeError'],
  'subtitle-page.js': ['initSubtitlePage', 'showSubtitlePage', 'stopSubtitlePagePolling']
};

const MODULE_ORDER = ['constants.js', 'state.js', 'format.js', 'util.js', 'dom.js',
  'api.js', 'toast.js', 'modal.js', 'subtitle-probe.js', 'subtitle-page.js'];

const results = [];

function record(id, name, ok, detail) {
  results.push({ id: id, name: name, status: ok ? 'PASS' : 'FAIL', detail: detail || '' });
  console.log((ok ? 'PASS' : 'FAIL') + ' ' + id + ' :: ' + name + (detail ? ' :: ' + detail : ''));
}

async function importFile(file) {
  return import(pathToFileURL(file).href + '?check=' + Date.now());
}

async function main() {
  installDomStubs();

  if (!fs.existsSync(JS_DIR)) {
    record('WEB-MOD-DIR', 'src/web/js 目录存在', false, 'missing: ' + JS_DIR);
    return;
  }

  let modulesOk = true;
  for (const name of MODULE_ORDER) {
    const file = path.join(JS_DIR, name);
    if (!fs.existsSync(file)) {
      record('WEB-MOD-' + name, '模块存在并可导入 ' + name, false, 'missing file');
      modulesOk = false;
      continue;
    }
    try {
      const mod = await importFile(file);
      const expected = EXPECTED[name] || [];
      const missing = expected.filter(function (key) { return !(key in mod); });
      record('WEB-MOD-' + name, '模块存在、导出完整且顶层无异常 ' + name,
        missing.length === 0,
        missing.length ? 'missing exports: ' + missing.join(', ') : '');
      if (missing.length) { modulesOk = false; }
    } catch (err) {
      record('WEB-MOD-' + name, '模块存在、导出完整且顶层无异常 ' + name, false,
        err && err.stack ? String(err.stack).slice(0, 500) : String(err));
      modulesOk = false;
    }
  }

  const appPath = path.join(WEB, 'app.js');
  let appOk = false;
  try {
    await importFile(appPath);
    appOk = true;
    record('WEB-MOD-APP', 'app.js 作为入口可解析全部 import 且顶层无异常', true);
  } catch (err) {
    record('WEB-MOD-APP', 'app.js 作为入口可解析全部 import 且顶层无异常', false,
      err && err.stack ? String(err.stack).slice(0, 800) : String(err));
  }

  // 行为回归：state/els/IDS 为共享可变引用，api 未授权处理器可注册。
  if (modulesOk && appOk) {
    try {
      const stateMod = await importFile(path.join(JS_DIR, 'state.js'));
      const domMod = await importFile(path.join(JS_DIR, 'dom.js'));
      const utilMod = await importFile(path.join(JS_DIR, 'util.js'));
      const formatMod = await importFile(path.join(JS_DIR, 'format.js'));
      const apiMod = await importFile(path.join(JS_DIR, 'api.js'));
      const sharedOk = stateMod.state && typeof stateMod.state === 'object'
        && stateMod.els && typeof stateMod.els === 'object'
        && Array.isArray(stateMod.IDS) && stateMod.IDS.length > 100;
      record('WEB-MOD-STATE', 'state/els/IDS 共享引用导出正常', !!sharedOk,
        sharedOk ? '' : 'unexpected state shape');
      const funcOk = typeof domMod.el === 'function' && typeof domMod.statusBadge === 'function'
        && typeof utilMod.clamp === 'function' && formatMod.fmtBytes(1048576) === '1 MB'
        && typeof apiMod.setUnauthorizedHandler === 'function';
      record('WEB-MOD-HELPERS', '关键工具函数导出且行为不变（el/statusBadge/clamp/fmtBytes）',
        !!funcOk, funcOk ? '' : 'helper behavior mismatch');
    } catch (err) {
      record('WEB-MOD-HELPERS', '关键工具函数导出且行为不变（el/statusBadge/clamp/fmtBytes）',
        false, String(err));
    }
      // ---- subtitle-probe.js 纯函数与请求用例（v0026） ----
      const probeMod = await importFile(path.join(JS_DIR, 'subtitle-probe.js'));
      const kindOk = probeMod.normalizeProbeKind('LLM') === 'llm'
        && probeMod.normalizeProbeKind('bad') === 'asr';
      record('WEB-MOD-PROBE-KIND', 'normalizeProbeKind 归一 asr/llm', kindOk);
      const payloadAsr = probeMod.readProbePayload('asr', {
        baseUrl: ' https://api.example/v1 ', apiKey: '', model: '', language: 'zh', targetLang: ''
      });
      const payloadLlm = probeMod.readProbePayload('llm', {
        baseUrl: 'https://api.example/v1', apiKey: 'sk-SECRET-KEY', model: 'gpt-4.1-mini', targetLang: 'zh'
      });
      record('WEB-MOD-PROBE-PAYLOAD', 'payload 只含非空字段且 kind 归一',
        payloadAsr.kind === 'asr' && payloadAsr.base_url === 'https://api.example/v1'
        && !('api_key' in payloadAsr) && !('model' in payloadAsr) && payloadAsr.language === 'zh'
        && !('target_lang' in payloadAsr)
        && payloadLlm.kind === 'llm' && payloadLlm.api_key === 'sk-SECRET-KEY'
        && payloadLlm.target_lang === 'zh',
        JSON.stringify({ asr: Object.keys(payloadAsr), llm: Object.keys(payloadLlm) }));
      const normalized = probeMod.normalizeModelList({
        models: [{ id: 'whisper-1', owned_by: 'openai' }, { id: 'b-model' }, { id: 'b-model' }, { id: '' }],
        count: 9, truncated: true, warning: '仅展示前几个'
      });
      record('WEB-MOD-PROBE-NORMALIZE', 'normalizeModelList 去重排序并保留 truncated/warning',
        normalized.models.map(function (m) { return m.id; }).join(',') === 'b-model,whisper-1'
        && normalized.models[1].owned_by === 'openai'
        && normalized.count === 9 && normalized.truncated === true
        && normalized.warning === '仅展示前几个',
        JSON.stringify(normalized));
      const listHint = probeMod.formatModelListHint('asr', normalized);
      record('WEB-MOD-PROBE-HINT', 'formatModelListHint 文案含数量/来源/截断',
        listHint.indexOf('获取到 2 个ASR模型') !== -1 && listHint.indexOf('/models') !== -1
        && listHint.indexOf('列表已截断') !== -1,
        listHint);
      const okAsr = probeMod.formatProbeResult('asr', {
        ok: true, latency_ms: 123.4, model: 'whisper-1',
        detail: { format: 'verbose_json', segments: 5 }
      });
      const okLlm = probeMod.formatProbeResult('llm', {
        ok: true, latency_ms: 60, model: 'gpt-4.1-mini',
        detail: { text_preview: '你好，世界' }, warning: '仅供参考'
      });
      record('WEB-MOD-PROBE-RESULT', 'formatProbeResult 展示延迟/ASR 段数/LLM 预览/warning 级别',
        okAsr.level === 'ok' && okAsr.text.indexOf('延迟 123 ms') !== -1
        && okAsr.text.indexOf('段落 5 个') !== -1 && okAsr.text.indexOf('verbose_json') !== -1
        && okLlm.level === 'warn' && okLlm.text.indexOf('预览：你好，世界') !== -1
        && okLlm.text.indexOf('仅供参考') !== -1,
        JSON.stringify({ asr: okAsr, llm: okLlm }));
      const errFormatted = probeMod.formatProbeError({
        code: 'PROVIDER_AUTH_FAILED', message: '认证失败，请检查 key sk-SECRET-KEY'
      });
      record('WEB-MOD-PROBE-ERROR', 'formatProbeError 使用中文 message 且脱敏',
        errFormatted.level === 'error' && errFormatted.text.indexOf('认证失败') !== -1
        && errFormatted.text.indexOf('sk-SECRET-KEY') === -1,
        errFormatted.text);

      // ---- subtitle-page.js：v0028 目录批量字幕页 ----
      const pageMod = await importFile(path.join(JS_DIR, 'subtitle-page.js'));
      const constantsMod = await importFile(path.join(JS_DIR, 'constants.js'));
      let pageInitOk = true;
      let pageInitError = '';
      try {
        pageMod.initSubtitlePage();
        pageMod.showSubtitlePage();
        pageMod.stopSubtitlePagePolling();
      } catch (err) {
        pageInitOk = false;
        pageInitError = String(err && err.message ? err.message : err);
      }
      record('WEB-MOD-SUBTITLE-PAGE', '字幕页初始化无异常且 VIEWS/VIEW_HEADINGS 含 subtitles',
        pageInitOk && constantsMod.VIEWS.indexOf('subtitles') !== -1
        && constantsMod.VIEW_HEADINGS.subtitles === 'subtitlesHeading',
        pageInitError);
      let capturedRequests = [];
      const originalFetch = globalThis.fetch;
      try {
        globalThis.fetch = async function (url, init) {
          capturedRequests.push({ url: url, init: init });
          return {
            ok: true, status: 200,
            headers: { get: function () { return 'application/json'; } },
            json: async function () { return { ok: true, kind: 'asr', models: [] }; }
          };
        };
        await probeMod.fetchModelList('asr', {
          baseUrl: 'https://api.example/v1', apiKey: 'sk-SECRET-KEY', model: 'whisper-1'
        });
        await probeMod.testProvider('llm', {
          baseUrl: 'https://api.example/v1', apiKey: 'sk-SECRET-KEY', model: 'gpt-4.1-mini', targetLang: 'zh'
        });
      } finally {
        globalThis.fetch = originalFetch;
      }
      const firstBody = capturedRequests[0] ? JSON.parse(capturedRequests[0].init.body) : {};
      const secondBody = capturedRequests[1] ? JSON.parse(capturedRequests[1].init.body) : {};
      record('WEB-MOD-PROBE-REQUESTS', 'fetchModelList/testProvider 请求路径、方法、body 正确',
        capturedRequests.length === 2
        && capturedRequests[0].url === '/api/subtitles/models' && capturedRequests[0].init.method === 'POST'
        && firstBody.kind === 'asr' && firstBody.api_key === 'sk-SECRET-KEY'
        && firstBody.model === 'whisper-1'
        && capturedRequests[1].url === '/api/subtitles/test' && capturedRequests[1].init.method === 'POST'
        && secondBody.kind === 'llm' && secondBody.target_lang === 'zh',
        JSON.stringify(capturedRequests.map(function (item) {
          return { url: item.url, method: item.init.method };
        })));
  }

  const failed = results.filter(function (item) { return item.status !== 'PASS'; });
  console.log('WEB-MODULES ' + (failed.length ? 'FAIL' : 'PASS') + ' (' +
    (results.length - failed.length) + '/' + results.length + ')');
  process.exitCode = failed.length ? 1 : 0;
}

main().catch(function (err) {
  console.error('FATAL ' + (err && err.stack ? err.stack : String(err)));
  process.exitCode = 2;
});
