<template>
  <div>
    <el-card>
      <NotionTable :columns="columns" :rows="rows" :loading="loading" expandable hide-id :open-id="focusId"
                   table-name="orders" @save="saveCell" @add="addRow" @delete="delRow" @reload="load">
        <template #toolbar>
          <el-upload ref="ocrUpload" class="ocr-up" multiple :show-file-list="false" :auto-upload="false"
                     accept="image/*" :on-change="onOcrPick">
            <div class="ocr-drop" :class="{ busy: ocrPending }">
              <el-icon class="ocr-ic"><Camera /></el-icon>
              <span>{{ ocrPending ? `后台识别中 ${ocrPending} 张…` : '点击选图 OCR识别（或拖图到页面）' }}</span>
            </div>
          </el-upload>
          <el-tag v-if="focusId" type="warning" closable disable-transitions class="focus-chip" @close="clearFocus">
            定位订单 #{{ focusId }} · 点 × 看全部
          </el-tag>
          <el-input v-model="filters.q" placeholder="搜物品/商品/单号/快递号" clearable style="width: 200px" @change="reload" />
          <el-select v-model="filters.platform" placeholder="来源" clearable style="width: 120px" @change="reload">
            <el-option v-for="p in ORDER_SOURCES" :key="p" :label="p" :value="p" />
          </el-select>
          <el-select v-model="filters.status" placeholder="状态" clearable style="width: 120px" @change="reload">
            <el-option v-for="s in ORDER_STATUS" :key="s" :label="s" :value="s" />
          </el-select>
          <el-select v-model="filters.platform_account" placeholder="账号昵称" clearable filterable style="width: 120px" @change="reload">
            <el-option v-for="a in accountOptions" :key="a" :label="a" :value="a" />
          </el-select>
          <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                          start-placeholder="起" end-placeholder="止" @change="reload" />
        </template>

        <template #cell-shipment_order_id="{ row }">
          <el-select :model-value="row.shipment_order_id" filterable placeholder="未集运"
                     size="small" class="ship-pick" popper-class="ship-pop"
                     @change="(v) => onPickShipment(row, v)">
            <template #label="{ value }">
              <span class="ship-sel">
                <b>{{ shipNo(value) }}</b>
                <el-tag v-if="shipById(value)" size="small" :style="statusStyle(shipById(value).status)">{{ shipById(value).status }}</el-tag>
              </span>
            </template>
            <!-- 清除固定在列表最上（集运单可能很多）；无归属时不显示 -->
            <el-option v-if="row.shipment_order_id" :value="-1" label="清除">
              <div class="ship-clear">清除（取消集运）</div>
            </el-option>
            <el-option v-for="j in sortedShipments" :key="j.id" :label="j.shipment_no || ('#' + j.id)" :value="j.id">
              <div class="ship-opt">
                <div class="ship-opt-top">
                  <b>{{ j.shipment_no || ('#' + j.id) }}</b>
                  <el-tag size="small" :style="statusStyle(j.status)">{{ j.status }}</el-tag>
                  <el-icon v-if="j.id === row.shipment_order_id" class="ship-ck"><Check /></el-icon>
                </div>
                <span class="ship-meta">{{ j.date }} · 运费 {{ j.jpy_settled != null ? fmtJPY(j.jpy_settled) : '待定' }}</span>
              </div>
            </el-option>
          </el-select>
        </template>
        <template #cell-items="{ row }">
          <span :class="{ ph: !(row.items && row.items.length), 'auto-txt': allTitleItems(row) }">{{ itemSummary(row) }}</span>
        </template>

        <template #expand="{ row }">
          <!-- 物品/邮费编辑：与物品列表编辑弹窗共用同一组件、同一写入链 -->
          <OrderItemsEditor :order="row" @conflict="load" />
        </template>

      </NotionTable>

      <div v-if="focusId && !loading && !total" class="focus-empty">
        未找到该订单（可能已删除）。<el-link type="primary" @click="clearFocus">显示全部</el-link>
      </div>

      <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                     :page-size="pageSize" :current-page="page" @current-change="onPage" />
    </el-card>

    <!-- 整窗拖拽：把图片拖到浏览器任意位置即在中间浮出上传框，松手识别（支持多张）。
         pointer-events:none 不拦截拖拽，drop 交给 window 监听统一处理，避免与工具栏重复触发。 -->
    <Teleport to="body">
      <div v-if="dragActive" class="ocr-overlay">
        <div class="ocr-overlay-box">
          <el-icon class="ocr-overlay-ic"><Camera /></el-icon>
          <div class="ocr-overlay-title">松开鼠标，识别截图（OCR）</div>
          <div class="ocr-overlay-sub">支持一次拖入多张 · 自动填单</div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Camera, Check } from '@element-plus/icons-vue'
