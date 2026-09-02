<template>
  <div>
    <PageHeader>
    所有订单的物品拉平成一张表（对接的最小单位）。列可拖动换位/拖宽。物品编辑请到「商品订单」页展开面板里做。
    </PageHeader>

    <NotionTable :columns="columns" :rows="rows" :loading="loading" :load-failed="loadFailed" :actions-width="60"
                 :empty-text="loadFailed ? MSG_LOAD_FAILED : '没有符合条件的记录'"
                 table-name="items" hide-id :addable="false" :deletable="false" @reload="load" @tags-changed="onTagsChanged">
      <template #toolbar-right>
        <!-- 导出当前筛选的全部行（不是这一页）。放 toolbar-right 与订单页/集运页同侧：
             它们都是「对整张表做一件事」，而左边那排是筛选。 -->
        <el-button :loading="exporting" @click="doExport">导出 CSV</el-button>
      </template>

      <template #toolbar>
        <el-input v-model="filters.q" :placeholder="MSG_SEARCH_ORDER_LIKE" clearable style="width: 200px" @change="applyFilters" />
        <el-select v-model="filters.platform" placeholder="来源" clearable style="width: 120px" @change="applyFilters">
          <el-option v-for="p in ORDER_SOURCES" :key="p" :label="p" :value="p" />
        </el-select>
        <!-- 选项 = 国内段 + 集运段：列表显示的是继承后的状态，只列国内段的话，
             界面上一堆「已发出」却在筛选框里选不到（与订单页同款） -->
        <el-select v-model="filters.fulfillmentStatus" placeholder="状态" clearable style="width: 120px" @change="applyFilters">
          <el-option-group label="国内段（商品订单）">
            <el-option v-for="s in PURCHASE_STATUS" :key="s" :label="s" :value="s" />
          </el-option-group>
          <el-option-group label="国际段（集运订单）">
            <el-option v-for="s in SHIPMENT_STATUS" :key="s" :label="s" :value="s" />
          </el-option-group>
        </el-select>
        <el-select v-model="filters.platform_account" placeholder="账号昵称" clearable filterable style="width: 120px" @change="applyFilters">
          <el-option v-for="a in accountOptions" :key="a" :label="a" :value="a" />
        </el-select>
        <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                        start-placeholder="起" end-placeholder="止" @change="applyFilters" />
      </template>

      <!-- 彩色标签（只读），配色与订单列表一致：账号用持久化色序、来源/状态用语义色 -->
      <template #cell-platform_account="{ row }">
        <el-tag v-if="row.platform_account" :style="tagStyleAt(acctColor[row.platform_account] ?? -1, row.platform_account)">{{ row.platform_account }}</el-tag>
        <span v-else class="ph">—</span>
      </template>
      <template #cell-platform="{ row }">
        <el-tag v-if="row.platform" :style="platformSemanticStyle(row.platform)">{{ row.platform }}</el-tag>
        <span v-else class="ph">—</span>
      </template>
      <!-- 与商品订单页同一口径：挂了集运单就显示继承来的集运状态。
           ⚠️ 这里必须自己取 fulfillment_status —— 列配置里的 col.display 对**有插槽的列无效**
           （NotionTable 的插槽分支优先于 GotionCell，而 display 只在 GotionCell 里被调用），
           曾因此让两个页面对同一张单显示不同状态、tooltip 还说着反话。 -->
      <template #cell-purchase_status="{ row }">
        <el-tag :style="statusStyle(row.fulfillment_status)">
          {{ row.fulfillment_status }}
        </el-tag>
      </template>
      <!-- 灰显=物品名与商品标题相同（无独立物品详情） -->
      <template #cell-name="{ row }">
        <span :class="{ 'auto-txt': isTitleItem(row) }" :title="isTitleItem(row) ? '物品名与商品标题相同（无独立物品详情）' : ''">{{ row.name }}</span>
      </template>
      <!-- 编辑：打开该物品所属订单的编辑弹窗（复用商品订单同一套物品编辑，不跳转） -->
      <template #actions="{ row }">
        <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
      </template>
    </NotionTable>

    <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                   :page-size="pageSize" :current-page="page" @current-change="onPage" />
    
    <!-- 物品编辑：改的是该物品所属的整张商品订单（含其全部物品 + 邮费），复用订单页同一编辑组件与写入链 -->
    <el-dialog v-model="editVisible" :title="editTitle" width="640px" top="6vh" append-to-body @closed="onEditClosed">
    <div v-if="editingOrder">
      <div class="edit-ctx">
        <span class="ec-shop">{{ editingOrder.title || '（无标题）' }}</span>
        <el-tag :style="statusStyle(editingOrder.purchase_status)">{{ editingOrder.purchase_status }}</el-tag>
        <span v-if="editingOrder.platform_account" class="ec-dim">账号 {{ editingOrder.platform_account }}</span>
        <span class="ec-dim">下单 {{ editingOrder.date }}</span>
        <span v-if="editingOrder.order_no" class="ec-dim">订单号 {{ editingOrder.order_no }}</span>
      </div>
      <OrderEditPanel :order="editingOrder" :shipments="shipmentOptions" :accounts="accountOptions" @saved="editDirty = true" @conflict="refetchEditing" />
    </div>
    <div v-else v-loading="true" style="height: 90px"></div>
    </el-dialog>
  </div>
