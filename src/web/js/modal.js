/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 弹窗系统（ES module）
   由 app.js 第 5 节拆出；焦点陷阱 / ESC / 焦点还原行为不变。
   activeModal/modalSeq 为模块私有状态，app.js 通过导出函数访问。
   ========================================================================== */

import { els } from './state.js';
import { $, clear, el, focusSoon, getFocusable, hide, icon, setText, show } from './dom.js';

var activeModal = null;
var modalSeq = 0;
export function openModal(options) {
  options = options || {};
  if (activeModal) { closeModal(true); }
  var root = els.modalRoot;
  clear(root);
  var previousFocus = document.activeElement;
  var titleId = 'modal-title-' + (++modalSeq);
  var dismissible = options.dismissible !== false;
  var titleEl = el('h2', { className: 'modal-title', attrs: { id: titleId, tabindex: '-1' }, text: options.title || '对话框' });
  var headMain = el('div', { className: 'modal-head-main' }, [
    titleEl,
    options.subtitle ? el('p', { className: 'modal-subtitle', text: options.subtitle }) : null
  ]);
  var header = el('div', { className: 'modal-header' }, [headMain]);
  var closeBtn = null;
  if (dismissible) {
    closeBtn = el('button', {
      className: 'modal-close', attrs: { type: 'button', 'aria-label': '关闭对话框' },
      on: { click: function () { closeModal(); } }
    }, [icon('close')]);
    header.appendChild(closeBtn);
  }
  var bodyEl = el('div', { className: 'modal-body' }, options.body || null);
  var footerEl = el('div', { className: 'modal-footer' });
  renderFooter(footerEl, options.footer);
  var sizeClass = options.size === 'lg' ? ' modal-lg' : (options.size === 'xl' ? ' modal-xl' : '');
  var modal = el('div', {
    className: 'modal' + sizeClass,
    attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId }
  }, [header, bodyEl, footerEl]);
  var backdrop = el('div', {
    className: 'modal-backdrop',
    on: { click: function () { if (activeModal && activeModal.dismissible) { closeModal(); } } }
  });
  root.appendChild(backdrop);
  root.appendChild(modal);
  hide(root);
  show(root);
  document.body.style.overflow = 'hidden';

  var entry = {
    root: root, modal: modal, bodyEl: bodyEl, footerEl: footerEl, titleEl: titleEl,
    previousFocus: previousFocus, dismissible: dismissible, onClose: options.onClose || null
  };
  activeModal = entry;

  var controller = {
    modal: modal,
    bodyEl: bodyEl,
    setTitle: function (text) { setText(titleEl, text); },
    setSubtitle: function (text) {
      var sub = $('.modal-subtitle', header);
      if (!sub && text) { sub = el('p', { className: 'modal-subtitle' }); headMain.appendChild(sub); }
      if (sub) { setText(sub, text || ''); sub.hidden = !text; }
    },
    setBody: function (node) { clear(bodyEl); if (node) { bodyEl.appendChild(node); } },
    setFooter: function (footer) { clear(footerEl); renderFooter(footerEl, footer); },
    setBusy: function (busy) { closeBtn.disabled = !!busy; },
    close: function () { closeModal(true); }
  };

  var focusTarget = options.initialFocus;
  if (typeof focusTarget === 'string') { focusTarget = $(focusTarget, modal); }
  var first = focusTarget || getFocusable(modal)[0] || titleEl;
  focusSoon(first);
  return controller;
}

export function renderFooter(footerEl, footer) {
  if (!footer) { return; }
  if (Array.isArray(footer)) {
    footer.forEach(function (node) { if (node) { footerEl.appendChild(node); } });
    return;
  }
  if (footer.left) {
    footerEl.appendChild(el('div', { className: 'footer-left' }, footer.left));
  }
  if (footer.right) {
    (Array.isArray(footer.right) ? footer.right : [footer.right]).forEach(function (node) {
      if (node) { footerEl.appendChild(node); }
    });
  }
}

export function closeModal(force) {
  var entry = activeModal;
  if (!entry) { return; }
  if (!entry.dismissible && force !== true) { return; }
  activeModal = null;
  if (entry.onClose) {
    try { entry.onClose(); } catch (e) { /* 忽略清理异常 */ }
  }
  clear(entry.root);
  hide(entry.root);
  document.body.style.overflow = '';
  if (entry.previousFocus && entry.previousFocus.isConnected) {
    try { entry.previousFocus.focus({ preventScroll: true }); } catch (e2) { /* 忽略 */ }
  }
}

export function trapModalKeys(event) {
  if (!activeModal) { return; }
  if (event.key === 'Escape') {
    if (activeModal.dismissible) {
      event.preventDefault();
      closeModal();
    }
    return;
  }
  if (event.key !== 'Tab') { return; }
  var modal = activeModal.modal;
  var focusables = getFocusable(modal);
  if (!focusables.length) { event.preventDefault(); return; }
  var first = focusables[0];
  var last = focusables[focusables.length - 1];
  var current = document.activeElement;
  if (!modal.contains(current)) { event.preventDefault(); first.focus(); return; }
  if (event.shiftKey && current === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && current === last) { event.preventDefault(); first.focus(); }
}

export function confirmDialog(options) {
  return new Promise(function (resolve) {
    var settled = false;
    var cancelBtn = el('button', {
      className: 'btn btn-ghost', attrs: { type: 'button' }, text: options.cancelText || '取消'
    });
    var okBtn = el('button', {
      className: 'btn ' + (options.danger === false ? 'btn-primary' : 'btn-danger'),
      attrs: { type: 'button' }, text: options.confirmText || '确认'
    });
    function finish(value) {
      if (settled) { return; }
      settled = true;
      closeModal(true);
      resolve(value);
    }
    cancelBtn.addEventListener('click', function () { finish(false); });
    okBtn.addEventListener('click', function () { finish(true); });
    openModal({
      title: options.title || '请确认',
      body: el('div', {}, options.body || el('p', { className: 'modal-note', text: options.message || '' })),
      size: 'md',
      footer: [cancelBtn, okBtn],
      initialFocus: options.danger === false ? okBtn : cancelBtn,
      onClose: function () { if (!settled) { settled = true; resolve(false); } }
    });
  });
}

/** 是否存在打开的弹窗（供 app.js 的焦点管理使用）。 */
export function isModalOpen() {
  return activeModal !== null;
}

/** 返回当前弹窗条目（只读用途：root/modal/dismissible 等）。 */
export function getActiveModal() {
  return activeModal;
}

/** 更新当前弹窗是否可关闭（关闭按钮 / ESC / 遮罩）。 */
export function setModalDismissible(value) {
  if (activeModal) { activeModal.dismissible = !!value; }
}