import { shipmentApi, ordersApi, tagsApi } from '@/api'
import { ORDER_SOURCES, ORDER_STATUS, statusStyle } from '@/constants'
import { fmtJPY } from '@/utils/money'
import { queueOrderWrite } from '@/utils/orderWrites'
import NotionTable from '@/components/NotionTable.vue'
import OrderItemsEditor from '@/components/OrderItemsEditor.vue'

// 用本地时区（用户在日本=JST）的当天，而非 UTC；否则 JST 0~9 点新建会记成前一天
const today = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

// 默认列顺序 + 统一列宽（≈ 刚好显示日期，取整多留一点 = 110）；用户可拖动改序/改宽，改动持久化
const COL_W = 110
const columns = [
  { key: 'date', label: '下单日期', type: 'date', width: COL_W },
  { key: 'platform_account', label: '账号昵称', type: 'tag', field: 'platform_account', width: COL_W },
  { key: 'platform', label: '来源', type: 'tag', field: 'platform', width: COL_W, placeholder: '来源' },
  { key: 'shop', label: '商品', type: 'text', long: true, width: COL_W },
  { key: 'items', label: '物品', readonly: true, width: COL_W, expand: true },
  { key: 'status', label: '状态', type: 'select', options: ORDER_STATUS, width: COL_W, clearable: false },
  { key: 'shipment_order_id', label: '集运订单', readonly: true, width: COL_W, placeholder: '选择' },
  { key: 'jpy_settled', label: '结算（円）', format: 'jpy', readonly: true, width: COL_W },
  { key: 'jpy_override', label: '覆盖（円）', type: 'int', format: 'jpy', width: COL_W, placeholder: '实付日元' },
  { key: 'price_cny', label: '人民币（元）', format: 'cny', readonly: true, width: COL_W },   // 由物品单价×数量派生，改价走展开面板
  { key: 'fx_rate', label: '汇率', type: 'decimal', width: COL_W, placeholder: '当天汇率' },
  { key: 'express_company', label: '快递公司', type: 'text', width: COL_W, placeholder: '快递公司' },
  { key: 'express_no', label: '快递号', type: 'text', width: COL_W, placeholder: '快递号' },
  { key: 'order_no', label: '订单号', type: 'text', width: COL_W, placeholder: '订单号' },
]

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 30
const focusId = ref(null)   // 跳转定位的订单 id（?focus=）
const filters = reactive({ q: '', platform: '', status: '', platform_account: '', range: null })
const shipmentOptions = ref([])
const accountOptions = ref([])   // 账号昵称下拉候选（标签接口）
async function loadAccounts() {
  try { accountOptions.value = (await tagsApi.list('platform_account')).map((t) => t.value) } catch (_) { /* 已提示 */ }
}

function shipById(id) { return shipmentOptions.value.find((j) => j.id === id) }
function shipNo(id) { const j = shipById(id); return j ? (j.shipment_no || ('#' + id)) : ('#' + id) }
// 打包中的集运单永远置顶（最常挂新订单）；其余保持原顺序（日期倒序）。sort 稳定，同组不乱。
const sortedShipments = computed(() =>
  [...shipmentOptions.value].sort((a, b) => (b.status === '打包中' ? 1 : 0) - (a.status === '打包中' ? 1 : 0)),
)
function onPickShipment(row, v) {   // -1 = 列表里的「清除」项；其余为集运单 id
  saveCell(row, 'shipment_order_id', v === -1 ? null : (v ?? null))
}

