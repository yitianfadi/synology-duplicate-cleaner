/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— DOM 构造 / 显示 / 焦点工具（ES module）
   由 app.js 第 2 节拆出；只做安全文本渲染，不使用 innerHTML。
   ========================================================================== */

import { STATUS_LABEL, STATUS_BADGE } from './constants.js';

export function $(sel, root) { return (root || document).querySelector(sel); }
export function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
export function byId(id) { return document.getElementById(id); }
export function clear(node) { if (!node) return; while (node.firstChild) { node.removeChild(node.firstChild); } }
export function show(node) { if (node) { node.hidden = false; } }
export function hide(node) { if (node) { node.hidden = true; } }
export function setHidden(node, hidden) { if (node) { node.hidden = !!hidden; } }
export function setText(node, text) { if (node) { node.textContent = (text === null || text === undefined) ? '' : String(text); } }

/** 安全创建 DOM 节点。opts: {className, text, attrs, dataset, on} */
export function el(tag, opts, children) {
  var node = document.createElement(tag);
  if (opts) {
    if (opts.className) { node.className = opts.className; }
    if (opts.text !== undefined && opts.text !== null) { node.textContent = String(opts.text); }
    if (opts.attrs) {
      Object.keys(opts.attrs).forEach(function (k) {
        var v = opts.attrs[k];
        if (v === null || v === undefined || v === false) { return; }
        if (k === 'checked' || k === 'disabled' || k === 'selected') { node[k] = !!v; return; }
        if (k === 'value') { node.value = v; return; }
        node.setAttribute(k, v === true ? '' : String(v));
      });
    }
    if (opts.dataset) {
      Object.keys(opts.dataset).forEach(function (k) { node.dataset[k] = String(opts.dataset[k]); });
    }
    if (opts.on) {
      Object.keys(opts.on).forEach(function (ev) { node.addEventListener(ev, opts.on[ev]); });
    }
  }
  appendChildren(node, children);
  return node;
}

export function appendChildren(node, children) {
  if (children === null || children === undefined) { return; }
  if (Array.isArray(children)) {
    children.forEach(function (child) { appendChildren(node, child); });
    return;
  }
  if (children instanceof Node) { node.appendChild(children); }
  else { node.appendChild(document.createTextNode(String(children))); }
}

export function icon(name, extraClass) {
  var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon' + (extraClass ? ' ' + extraClass : ''));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  use.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', '#i-' + name);
  svg.appendChild(use);
  return svg;
}
export function setBusy(btn, busy, label) {
  if (!btn) { return; }
  if (busy) {
    if (!btn.__originalChildren) {
      btn.__originalChildren = Array.prototype.slice.call(btn.childNodes).map(function (n) { return n.cloneNode(true); });
    }
    btn.disabled = true;
    clear(btn);
    btn.appendChild(el('span', { className: 'spinner' }));
    btn.appendChild(document.createTextNode(label || '处理中…'));
  } else {
    btn.disabled = false;
    if (btn.__originalChildren) {
      clear(btn);
      btn.__originalChildren.forEach(function (n) { btn.appendChild(n.cloneNode(true)); });
    }
  }
}
export function getFocusable(root) {
  return $$('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])', root)
    .filter(function (node) { return node.offsetParent !== null || node === document.activeElement; });
}

export function focusSoon(node) {
  if (!node) { return; }
  setTimeout(function () {
    try { node.focus({ preventScroll: true }); } catch (e) { node.focus(); }
  }, 0);
}

export function scrollToNode(node) {
  if (!node) { return; }
  try { node.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (e) { node.scrollIntoView(); }
}

export function syncTopStackHeight() {
  var stack = document.querySelector('.top-stack');
  if (!stack) { return; }
  var height = stack.offsetHeight;
  if (height > 0) {
    document.documentElement.style.setProperty('--top-stack-h', height + 'px');
  }
}

export function statusBadge(status) {
  var label = STATUS_LABEL[status] || status || '未知';
  return el('span', { className: 'badge ' + (STATUS_BADGE[status] || 'badge-gray'), text: label });
}
