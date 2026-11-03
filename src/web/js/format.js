/* ==========================================================================
   重复文件清理 · DuplicateCleaner —— 数字 / 大小 / 时间格式化（ES module）
   由 app.js 第 2 节拆出；输出文案保持完全一致。
   ========================================================================== */

export function fmtBytes(bytes, digits) {
  var n = Number(bytes);
  if (!isFinite(n) || n <= 0) { return '0 B'; }
  var units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  var i = 0;
  var v = n;
  while (v >= 1024 && i < units.length - 1) { v = v / 1024; i += 1; }
  var d = digits;
  if (d === undefined || d === null) {
    d = i === 0 ? 0 : (v >= 100 ? 0 : (v >= 10 ? 1 : 2));
  }
  var s = v.toFixed(d);
  if (s.indexOf('.') !== -1) { s = s.replace(/0+$/, '').replace(/\.$/, ''); }
  return s + ' ' + units[i];
}

export function fmtInt(value) {
  var n = Number(value);
  if (!isFinite(n)) { return '0'; }
  try { return n.toLocaleString('zh-CN'); } catch (e) { return String(n); }
}

export function fmtDateTime(iso) {
  if (!iso) { return '—'; }
  var d = new Date(iso);
  if (isNaN(d.getTime())) { return String(iso); }
  function p(n) { return n < 10 ? '0' + n : String(n); }
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
    p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

export function fmtElapsed(seconds) {
  var sec = Number(seconds);
  if (!isFinite(sec) || sec < 0) { return '—'; }
  sec = Math.round(sec);
  if (sec < 60) { return sec + ' 秒'; }
  var m = Math.floor(sec / 60);
  var s = sec % 60;
  if (m < 60) { return m + ' 分 ' + s + ' 秒'; }
  var h = Math.floor(m / 60);
  return h + ' 小时 ' + (m % 60) + ' 分';
}
