<template>
  <div>
    <el-card>
      <NotionTable :columns="columns" :rows="rows" :loading="loading" expandable
                   table-name="shipment" @save="saveCell" @add="addRow" @delete="delRow" @reload="load">
        <template #toolbar>
          <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                          start-placeholder="起" end-placeholder="止" @change="reload" />
          <el-select v-model="filters.status" placeholder="状态" clearable style="width: 110px" @change="reload">
            <el-option v-for="s in SHIPMENT_STATUS" :key="s" :label="s" :value="s" />
          </el-select>
          <el-input v-model="filters.q" placeholder="搜集运单号" clearable style="width: 150px" @change="reload" />
          <el-upload ref="pkgUpload" class="ocr-up" multiple :show-file-list="false" :auto-upload="false"
                     accept="image/*" :on-change="onPkgPick">
            <div class="ocr-drop" :class="{ busy: pkgPending }">
              <el-icon class="ocr-ic"><Camera /></el-icon>
              <span>{{ pkgPending ? `后台识别中 ${pkgPending} 张…` : '点击选图 OCR建单（或拖「成品包裹」图到页面）' }}</span>
            </div>
          </el-upload>
        </template>

        <template #cell-orders="{ row }">
          <span :class="row.orders && row.orders.length ? '' : 'ph'">{{ tbSummary(row) }}</span>
        </template>

        <!-- 绑定快递单：每行一个投放区，把该包裹的「内含快递」截图拖到这里即自动关联商品订单。
             与整窗拖拽（建单）是两个互不含糊的目标：拖到行上=绑快递，拖到别处=建单。 -->
        <template #cell-bind_express="{ row }">
          <div class="bind-drop" :class="{ armed: dragActive, over: dragOverId === row.id, busy: bindBusy === row.id }"
               @click="pickForRow(row)"
               @dragenter.prevent.stop="dragOverId = row.id"
               @dragover.prevent.stop="dragOverId = row.id"
               @dragleave.prevent.stop="dragOverId = null"
               @drop.prevent.stop="onRowDrop(row, $event)">
            <el-icon class="bind-ic"><Loading v-if="bindBusy === row.id" /><Upload v-else /></el-icon>
            <span>{{ bindBusy === row.id ? '识别中…' : '拖入内含快递图' }}</span>
          </div>
        </template>

        <template #expand="{ row }">
          <div class="expand">
            <div class="ex-title">关联商品订单（在此点选增删；商品页「集运(点选)」列也能改；
              也可把「内含快递」截图拖到本行的「绑定快递单」格自动关联）</div>
            <el-table v-if="row.orders && row.orders.length" :data="row.orders" size="small">
              <el-table-column label="下单日期" width="110">
                <template #default="{ row: t }"><span :class="t.date ? '' : 'ph'">{{ t.date || '—' }}</span></template>
              </el-table-column>
              <el-table-column label="订单号" min-width="130">
                <template #default="{ row: t }">
                  <el-link type="primary" :underline="false" @click="gotoOrder(t)">{{ t.order_no || ('#' + t.id) }}</el-link>
                </template>
              </el-table-column>
              <el-table-column label="商品" min-width="160" show-overflow-tooltip>
                <template #default="{ row: t }"><span :class="t.shop ? '' : 'ph'">{{ t.shop || '—' }}</span></template>
              </el-table-column>
              <el-table-column label="物品" min-width="180">
                <template #default="{ row: t }">
                  <span :class="t.items && t.items.length ? '' : 'ph'">{{ itemSummary(t) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="结算（円）" width="110">
                <template #default="{ row: t }">{{ fmtJPY(t.jpy_settled) }}</template>
              </el-table-column>
              <el-table-column label="" width="72">
                <template #default="{ row: t }">
                  <el-button link type="danger" size="small" @click="detach(row, t)">移除</el-button>
                </template>
              </el-table-column>
            </el-table>
            <div v-else class="ph">暂无关联商品订单</div>
            <div class="add-line">
              <el-select :model-value="null" filterable placeholder="＋ 添加商品订单（未挂靠）"
                         size="small" class="tb-pick" @change="(id) => attach(row, id)">
                <el-option v-for="t in unassignedOptions" :key="t.id" :label="t.order_no || ('#' + t.id)" :value="t.id">
                  <div class="tb-opt">
                    <b>{{ t.order_no || ('#' + t.id) }}</b>
                    <span class="tb-meta">{{ itemSummary(t) }} · {{ fmtJPY(t.jpy_settled) }}</span>
                  </div>
                </el-option>
              </el-select>
              <span v-if="!unassignedOptions.length" class="ph small">没有未挂靠的商品订单</span>
            </div>
          </div>
        </template>

      </NotionTable>

      <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                     :page-size="pageSize" :current-page="page" @current-change="onPage" />
    </el-card>

    <!-- 行内「绑定快递单」的点选入口共用这一个 input（每行各挂一个太浪费） -->
    <input ref="rowFileInput" type="file" accept="image/*" class="hidden-file" @change="onRowPick">

    <!-- 拖拽提示：用顶部横幅而非整屏遮罩——遮罩会盖住行内的「绑定快递单」投放区，
         用户就没法瞄准了。横幅 pointer-events:none，不拦截拖拽。 -->
    <Teleport to="body">
      <div v-if="dragActive" class="drag-hint">
        <el-icon class="drag-hint-ic"><Camera /></el-icon>
        <span><b>松手 = 识别「成品包裹」截图建单</b>（可多张）</span>
        <span class="drag-hint-sep">·</span>
        <span>拖到某行的「绑定快递单」格 = 关联该包裹的内含快递</span>
      </div>
    </Teleport>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Camera, Loading, Upload } from '@element-plus/icons-vue'
import { shipmentApi, ordersApi } from '@/api'
import { SHIPMENT_STATUS } from '@/constants'
import { fmtJPY } from '@/utils/money'
import NotionTable from '@/components/NotionTable.vue'

const router = useRouter()
// 点关联订单的订单号 → 跳到商品页、隔离显示该单并自动展开（用 id，兼容无订单号的单）
function gotoOrder(t) { router.push({ path: '/orders', query: { focus: t.id } }) }

// 用本地时区（用户在日本=JST）的当天，而非 UTC；否则 JST 0~9 点新建会记成前一天
const today = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const columns = [
  { key: 'date', label: '日期', type: 'date', width: 130 },
  { key: 'shipment_no', label: '集运单号', type: 'text', minWidth: 120, placeholder: '集运单号' },
  { key: 'intl_tracking_no', label: '国际运单号', type: 'text', minWidth: 120 },
  { key: 'recipient', label: '收货人', type: 'tag', field: 'recipient', width: 100 },
  { key: 'weight', label: '重量kg', type: 'decimal', width: 80 },
  { key: 'status', label: '状态', type: 'select', options: SHIPMENT_STATUS, width: 100, clearable: false },
  { key: 'price_cny', label: '运费（元）', type: 'decimal', format: 'cny', width: 110, placeholder: '实付运费' },
  { key: 'fx_rate', label: '汇率', type: 'decimal', width: 80, placeholder: '当天汇率' },
  { key: 'special_fee_jpy', label: '特殊费（円）', type: 'int', format: 'jpy', width: 110, placeholder: '关税/消费税' },
  { key: 'jpy_override', label: '覆盖（円）', type: 'int', format: 'jpy', width: 110, placeholder: '实付日元' },
  { key: 'jpy_settled', label: '结算（円）', format: 'jpy', readonly: true, width: 110 },
  { key: 'orders', label: '商品订单', readonly: true, minWidth: 160, expand: true },
  // 虚拟列（行上无同名字段）：只作「内含快递」截图的投放区，见 #cell-bind_express
  { key: 'bind_express', label: '绑定快递单', readonly: true, width: 150 },
]

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 30
const filters = reactive({ range: null, status: '', q: '' })
const unassignedOptions = ref([])

async function load() {
  loading.value = true
  try {
    const params = { limit: pageSize, offset: (page.value - 1) * pageSize }
    if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
    if (filters.status) params.status = filters.status
    if (filters.q) params.q = filters.q
    const res = await shipmentApi.list(params)
    rows.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}
function reload() { page.value = 1; load() }
function onPage(p) { page.value = p; load() }

async function saveCell(row, key, value) {
  try {
    const updated = await shipmentApi.update(row.id, { version: row.version, [key]: value })
    Object.assign(row, updated)
  } catch (e) {
    if (e.response?.status === 409) { ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
  }
}

async function addRow(data = {}) {
  try {
    const created = await shipmentApi.create({ date: today(), status: '打包中', ...data })
    rows.value.unshift(created)
    total.value++
    return created                    // OCR 建单据此判断是否真的建成（失败时拦截器已提示）
  } catch (_) { return null }
}

async function delRow(row) {
  try {
    await ElMessageBox.confirm(`删除集运订单 ${row.shipment_no || row.id}？`, '确认', { type: 'warning' })
  } catch (_) { return }
  try {
    await shipmentApi.remove(row.id)
    ElMessage.success('已删除')
    if (rows.value.length === 1 && page.value > 1) page.value--   // 删掉本页最后一行 → 回上一页，避免停在空页
    load()                                                        // 重新拉取：分页/总数与后端同步
  } catch (_) { /* 拦截器已提示 */ }
}

function itemSummary(t) {
  if (!t.items || !t.items.length) return '—'
  return t.items.map((i) => `（${i.quantity}x）${i.name}`).join('，')
}
function tbSummary(row) {
  const list = row.orders || []
  if (!list.length) return '点击添加'
  return `${list.length} 单：${list.map((t) => t.order_no || ('#' + t.id)).join('，')}`
}
async function loadUnassigned() {
  try {
    const res = await ordersApi.list({ unassigned: true, limit: 200 })
    unassignedOptions.value = res.items
  } catch (_) { /* 拦截器已提示；避免 onMounted 里未捕获的 promise 拒绝 */ }
}
async function attach(shipmentRow, tbId) {
  if (!tbId) return
  try {
    const updated = await shipmentApi.attachOrder(shipmentRow.id, tbId)
    Object.assign(shipmentRow, updated)
    await loadUnassigned()
    ElMessage.success('已关联')
  } catch (_) { /* 拦截器已提示（含 422：已挂靠其他单） */ }
}
// --- OCR：两个投放目标 -------------------------------------------------------
// ① 整窗拖拽 / 工具栏选图 → 「成品包裹」截图 → 建集运单（可多张，后台串行队列）
// ② 拖到某行的「绑定快递单」格 → 「内含快递」截图 → 关联该包裹的商品订单
// 串行而非并发的理由同商品订单页：后端 OCR 本就用锁串行，且浏览器每域名 ~6 连接，
// 并发慢请求会把随后的建单请求挤到超时。每张独立 try/catch，单张失败不打断队列。
const pkgUpload = ref(null)
const pkgPending = ref(0)         // 队列中待处理 + 处理中的张数
const pkgQueue = []
let pkgRunning = false
const dragActive = ref(false)     // 整窗有文件拖入：亮起各行投放区 + 顶部横幅
let dragDepth = 0                 // dragenter/leave 会因子元素冒泡多次触发，用计数判断真正离开
const dragOverId = ref(null)      // 正悬停其上的行 id
const bindBusy = ref(null)        // 正在识别内含快递的集运单 id
const rowFileInput = ref(null)
let pickTargetRow = null          // 行内投放区「点击选图」的目标行

function onPkgPick(uploadFile) { enqueuePkg(uploadFile?.raw ? [uploadFile.raw] : []) }

function enqueuePkg(files) {
  const imgs = files.filter((f) => f && (!f.type || f.type.startsWith('image/')))
  const skipped = files.length - imgs.length
  if (skipped) ElMessage.warning(`已跳过 ${skipped} 个非图片文件`)   // 拖拽不受 accept 约束
  if (!imgs.length) return
  pkgQueue.push(...imgs)
  pkgPending.value += imgs.length
  pumpPkg()
}

async function pumpPkg() {
  if (pkgRunning) return          // 已有 worker 在跑；新入队的图会被同一循环取走
  pkgRunning = true
  try {
    while (pkgQueue.length) {
      const file = pkgQueue.shift()
      try {
        await processPkg(file)
      } finally {
        pkgPending.value--
      }
    }
  } finally {
    pkgRunning = false
    pkgUpload.value?.clearFiles?.()   // 队列排空后清内部列表，便于重复选同一张图
  }
}

async function processPkg(file) {
  try {
    const res = await shipmentApi.ocr(file)
    if (res.express_nos?.length && !res.shipment_no && !res.intl_tracking_no) {
      // 拖错了：这是「内含快递」页，它没有集运单号，无从判断该挂到哪一单
      ElMessage.warning('这是「内含快递」截图，请把它拖到目标集运单那一行的「绑定快递单」格')
      return
    }
    if (!res.shipment_no && !res.intl_tracking_no) {
      ElMessage.warning('未识别到集运单号/国际单号，请确认上传的是「成品包裹」页截图')
      return
    }
    // 集运单号有唯一约束：先查重，命中则提示而不是让后端抛约束错误
    if (res.shipment_no) {
      const dup = await shipmentApi.list({ q: res.shipment_no, limit: 5 })
      if ((dup.items || []).some((r) => r.shipment_no === res.shipment_no)) {
        ElMessage.warning(`集运单 ${res.shipment_no} 已存在，可直接把「内含快递」图拖到该行`)
        return
      }
    }
    const created = await addRow({
      date: res.date || today(),
      shipment_no: res.shipment_no,
      intl_tracking_no: res.intl_tracking_no,
      note: res.channel,                 // 渠道（如「日本空运-广东直飞EMS」）暂存备注
    })
    if (created) ElMessage.success(`已建单 ${res.shipment_no || res.intl_tracking_no}，把「内含快递」图拖到该行即可关联商品订单`)
  } catch (_) { /* 拦截器已提示 */ }
}

// --- 行内「绑定快递单」投放区 ---
function pickForRow(row) {
  if (bindBusy.value) return
  pickTargetRow = row
  rowFileInput.value.value = ''        // 清空，否则连选同一张图不触发 change
  rowFileInput.value.click()
}
function onRowPick(e) {
  const file = e.target.files?.[0]
  if (file && pickTargetRow) bindExpress(pickTargetRow, file)
}
function onRowDrop(row, e) {
  dragOverId.value = null
  dragActive.value = false
  dragDepth = 0
  const files = Array.from(e.dataTransfer?.files || []).filter((f) => !f.type || f.type.startsWith('image/'))
  if (!files.length) return
  if (files.length > 1) ElMessage.warning('一行一次只处理一张「内含快递」截图，已取第一张')
  bindExpress(row, files[0])
}

async function bindExpress(shipmentRow, file) {
  if (bindBusy.value) return
  bindBusy.value = shipmentRow.id
  try {
    const res = await shipmentApi.ocrExpress(shipmentRow.id, file)
    if (!res.express_nos.length) {
      ElMessage.warning('未识别到快递单号，请确认拖入的是「内含快递」页截图')
      return
    }
    Object.assign(shipmentRow, res.shipment)
    await loadUnassigned()
    const parts = [`已关联 ${res.attached.length} 单`]
    if (res.skipped.length) parts.push(`跳过 ${res.skipped.length} 单（已挂其他集运单）`)
    if (res.unmatched.length) parts.push(`未匹配 ${res.unmatched.length} 个快递号：${res.unmatched.join('、')}`)
    const text = parts.join('；')
    // 有跳过/未匹配就用 warning 常驻久一点，让用户看清是哪几个号
    if (res.skipped.length || res.unmatched.length) ElMessage({ type: 'warning', message: text, duration: 8000 })
    else ElMessage.success(text)
  } catch (_) { /* 拦截器已提示 */ } finally {
    bindBusy.value = null
  }
}

// --- 整窗拖拽（建单）。行内投放区的 drop 已 stopPropagation，不会冒泡到这里 ---
function isFileDrag(e) {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')
}
function onWinDragEnter(e) {
  if (!isFileDrag(e)) return          // 列头换序拖的是 text/plain，不会误触
  e.preventDefault(); dragDepth++; dragActive.value = true
}
function onWinDragOver(e) {
  if (!isFileDrag(e)) return
  e.preventDefault()                  // 必须 preventDefault，否则不触发 drop
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
}
function onWinDragLeave(e) {
  if (!isFileDrag(e)) return
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) { dragActive.value = false; dragOverId.value = null }
}
function onWinDrop(e) {
  if (!isFileDrag(e)) return
  e.preventDefault(); dragDepth = 0; dragActive.value = false; dragOverId.value = null
  enqueuePkg(Array.from(e.dataTransfer.files || []))
}

async function detach(shipmentRow, tbRow) {
  try {
    const updated = await shipmentApi.detachOrder(shipmentRow.id, tbRow.id)
    Object.assign(shipmentRow, updated)
    await loadUnassigned()
    ElMessage.success('已移除')
  } catch (_) { /* 拦截器已提示 */ }
}

onMounted(() => {
  load()
  loadUnassigned()
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
.ph { color: #5b6880; }
.expand { padding: 12px 20px; }
.ex-title { color: #9ba8bf; font-size: 13px; margin-bottom: 8px; }

/* 工具栏 OCR 建单入口（拖拽由 window 监听统一处理，这里只负责点击选图） */
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

/* 行内「绑定快递单」投放区：平时低调，拖拽中(armed)亮起，悬停(over)高亮 */
.bind-drop {
  display: flex; align-items: center; justify-content: center; gap: 5px;
  height: 26px; padding: 0 8px; border: 1px dashed #3a4a6b; border-radius: 4px;
  color: #6b7a99; font-size: 12px; white-space: nowrap; cursor: pointer;
  transition: border-color .15s, background .15s, color .15s;
}
.bind-drop:hover { border-color: #409eff; color: #7f9cff; }
.bind-drop.armed { border-color: #5a7cc0; color: #7f9cff; background: rgba(64, 158, 255, 0.06); }
.bind-drop.over {
  border-color: #67c23a; color: #9ae06a; background: rgba(103, 194, 58, 0.14);
  box-shadow: 0 0 0 2px rgba(103, 194, 58, 0.25);
}
.bind-drop.busy { color: #7d8aa3; cursor: default; }
/* 子元素不接收拖拽事件，否则 dragleave 会在图标/文字间来回误触发导致高亮闪烁 */
.bind-drop > * { pointer-events: none; }
.bind-ic { font-size: 13px; }
.hidden-file { display: none; }

/* 拖拽提示横幅：顶部居中、不拦截事件，故不会遮住行内投放区 */
.drag-hint {
  position: fixed; top: 18px; left: 50%; transform: translateX(-50%); z-index: 9000;
  pointer-events: none; display: flex; align-items: center; gap: 8px;
  padding: 12px 22px; border: 1px dashed #409eff; border-radius: 10px;
  background: rgba(16, 25, 44, 0.95); box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  color: #cfe0ff; font-size: 13px; white-space: nowrap;
}
.drag-hint b { color: #eaf1ff; }
.drag-hint-ic { font-size: 18px; color: #6ea8ff; }
.drag-hint-sep { color: #55658a; }
.add-line { margin-top: 10px; display: flex; align-items: center; gap: 10px; }
.tb-pick { width: 320px; }
.tb-opt { display: flex; flex-direction: column; line-height: 1.25; }
.tb-meta { color: #7d8aa3; font-size: 11px; }
.small { font-size: 12px; }
</style>
