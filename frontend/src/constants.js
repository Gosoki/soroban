// 状态枚举（必须与后端 models/base.py 的枚举值一致）
// 商品订单只记**国内段**：待付款→待发货→待收货→已签收（国内快递签收）。
// 国际段（集运中/送达）不在这里——挂上集运单后界面显示的是那张单的状态（后端算好的 fulfillment_status），
// 释放出来则回落到订单自己的状态。同一件事只有一处记录，不会两边打架。
// 退款 / 交易关闭是旁支终态。顺序与后端 PURCHASE_STATUS_RANK 一致（见下方 PURCHASE_STATUS_RANK）。
export const PURCHASE_STATUS = ['待付款', '待发货', '待收货', '已签收', '退款', '交易关闭']
export const SHIPMENT_STATUS = ['打包中', '已发出', '已送达', '已取消']
export const IMPORT_STATUS = ['待处理', '已导入', '已忽略']   // 暂存导入工作流状态
export const ORDER_SOURCES = ['闲鱼', '淘宝', '京东', '拼多多', '其他']   // 订单来源平台（OCR 可自动识别）

// 订单的「已签收」= 国内快递签收（淘宝「交易成功」）；集运单的「已送达」= 国际包裹到本人手上。
// 两者曾是同一个字面量（跨表集合共用时是实打实的坑），现已拆开，各占一个条目。
function statusTagType(s) {   // 内部用：语义色映射，对外走 statusStyle
  return {
    待付款: 'info', 待发货: 'primary', 待收货: 'warning',
    已签收: 'success',
    已送达: 'success',   // 集运单：国际包裹送到本人手上
    退款: 'danger', 交易关闭: 'info',
    打包中: 'warning', 已发出: 'primary', 已取消: 'info',
  }[s] || 'info'
}

// 平台名**不是状态**。原先它俩挤在同一张色表里，靠「值不重名」侥幸共存——
// 加一个叫「淘宝」的状态（或叫「已发出」的平台）就会互相串色。拆成两张表，各管各的。
function platformTagType(s) {
  return { 闲鱼: 'warning', 淘宝: 'primary', 京东: 'danger' }[s] || 'info'
}

function stagingTag(s) {   // 内部用：对外走 stagingStyle
  return { 待处理: 'warning', 已导入: 'success', 已忽略: 'info' }[s] || 'info'
}

// 标签调色盘（管理型标签列：淘宝号/收货人等）——每个值哈希到固定一色，暗色主题友好的柔和底色
export const TAG_PALETTE = [
  { bg: 'rgba(59,130,246,.18)', border: 'rgba(59,130,246,.45)', text: '#8ab4ff' },   // 蓝
  { bg: 'rgba(16,185,129,.18)', border: 'rgba(16,185,129,.45)', text: '#4ade9f' },   // 绿
  { bg: 'rgba(245,158,11,.18)', border: 'rgba(245,158,11,.45)', text: '#f0b64d' },   // 琥珀
  { bg: 'rgba(239,68,68,.18)',  border: 'rgba(239,68,68,.45)',  text: '#f78b8b' },   // 红
  { bg: 'rgba(139,92,246,.18)', border: 'rgba(139,92,246,.45)', text: '#b79cff' },   // 紫
  { bg: 'rgba(236,72,153,.18)', border: 'rgba(236,72,153,.45)', text: '#f38bc4' },   // 粉
  { bg: 'rgba(20,184,166,.18)', border: 'rgba(20,184,166,.45)', text: '#4fd6c4' },   // 青
  { bg: 'rgba(249,115,22,.18)', border: 'rgba(249,115,22,.45)', text: '#fba95f' },   // 橙
  { bg: 'rgba(99,102,241,.18)', border: 'rgba(99,102,241,.45)', text: '#9fa2ff' },   // 靛
  { bg: 'rgba(132,204,22,.18)', border: 'rgba(132,204,22,.45)', text: '#a7e05a' },   // 黄绿
]

function tagColor(value) {   // 内部用：哈希取色，仅作 tagStyleAt 的回退
  const s = String(value ?? '')
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return TAG_PALETTE[h % TAG_PALETTE.length]
}
function _css(c) {
  return `background:${c.bg};border-color:${c.border};color:${c.text}`
}
function tagStyle(value) {   // 内部用：按值哈希取色，仅作 tagStyleAt 的回退
  return _css(tagColor(value))
}

// 按标签在该字段可选集里的「序号」取色：第 1/2/3… 个各取调色盘第 0/1/2… 色，
// 前 10 个保证互不相同（哈希取模会撞桶，加三四个就可能重复）。序号 <0（值暂不在集里、
// 如首帧标签还没加载）回退到按值哈希，保证仍是确定色、不闪。
export function tagStyleAt(index, value) {
  if (index === null || index === undefined || index < 0) return tagStyle(value)
  return _css(TAG_PALETTE[index % TAG_PALETTE.length])
}