</template>

<script setup>
import PageHeader from '@/components/PageHeader.vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { itemsApi, ordersApi, shipmentApi, tagsApi } from '@/api'
import { MSG_FILTER_CLEARED, MSG_LOAD_FAILED, MSG_NOTHING_TO_EXPORT, MSG_SEARCH_ORDER_LIKE, ORDER_SOURCES, PAGE_SIZE, PURCHASE_STATUS, SHIPMENT_STATUS, platformSemanticStyle, statusStyle, tagStyleAt } from '@/constants'
import { exportCsv } from '@/utils/exportCsv'
import NotionTable from '@/components/NotionTable.vue'
import OrderEditPanel from '@/components/OrderEditPanel.vue'

// 默认列顺序 + 宽度；用户可拖动改序/改宽，持久化到后端（table-name="items"）
const columns = [
  { key: 'date', label: '下单日期', readonly: true, width: 100 },
  { key: 'platform_account', label: '账号', readonly: true, width: 100,
    // 同上：只是上色，文字仍是 row.platform_account
    exportRaw: true },
  { key: 'platform', label: '来源', readonly: true, width: 80,
    // 插槽只是把值套进 el-tag 上色，文字仍是 row.platform
    exportRaw: true },
  { key: 'title', label: '商品', readonly: true, width: 130 },
  { key: 'name', label: '物品名', readonly: true, width: 180,
    // 插槽只是给「物品名=商品标题」的行加个灰字样式，文字仍是 row.name —— 导出原始值就是屏幕上那个
    exportRaw: true },
  { key: 'quantity', label: '数量', readonly: true, width: 64 },
  { key: 'unit_price_cny', label: '单价（元）', format: 'cny', readonly: true, width: 100 },
  { key: 'amount_cny', label: '金额（元）', format: 'cny', readonly: true, width: 100 },
  // 与商品订单页同一口径：挂了集运单就显示继承来的集运状态、整格置灰（标签仍是原色）。
  // 本页所有列都是只读的（物品要改去订单页展开面板），这里的 lock 纯粹是**视觉提示**：
  // 让人一眼看出这行的状态不是订单自己的，而是跟着集运单走的。
  // 状态：**屏幕上**的值由 #cell-purchase_status 插槽自己取（插槽优先于 GotionCell）。
  // 这里以前写着「col.display 是死代码、不要写」——那句话在导出功能出现之前是对的，
  // 现在不是了：`utils/exportCsv.js` 的 `cell()` 就以 `col.display` 为准。
  // 不写的话，屏幕上按「已发出」筛出来的一批，导出的文件里那一格写着「待发货」
  // ——筛的是 A、导出的是 B，而这份文件正是要发给别人的。
  // 所以 display 在这一列的作用是：**屏幕上不起作用（插槽优先），导出时它是唯一真相**。
  // lock 仍然有效：它作用在 <td> 上，与插槽无关，负责把整格置灰做视觉提示。
  {
    key: 'purchase_status', label: '状态', readonly: true, width: 84,
    display: (row) => row.fulfillment_status ?? row.purchase_status,
    lock: (row) => !!row.shipment_order_id,
    lockHint: '该订单已挂靠集运订单，状态跟随那张单',
  },
  { key: 'order_no', label: '订单号', readonly: true, width: 130 },
  { key: 'express_no', label: '快递号', readonly: true, width: 110 },
]

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = PAGE_SIZE
const filters = reactive({ q: '', platform: '', fulfillmentStatus: '', platform_account: '', range: null })

