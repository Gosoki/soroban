import axios from 'axios'
import { ElLoading, ElMessage } from 'element-plus'

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

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token')
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (res) => { hideOfflineOverlay(); return res.data },
  (err) => {
    if (err.code === 'ERR_CANCELED') return Promise.reject(err)
    if (isNetworkError(err)) { showOfflineOverlay(); return Promise.reject(err) }
    if (err.response?.status === 401) {
      localStorage.removeItem('auth_token')
      localStorage.removeItem('auth_user')
      if (window.location.hash !== '#/login') window.location.hash = '#/login'
    }
    // 409（乐观锁冲突 / 迁移前的变更确认）交给页面处理，不在这里弹通用错误
    if (err.response?.status !== 409) {
      let detail = err.response?.data?.detail
      // FastAPI 校验错误的 detail 是数组（[{msg,loc}...]）——展平成可读文案，别把具体原因丢成通用提示
      if (Array.isArray(detail)) detail = detail.map((e) => e?.msg || String(e)).join('；')
      const text = (typeof detail === 'string' && detail) ? detail : (err.message || '请求失败')
      // 503 = 数据库迁移期间的只读屏障：是暂时状态、稍后重试即可，用 warning 而非 error，
      // 别让用户以为账本坏了。
      if (err.response?.status === 503) ElMessage.warning(text)
      else ElMessage.error(text)
    }
    return Promise.reject(err)
  }
)

export default http
