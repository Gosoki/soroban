// 金额展示格式化。后端是权威：派生的日元由 compute_money 算出，保存后以返回值为准。

// 日元：单位「円」放数字后面（12,345円）；人民币：「￥」放数字前面（￥123.00）
// 非数字（null/空/意外字符串）一律降级为「—」占位，绝不把 NaN 漏给用户看。
export function fmtJPY(n) {
  const x = Number(n)
  return n === null || n === undefined || n === '' || !Number.isFinite(x) ? '—' : x.toLocaleString('ja-JP') + '円'
}

export function fmtCNY(n) {
  const x = Number(n)
  return n === null || n === undefined || n === '' || !Number.isFinite(x) ? '—' : '￥' + x.toFixed(2)
}

/** 这一行算不算「有钱、却没折算成日元」。
 *
 * **这是同一条判据的第三种形态。** 另外两种在后端：
 *   · `app/models/base.py::is_unconverted`  —— Python 形态，全仓唯一真相
 *   · `app/models/base.py::unconverted_clause` —— SQL 形态，给聚合用
 * 那两份的文档写着它们历史上分叉过两次（审计报告 §151.3、§169），每次都是漏抄 `!= 0`，
 * 现象不是报错而是**两个数字互相打脸**：同一件事，页脚说 1 条、看板说 0 条。
 *
 * 所以这一份不靠「记得三处都改」这条约定活着：
 * `test_the_unconverted_rule_says_the_same_thing_in_python_and_in_the_browser`
 * 拿同一张用例表同时喂给 Python 版和这一份，两边必须逐条一致。
 * 改判据时三处一起改，那条守卫会当场告诉你漏了哪一份。
 *
 * `!= 0` 那一条是必须的：显式填 0 的行（预付 / 包邮 / 全是赠品）折算过去也是 0 円，
 * 没有任何金额会被合计吞掉，报出来只是噪音——而用户按告警去补汇率也消不掉它。
 */
export function isUnconverted(row) {
  const p = row?.price_cny
  if (p === null || p === undefined || p === '') return false
  const n = Number(p)
  if (!Number.isFinite(n) || n === 0) return false
  return row.jpy_settled === null || row.jpy_settled === undefined
}
