// 只读屏障（503）的重试策略。**刻意不 import 任何东西**——这样 node 能直接跑它，
// 由 tests/test_consistency.py 的 node harness 做真行为测试，而不是去 grep http.js。
// （这个仓库已经有 6 处守卫栽在「按字符串判源码」上。）
//
// 背景：备份 / 数据库迁移期间会挂只读屏障，其间所有写请求一律 503。
// 备份通常不到一秒，但只要撞上，用户那一格编辑就直接没了——他看到一句「稍后重试」，
// 而刚敲的内容已经从格子里退回旧值。这套系统会有 2–3 个人同时用，撞上是常态。
//
// **重试安全的前提**（这段代码成立的全部依据）：凡是以 503 结束的写请求，
// 都**不可能已经落库**。今天有三种 503，逐条说明为什么成立：
//   · 只读屏障  —— 在中间件里、`call_next` 之前就拒绝了，请求碰都没碰数据库
//     （后端的 `test_a_barrier_503_never_reaches_the_handler` 钉着这一条）；
//   · 连接池满  —— 连接都没拿到；
//   · SQLite 写锁冲突（SQLITE_BUSY/LOCKED）—— 这一条**确实执行了一部分**，
//     但事务必然回滚（SQLite 保证 BUSY 时那次 COMMIT 没有发生），
//     而且账本的四条写路径都带乐观锁 `version`，真要重复落库也会先撞 409。
// 反过来说：将来若新增一种「写了一半、可能已提交」的 503，这里必须跟着改。

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
