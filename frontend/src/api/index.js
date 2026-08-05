import http from './http'

export const authApi = {
  login(username, password) {
    const body = new URLSearchParams()
    body.append('username', username)
    body.append('password', password)
    return http.post('/auth/login', body)
  },
  me: () => http.get('/auth/me'),
  changePassword: (old_password, new_password) =>
    http.post('/auth/change-password', { old_password, new_password }),
}

// 截图上传：首次调用要加载 OCR 模型，耗时可能超默认 15s，故单独放宽超时
function postImage(url, file) {
  const form = new FormData()
  form.append('file', file)
  return http.post(url, form, {
    headers: { 'Content-Type': 'multipart/form-data' }, timeout: 60000,
  })
}

export const ordersApi = {
  list: (params) => http.get('/orders', { params }),
  get: (id) => http.get(`/orders/${id}`),
  create: (data) => http.post('/orders', data),
  update: (id, data) => http.patch(`/orders/${id}`, data),
  remove: (id) => http.delete(`/orders/${id}`),
  ocr: (file) => postImage('/orders/ocr', file),
}

export const shipmentApi = {
  list: (params) => http.get('/shipment', { params }),
  get: (id) => http.get(`/shipment/${id}`),
  create: (data) => http.post('/shipment', data),
  update: (id, data) => http.patch(`/shipment/${id}`, data),
  remove: (id) => http.delete(`/shipment/${id}`),
  attachOrder: (shipmentId, orderId) => http.post(`/shipment/${shipmentId}/order/${orderId}`),
  detachOrder: (shipmentId, orderId) => http.delete(`/shipment/${shipmentId}/order/${orderId}`),
  ocr: (file) => postImage('/shipment/ocr', file),                       // 成品包裹截图 → 建单字段
  ocrExpress: (id, file) => postImage(`/shipment/${id}/ocr-express`, file), // 内含快递截图 → 联动挂靠
}

export const miscApi = {
  list: (params) => http.get('/misc', { params }),
  create: (data) => http.post('/misc', data),
  update: (id, data) => http.patch(`/misc/${id}`, data),
  remove: (id) => http.delete(`/misc/${id}`),
}

// 物品列表（对接最小单位；只读，编辑仍在商品订单页展开面板）
export const itemsApi = {
  list: (params) => http.get('/items', { params }),
}

export const stagingApi = {
  list: (params) => http.get('/staging', { params }),
  create: (data) => http.post('/staging', data),
  update: (id, data) => http.patch(`/staging/${id}`, data),
  remove: (id) => http.delete(`/staging/${id}`),
  ignore: (id) => http.post(`/staging/${id}/ignore`),
  import: (id) => http.post(`/staging/${id}/import`),
}

export const dashboardApi = { get: () => http.get('/dashboard') }
export const fxApi = { get: () => http.get('/fx') }

export const layoutApi = {
  get: (table) => http.get(`/layout/${table}`),
  save: (table, columns) => http.put(`/layout/${table}`, { columns }),
}

// 爬虫插件（soroban 扫 scraper/soroban-scraper-* 作为插件；管理层在插件管理页）
export const pluginsApi = {
  list: () => http.get('/plugins'),
  saveConfig: (id, cfg) => http.put(`/plugins/${id}/config`, cfg),
  login: (id, account) => http.post(`/plugins/${id}/login`, null, { params: { account } }),
  fetch: (id, account) => http.post(`/plugins/${id}/fetch`, null, { params: account ? { account } : {} }),
  addAccount: (id, name, platform) => http.post(`/plugins/${id}/account`, null, { params: { name, platform } }),
  setAccountEnabled: (id, account, enabled) => http.patch(`/plugins/${id}/account`, null, { params: { account, enabled } }),
  deleteAccount: (id, account) => http.delete(`/plugins/${id}/account`, { params: { account } }),
  renameAccount: (id, oldName, newName) =>
    http.post(`/plugins/${id}/account/rename`, null, { params: { old: oldName, new: newName } }),
  // 按账号删订单：暂存(暂存订单页) / 账本(商品订单页，软删)
  deleteAccountStaging: (id, account) => http.delete(`/plugins/${id}/account/staging`, { params: { account } }),
  deleteAccountOrders: (id, account) => http.delete(`/plugins/${id}/account/orders`, { params: { account } }),
}

// 数据库迁移/切换（SQLite ↔ MySQL，双向）。target 三选一：
//   { connection_id }              — 一键复用已存连接
//   { backend: 'sqlite' }          — 本地 SQLite
//   { backend: 'mysql', host, ... } — 新 MySQL 连接
export const dbApi = {
  status: () => http.get('/db/status'),
  test: (target) => http.post('/db/test', target),
  // 迁移含建库+建表+拷数据，耗时可能长，放宽超时
  migrate: (target) => http.post('/db/migrate', target, { timeout: 120000 }),
  // 热切换（无需重启）
  switch: (target) => http.post('/db/switch', target, { timeout: 60000 }),
  removeConnection: (id) => http.delete(`/db/connections/${id}`),
}

export const tagsApi = {
  list: (field) => http.get(`/tags/${field}`),
  add: (field, value) => http.post(`/tags/${field}`, { value }),
  remove: (field, value) => http.delete(`/tags/${field}/${encodeURIComponent(value)}`),
  setColor: (field, value, color) => http.put(`/tags/${field}/color`, null, { params: { value, color } }),
  // 改名：platform_account 牵连插件磁盘会话/配置 → 走插件全链路端点；其它字段走通用标签改名
  rename: (field, oldVal, newVal) =>
    field === 'platform_account'
      ? http.post(`/plugins/taobao/account/rename`, null, { params: { old: oldVal, new: newVal } })
      : http.post(`/tags/${field}/rename`, null, { params: { old: oldVal, new: newVal } }),
}
