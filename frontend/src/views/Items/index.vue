<template>
  <div>
    <PageHeader>
    所有订单的物品拉平成一张表（对接的最小单位）。列可拖动换位/拖宽。物品编辑请到「商品订单」页展开面板里做。
    </PageHeader>

    <NotionTable :columns="columns" :rows="rows" :loading="loading" :actions-width="60"
                 :empty-text="loadFailed ? '加载失败——请检查网络或后端，然后重试' : '没有符合条件的记录'"
                 table-name="items" hide-id :addable="false" :deletable="false" @reload="load">
      <template #toolbar>
        <el-input v-model="filters.q" placeholder="搜物品/商品/单号/快递号" clearable style="width: 200px" @change="reload" />
        <el-select v-model="filters.platform" placeholder="来源" clearable style="width: 120px" @change="reload">
          <el-option v-for="p in ORDER_SOURCES" :key="p" :label="p" :value="p" />
        </el-select>
        <!-- 选项 = 国内段 + 集运段：列表显示的是继承后的状态，只列国内段的话，
             界面上一堆「已发出」却在筛选框里选不到（与订单页同款） -->
        <el-select v-model="filters.fulfillmentStatus" placeholder="状态" clearable style="width: 120px" @change="reload">
          <el-option-group label="国内段（商品订单）">
            <el-option v-for="s in PURCHASE_STATUS" :key="s" :label="s" :value="s" />
          </el-option-group>
          <el-option-group label="国际段（集运订单）">
            <el-option v-for="s in SHIPMENT_STATUS" :key="s" :label="s" :value="s" />
          </el-option-group>
        </el-select>
        <el-select v-model="filters.platform_account" placeholder="账号昵称" clearable filterable style="width: 120px" @change="reload">
          <el-option v-for="a in accountOptions" :key="a" :label="a" :value="a" />
        </el-select>
        <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                        start-placeholder="起" end-placeholder="止" @change="reload" />
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
import { itemsApi, ordersApi, shipmentApi, tagsApi } from '@/api'
import { ORDER_SOURCES, PURCHASE_STATUS, SHIPMENT_STATUS, platformSemanticStyle, statusStyle, tagStyleAt } from '@/constants'
import NotionTable from '@/components/NotionTable.vue'
import OrderEditPanel from '@/components/OrderEditPanel.vue'

// 默认列顺序 + 宽度；用户可拖动改序/改宽，持久化到后端（table-name="items"）
const columns = [
  { key: 'date', label: '下单日期', readonly: true, width: 100 },
  { key: 'platform_account', label: '账号', readonly: true, width: 100 },
  { key: 'platform', label: '来源', readonly: true, width: 80 },
  { key: 'title', label: '商品', readonly: true, width: 130 },
  { key: 'name', label: '物品名', readonly: true, width: 180 },
  { key: 'quantity', label: '数量', readonly: true, width: 64 },
  { key: 'unit_price_cny', label: '单价（元）', format: 'cny', readonly: true, width: 100 },
  { key: 'amount_cny', label: '金额（元）', format: 'cny', readonly: true, width: 100 },
  // 与商品订单页同一口径：挂了集运单就显示继承来的集运状态、整格置灰（标签仍是原色）。
  // 本页所有列都是只读的（物品要改去订单页展开面板），这里的 lock 纯粹是**视觉提示**：
  // 让人一眼看出这行的状态不是订单自己的，而是跟着集运单走的。
  // 状态：值由 #cell-purchase_status 插槽自己取（插槽优先于 GotionCell，col.display 在这里是**死代码**，
  // 所以不写 display —— 写了只会误导下一个人以为它在起作用）。
  // lock 仍然有效：它作用在 <td> 上，与插槽无关，负责把整格置灰做视觉提示。
  {
    key: 'purchase_status', label: '状态', readonly: true, width: 84,
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
const pageSize = 30
const filters = reactive({ q: '', platform: '', fulfillmentStatus: '', platform_account: '', range: null })

// 账号标签的持久化配色（与其它页同一套色序，保证同一账号处处同色）+ 账号候选（编辑弹窗下拉用）
const acctColor = reactive({})
const accountOptions = ref([])
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
let loadSeq = 0
async function load() {
  const my = ++loadSeq
  loading.value = true
  try {
    const params = { limit: pageSize, offset: (page.value - 1) * pageSize }
    if (filters.q) params.q = filters.q
    if (filters.platform) params.platform = filters.platform
    if (filters.fulfillmentStatus) params.fulfillment_status = filters.fulfillmentStatus
    if (filters.platform_account) params.platform_account = filters.platform_account
    if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
    const res = await itemsApi.list(params)
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
function reload() { page.value = 1; load() }
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
