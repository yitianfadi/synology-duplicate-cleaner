/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 通用工具（ES module）
   由 app.js 第 2 节拆出；纯函数 + 剪贴板复制，无业务状态。
   ========================================================================== */

export function clamp(value, min, max) { return Math.min(max, Math.max(min, value)); }

export function debounce(fn, wait) {
  var timer = null;
  return function () {
    var args = arguments;
    var self = this;
    if (timer) { clearTimeout(timer); }
    timer = setTimeout(function () { timer = null; fn.apply(self, args); }, wait);
  };
}

export function clonePlain(value) {
  try { return JSON.parse(JSON.stringify(value)); } catch (e) { return null; }
}

/** 解析扩展名输入：逗号/空格分隔，自动补点号，最长 16 字符（含点）。 */
export function normalizeExtList(raw) {
  var text = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!text) { return { list: [], error: '' }; }
  var parts = text.split(/[\s,，;；]+/).filter(function (s) { return s.length > 0; });
  var list = [];
  for (var i = 0; i < parts.length; i += 1) {
    var v = parts[i].trim();
    if (!v) { continue; }
    if (v.charAt(0) !== '.') { v = '.' + v; }
    if (v === '.' || /[\\/]/.test(v)) {
      return { list: [], error: '后缀“' + parts[i] + '”格式不正确，只能包含字母、数字、点、下划线和短横线' };
    }
    if (v.length > 16) {
      return { list: [], error: '后缀“' + parts[i] + '”过长（含点号最多 16 个字符）' };
    }
    if (list.indexOf(v) === -1) { list.push(v); }
  }
  return { list: list, error: '' };
}

export function extListToText(list) {
  if (!Array.isArray(list)) { return ''; }
  return list.join(',');
}

export function normalizePathList(list) {
  if (!Array.isArray(list)) { return []; }
  var out = [];
  list.forEach(function (item) {
    var p = typeof item === 'string' ? item : (item && item.path);
    if (typeof p === 'string' && p && out.indexOf(p) === -1) { out.push(p); }
  });
  return out;
}

export function isUnderRoots(path, roots) {
  if (!path || !roots || !roots.length) { return false; }
  return roots.some(function (root) {
    var rp = typeof root === 'string' ? root : root.path;
    if (!rp) { return false; }
    var normalized = String(rp).replace(/[\\/]+$/, '');
    return path === normalized ||
      path.indexOf(normalized + '/') === 0 ||
      path.indexOf(normalized + '\\') === 0;
  });
}

export function encodePath(path) { return encodeURIComponent(String(path === null || path === undefined ? '' : path)); }
export function legacyCopy(text) {
  return new Promise(function (resolve, reject) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (ok) { resolve(); } else { reject(new Error('当前浏览器不支持自动复制')); }
    } catch (e) { reject(e); }
  });
}

export function copyText(text) {
  var value = String(text === null || text === undefined ? '' : text);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(value).catch(function () { return legacyCopy(value); });
  }
  return legacyCopy(value);
}