// 账号标签的持久化配色（与其它页同一套色序，保证同一账号处处同色）+ 账号候选（编辑弹窗下拉用）
const acctColor = reactive({})
const accountOptions = ref([])
// 表格里改了账号标签之后，本页那份候选集要跟着变——编辑面板的 `:accounts` 吃的就是它。
// 不同步的话，新加的账号在编辑面板里永远选不到，直到切页回来才自愈。
function onTagsChanged({ field, values, gone = [] }) {
  if (field === 'platform_account') accountOptions.value = values

  // **通用地清掉停在旧值上的筛选**（口径与订单页/暂存页逐字相同）：
  // 标签改名后库里再没有旧值，拿它精确匹配会查回 0 行，
  // 空态显示「没有符合条件的记录」——用户刚改完名就看到「东西没了」。
  // **判据是 `gone`（被改名/删除的那些），不是「不在候选里」。**
  // 后者会在两种「什么都没发生」的情形下误清：① 这个事件加载时也会发（点一下列头的 ⚙）；
  // ② 筛选下拉的候选未必来自标签表（订单页「来源」用的是常量 ORDER_SOURCES）。
  // 实测过的后果：筛「来源=淘宝」→ 点一下 ⚙ → 筛选被清掉 + 弹一句
  // 「已改名或删除」，而它从来就不在标签表里。
  if (filters[field] && gone.includes(filters[field])) {
    filters[field] = ''
    ElMessage.info(MSG_FILTER_CLEARED)
    applyFilters()
  }
}
async function loadAcctColors() {
  try {
    const tags = await tagsApi.list('platform_account')
    tags.forEach((t) => { acctColor[t.value] = t.color })
    accountOptions.value = tags.map((t) => t.value)
  } catch (_) { /* 拦截器已提示 */ }
}

// 请求序号：筛选/翻页可以在上一次响应回来前再发一次，慢的那次后到会把新数据整个覆盖掉
// （表现为「清了筛选却只剩一部分」「内容是第2页、页码高亮第3页」）。只认最后一次发出的请求。
const loadFailed = ref(false)   // 上一次加载是否失败：空态文案据此说实话
const exporting = ref(false)

// 当前筛选 → 查询参数。**列表与导出共用这一份**：各写一份的话，
// 导出的 CSV 会和屏幕上看到的不是同一批行，而这种文件往往是要发给别人的。
function filterParams() {
  const params = {}
  if (filters.q) params.q = filters.q
  if (filters.platform) params.platform = filters.platform
  if (filters.fulfillmentStatus) params.fulfillment_status = filters.fulfillmentStatus
  if (filters.platform_account) params.platform_account = filters.platform_account
  if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
  return params
}

// 两条口径都在 utils/exportCsv.js 里说明了理由——反过来做会产出
// 「看起来对、其实少了东西」的文件，而这种文件往往会被当成完整账目发给别人。
async function doExport() {
  exporting.value = true
  try {
    const n = await exportCsv({
      fetchPage: (limit, offset) => itemsApi.list({ ...filterParams(), limit, offset }),
      columns,
      name: 'items',
    })
    if (!n) ElMessage.info(MSG_NOTHING_TO_EXPORT)
    else ElMessage.success(`已导出 ${n} 件物品`)
  } catch (_) { /* 拦截器已提示 */ } finally { exporting.value = false }
}