// 状态标签统一成同款「柔和底色」——只是按语义 type 取色（不用哈希），保留含义、观感一致
const GREY = { bg: 'rgba(148,163,184,.16)', border: 'rgba(148,163,184,.4)', text: '#aab6c9' }
const TYPE_TINT = {
  primary: TAG_PALETTE[0], success: TAG_PALETTE[1], warning: TAG_PALETTE[2], danger: TAG_PALETTE[3], info: GREY,
}
export function typeStyle(type) {
  return _css(TYPE_TINT[type] || GREY)
}
export function statusStyle(s) {
  return typeStyle(statusTagType(s))
}
export function importStatusStyle(s) {
  return typeStyle(stagingTag(s))
}
// 平台标签的**语义色**：闲鱼橙 / 淘宝蓝 / 京东红，写死的三色表。
// 与 Plugins 页的 platformTagStyle 是两回事——那个取的是用户在标签管理里配的颜色。
// 两者渲染同一种东西却取色来源不同，同名会诱导后人「顺手去重」，把用户配色静默做没。
export function platformSemanticStyle(s) {
  return typeStyle(platformTagType(s))
}

// 采购段推进序与终态。**放这里而不是放某个页面里**：订单页与（将来的）任何页面都要用同一份，
// 抄两份必然漂移——上一轮 OCR 合并把终态盖掉的事故，根因就是前后端各存了一份规则。
// 必须与后端 models/base.py 的 PURCHASE_STATUS_RANK / PURCHASE_TERMINAL_STATUSES 一致。
export const PURCHASE_STATUS_RANK = { 待付款: 0, 待发货: 1, 待收货: 2, 已签收: 3 }
export const PURCHASE_TERMINAL = ['退款', '交易关闭']

// 能不能把 cur 推进到 next。终态是明确结论，自动识别不该推翻它。
export function canAdvancePurchase(cur, next) {
  if (!next || next === cur) return false
  if (PURCHASE_TERMINAL.includes(cur)) return false
  if (PURCHASE_TERMINAL.includes(next)) return true
  return (PURCHASE_STATUS_RANK[next] ?? -1) > (PURCHASE_STATUS_RANK[cur] ?? -1)
}

// 「人民币（元）」列的表头说明（订单页 / 暂存页共用；NotionTable 的 col.help 会渲染成「?」）。
// 放这里而不是各页各写一份：同一个口径解释出现在两个页面，抄两遍必然漂移。
export const PRICE_HELP = `「人民币」由物品派生：Σ(单价 × 数量) + 邮费。
已有的行改价要去展开面板（那里也能直接填货款）；**最上面那行新建行可以直接填**，会折成一条物品（名称取商品标题、数量 1）。

⚠️ 与淘宝实付可能差几分。淘宝给的是**四舍五入到分的单价**，遇到「多件 + 整单折扣」时乘回去除不尽：
　总价 190.00 − 优惠 5.70 = 实付 184.30，买 4 件
　→ 真实单价 184.30÷4 = 46.075，淘宝给 46.08
　→ 46.08 × 4 = 184.32，比实付多 2 分

误差上限 = 数量 × 0.005 元（4 件 2 分，100 件 5 毛），方向不固定。这是已知取舍，不影响记账用途。

要完全消掉需要给订单加「整单优惠」字段——淘宝那边有这个数据，且同一次请求就能拿到，不增加爬虫访问量。`


// 汇率来源的展示名。**纯展示层的翻译表**——核心只认识 "manual"（用户手填），
// 其余标识由汇率插件自报（见 plugins/soroban-plugin-fx 的 sources 参数）。
// 表里没有的原样显示裸 key：宁可显示 "xxx"，也不要为一个标签让核心去认识插件。
export const FX_SOURCE_NAMES = {
  boc: '中国银行', google: '谷歌财经', erapi: '通用汇率 API', manual: '手填',
}
export function fxSourceName(r) {
  return FX_SOURCE_NAMES[r?.source] || r?.source_label || r?.source || ''
}

// —— 提示时长：全仓只有两档，别再出现第三个数字 ——
//
// Element Plus 默认 3000ms。「要看清具体内容」的提示（列了单号、说明为什么没生效、
// 服务端 detail）3 秒读不完，一律用 8000。这个数字原先在 4 个文件里各写一遍，
// 第 5 处很自然地写成了 10000——同一类提示两种时长，就是割裂感的来源。
export const TOAST_LONG = 8000

/** 需要看清内容的长提示。type 同 ElMessage：success | warning | info | error。 */
export function longToast(ElMessage, type, message) {
  ElMessage({ type, message, duration: TOAST_LONG })
}
