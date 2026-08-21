// 只读屏障（503）的重试策略。**刻意不 import 任何东西**——这样 node 能直接跑它，
// 由 tests/test_consistency.py 的 node harness 做真行为测试，而不是去 grep http.js。
// （这个仓库已经有 6 处守卫栽在「按字符串判源码」上。）
//
// 背景：备份 / 数据库迁移期间会挂只读屏障，其间所有写请求一律 503。
// 备份通常不到一秒，但只要撞上，用户那一格编辑就直接没了——他看到一句「稍后重试」，
// 而刚敲的内容已经从格子里退回旧值。这套系统会有 2–3 个人同时用，撞上是常态。
//
// **重试安全的前提**（这段代码成立的全部依据）：两种 503 都在请求**碰到数据库之前**
// 就返回了——屏障那条在中间件里、`call_next` 之前拒绝；连接池那条是连接都没拿到。
// 被 503 拒掉的写请求**一次都没有执行过**，重发不会重复落库。
// 反过来说：将来若有别处开始在「写了一半」之后返回 503，这里必须跟着改。
// 后端的 `test_a_barrier_503_never_reaches_the_handler` 钉着这个前提。

/** 重试间隔（毫秒）。数组长度 = 最多重试几次。
 *
 * 短而少：屏障的硬上限是 900 秒（长迁移），那种情况本来就该让用户看到提示，
 * 而不是把界面卡在这里干等。两次都失败就按原样把错误交出去。
 */
export const RETRY_503_DELAYS = [400, 1200]

/**
 * 这次失败要不要重试？要的话返回等待毫秒数，不要则返回 null。
 *
 * @param {object} err  axios 的错误对象
 * @returns {number|null}
 */
export function retryDelayFor(err) {
  if (err?.response?.status !== 503) return null
  const cfg = err.config
  if (!cfg || cfg.__noRetry) return null       // 调用方显式声明「这条别重试」
  const n = cfg.__retry503 || 0
  return n < RETRY_503_DELAYS.length ? RETRY_503_DELAYS[n] : null
}

/** 记一次重试（调用方在真正重发之前调）。返回同一个 config，便于串起来写。 */
export function markRetried(cfg) {
  cfg.__retry503 = (cfg.__retry503 || 0) + 1
  return cfg
}