function itemSummary(row) {
  if (!row.items || !row.items.length) return '—'
  return row.items.map((it) => `（${it.quantity}x）${it.name}`).join('，')
}
// 灰显 = 物品名与商品标题相同（无独立物品详情，多为自动占位）；有真实物品名即正常
function isTitleItem(row, it) {
  return !!it.name && (it.name || '').trim() === (row.shop || '').trim()
}
// 列表「物品」格：全是标题占位（自动生成）时整格灰显
function allTitleItems(row) {
  return !!(row.items && row.items.length) && row.items.every((it) => isTitleItem(row, it))
}

async function load() {
  loading.value = true
  try {
    const params = { limit: pageSize, offset: (page.value - 1) * pageSize }
    if (filters.q) params.q = filters.q
    if (filters.platform) params.platform = filters.platform
    if (filters.status) params.status = filters.status
    if (filters.platform_account) params.platform_account = filters.platform_account
    if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
    if (focusId.value) params.id = focusId.value          // 跳转定位：隔离显示该单
    const res = await ordersApi.list(params)
    rows.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}
function reload() { page.value = 1; load() }
function onPage(p) { page.value = p; load() }

async function loadShipment() {
  const res = await shipmentApi.list({ limit: 200 })
  shipmentOptions.value = res.items
}

async function saveCell(row, key, value) {
  try {
    // 入队串行：格子保存与展开面板/物品编辑器对同一订单的写不再并发撞 version
    await queueOrderWrite(row.id, async () => {
      const updated = await ordersApi.update(row.id, { version: row.version, [key]: value })
      // 不覆盖 items：展开面板里可能有尚未点「保存物品」的编辑，普通单元格保存不应清掉它
      const { items, ...rest } = updated
      Object.assign(row, rest)
    })
  } catch (e) {
    if (e.response?.status === 409) { ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
  }
}

// OCR 识别订单截图：抽取快递公司/快递号/订单号/成交价，识别到就新建一行并回填。
// 后台并发：每张图各起一次请求、互不阻塞，识别中前端可继续拖入更多图；ocrPending 记在飞的张数。
// 后台串行队列：拖入/选择的图片进队列，逐张识别。为何串行而非并发——
// ① 后端 OCR 本就用锁串行，前端并发并不提速；② 浏览器每域名仅 ~6 个连接，一次性发出
// 多个「慢 OCR」请求会占满连接、把随后的建单请求挤到超时 → 表现为「后续中断」。
// 串行 + 每张独立 try/catch：单张失败或「订单号+来源」重复都只跳过该张，绝不打断后续。
// 处理期间可继续拖入，新图追加到队列末尾。
const ocrPending = ref(0)   // 队列中待处理 + 处理中的总张数（用于提示与状态）
const ocrUpload = ref(null)
const ocrQueue = []
let ocrRunning = false
const dragActive = ref(false)   // 整窗拖拽中：中间浮出上传框
let dragDepth = 0               // dragenter/leave 会因子元素冒泡多次触发，用计数判断是否真正离开窗口

function onOcrPick(uploadFile) { enqueueOcr(uploadFile?.raw ? [uploadFile.raw] : []) }   // el-upload 点选/多选

function enqueueOcr(files) {
  const imgs = files.filter((f) => f && (!f.type || f.type.startsWith('image/')))
  const skipped = files.length - imgs.length
  if (skipped) ElMessage.warning(`已跳过 ${skipped} 个非图片文件`)   // 拖拽不受 accept 约束
  if (!imgs.length) return
  ocrQueue.push(...imgs)
  ocrPending.value += imgs.length
  pumpOcr()
}

async function pumpOcr() {
  if (ocrRunning) return          // 已有 worker 在跑；新入队的图会被同一循环取走
  ocrRunning = true
  try {
    while (ocrQueue.length) {
      const file = ocrQueue.shift()
      try {
        await processOcr(file)    // 单张：识别 → 建行；内部已吞错，任何失败都不中断队列
      } finally {
        ocrPending.value--
      }
    }
  } finally {
    ocrRunning = false
    ocrUpload.value?.clearFiles?.()   // 队列排空后清 el-upload 内部列表，便于重复选同一张图
  }
}

async function processOcr(file) {
  try {
    const res = await ordersApi.ocr(file)
    if (res.reject_reason) {   // 拿错平台截图（淘宝/京东）→ 提示改用爬虫，不建单
      ElMessage.warning(`「${file.name}」${res.reject_reason}`)
      return
    }
    const data = {}
    if (res.order_date) data.date = res.order_date          // 下单时间 → 下单日期
    if (res.platform) data.platform = res.platform
    if (res.product) data.shop = res.product                // 商品名称 →「商品」列(shop)
    if (res.express_company) data.express_company = res.express_company
    if (res.express_no) data.express_no = res.express_no
    if (res.order_no) data.order_no = res.order_no
    if (res.price_cny != null && res.price_cny !== '') data.price_cny = res.price_cny
    if (res.status) data.status = res.status                // 有快递单号→待收货，否则待发货
    // status/platform 恒有值，故按「实质字段」判断是否真识别到内容
    const recognized = res.order_no || res.express_no || res.order_date || res.product ||
      (res.price_cny != null && res.price_cny !== '')
    if (!recognized) {
      ElMessage.warning(`未能从「${file.name}」识别到快递/订单信息，请手动填写`)
      return
    }
    // 通过订单号匹配已存在订单：命中→更新（回填下单时间、补齐缺失字段），否则新建。
    // 支持「同一单先后拍多张截图（物流页/详情页）」逐步补全同一行，而非重复建行。
    if (data.order_no) {
      const existing = await findByOrderNo(data.order_no)
      if (existing) { await mergeByOrderNo(existing, data); return }
    }
    const created = await addRow(data)
    if (!created) return   // 新建失败（如订单号+来源重复），addRow 已给提示，不再报成功
    ElMessage.success(`已识别并新建订单 · ${ocrSummary(data)}`)
    if (focusId.value) clearFocus()   // 若正处于「定位单条」隔离视图：新单不属于该过滤，跳回全部列表免得成幽灵行
  } catch (_) {
    // 依赖未装(503)/图片错误(400)/超时 等由 http 拦截器统一提示；不抛出，避免中断队列
  }
}

// 按订单号精确查已存在订单（用后端精确 order_no 参数，不用模糊 q——否则子串命中多、真身可能被 limit 截掉致漏判重复建单）
async function findByOrderNo(orderNo) {
  try {
    const res = await ordersApi.list({ order_no: orderNo, limit: 1 })
    return res.items[0] || null
  } catch (_) { return null }
}

// 交易状态生命周期序：只准前进（待付款→待发货→待收货→已签收→集运中→已到达），不回退；
// 退款/交易关闭是旁支终态、未知值同样取 -1。必须与后端 models/base.py 的 ORDER_STATUS_RANK 一致
// ——集运页「内含快递」自动挂靠也按同一张表判定是否把订单推进到「集运中」。
const STATUS_RANK = { 待付款: 0, 待发货: 1, 待收货: 2, 已签收: 3, 集运中: 4, 已到达: 5 }
function statusRank(s) { return STATUS_RANK[s] ?? -1 }

// 命中同订单号：下单时间总是回填；状态仅「推进」时更新（如补上快递单号→待收货）；
// 其余字段仅在原值为空时补齐（不覆盖已有数据）
// 命中 patch：下单时间总回填；状态仅前进；其余字段仅在原值为空时补齐（对 base 版本重算）
function buildMergePatch(base, data) {
  const patch = { version: base.version }
  if (data.date) patch.date = data.date
  if (data.status && statusRank(data.status) > statusRank(base.status)) patch.status = data.status
  for (const k of ['platform', 'shop', 'express_company', 'express_no', 'price_cny']) {
    const cur = base[k]
    if (data[k] != null && data[k] !== '' && (cur == null || cur === '')) patch[k] = data[k]
  }
  return patch
}

async function mergeByOrderNo(existing, data) {
  let base = existing
  let patch = buildMergePatch(base, data)
  if (Object.keys(patch).length <= 1) {   // 只有 version → 无新增信息
    ElMessage.info(`订单号 ${data.order_no} 已存在，无新增信息`)
    return
  }
  // 若这单正被用户在别处编辑而 version 变了，OCR 补的字段不该直接丢：拉最新版本、按新状态重算 patch，再试一次。
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const updated = await ordersApi.update(base.id, patch)
      const idx = rows.value.findIndex((r) => r.id === base.id)   // 在当前页则就地刷新
      if (idx >= 0) { const { items, ...rest } = updated; Object.assign(rows.value[idx], rest); sortRows() }
      ElMessage.success(`已按订单号匹配更新 · 订单号 ${data.order_no}${patch.date ? ' · 下单时间 ' + patch.date : ''}`)
      return
    } catch (e) {
      if (e.response?.status !== 409) return   // 非冲突：拦截器已提示
      const fresh = await findByOrderNo(data.order_no)
      if (!fresh) break
      base = fresh
      patch = buildMergePatch(base, data)
      if (Object.keys(patch).length <= 1) return   // 并发编辑已把这些字段补齐，无需再改
    }
  }
  ElMessage.warning('数据已变，已刷新'); load()   // 两次仍冲突：兜底整表刷新
}

