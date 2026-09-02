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
import { isUnconverted } from './money'

/** 筛选是否生效。空串/null/undefined/空数组/**false** 都算「没筛」。
 *
 * `false` 那一条是必须的：开关型筛选（订单页的「仅未挂靠」）在 filters 里恒有一个
 * 布尔值。不把 false 算作「没筛」的话，这一页的 `anyFilterActive` **永远返回 true**，
 * 于是「没筛选时本地插入、零请求」那条快路径整个失效——没有任何报错，
 * 只是每次新建都多打一次库、列表闪一下。
 */
export function anyFilterActive(filters) {
  return Object.values(filters || {}).some(
    (v) => v !== '' && v !== null && v !== undefined && v !== false
      && !(Array.isArray(v) && v.length === 0),
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
  rows, total, page, filters, load, dateKey = 'date', pageSize, sumJpy, unconverted,
}) {
  if (!anyFilterActive(filters) && page.value === 1) {
    rows.value.unshift(created)
    sortByDateDesc(rows.value, dateKey)
    total.value++
    // **页脚那三个数必须一起动。** 条数走这条增量路径，而 `sum_jpy` / `unconverted`
    // 只在 `load()` 里赋值——而快路径零请求。于是页脚会变成
    // 「共 91 条 · 筛选合计 500,000 円」，屏幕上那 91 条其实是 530,000 円，
    // 少的正好是刚录的几笔，而且一直保持到下一次 load()。
    // `TableFooterSum` 自己的注释把「合计静默变小而条数照旧、界面上没有任何异常」
    // 称作这一栏最危险的失败形态——快路径正好造出这个形状。
    //
    // 增量是**精确**的，不是估算：后端的 `sum_jpy` 就是 `SUM(jpy_settled)`，
    // `unconverted` 就是逐行套 `is_unconverted`，两者都只看这一行自己的字段，
    // 而这两个字段都在新建的响应里（`MoneyOut`）。
    if (sumJpy && typeof sumJpy.value === 'number') {
      sumJpy.value += Number(created.jpy_settled) || 0
    }
    if (unconverted && isUnconverted(created)) unconverted.value++
    // **插完要截回每页条数。** 不截的话第 1 页会显示 31 行，而分页器仍按 30/页 算 ——
    // 翻到第 2 页时，第 1 页底部那条会**再出现一次**（同一个 id 显示两次），刷新才恢复。
    // 上面那几条注释逐条列了本地插入会与后端对不上的几种表现，独独漏了这一种。
    //
    // `pageSize` **由调用方传**（各页取自 constants.PAGE_SIZE），这里刻意不 import 它：
    // 本文件被 `test_consistency` 当作**纯模块**在 node 里原样跑（只桩掉 element-plus 那一句），
    // 多一个别名 import 就要多一个桩，而这个文件的价值恰恰在于「能被原样跑起来」。
    // 漏传由 `test_every_after_create_call_passes_the_page_size` 守着。
    if (pageSize && rows.value.length > pageSize) rows.value.length = pageSize
    // **截断可能把刚建的那条自己截掉。** 补录一条日期靠前的记录时就会发生
    // （杂项最常见：补上个月的手续费）——它按日期倒序排到末尾，
    // 而第 1 页已经满 30 行，正好落在截断线外。
    //
    // 原先这里无条件 `return true`：格子清空了、列表里从上到下找不到、
    // **一句提示都没有**（分页器那个「共 N 条」+1 没人会盯）。
    // 用户合理地判断「没存上」，再录一次——**而杂项支出没有任何唯一约束，
    // 同一笔钱就干干净净地记了两遍**（商品/集运还有唯一索引兜底，杂项没有）。
    // 这与本文件下面那段「刷新失败 ≠ 新建失败」防的是同一个结局，
    // 只是触发路径不同：那条是刷新挂了，这条是自己把它截掉了。
    //
    // 慢路径对同一结局是明确说话的，快路径不能哑。但**措辞不能照抄**：
    // 那句说的是「不在当前筛选条件内」，而这里一个筛选都没有——
    // 真实原因是它排到了当前页之后。说错原因和不说一样会把人引偏。
    const shown = rows.value.some((r) => r.id === created.id)
    if (!shown) ElMessage.info('已保存，但它按日期排在当前页之后，翻页或刷新即可看到')
    return shown
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
