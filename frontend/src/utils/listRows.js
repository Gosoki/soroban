/**
 * 四个列表页（商品订单 / 集运 / 杂项 / 暂存）共用的「新建后 / 删除后，本地列表怎么办」。
 *
 * 为什么要收敛成一份：这四页各自写了一遍 `rows.unshift(created); total++`，于是同一个缺口
 * 在四个地方以不同形态露出来——
 *   · 筛「已送达」时新建一条「打包中」，它照样插到列表首位，分页条还显示「共 4 条」，
 *     而后端在任何查询下都给不出这个 4；刷新一下这行就消失，看起来像「刚才那单没存上」。
 *   · 停在第 3 页时新建，新行插在第 3 页的视图里，可它按排序属于第 1 页。
 *   · 只有商品订单页做了 sortRows，另外三页新建历史日期的单会一直赖在顶部。
 *   · 删除时只有商品订单页做本地剔除不重拉，被顶上来的那条在刷新前两页都看不到。
 *
 * 口径（四页必须一致，不能一页一个样）：
 *   · 没有任何筛选、且停在第 1 页 → 本地插入 + 归位 + total+1。零请求，手感最快。
 *   · 否则 → 回到第 1 页重新拉取，让**后端**决定这条该不该出现、出现在哪。
 *     拉完发现它不在列表里，才提示「不在当前筛选内」——不预判，所以提示永远是真的。
 *     选择保留筛选而不是替用户清掉：筛选是用户精心设的，清掉不可撤销；
 *     而「存了但没显示」只要说清楚就不会被误解成没存上。
 */
import { ElMessage } from 'element-plus'

/** 筛选是否生效。空串/null/undefined/空数组都算「没筛」。 */
export function anyFilterActive(filters) {
  return Object.values(filters || {}).some(
    (v) => v !== '' && v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0),
  )
}

/** 与后端 order_by 一致：日期降序，同日按 id 降序。 */
export function sortByDateDesc(rows, dateKey = 'date') {
  rows.sort((a, b) => (a[dateKey] < b[dateKey] ? 1 : a[dateKey] > b[dateKey] ? -1 : b.id - a.id))
}

/**
 * 新建成功后同步列表。返回这条记录当前是否显示在列表里。
 * `dateKey` 是该页的排序日期列（暂存页是 order_date）。
 */
export async function afterCreate(created, { rows, total, page, filters, load, dateKey = 'date' }) {
  if (!anyFilterActive(filters) && page.value === 1) {
    rows.value.unshift(created)
    sortByDateDesc(rows.value, dateKey)
    total.value++
    return true
  }
  page.value = 1
  await load()
  const shown = rows.value.some((r) => r.id === created.id)
  if (!shown) ElMessage.info('已保存，但它不在当前筛选条件内，故未显示在列表中')
  return shown
}

/** 删除成功后同步列表：必须重拉，否则被顶上来的那条在刷新前哪一页都看不到。 */
export async function afterDelete({ rows, page, load }) {
  if (rows.value.length === 1 && page.value > 1) page.value--   // 删掉本页最后一行 → 回上一页
  await load()
}
