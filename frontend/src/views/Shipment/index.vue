<template>
  <div>
    <PageHeader>
    <b>集运订单</b>：把多张商品订单打包发往日本的那一段。运费按单据日期的汇率折算。
    展开一行可以挂靠商品订单；也可以把「内含快递」截图拖到某一行的绑定格，
    按快递单号自动挂靠。
    </PageHeader>

    <NotionTable :columns="columns" :rows="rows" :loading="loading" expandable
                 table-name="shipment" :empty-text="loadFailed ? '加载失败——请检查网络或后端，然后重试' : '没有符合条件的记录'" @save="saveCell" @add="addRow" @delete="delRow" @reload="load">
      <template #toolbar>
        <el-input v-model="filters.q" placeholder="搜集运单号" clearable style="width: 200px" @change="reload" />
        <el-select v-model="filters.shipmentStatus" placeholder="状态" clearable style="width: 120px" @change="reload">
          <el-option v-for="s in SHIPMENT_STATUS" :key="s" :label="s" :value="s" />
        </el-select>
        <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                        start-placeholder="起" end-placeholder="止" @change="reload" />
      </template>

      <template #toolbar-right>
        <OcrButton ref="pkgUpload" :pending="pkgPending" @pick="onPkgPick">
          点「OCR」选图，或把图<b>拖到页面任意位置</b>松手，识别<b>「成品包裹」页</b>截图建集运单
          （集运单号、国际运单号、下单时间、渠道）。
          <br>另一种截图是<b>「内含快递」页</b>——那个要拖到某一行的「绑定快递单」格里，
          按快递单号自动挂靠商品订单；拖到页面上会提示你拖错地方了。
        </OcrButton>
      </template>

      <template #cell-orders="{ row }">
        <span :class="row.orders && row.orders.length ? '' : 'ph'">{{ tbSummary(row) }}</span>
      </template>

      <!-- 绑定快递单：每行一个投放区，把该包裹的「内含快递」截图拖到这里即自动关联商品订单。
           与整窗拖拽（建单）是两个互不含糊的目标：拖到行上=绑快递，拖到别处=建单。 -->
      <template #cell-bind_express="{ row }">
        <div class="bind-drop" :class="{ armed: dragActive, over: dragOverId === row.id, busy: isBinding(row) }"
             @click="pickForRow(row)"
             @dragenter.prevent.stop="dragOverId = row.id"
             @dragover.prevent.stop="dragOverId = row.id"
             @dragleave.prevent.stop="dragOverId = null"
             @drop.prevent.stop="onRowDrop(row, $event)">
          <el-icon class="bind-ic"><Loading v-if="isBinding(row)" /><Upload v-else /></el-icon>
          <span>{{ isBinding(row) ? '识别中…' : '拖入内含快递图' }}</span>
        </div>
      </template>

      <template #expand="{ row }">
        <div class="expand">
          <div class="ex-title">关联商品订单（在此点选增删；商品页「集运(点选)」列也能改；
            也可把「内含快递」截图拖到本行的「绑定快递单」格自动关联）</div>
          <el-table v-if="row.orders && row.orders.length" :data="row.orders">
            <el-table-column label="下单日期" width="110">
              <template #default="{ row: t }"><span :class="t.date ? '' : 'ph'">{{ t.date || '—' }}</span></template>
            </el-table-column>
            <el-table-column label="订单号" min-width="130">
              <template #default="{ row: t }">
                <el-link type="primary" :underline="false" @click="gotoOrder(t)">{{ t.order_no || ('#' + t.id) }}</el-link>
              </template>
            </el-table-column>
            <el-table-column label="商品" min-width="160" show-overflow-tooltip>
              <template #default="{ row: t }"><span :class="t.title ? '' : 'ph'">{{ t.title || '—' }}</span></template>
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
                <el-button link type="danger" @click="detach(row, t)">移除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-else class="ph">暂无关联商品订单</div>
          <div class="add-line">
            <!-- 远程搜索：本地那 200 条只是**默认展示**，输订单号能搜到更旧的。
                 原先只有客户端 filterable + `limit:200` 顶格（后端 `le=200` 就是硬上限），
                 而排序是 `date desc`——被砍掉的**恰好是日期最旧、最该发出去的那批**，
                 且下拉不给任何「还有 N 条」的痕迹，输订单号只会得到「无匹配数据」。 -->
            <el-select :model-value="null" filterable placeholder="＋ 添加商品订单（未挂靠）" class="tb-pick"
                       remote :remote-method="searchUnassigned" :loading="pickSearching"
                       remote-show-suffix reserve-keyword
                       :no-data-text="pickHits === null ? '输入订单号可搜索更早的单' : '没有匹配的未挂靠订单'"
                       @visible-change="onPickDropdown" @change="(id) => attach(row, id)">
              <el-option v-for="t in pickOptions" :key="t.id" :label="t.order_no || ('#' + t.id)" :value="t.id">
                <div class="tb-opt">
                  <b>{{ t.order_no || ('#' + t.id) }}</b>
                  <span class="tb-meta">{{ itemSummary(t) }} · {{ fmtJPY(t.jpy_settled) }}</span>
                </div>
              </el-option>
            </el-select>
            <!-- **只有真的一条都没有时才这么说。** 之前它在「被 200 截断」时也会出现，
                 把用户骗去以为不存在，而其实还有更旧的、以及另外两条可用路径。 -->
            <span v-if="!unassignedOptions.length && unassignedTotal === 0" class="ph small">没有未挂靠的商品订单</span>
            <span v-else-if="unassignedTotal > unassignedOptions.length" class="ph small">
              仅列出最近 {{ unassignedOptions.length }} 条（共 {{ unassignedTotal }}），输订单号可搜更早的
            </span>
          </div>
        </div>
      </template>

    </NotionTable>

    <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                   :page-size="pageSize" :current-page="page" @current-change="onPage" />

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
import PageHeader from '@/components/PageHeader.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Camera, Loading, Upload } from '@element-plus/icons-vue'
import { shipmentApi, ordersApi } from '@/api'
import { checkImageSize } from '@/utils/imageGate'
import { useWindowFileDrop } from '@/utils/windowFileDrop'
import { handled } from '@/api/http'
import { PAGE_SIZE, SHIPMENT_STATUS, longToast } from '@/constants'
import { fmtJPY } from '@/utils/money'
import { today } from '@/utils/datetime'
import { afterCreate, afterDelete } from '@/utils/listRows'
import NotionTable from '@/components/NotionTable.vue'
import OcrButton from '@/components/OcrButton.vue'

