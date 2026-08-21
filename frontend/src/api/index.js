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
function postImage(url, file, extra) {
  const form = new FormData()
  form.append('file', file)
  // 额外的表单字段（如 OCR 的来源平台）。**空值一律不 append**：
  // FastAPI 的 Form(None) 收到空串是「传了个空的」而不是「没传」，
  // 而「没传 = 保持自动判别」是那个参数的缺省语义。
  Object.entries(extra || {}).forEach(([k, v]) => { if (v) form.append(k, v) })
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
  ocr: (file, platformHint) => postImage('/orders/ocr', file, { platform_hint: platformHint }),
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
export const fxApi = {
  // 按天汇总 + 某天的各次抓取。一天可以有多条，页面据此回看「那天几点是多少」
  history: (days = 30) => http.get('/fx/history', { params: { days } }),
  historyDay: (on) => http.get(`/fx/history/${on}`),
  // 手填某一天的汇率（补历史）。只追加一条，不覆盖已有行、不改已折算的旧单。
  setManual: (date, rate) => http.post('/fx', { date, rate }),
  // （原先这里挂着一段「超时必须放宽」的注释，说的是**已经不存在的** `fx.refresh`——
  //   那时 soroban 自己会串行走完整条汇率源链。现在 `GET /api/fx` 是纯读，
  //   抓取由汇率插件的子进程完成，默认 15s 足够。留着那段注释会让人给一个纯读接口
  //   加上几分钟的超时。）
  get: () => http.get('/fx'),
}

export const layoutApi = {
  get: (table) => http.get(`/layout/${table}`),
  save: (table, columns) => http.put(`/layout/${table}`, { columns }),
}

// 插件（soroban 扫 plugins/soroban-plugin-* ；管理层在插件管理页）。
// 插件不只是爬虫——汇率、国际快递查询都没有「爬」的语义。
export const pluginsApi = {
  list: () => http.get('/plugins'),
  install: (id, withBrowser = true) =>
    http.post(`/plugins/${id}/install`, null, { params: { with_browser: withBrowser } }),
  saveConfig: (id, cfg) => http.put(`/plugins/${id}/config`, cfg),
  // 授权：插件只能进它声明过、且你在这里勾选过的那些门（默认一个都不给）
  saveGrants: (id, granted) => http.put(`/plugins/${id}/grants`, { granted }),
  // 插件私有参数（清单里 [[params]] 声明，核心只负责存/校验/渲染/下发）
  saveParams: (id, params) => http.put(`/plugins/${id}/params`, { params }),
  // 清理已删插件在库里的残留配置（授权/定时/账号/上次结果）
  forget: (id) => http.delete(`/plugins/${id}/config`),
  // 通用命令端点：动词由清单声明，加插件/加动词都不用再往这里加方法
  run: (id, command, account) =>
    http.post(`/plugins/${id}/run/${command}`, null, { params: account ? { account } : {} }),
  // （原先这里还有 login / fetch 两个薄封装，把动词名写死在了前端。
  //   后端早就统一成 `run/{command}` 了，账号行现在也按清单渲染，两者已无人调用，删掉。
  //   各开一个端点的下场是校验各写一套——遗留的 /fetch 曾绕过「停用」总开关、
  //   不校验命令 needs、不下发插件参数、扇出还共用同一枚令牌。）
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
  // 备份要把整本账读一遍并写成快照，与迁移同量级，放宽超时
  backups: () => http.get('/db/backups'),
  createBackup: () => http.post('/db/backups', null, { timeout: 120000 }),
  // 恢复刻意**没有**前端入口：那是唯一一条能一键清空账本的操作，
  // 只留在需要手敲 yes 的命令行里（backend: python -m tools.backup_db --restore <文件>）。
}

export const tagsApi = {
  list: (field) => http.get(`/tags/${field}`),
  add: (field, value) => http.post(`/tags/${field}`, { value }),
  remove: (field, value) => http.delete(`/tags/${field}/${encodeURIComponent(value)}`),
  setColor: (field, value, color) => http.put(`/tags/${field}/color`, null, { params: { value, color } }),
  // 改名一律走通用端点。**由后端按「有没有插件声明这一列」分派**，前端不认插件 id——
  // 原先这里把 `taobao` 焊在 URL 里：插件目录不在时（源码安装、自定 PLUGIN_DIR），
  // 手工录单产生的账号名既删不掉（409 in_use）也改不了名（404「未发现插件: taobao」），
  // 而那句报错和用户正在做的事毫无关系。
  // 真正由插件管理的字段，后端会回 400 并让用户去「插件管理」页——那里才有磁盘会话迁移。
  rename: (field, oldVal, newVal) =>
    http.post(`/tags/${field}/rename`, null, { params: { old: oldVal, new: newVal } }),
}

// 运行期设置（存库、「设置」页上改）。与 backend/.env 的部署配置是两回事。
export const settingsApi = {
  get: () => http.get('/settings'),
  // 只传要改的键：整包提交会把别人刚在另一个标签页改过的项一起盖回去
  save: (values) => http.put('/settings', { values }),
}
