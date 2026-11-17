/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 共享标签 / 判定函数（v0057 从 app.js 拆出）
   --------------------------------------------------------------------------
   行为与拆出前完全一致；依赖 js/ 下的基础模块，不依赖 app.js。ES2019。
   规范：不使用 innerHTML，不可信文本一律 textContent。
   ========================================================================== */


export function detectScanMode(scan) {
  if (!scan) { return 'exact'; }
  var candidates = [
    scan.scan_mode,
    scan.config && scan.config.scan_mode,
    scan.options && scan.options.scan_mode
  ];
  for (var i = 0; i < candidates.length; i += 1) {
    if (candidates[i] === 'similar') { return 'similar'; }
    if (candidates[i] === 'exact') { return 'exact'; }
  }
  return 'exact';
}

export function reasonLabel(map, reason, fallback) {
  return (map && map[reason]) || fallback || reason || '—';
}
