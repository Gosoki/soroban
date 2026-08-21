import axios from 'axios'
import { ElLoading, ElMessage } from 'element-plus'
import { markRetried, retryDelayFor } from './retry'

// baseURL '/api'：各模块用 '/orders' 等拼接；Vite 代理 /api → 后端。
const http = axios.create({ baseURL: '/api', timeout: 15000 })

// ---- 后端断连：全屏提示（单例）----
let offlineLoading = null
let healthTimer = null

function isNetworkError(err) {
  return err.code === 'ERR_NETWORK' || err.message === 'Network Error'
}
function startHealthPolling() {
  if (healthTimer) return
  healthTimer = setInterval(async () => {
    try {
      const resp = await fetch('/api/health', { cache: 'no-store' })
      if (resp.ok) hideOfflineOverlay()
    } catch (_) { /* keep waiting */ }
  }, 3000)
}
function showOfflineOverlay() {
  if (offlineLoading) return
  offlineLoading = ElLoading.service({
    fullscreen: true, lock: true,
    text: '无法连接后端，请检查服务器', background: 'rgba(0,0,0,0.7)',
  })
  startHealthPolling()
}
function hideOfflineOverlay() {
  if (offlineLoading) { offlineLoading.close(); offlineLoading = null }
  if (healthTimer) { clearInterval(healthTimer); healthTimer = null }
}

// 已经被页面接管的那些 409。用 WeakSet 是因为键是 error 对象本身：
// 页面不再引用它时自动回收，不需要谁来清理。
const pending409 = new WeakSet()

/** 页面自己给了更好的提示 → 取消拦截器那条兜底提示。
 *
 * 忘了调只会多一条无害的提示；而不调的默认（原先的行为）是**什么都不显示**。
 * 这个方向是刻意选的：一个多余的 toast 谁都看得见并且会来修，
 * 一次静默的点击则要等到用户以为「保存成功了」之后才发现。 */
export function handled(e) {
  if (e) pending409.delete(e)
}

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token')
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 只读屏障（503）的自动重试。策略与「能不能重试」的判断在 ./retry.js——
// 那个文件刻意零依赖，node 能直接跑，由后端的一致性测试做真行为测试。
function _sleep(ms) { return new Promise((r) => setTimeout(r, ms)) }

http.interceptors.response.use(
  (res) => { hideOfflineOverlay(); return res.data },
  async (err) => {
    if (err.code === 'ERR_CANCELED') return Promise.reject(err)
    // 屏障 503：短暂重试几次再放弃（判据与间隔见 ./retry.js）
    const wait = retryDelayFor(err)
    if (wait !== null) {
      await _sleep(wait)
      return http.request(markRetried(err.config))
    }
    if (isNetworkError(err)) { showOfflineOverlay(); return Promise.reject(err) }
    if (err.response?.status === 401) {
      localStorage.removeItem('auth_token')
      localStorage.removeItem('auth_user')
      if (window.location.hash !== '#/login') window.location.hash = '#/login'
    }
    const status = err.response?.status
    let detail = err.response?.data?.detail
    // FastAPI 校验错误的 detail 是数组（[{msg,loc}...]）——展平成可读文案，别把具体原因丢成通用提示
    if (Array.isArray(detail)) detail = detail.map((e) => e?.msg || String(e)).join('；')
    const hasDetail = typeof detail === 'string' && !!detail
    if (status === 409) {
      // ---- 409：默认从「静默」翻转成「兜底一定说话」-------------------------
      // 原先的约定是「409 交给页面处理，拦截器不弹」。约定本身没错，错的是它的
      // **失败方向**：页面忘了处理 → 点下去**完全没反应**，是最难排查的那种
      // 「没坏但也不动」。而 409 的调用点已经涨到 22 处，其中 6 处漏了处理——
      // 复发率本身就证明「靠每个新调用点的作者记住」这条路走不通。
      //
      // 所以改成延后一拍的兜底：页面自己给了更好的提示就调 `handled(e)` 取消，
      // 忘了调就由这里说话。两种失败方向的代价差得很远——
      //   · 漏处理 → 用户看到后端给的原因（而不是什么都看不到）；
      //   · 重复 → 多一条无害的提示。
      // 用 setTimeout(0) 而不是立即弹：catch 块跑在微任务里，一定早于这个宏任务，
      // 所以「页面已处理」的取消永远来得及。
      pending409.add(err)
      setTimeout(() => {
        if (!pending409.has(err)) return
        pending409.delete(err)
        ElMessage.warning(hasDetail ? detail : '数据已变，请刷新后重试')
      }, 0)
      return Promise.reject(err)
    }
    // 超时是全站唯一一句会漏出**英文**的提示（axios 给的是 `timeout of 15000ms exceeded`）。
    // 它与「连接被拒」是两种不同的故障：后端还活着但卡住了（迁移期的长事务、
    // 连接池排队、握手阻塞）。这里只把话说成中文；要不要顺带弹全屏离线提示
    // 是另一个取舍（那会在后端其实健康时闪一下），见审计报告的待定项。
    const timedOut = err.code === 'ECONNABORTED' || /timeout/i.test(err.message || '')
    const text = hasDetail ? detail
      : (timedOut ? '后端一直没有响应（已等待超时）。它可能正忙或卡住了，稍后重试。'
                  : (err.message || '请求失败'))
    // 503 = 数据库迁移期间的只读屏障：是暂时状态、稍后重试即可，用 warning 而非 error，
    // 别让用户以为账本坏了。
    if (status === 503) ElMessage.warning(text)
    else ElMessage.error(text)
    return Promise.reject(err)
  }
)

export default http