function ocrSummary(data) {
  const parts = []
  if (data.date) parts.push(`下单 ${data.date}`)
  if (data.status) parts.push(`状态 ${data.status}`)
  if (data.platform) parts.push(`来源 ${data.platform}`)
  if (data.shop) parts.push(`商品 ${data.shop}`)
  if (data.express_company) parts.push(`快递 ${data.express_company}`)
  if (data.express_no) parts.push(`快递号 ${data.express_no}`)
  if (data.order_no) parts.push(`订单号 ${data.order_no}`)
  if (data.price_cny) parts.push(`成交价 ¥${data.price_cny}`)
  return parts.join(' · ')
}

// —— 整窗拖拽上传：拖图进浏览器任意位置 → 中间浮出上传框，松手识别 ——
function isFileDrag(e) {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')
}
function onWinDragEnter(e) {
  if (!isFileDrag(e)) return
  e.preventDefault(); dragDepth++; dragActive.value = true
}
function onWinDragOver(e) {
  if (!isFileDrag(e)) return
  e.preventDefault()                       // 必须 preventDefault，否则不触发 drop
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
}
function onWinDragLeave(e) {
  if (!isFileDrag(e)) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) dragActive.value = false
}
function onWinDrop(e) {
  if (!isFileDrag(e)) return
  e.preventDefault(); dragDepth = 0; dragActive.value = false
  enqueueOcr(Array.from(e.dataTransfer.files || []))   // 多张入队，后台逐张识别
}

