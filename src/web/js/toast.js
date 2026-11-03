/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— Toast 提示（ES module）
   由 app.js 第 4 节拆出；自动消失 / 最多 4 条的行为不变。
   ========================================================================== */

import { els } from './state.js';
import { $$, el, icon } from './dom.js';

export function toast(message, type, duration) {
  type = type || 'info';
  if (!els.toastRoot) { return; }
  var current = $$('.toast', els.toastRoot);
  if (current.length >= 4) { removeToast(current[0]); }
  var iconName = type === 'error' ? 'warning' : (type === 'success' ? 'check' : 'info');
  var node = el('div', {
    className: 'toast toast-' + type,
    attrs: { role: type === 'error' ? 'alert' : 'status' }
  }, [icon(iconName), el('span', { className: 'toast-msg', text: message })]);
  els.toastRoot.appendChild(node);
  var ms = duration || (type === 'error' ? 5600 : 3200);
  node.__timer = setTimeout(function () { removeToast(node); }, ms);
}

export function removeToast(node) {
  if (!node || node.__removed) { return; }
  node.__removed = true;
  if (node.__timer) { clearTimeout(node.__timer); }
  node.classList.add('is-leaving');
  setTimeout(function () {
    if (node.parentNode) { node.parentNode.removeChild(node); }
  }, 220);
}