const router = useRouter()
// 点关联订单的订单号 → 跳到商品页、隔离显示该单并自动展开（用 id，兼容无订单号的单）
function gotoOrder(t) { router.push({ path: '/orders', query: { focus: t.id } }) }


const columns = [
  { key: 'date', label: '日期', type: 'date', width: 130, clearable: false },
  { key: 'shipment_no', label: '集运单号', type: 'text', minWidth: 120, placeholder: '集运单号' },
  { key: 'intl_tracking_no', label: '国际运单号', type: 'text', minWidth: 120 },
  { key: 'recipient', label: '收货人', type: 'tag', field: 'recipient', width: 100 },
  { key: 'weight', label: '重量kg', type: 'decimal', width: 80 },
  { key: 'shipment_status', label: '状态', type: 'select', options: SHIPMENT_STATUS, width: 100, clearable: false },
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
const pageSize = PAGE_SIZE
const filters = reactive({ range: null, shipmentStatus: '', q: '' })
const unassignedOptions = ref([])
const unassignedTotal = ref(0)   // 后端说共有多少条未挂靠；用来判断本地这 200 条是不是被截断了

// 请求序号：筛选/翻页可以在上一次响应回来前再发一次，慢的那次后到会把新数据整个覆盖掉
// （表现为「清了筛选却只剩一部分」「内容是第2页、页码高亮第3页」）。只认最后一次发出的请求。
let loadSeq = 0
// 上一次加载是否失败：空态文案据此说实话。
// 「请求挂了」与「真的没有记录」渲染成同一句「没有符合条件的记录」，是这个项目反复栽的那类
// ——拦截器那句 toast 三秒就没了，此后这一屏与「真的还没记过账」完全无法区分。
const loadFailed = ref(false)
async function load() {
  const my = ++loadSeq
  loading.value = true
  try {
    const params = { limit: pageSize, offset: (page.value - 1) * pageSize }
    if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
    if (filters.shipmentStatus) params.shipment_status = filters.shipmentStatus
    if (filters.q) params.q = filters.q
    const res = await shipmentApi.list(params)
    if (my !== loadSeq) return          // 已有更新的请求发出，丢弃这次的结果
    rows.value = res.items
    total.value = res.total
    loadFailed.value = false
  } catch (_) {
    // 拦截器已提示原因；这里负责让**页面本身**留下痕迹，否则空态在说假话。
    if (my === loadSeq) loadFailed.value = true
  } finally {
    if (my === loadSeq) loading.value = false
  }
}
function reload() { page.value = 1; load() }
function onPage(p) { page.value = p; load() }

async function saveCell(row, key, value) {
  try {
    const updated = await shipmentApi.update(row.id, { version: row.version, [key]: value })
    Object.assign(row, updated)
  } catch (e) {
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
  }
}

async function addRow(data = {}, done) {
  try {
    const created = await shipmentApi.create({ date: today(), shipment_status: '打包中', ...data })
    await afterCreate(created, { rows, total, page, filters, load, pageSize })
    done?.(true)
    return created                    // OCR 建单据此判断是否真的建成（失败时拦截器已提示）
  } catch (e) {
    done?.(false)   // 失败时保留幽灵行里的草稿，让用户就地改
    // 409 被 http 拦截器刻意跳过（留给页面处理）。不在这里提示的话，撞集运单号唯一约束时
    // 页面「什么都没发生」：没有 toast、没有新行、幽灵行里刚敲的单号也被 commitNew 清掉了。
    if (e.response?.status === 409) {
      handled(e)
      const who = data.shipment_no ? `集运单号「${data.shipment_no}」` : '该记录'
      ElMessage.warning(`${who} 已存在，未添加`)
    }
    return null
  }
}

async function delRow(row) {
  try {
    await ElMessageBox.confirm(`删除集运订单「${row.shipment_no || row.id}」？`, '删除集运订单',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch (_) { return }
  try {
    await shipmentApi.remove(row.id)
    ElMessage.success('已删除')
    await afterDelete({ rows, page, load })
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
    unassignedTotal.value = res.total          // 截断了没有，只有它说得清
  } catch (_) { /* 拦截器已提示；避免 onMounted 里未捕获的 promise 拒绝 */ }
}

// 远程搜索未挂靠订单。照 OrderEditPanel 的 searchShipment 同一形状（seq 防乱序）。
// **`pickHits` 用 null 区分「没在搜」与「搜了但零命中」**——合成一个值的话，
// 零命中时会走到「非搜索态」的文案上去，这个坑本仓已经记过一次。
const pickHits = ref(null)
const pickSearching = ref(false)
let pickSeq = 0
const pickOptions = computed(() => pickHits.value ?? unassignedOptions.value)

async function searchUnassigned(kw) {
  const q = (kw || '').trim()
  if (!q) { pickHits.value = null; pickSearching.value = false; return }
  const my = ++pickSeq
  pickSearching.value = true
  try {
    // `unassigned` 与 `q` 在后端是各自独立 append 到 conds 的，可以组合——不用改后端
    const res = await ordersApi.list({ unassigned: true, q, limit: 50 })
    if (my === pickSeq) pickHits.value = res.items          // 迟到的响应不覆盖新结果
  } catch (_) { /* 拦截器已提示 */ } finally {
    if (my === pickSeq) pickSearching.value = false
  }
}
function onPickDropdown(visible) {
  if (!visible) { pickHits.value = null; pickSeq++ }        // 关下拉即回到默认列表
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
// 整窗拖图走共享实现（见 utils/windowFileDrop.js：判据/preventDefault/注册/反注册
// 是一整套，少一样就是静默故障——暂存页就漏过「注册」）。
const { dragActive, reset: resetWindowDrag } = useWindowFileDrop(enqueuePkg)
const dragOverId = ref(null)      // 正悬停其上的行 id（本页特有：每行一个投放区）
// 拖拽一结束就清掉行内高亮。原先这一步写在 onWinDragLeave/onWinDrop 里，
// 收进共享实现之后由这里接管——不清的话，松手后那一行会一直亮着。
watch(dragActive, (on) => { if (!on) dragOverId.value = null })
const bindingRowId = ref(null)    // **正在识别**内含快递的那一行 id（排队中的行见 isBinding）
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
  // 分辨率超上限的在本机就拦下来：后端是硬拒绝（400），
  // 传上去只是让用户在慢网络上白等一趟。判据与后端逐字节相同。
  const tooBig = await checkImageSize(file)
  if (tooBig) { ElMessage.warning(tooBig); return }
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
//
// 这里原先是一个**全局**单闸：`if (bindBusy.value) return`，而 bindBusy 存的是行 id、
// 只被当布尔用。后果是 A 行正在识别时（真实 OCR 是秒级的）往 B 行拖图 —— B 行照样亮绿环、
// 光标是 pointer、松手看起来一切正常，而那张图**一次请求都没发**，也没有任何提示；
// 紧接着 A 行完成还会弹一句绿色的「已关联 N 单」，更坐实了「两张都成了」的错觉。
// `@drop.prevent.stop` 的 .stop 又挡住了冒泡，图连整窗建单队列都进不去，是彻底销毁。
//
// 改成排队（与本文件的 pkgQueue 同款）：后端 OCR 本来就是串行的（_infer_lock），
// 排队既不会更慢，又保证用户交出去的每一张图都真的被处理。
function pickForRow(row) {
  pickTargetRow = row
  rowFileInput.value.value = ''        // 清空，否则连选同一张图不触发 change
  rowFileInput.value.click()
}
function onRowPick(e) {
  const file = e.target.files?.[0]
  // **走队列，不要直接调 bindExpress。** 拖拽那条路（onRowDrop）是排队的，
  // 而点选这条原先直接发请求——A 行识别中点 B 行选图就是两个请求并发，
  // 而 `bindingRowId` 是**单槽**：B 一开始就把它改写，A 的「识别中…」当场消失、
  // 格子恢复可点，用户以为没提交、再拖一次；先回来的那个在 finally 里清成 null，
  // 另一行的忙态也跟着没了而请求还在飞。
  // 后端不会写脏数据（OCR 有 _infer_lock、挂靠 UPDATE 带 EXISTS 守卫），
  // 坏的是**界面对「谁在跑」说假话**，外加重复的多秒 OCR。
  if (file && pickTargetRow) enqueueBind(pickTargetRow, file)
}
function onRowDrop(row, e) {
  dragOverId.value = null
  // **这一行不能省，也不能自己写。** 模板上是 `@drop.prevent.stop`，`.stop` 挡住了冒泡，
  // 所以整窗那个 drop 处理器不会触发——它的内部计数器得由这里复位，
  // 否则下次拖完提示层就下不去了。
  // 原先这里写的是 `dragActive.value = false; dragDepth = 0`：后半句引用了一个
  // composable 抽出去之后就不存在的变量，ESM 严格模式当场 ReferenceError，
  // 于是**下面的 enqueueBind 永远到不了**——拖图进来高亮正常消失、看着像收下了，
  // 而实际上一次请求都不发，整条「拖图绑定内含快递」的路径静默死掉。
  resetWindowDrag()
  const files = Array.from(e.dataTransfer?.files || []).filter((f) => !f.type || f.type.startsWith('image/'))
  if (!files.length) return
  if (files.length > 1) ElMessage.warning('一行一次只处理一张「内含快递」截图，已取第一张')
  enqueueBind(row, files[0])
}

// 排队：把 {行, 图} 推进队列，逐张串行识别。行的「识别中…」显示条件改成
// 「正在识别本行 或 本行还在队列里」，否则会出现「排着队但没有任何一行显示识别中」。
const bindQueue = ref([])
let bindRunning = false
function isBinding(row) {
  return bindingRowId.value === row.id || bindQueue.value.some((j) => j.row.id === row.id)
}
function enqueueBind(row, file) {
  bindQueue.value.push({ row, file })
  pumpBind()
}
async function pumpBind() {
  if (bindRunning) return
  bindRunning = true
  try {
    while (bindQueue.value.length) {
      const { row, file } = bindQueue.value.shift()
      await bindExpress(row, file)
    }
  } finally {
    bindRunning = false
  }
}

async function bindExpress(shipmentRow, file) {
  // 三条 OCR 上传路径都要预检，漏一条就是「另外两处会提前提示、这一处传完才报错」
  const tooBig = await checkImageSize(file)
  if (tooBig) { ElMessage.warning(tooBig); return }
  bindingRowId.value = shipmentRow.id
  try {
    const res = await shipmentApi.ocrExpress(shipmentRow.id, file)
    const unreadable = res.unreadable || 0
    if (!res.express_nos.length) {
      ElMessage.warning(unreadable
        ? `识别到 ${unreadable} 行「快递单号」，但一个号都没读出来（可能是断行或图太糊），请重截一张更清晰的`
        : '未识别到快递单号，请确认拖入的是「内含快递」页截图')
      return
    }
    Object.assign(shipmentRow, res.shipment)
    await loadUnassigned()
    const parts = [`已关联 ${res.attached.length} 单`]
    if (res.skipped.length) parts.push(`跳过 ${res.skipped.length} 单（已挂其他集运单）`)
    if (res.unmatched.length) parts.push(`未匹配 ${res.unmatched.length} 个快递号：${res.unmatched.join('、')}`)
    // 「看见了快递单号这一行、却没读出号」必须说出来。它原先在响应里根本不存在，
    // 于是 3 行坏 1 行的截图会安安静静地弹绿色「已关联 2 单」，少挂的那单无人知晓。
    if (unreadable) parts.push(`另有 ${unreadable} 行未能读出单号，请核对下方识别结果`)
    // 识别到的号一并列出：这个字段一直都在（注释写着「供人工核对」），却从来没渲染过。
    parts.push(`识别到：${res.express_nos.join('、')}`)
    const text = parts.join('；')
    // 有跳过/未匹配/读不出就用 warning 常驻久一点，让用户看清是哪几个号
    if (res.skipped.length || res.unmatched.length || unreadable) {
      longToast(ElMessage, 'warning', text)      // 与全仓「长提示」同一档，见 constants.TOAST_LONG
    } else ElMessage.success(text)
  } catch (_) { /* 拦截器已提示 */ } finally {
    bindingRowId.value = null
  }
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
})
</script>

<style scoped>
.expand { padding: 12px 20px; }
.ex-title { color: var(--txt-2); font-size: 13px; margin-bottom: 8px; }

/* 工具栏 OCR 建单入口（拖拽由 window 监听统一处理，这里只负责点击选图） */

/* 行内「绑定快递单」投放区：平时低调，拖拽中(armed)亮起，悬停(over)高亮 */
.bind-drop {
  display: flex; align-items: center; justify-content: center; gap: 5px;
  height: 26px; padding: 0 8px; border: 1px dashed var(--border-strong); border-radius: 4px;
  color: #6b7a99; font-size: 12px; white-space: nowrap; cursor: pointer;
  transition: border-color .15s, background .15s, color .15s;
}
.bind-drop:hover { border-color: var(--brand); color: var(--brand-soft); }
.bind-drop.armed { border-color: var(--brand-line-strong); color: var(--brand-soft); background: var(--brand-faint); }
.bind-drop.over {
  /* 这条规则里原本有三种绿：前景一种、底色与外环是 Element 旧绿、边框才是 token 的
     --ok。三种绿并排出现在同一个投放区上。收敛成一套。 */
  border-color: var(--ok); color: var(--ok); background: var(--ok-weak);
  box-shadow: 0 0 0 2px var(--ok-ring);
}
.bind-drop.busy { color: var(--txt-3); cursor: default; }
/* 子元素不接收拖拽事件，否则 dragleave 会在图标/文字间来回误触发导致高亮闪烁 */
.bind-drop > * { pointer-events: none; }
.bind-ic { font-size: 13px; }
.hidden-file { display: none; }

/* 拖拽提示横幅：顶部居中、不拦截事件，故不会遮住行内投放区 */
.drag-hint {
  position: fixed; top: 18px; left: 50%; transform: translateX(-50%); z-index: 9000;
  pointer-events: none; display: flex; align-items: center; gap: 8px;
  padding: 12px 22px; border: 1px dashed var(--brand); border-radius: 10px;
  background: rgba(16, 25, 44, 0.95); box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  color: #cfe0ff; font-size: 13px; white-space: nowrap;
}
.drag-hint b { color: #eaf1ff; }
.drag-hint-ic { font-size: 18px; color: #6ea8ff; }
.drag-hint-sep { color: #55658a; }
.add-line { margin-top: 10px; display: flex; align-items: center; gap: 10px; }
.tb-pick { width: 320px; }
.tb-opt { display: flex; flex-direction: column; line-height: 1.25; }
.tb-meta { color: var(--txt-3); font-size: 11px; }
.small { font-size: 12px; }
</style>
