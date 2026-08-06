// 后端时间戳的解析与展示。
//
// 后端所有时间都是 UTC（models/base.py 的 utcnow()），但 SQLite/MySQL 的 DATETIME 列存不下
// 时区，读回来是 naive，FastAPI 序列化成 "2026-08-05T10:00:00"——**不带 Z、不带偏移**。
// 而按 ECMAScript 规范，不带偏移的 date-time 一律按**本地时区**解析：UTC 10:00 会被当成
// JST 10:00 显示，凭空差 9 小时。所以必须先补 Z 再交给 Date。
//
// 这条规则原先在 Staging 页写了一份、Plugins 页漏了，导致「上次抓取」时间一直早 9 小时。
// 收敛到这里，两处共用。

/** 后端时间戳字符串 → Date；已带 Z/偏移的原样解析，naive 的按 UTC 解析。无效返回 null。 */
export function parseUtc(s) {
  if (!s) return null
  const d = new Date(/[Zz]|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + 'Z')
  return Number.isNaN(d.getTime()) ? null : d
}

/** 后端时间戳 → 本地日期 YYYY-MM-DD（无效/空显示「—」）。 */
export function fmtDate(s) {
  const d = parseUtc(s)
  if (!d) return '—'
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** 后端时间戳 → 本地日期时间（无效/空显示「—」）。 */
export function fmtDateTime(s) {
  const d = parseUtc(s)
  return d ? d.toLocaleString('ja-JP') : '—'
}

/** 今天的本地日期 YYYY-MM-DD。用户在日本(JST)，用 UTC 会让 0~9 点新建的记录记成前一天。 */
export function today() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