// 保持当前页按「下单日期」降序（与后端 order_by 一致）；新建/改期后即时重排，避免 unshift 打乱顺序
function sortRows() {
  rows.value.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : b.id - a.id))
}

async function addRow(data = {}) {
  try {
    // status 不写死：后端 OrderBase 默认「待发货」，避免枚举改名后前端残留非法值（曾用'已付'→422）
    const created = await ordersApi.create({ date: today(), ...data })
    rows.value.unshift(created)
    sortRows()                 // 按下单日期归位（OCR 可能录入历史日期，勿留在顶部）
    total.value++
    return created
  } catch (e) {
    // 409 被 http 拦截器刻意跳过（留给页面处理）→ 这里对「订单号+来源」重复给明确提示
    if (e.response?.status === 409) {
      const who = data.order_no
        ? `订单号「${data.order_no}」${data.platform ? '·' + data.platform : ''}`
        : '该记录'
      ElMessage.warning(`${who} 已存在（订单号+来源需唯一），未添加`)
    }
    return null   // 其余错误由拦截器提示；统一返回 null 让调用方知道未新建
  }
}

async function delRow(row) {
  try {
    await ElMessageBox.confirm(`删除订单 ${row.order_no || row.id}？`, '确认', { type: 'warning' })
  } catch (_) { return }
  try {
    await ordersApi.remove(row.id)
    rows.value = rows.value.filter((r) => r.id !== row.id)
    total.value--
    ElMessage.success('已删除')
  } catch (_) { /* 拦截器已提示 */ }
}

