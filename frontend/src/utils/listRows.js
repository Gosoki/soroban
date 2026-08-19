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

/** 与后端 order_by 一致：日期降序，同日按 id 降序。
 *
 * **空值必须先挑出来**，不能直接丢进 `<` / `>`：JS 里 `null < 'x'` 与 `null > 'x'` **都是 false**，
 * 于是含空值的行对会一路落到 `b.id - a.id`，而那与「按日期排」不是同一个序——
 * 结果是比较器**不满足传递性**，`Array.sort` 的行为随输入顺序而变。
 * 实测（node）：同一批 6 行只改输入顺序得到 **3 种不同结果**，而且**连非空行都会排错**
 * （08-03 被排到 08-02 前面这种本来正确的对，被环带歪）。
 * 空的排末尾：它们在后端也排在最后（NULL 在 desc 里靠后），口径一致。
 */
export function sortByDateDesc(rows, dateKey = 'date') {
  rows.sort((a, b) => {
    const x = a[dateKey], y = b[dateKey]
    if (x == null && y == null) return b.id - a.id
    if (x == null) return 1                    // 空的往后
    if (y == null) return -1
    return x < y ? 1 : x > y ? -1 : b.id - a.id
  })
}


/**
 * 新建成功后同步列表。返回这条记录当前是否显示在列表里。
 * `dateKey` 是该页的排序日期列——**必须与那一页后端的 order_by 同一列**。
 * 暂存页是 `scraped_at`（后端 `staging.py` 排的就是它），不是 order_date：
 * 后者可以为 NULL（OCR 认不出「下单时间」就不下发这个键、幽灵行也不预填），
 * 拿它排序会让本地插入的顺序与刷新后的顺序对不上。
 */
export async function afterCreate(created, {
  rows, total, page, filters, load, dateKey = 'date', pageSize,
}) {
  if (!anyFilterActive(filters) && page.value === 1) {
    rows.value.unshift(created)
    sortByDateDesc(rows.value, dateKey)
    total.value++
    // **插完要截回每页条数。** 不截的话第 1 页会显示 31 行，而分页器仍按 30/页 算 ——
    // 翻到第 2 页时，第 1 页底部那条会**再出现一次**（同一个 id 显示两次），刷新才恢复。
    // 上面那几条注释逐条列了本地插入会与后端对不上的几种表现，独独漏了这一种。
    //
    // `pageSize` **由调用方传**（各页取自 constants.PAGE_SIZE），这里刻意不 import 它：
    // 本文件被 `test_consistency` 当作**纯模块**在 node 里原样跑（只桩掉 element-plus 那一句），
    // 多一个别名 import 就要多一个桩，而这个文件的价值恰恰在于「能被原样跑起来」。
    // 漏传由 `test_every_after_create_call_passes_the_page_size` 守着。
    if (pageSize && rows.value.length > pageSize) rows.value.length = pageSize
    return true
  }
  page.value = 1
  // **刷新失败 ≠ 新建失败。** 这一句原先是裸 await：列表页的 `load()` 只有 try/finally
  // （没有 catch），所以刷新挂掉会一路抛出 `afterCreate`，被调用方的 catch 接住并
  // `done(false)` ⇒ NotionTable **不清草稿**（那是「没保存成功」的语义）⇒
  // 用户看着自己刚敲的字还在，以为没存上，再按一次回车。
  // 而那一笔**已经落库了**：商品/暂存/集运撞唯一索引会得到一句莫名的「已存在」，
  // 而**杂项支出没有任何唯一约束——同一笔钱会干干净净地记两遍。**
  //
  // 这条链上每一环单独看都合理，合起来才是数据错误。判据只有一条：
  // 草稿该不该清，只取决于**新建成功没有**，与列表刷新得不得动无关。
  let refreshed = true
  try {
    await load()
  } catch (_) {
    refreshed = false
    // 拦截器已经提示过失败原因；这里补的是「你那笔存住了」——
    // 这句话必须说，否则用户唯一的信息是一条报错，他会认为没存上。
    ElMessage.warning('已保存，但列表没能刷新——请手动刷新查看')
  }
  // 刷新没成功时 rows 还是旧的，`some` 必然找不到新建那条；
  // 此时那句「不在当前筛选条件内」是**假话**（它只是没被拉回来）。
  const shown = refreshed && rows.value.some((r) => r.id === created.id)
  if (refreshed && !shown) ElMessage.info('已保存，但它不在当前筛选条件内，故未显示在列表中')
  return shown
}

/** 删除成功后同步列表：必须重拉，否则被顶上来的那条在刷新前哪一页都看不到。 */
export async function afterDelete({ rows, page, load }) {
  if (rows.value.length === 1 && page.value > 1) page.value--   // 删掉本页最后一行 → 回上一页
  await load()
}