let loadSeq = 0
async function load() {
  const my = ++loadSeq
  loading.value = true
  try {
    const res = await itemsApi.list(
      { ...filterParams(), limit: pageSize, offset: (page.value - 1) * pageSize })
    if (my !== loadSeq) return          // 已有更新的请求发出，丢弃这次的结果
    rows.value = res.items
    total.value = res.total
    loadFailed.value = false
  } catch (_) {
    // **失败不能长得像空**。原先这里只有 try/finally：请求挂了 rows 保持空数组，
    // 页面渲染成「没有符合条件的记录 / 共 0 条」——而拦截器那句 toast 3 秒后就没了，
    // 此后这一屏与「真的一件物品都没有」完全无法区分，用户会以为数据没了。
    if (my === loadSeq) loadFailed.value = true
  } finally {
    if (my === loadSeq) loading.value = false
  }
}
// 名字要说清它做了两件事：**回到第一页** + 重新拉数据。
// 叫 reload 时它比行为窄，读的人会以为只是「重拉当前页」，
// 而筛选条件变了却不回第一页的话，用户会停在一个空的第 3 页上。
// ⚠️ 与 NotionTable 的组件事件 `@reload="load"` 是两回事：那个是「表格请父页重拉行」，
// 不重置分页，名字没问题。
function applyFilters() { page.value = 1; load() }
function onPage(p) { page.value = p; load() }

// 灰显 = 物品名与商品标题相同（无独立物品详情）
function isTitleItem(row) {
  return !!row.name && (row.name || '').trim() === (row.title || '').trim()
}

// —— 编辑：打开该物品所属订单，复用 OrderItemsEditor（同一写入链）——
const editVisible = ref(false)
const editingOrder = ref(null)   // ordersApi.get 拉来的整单（含 items/postage/version/shop）
const editingId = ref(null)
const editDirty = ref(false)     // 本次弹窗内是否发生过保存（关窗时据此决定要不要重载列表）
const shipmentOptions = ref([])  // 供「所属集运」下拉；进页时拉一次
async function loadShipment() {
  try { shipmentOptions.value = (await shipmentApi.list({ limit: 200, brief: true })).items } catch (_) { /* 已提示 */ }
}
const editTitle = computed(() => '编辑物品所属订单' + (editingOrder.value?.order_no ? ' · ' + editingOrder.value.order_no : ''))

// 与 load() 同款的请求序号门：只认**最后一次打开/重取**，迟到的响应一律作废。
// 没有它的话 editingOrder 由「最后返回」的响应决定而不是「最后点击」的那一次——
// 连点两行「编辑」时慢响应会顶掉弹窗内容，而 OrderEditPanel/OrderItemsEditor 的每一次写
// 都用 props.order.id，于是用户后续所有编辑都落到**另一张订单**上。
let editSeq = 0

async function openEdit(row) {
  const my = ++editSeq
  editDirty.value = false
  editingOrder.value = null
  editingId.value = row.order_id
  editVisible.value = true
  try {
    const o = await ordersApi.get(row.order_id)
    if (my !== editSeq) return             // 已有更新的点击，丢弃这次结果
    editingOrder.value = o
  } catch (_) {
    if (my === editSeq) editVisible.value = false   // 订单可能已删/无权限；拦截器已提示
  }
}
// 409：订单被并发改过 → 拉最新，让用户在最新基础上继续改
async function refetchEditing() {
  editDirty.value = true
  const id = editingId.value
  if (!id) return
  const my = ++editSeq                     // 重取也要占号，否则它会被在途的 openEdit 覆盖
  try {
    const o = await ordersApi.get(id)
    if (my === editSeq) editingOrder.value = o
  } catch (_) { /* 已提示 */ }
}
function onEditClosed() {
  editSeq++                                // 序号前进：在途响应作废，别在关闭后又把内容填回来
  const dirty = editDirty.value
  editingOrder.value = null; editingId.value = null; editDirty.value = false
  if (dirty) load()   // 有改动才刷新拍平的物品列表，反映新单价/数量/金额
}

onMounted(() => { loadAcctColors(); loadShipment(); load() })
</script>

<style scoped>
.auto-txt { color: var(--txt-3); font-style: italic; }
.edit-ctx { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 0 20px 4px; font-size: 13px; }
.edit-ctx .ec-shop { color: var(--txt-1); font-weight: 600; }
.edit-ctx .ec-dim { color: var(--txt-3); font-size: 12px; }
</style>