// 集运页点订单号跳转过来：?focus=<id> → 隔离显示该单并自动展开；重复跳转（改 query）也响应。
// immediate 负责首次加载，故 onMounted 不再重复调 load。
const route = useRoute()
const router = useRouter()
watch(() => route.query.focus, (v) => {
  focusId.value = (v !== undefined && v !== null && v !== '') ? Number(v) : null
  page.value = 1
  load()
}, { immediate: true })
function clearFocus() { router.replace({ path: '/orders', query: {} }) }

onMounted(() => {
  loadShipment()
  loadAccounts()
  window.addEventListener('dragenter', onWinDragEnter)
  window.addEventListener('dragover', onWinDragOver)
  window.addEventListener('dragleave', onWinDragLeave)
  window.addEventListener('drop', onWinDrop)
})
onBeforeUnmount(() => {
  window.removeEventListener('dragenter', onWinDragEnter)
  window.removeEventListener('dragover', onWinDragOver)
  window.removeEventListener('dragleave', onWinDragLeave)
  window.removeEventListener('drop', onWinDrop)
})
</script>

<style scoped>
.pager { margin-top: 12px; justify-content: flex-end; }
/* OCR 上传：工具栏里的点选按钮（拖拽走整窗覆盖层，这里只负责点击选图）。 */
.ocr-up { display: inline-flex; }
.ocr-up :deep(.el-upload) { display: inline-flex; }
.ocr-drop {
  display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 14px;
  border: 1px dashed #3a4a6b; border-radius: 4px; color: #7f9cff; font-size: 13px;
  white-space: nowrap; cursor: pointer;
}
.ocr-drop:hover { border-color: #409eff; background: rgba(64, 158, 255, 0.08); }
.ocr-drop.busy { color: #7d8aa3; }
.ocr-ic { font-size: 15px; }

/* 整窗拖拽覆盖层：居中的上传提示框，不拦截拖拽事件 */
.ocr-overlay {
  position: fixed; inset: 0; z-index: 9000; pointer-events: none;
  display: flex; align-items: center; justify-content: center;
  background: rgba(6, 12, 24, 0.72);
}
.ocr-overlay-box {
  width: min(460px, 76vw); padding: 40px 32px; text-align: center;
  border: 2px dashed #409eff; border-radius: 16px;
  background: rgba(16, 25, 44, 0.92); box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
}
.ocr-overlay-ic { font-size: 44px; color: #6ea8ff; margin-bottom: 12px; }
.ocr-overlay-title { font-size: 18px; font-weight: 600; color: #eaf1ff; }
.ocr-overlay-sub { margin-top: 6px; font-size: 13px; color: #8a9ab8; }
.focus-chip { font-weight: 500; }
.focus-empty { color: #9ba8bf; font-size: 13px; padding: 16px; text-align: center; }
.ph { color: #5b6880; }
.auto-txt { color: #6b7488; font-style: italic; }   /* 列表「物品」格：自动生成(名=标题)时灰显 */
/* 集运点选：内嵌无边框，像格子里的选择 */
.ship-pick { width: 100%; }
.ship-pick :deep(.el-select__wrapper),
.ship-pick :deep(.el-input__wrapper) { box-shadow: none !important; background: transparent; }
/* 隐藏下拉箭头/清除叉，避免误触；清除改放到下拉列表里 */
.ship-pick :deep(.el-select__suffix) { display: none; }
.ship-clear { color: #9ba8bf; font-size: 12px; }
.ship-opt { display: flex; flex-direction: column; gap: 3px; line-height: 1.3; }
.ship-opt-top { display: flex; align-items: center; gap: 8px; }
.ship-meta { color: #7d8aa3; font-size: 11px; }
.ship-ck { margin-left: auto; color: #67c23a; font-size: 14px; }
/* 单元格里显示所选集运单：单号 + 状态标签 */
.ship-sel { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.ship-sel b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
