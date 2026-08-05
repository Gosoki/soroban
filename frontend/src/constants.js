// 状态枚举（必须与后端 models/base.py 的枚举值一致）
// 商品订单生命周期：待付款→待发货→待收货→已签收（国内快递到集运仓）→集运中→已到达；
// 退款 / 交易关闭是旁支终态。顺序与后端 ORDER_STATUS_RANK 一致（见 Orders 页 STATUS_RANK）。
export const ORDER_STATUS = ['待付款', '待发货', '待收货', '已签收', '集运中', '已到达', '退款', '交易关闭']
export const SHIPMENT_STATUS = ['打包中', '已发出', '已签收', '已取消']
export const STAGING_STATUS = ['待处理', '已导入', '已忽略']   // 暂存导入工作流状态
export const ORDER_SOURCES = ['闲鱼', '淘宝', '京东', '拼多多', '其他']   // 订单来源平台（OCR 可自动识别）

// 注意：订单的「已签收」与集运的「已签收」是同一个字面量（不同的列、不同的后端白名单），
// 在这张共用色表里共享一个条目，语义都是「到手/签收成功」，同色即可。
function statusTagType(s) {   // 内部用：语义色映射，对外走 statusStyle
  return {
    待付款: 'info', 待发货: 'primary', 待收货: 'warning',
    已签收: 'success', 集运中: 'primary', 已到达: 'success',
    退款: 'danger', 交易关闭: 'info',
    打包中: 'warning', 已发出: 'primary', 已取消: 'info',
    闲鱼: 'warning', 淘宝: 'primary', 京东: 'danger',
  }[s] || 'info'
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
export function stagingStyle(s) {
  return typeStyle(stagingTag(s))
}
