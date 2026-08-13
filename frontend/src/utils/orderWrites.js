// 同一订单的 PATCH 串行化。
// 订单页格子、展开面板、整单编辑面板各自独立发 ordersApi.update，都在调用时读一次
// order.version、拿到响应后才自增。两次编辑若在第一个响应回来前重叠，会带着同一个旧
// version 发出，第二个必 409 → 前端提示「已刷新」并整表重载，用户刚敲的那笔被悄悄丢掉。
//
// 这里按订单 id 把写操作排成一条链：后一个任务在前一个任务（含其 version 回写）完成后才跑，
// 从而恒读到最新 version。任务里必须**完整**包含「读 version → PATCH → 回写 order」，
// 才能保证下一个任务读到的是回写后的新版本。
const chains = new Map()

export function queueOrderWrite(orderId, task) {
  const prev = chains.get(orderId) || Promise.resolve()
  const run = prev.then(task, task)   // 前一个无论成败都接着跑本任务，避免一次失败卡死整条链
  const tail = run.catch(() => {}).finally(() => {
    if (chains.get(orderId) === tail) chains.delete(orderId)   // 链尾跑完即回收，防 Map 泄漏
  })
  chains.set(orderId, tail)
  return run                          // 调用方拿到本任务的真实结果/异常
}

// 响应里的 items 什么时候是权威的：**本次 PATCH 自己送了 items** 才覆盖。
// 订单行与暂存行同用（两者都是「标量 + 可选 items 数组」的形状），故不叫 applyOrderUpdate。
//
// 送了 items 时后端会 build_items 整体替换（种子价会被折算成第一条的单价），响应才是真相；
// 没送 items 时后端根本不碰它（`payload.items is None`），此时响应里那份 items 与本地等价，
// 覆盖过去只会把展开面板里用户正在敲、尚未落库的物品数组整个换掉。
//
// 两个方向都出过事：OCR 按单号合并**送了** items 却照抄「排除 items」的写法，
// 页面上的物品停在合并前的「单价 0 / auto=true」，随后任何一次物品编辑都会把这份陈旧数组
// 整体 PATCH 回去，刚补进去的钱就没了。判据只有一个——看 patch 里有没有 items。
// `itemsStale`：送出去之后、响应回来之前，本地的 items 又被人改过了。
// 此时那份响应算的是**旧数组**，整体覆盖等于把用户这几百毫秒里敲的东西回滚掉——
// 而且是当着他的面回滚：`el-input` 的 nativeInputValue watcher **没有聚焦豁免**
// （唯一那条 activeElement 判断只在带 `.trim` 修饰符时生效），
// 会直接改写正在输入的那个 DOM 节点、光标弹到末尾。用户多半没看出来，接着敲，
// 于是拼出一个错名并在下一次保存时写进库。全程无提示、无撤销。
//
// 这种情况只采纳标量。代价是这一轮拿不到后端重折算的单价——但既然用户正在编辑，
// 他下一次保存会把完整数组再送一遍，那时就对齐了。
// 判据放在调用方：只有它知道自己送出去的是哪一份。
export function applyRowUpdate(target, patch, updated, { itemsStale = false } = {}) {
  if (patch && patch.items !== undefined && !itemsStale) {
    Object.assign(target, updated)
    return
  }
  const { items, ...rest } = updated
  Object.assign(target, rest)
}
