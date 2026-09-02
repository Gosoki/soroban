<template>
  <div>
    <PageHeader>
    账本里的<b>商品订单</b>。一单可含多件物品，订单价 = Σ(单价×数量) + 邮费，由物品派生。
    已导入的暂存单也落在这里。<b>截图 OCR 录单在「暂存」页</b>——这一页没有接拖拽，
    往这儿拖图会被浏览器当成打开文件，整个页面被顶掉、没保存的编辑一起没。
    <!-- 「按浏览器记住」是假话：`ColumnLayout` 以表名为唯一主键，**8 个人共用同一行**，
         存在后端。说成浏览器本地的话，甲发现自己的列序被改了会去清缓存、换浏览器找原因，
         而真实原因是乙拖了一下；反过来乙也会以为「我这台机器上怎么摆是我自己的事」。 -->
    列可拖动换位/拖宽，<b>列序与列宽是大家共用的一份</b>（存在服务端，不是你这台机器上的）。
    </PageHeader>

    <NotionTable :columns="columns" :rows="rows" :loading="loading" :load-failed="loadFailed" expandable hide-id :open-id="focusId"
                 table-name="orders" :empty-text="loadFailed ? MSG_LOAD_FAILED : '没有符合条件的记录'" @save="saveCell" @add="addRow" @delete="delRow" @reload="load" @tags-changed="onTagsChanged">
      <template #toolbar>
        <el-input v-model="filters.q" :placeholder="MSG_SEARCH_ORDER_LIKE" clearable style="width: 200px" @change="applyFilters" />
        <el-select v-model="filters.platform" placeholder="来源" clearable style="width: 120px" @change="applyFilters">
          <el-option v-for="p in ORDER_SOURCES" :key="p" :label="p" :value="p" />
        </el-select>
        <!-- 选项 = 国内段 + 集运段：列表显示的是继承后的状态，只列国内段的话，
             界面上一堆「已发出」却在筛选框里选不到它 -->
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
        <el-select v-model="filters.recipient" placeholder="收货人" clearable filterable style="width: 120px" @change="applyFilters">
          <el-option v-for="r in recipientOptions" :key="r" :label="r" :value="r" />
        </el-select>
        <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                        start-placeholder="起" end-placeholder="止" @change="applyFilters" />
        <!-- 「还没装箱的有哪些」是发货前最常问的一句。后端早就支持 unassigned，
             只是界面上没有入口——用 checkbox 而不是再加一个下拉：它是二选一的开关，
             塞进「状态」那种多选下拉里会和真正的状态值混在一起。 -->
        <el-checkbox v-model="filters.unassigned" @change="applyFilters">仅未挂靠</el-checkbox>
        <el-tag v-if="focusId" :style="typeStyle('warning')" closable disable-transitions class="focus-chip" @close="clearFocus">
          定位订单 #{{ focusId }} · 点 × 看全部
        </el-tag>
      </template>
      <template #toolbar-right>
        <!-- 导出当前筛选的全部行（不是这一页）。放 toolbar-right 与集运页的 OCR 按钮同侧：
             它们都是「对整张表做一件事」，而左边那排是筛选。 -->
        <el-button :loading="exporting" @click="doExport">导出 CSV</el-button>
      </template>


      <template #cell-shipment_order_id="{ row }">
        <el-select :model-value="row.shipment_order_id" filterable placeholder="未集运" class="ship-pick" popper-class="ship-pop"
                   :persistent="false"
                   remote :remote-method="searchShipment" :loading="shipSearching"
                   remote-show-suffix reserve-keyword
                   no-data-text="没有匹配的集运单"
                   @visible-change="onShipDropdown"
                   @change="(v) => onPickShipment(row, v)">
          <template #label="{ value }">
            <span class="ship-sel">
              <b>{{ shipNo(value, row) }}</b>
              <el-tag v-if="row.fulfillment_status && row.shipment_order_id"
                      :style="statusStyle(row.fulfillment_status)">{{ row.fulfillment_status }}</el-tag>
            </span>
          </template>
          <!-- 清除固定在列表最上（集运单可能很多）；无归属时不显示。
               搜索态下也不显示：它不是命中结果，留着会顶住 options.size，让「没有匹配」提示出不来 -->
          <el-option v-if="row.shipment_order_id && !shipHits" :value="-1" label="清除">
            <div class="ship-clear">清除（取消集运）</div>
          </el-option>
          <el-option v-for="j in sortedShipments" :key="j.id" :label="j.shipment_no || ('#' + j.id)" :value="j.id">
            <div class="ship-opt">
              <div class="ship-opt-top">
                <b>{{ j.shipment_no || ('#' + j.id) }}</b>
                <el-tag :style="statusStyle(j.shipment_status)">{{ j.shipment_status }}</el-tag>
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

      <template #footer>
        <TableFooterSum :total="total" :sum-jpy="sumJpy" :unconverted="unconverted" />
      </template>
    </NotionTable>

    <div v-if="focusId && !loading && !total" class="focus-empty">
      未找到该订单（可能已删除）。<el-link type="primary" :underline="false" @click="clearFocus">显示全部</el-link>
    </div>

    <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                   :page-size="pageSize" :current-page="page" @current-change="onPage" />

  </div>
</template>

<script setup>
import { outcomeIsUnknown } from '@/api/retry'
import PageHeader from '@/components/PageHeader.vue'
import TableFooterSum from '@/components/TableFooterSum.vue'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { exportCsv } from '@/utils/exportCsv'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check } from '@element-plus/icons-vue'
import { shipmentApi, ordersApi, tagsApi } from '@/api'
import { handled } from '@/api/http'
import { MSG_FILTER_CLEARED, MSG_LOAD_FAILED, MSG_NOTHING_TO_EXPORT, MSG_SEARCH_ORDER_LIKE, MSG_STALE_RELOADED, ORDER_SOURCES, PAGE_SIZE, PRICE_HELP, PURCHASE_STATUS, SHIPMENT_STATUS, statusStyle, typeStyle } from '@/constants'
import { fmtJPY } from '@/utils/money'
import { applyRowUpdate, queueRowWrite } from '@/utils/rowWrites'
import { today } from '@/utils/datetime'
import { afterCreate, afterDelete } from '@/utils/listRows'
import NotionTable from '@/components/NotionTable.vue'
import OrderItemsEditor from '@/components/OrderItemsEditor.vue'


// 默认列顺序 + 统一列宽（≈ 刚好显示日期，取整多留一点 = 110）；用户可拖动改序/改宽，改动持久化
const COL_W = 110
const columns = [
  { key: 'date', label: '下单日期', type: 'date', width: COL_W, clearable: false },
  { key: 'platform_account', label: '账号昵称', type: 'tag', field: 'platform_account', width: COL_W },
  { key: 'platform', label: '来源', type: 'tag', field: 'platform', width: COL_W, placeholder: '来源' },
  { key: 'title', label: '商品', type: 'text', long: true, width: COL_W },
  { key: 'items', label: '物品', readonly: true, width: COL_W, expand: true,
    // 数组列：cell() 的数组分支产出的正是「（2x）名称」，与 itemSummary 同形；空数组导出空串比屏幕上那个「—」更适合 CSV
    exportRaw: true },
  // 状态：点标签就能选（和其它标签列一致）。挂着集运单时**按行锁定**——显示继承来的集运状态、
  // 整格置灰不可点，但标签本身保持原色；释放后自动恢复可选。
  // display 只影响显示，写回仍走 key='status'（订单自己的国内段状态）。
  {
    key: 'purchase_status', label: '状态', type: 'select', options: PURCHASE_STATUS, width: COL_W, clearable: false,
    display: (row) => row.fulfillment_status ?? row.purchase_status,
    lock: (row) => !!row.shipment_order_id,
    lockHint: '跟随所挂集运订单的状态；从集运单里释放后可改',
  },
  // display 只给导出用（屏幕上这一列走 #cell-shipment_order_id 插槽，插槽优先）。
  // 不写的话导出的是数据库自增 id：屏幕上「SP-777」，文件里「1」。
  {
    key: 'shipment_order_id', label: '集运订单', readonly: true, width: COL_W, placeholder: '选择',
    display: (row) => row.shipment_no || (row.shipment_order_id ? '#' + row.shipment_order_id : ''),
  },
  { key: 'jpy_settled', label: '结算（円）', format: 'jpy', readonly: true, width: COL_W },
  { key: 'jpy_override', label: '覆盖（円）', type: 'int', format: 'jpy', width: COL_W, placeholder: '实付日元' },
  // help：口径 + 「为什么和淘宝实付差几分」，表头点「?」可见（见 docs/README.md 第五十三版）
  // 数据行只读（金额由物品单价×数量派生，改价走展开面板）；
  // **新建行可填**——新建一单时总得有个地方把金额填进去，后端会把它折成一条物品（见 build_items）。
  {
    key: 'price_cny', label: '人民币（元）', format: 'cny', readonly: true, newEditable: true,
    type: 'decimal', placeholder: '货款', width: COL_W, help: PRICE_HELP,
  },
  { key: 'fx_rate', label: '汇率', type: 'decimal', width: COL_W, placeholder: '当天汇率' },
  { key: 'express_company', label: '快递公司', type: 'text', width: COL_W, placeholder: '快递公司' },
  { key: 'express_no', label: '快递号', type: 'text', width: COL_W, placeholder: '快递号' },
  { key: 'order_no', label: '订单号', type: 'text', width: COL_W, placeholder: '订单号' },
  // 下面四列的数据**一直都在**（每次响应都在回），只是列数组里没有它们：
  //   · `url`          —— 爬虫一直在灌商品链接，想看却要绕到物品页开编辑面板；
  //   · `jpy_auto`     —— 按汇率算出来的日元。它与 `jpy_settled` 并列才看得出
  //                       「这一行的钱被手工覆盖过」；只显示结算额时，覆盖过的行
  //                       和正常折算的行长得一模一样；
  //   · `override_note` —— 填了覆盖值却不说为什么，过三个月自己也想不起来；
  //   · `note`         —— `LedgerBase` 上的备注列。
  { key: 'url', label: '商品链接', type: 'text', long: true, width: COL_W, placeholder: '商品链接' },
  { key: 'jpy_auto', label: '折算（円）', format: 'jpy', readonly: true, width: COL_W },
  { key: 'override_note', label: '覆盖原因', type: 'text', long: true, width: COL_W },
  { key: 'note', label: '备注', type: 'text', long: true, minWidth: 160 },
]

const rows = ref([])
const total = ref(0)
// 页脚合计。**服务端算的、覆盖当前筛选的全部行**，不是屏幕上这一页——
// 前端自己把当页加一遍的话，翻页时这个数会跟着变，等于没有。
const sumJpy = ref(null)
const unconverted = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = PAGE_SIZE
const focusId = ref(null)   // 跳转定位的订单 id（?focus=）
const filters = reactive({ q: '', platform: '', fulfillmentStatus: '', platform_account: '',
                           recipient: '', unassigned: false, range: null })
const shipmentOptions = ref([])
const accountOptions = ref([])   // 账号昵称下拉候选（标签接口）
// 收货人在**集运表**上，不在订单上；后端按子查询筛。候选走同一套标签接口。
const recipientOptions = ref([])
// 表格里改了标签（改名/新增/删除/改色）之后，工具栏那份候选集要跟着变。
// **还要管仍停在旧名的筛选值**：改名时旧名在库里已经不存在了，
// 拿它精确匹配会查回 0 行，空态显示「没有符合条件的记录」——
// 用户刚改完名就看到「单子没了」。清掉筛选比留着一个查不到东西的值好，
// 而且要说一句，否则他会以为筛选自己乱了。
function onTagsChanged({ field, values }) {
  // 下拉候选：谁有对应的 ref 就更新谁
  if (field === 'recipient') recipientOptions.value = values
  if (field === 'platform_account') accountOptions.value = values

  // **通用地清掉停在旧值上的筛选。** 标签改名之后库里再没有旧值，
  // 拿它精确匹配会查回 0 行，空态显示「没有符合条件的记录」——
  // 用户刚改完名就看到「单子没了」，而且他多半不会想到去点筛选框的 ✕。
  //
  // 原先是**按字段逐个 if**（只处理了 recipient 与 platform_account），
  // 于是「来源(platform)」这个同样是标签列、同样有筛选框的字段一直漏在外面。
  // 按字段枚举正是它被漏掉的原因，所以改成看 `filters` 上有没有同名键——
  // 将来再接一个标签字段进来，这里不用改。
  if (filters[field] && !values.includes(filters[field])) {
    filters[field] = ''
    ElMessage.info(MSG_FILTER_CLEARED)
    applyFilters()
  }
}

async function loadAccounts() {
  try { accountOptions.value = (await tagsApi.list('platform_account')).map((t) => t.value) } catch (_) { /* 已提示 */ }
  try { recipientOptions.value = (await tagsApi.list('recipient')).map((t) => t.value) } catch (_) { /* 已提示 */ }
}

// null = 当前不在搜索态；数组 = 本次搜索的命中（**可能是空数组 = 零命中**）。
// 不能用 `.length` 当搜索态判据：零命中同样是空数组，会静默回退成默认那批，
// 表现为「搜不到 → 把全部集运单当成命中结果摆出来」，而 remote 模式下 el-select
// 不做本地过滤（父组件给什么就渲染什么），no-data-text 也永远不会出现。
const shipHits = ref(null)
const shipSearching = ref(false)

function shipById(id) {
  return shipmentOptions.value.find((j) => j.id === id) || (shipHits.value || []).find((j) => j.id === id)
}
// 显示优先用订单行自带的 shipment_no：下拉只有前 200 张，挂在那之外的单曾显示成 `#101`。
// 只有在拿不到行（比如 #label 插槽只给了 value）时才回落查下拉。
function shipNo(id, row) {
  if (row?.shipment_no) return row.shipment_no
  const j = shipById(id)
  return j?.shipment_no || ('#' + id)
}
// 打包中的集运单永远置顶（最常挂新订单）；其余保持原顺序（日期倒序）。sort 稳定，同组不乱。
// 搜索态下展示命中结果，否则展示默认那批。
const sortedShipments = computed(() => {
  const base = shipHits.value ?? shipmentOptions.value
  return [...base].sort((a, b) => (b.shipment_status === '打包中' ? 1 : 0) - (a.shipment_status === '打包中' ? 1 : 0))
})
// 远程搜索：默认只拉前 200 张，更早的集运单靠输入单号现查。
// 清空输入即回到默认那批（remote 模式下 el-select 会用空串回调一次）。
let shipSeq = 0
async function searchShipment(kw) {
  const q = (kw || '').trim()
  if (!q) { shipHits.value = null; shipSearching.value = false; return }
  const my = ++shipSeq
  shipSearching.value = true
  try {
    const res = await shipmentApi.list({ q, limit: 50, brief: true })
    if (my === shipSeq) shipHits.value = res.items       // 迟到的响应不覆盖新结果
  } catch (_) { /* 拦截器已提示 */ } finally {
    if (my === shipSeq) shipSearching.value = false
  }
}
// 关闭下拉时丢掉搜索结果，下次打开回到默认那批。
// 不清的话，上一行搜过的窄结果会带到下一行，看起来像「集运单丢了」。
function onShipDropdown(visible) {
  if (!visible) { shipHits.value = null; shipSeq++ }     // seq 前进：在途响应作废
}

function onPickShipment(row, v) {   // -1 = 列表里的「清除」项；其余为集运单 id
  saveCell(row, 'shipment_order_id', v === -1 ? null : (v ?? null))
}

function itemSummary(row) {
  if (!row.items || !row.items.length) return '—'
  return row.items.map((it) => `（${it.quantity}x）${it.name}`).join('，')
}
// 灰显 = 物品名与商品标题相同（无独立物品详情，多为自动占位）；有真实物品名即正常
function isTitleItem(row, it) {
  return !!it.name && (it.name || '').trim() === (row.title || '').trim()
}
// 列表「物品」格：全是标题占位（自动生成）时整格灰显
function allTitleItems(row) {
  return !!(row.items && row.items.length) && row.items.every((it) => isTitleItem(row, it))
}

// 请求序号：筛选/翻页可以在上一次响应回来前再发一次，慢的那次后到会把新数据整个覆盖掉
// （表现为「清了筛选却只剩一部分」「内容是第2页、页码高亮第3页」）。只认最后一次发出的请求。
let loadSeq = 0
// 上一次加载是否失败：空态文案据此说实话。
// 「请求挂了」与「真的没有记录」渲染成同一句「没有符合条件的记录」，是这个项目反复栽的那类
// ——拦截器那句 toast 三秒就没了，此后这一屏与「真的还没记过账」完全无法区分。
const loadFailed = ref(false)
const exporting = ref(false)

// 导出当前筛选的**全部行**（不是这一页），列以页面正在显示的那份列配置为准。
// 两条口径都在 utils/exportCsv.js 里说明了理由——反过来做会产出
// 「看起来对、其实少了东西」的文件，而这种文件往往会被当成完整账目发给别人。
async function doExport() {
  exporting.value = true
  try {
    const n = await exportCsv({
      fetchPage: (limit, offset) => ordersApi.list({ ...filterParams(), limit, offset }),
      columns,
      name: 'orders',
    })
    if (!n) ElMessage.info(MSG_NOTHING_TO_EXPORT)
    else ElMessage.success(`已导出 ${n} 条商品订单`)
  } catch (_) { /* 拦截器已提示 */ } finally { exporting.value = false }
}

// 当前筛选 → 查询参数。**列表与导出共用这一份**：各写一份的话，
// 导出的 CSV 会和屏幕上看到的不是同一批行，而这种文件往往是要发给别人的。
function filterParams() {
  const params = {}
  if (filters.q) params.q = filters.q
  if (filters.platform) params.platform = filters.platform
  if (filters.fulfillmentStatus) params.fulfillment_status = filters.fulfillmentStatus
  if (filters.platform_account) params.platform_account = filters.platform_account
  if (filters.recipient) params.recipient = filters.recipient
  if (filters.unassigned) params.unassigned = true
  if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
  if (focusId.value) params.id = focusId.value          // 跳转定位：隔离显示该单
  return params
}

async function load() {
  const my = ++loadSeq
  loading.value = true
  try {
    const res = await ordersApi.list({ ...filterParams(), limit: pageSize,
                                      offset: (page.value - 1) * pageSize })
    if (my !== loadSeq) return          // 已有更新的请求发出，丢弃这次的结果
    rows.value = res.items
    total.value = res.total
    sumJpy.value = res.sum_jpy
    unconverted.value = res.unconverted
    loadFailed.value = false
  } catch (_) {
    // 拦截器已提示原因；这里负责让**页面本身**留下痕迹，否则空态在说假话。
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

async function loadShipment() {
  // 它在 onMounted 里被调一次、且只调这一次。不接 catch 的话失败就是一个**未捕获的
  // promise 拒绝**：`shipmentOptions` 永远停在 []，「所属集运」下拉从此空着，
  // 而这一页没有任何重取路径（`load()` 的 loadFailed 只覆盖列表本身，不管它）。
  // Items 和 Shipment 两页的同一个函数都接了，其中一处的注释写的正是
  // 「避免 onMounted 里未捕获的 promise 拒绝」——三处同一件事，这里是唯一漏掉的。
  try {
    shipmentOptions.value = (await shipmentApi.list({ limit: 200, brief: true })).items
  } catch (_) { /* 拦截器已提示 */ }
}

async function saveCell(row, key, value) {
  try {
    // 入队串行：格子保存与展开面板/物品编辑器对同一订单的写不再并发撞 version
    await queueRowWrite(`order:${row.id}`, async () => {
      const patch = { version: row.version, [key]: value }
      const updated = await ordersApi.update(row.id, patch)
      // 本次没送 items → 不覆盖：展开面板里可能有尚未点「保存物品」的编辑（见 applyRowUpdate）
      applyRowUpdate(row, patch, updated)
    })
  } catch (e) {
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || MSG_STALE_RELOADED); load() }
  }
}

// OCR 识别订单截图：抽取快递公司/快递号/订单号/成交价，识别到就新建一行并回填。
async function addRow(data = {}, done) {
  try {
    // status 不写死：后端 OrderBase 默认「待发货」，避免枚举改名后前端残留非法值（曾用'已付'→422）
    const created = await ordersApi.create({ date: today(), ...data })
    // 处于 ?focus= 隔离视图时，新建的单**不属于**这个过滤条件。
    // 直接 unshift 进去的话：列表里混进一行不该在这儿的记录、total 从 1 变 2，
    // 而刷新一下它又凭空消失——用户会以为刚才那单没存上。
    // 原先只有 OCR 那条路径清了隔离态，幽灵行手工新建这条漏了；
    // 放进 addRow 里，两条路径（以及以后任何新入口）自动都对。
    // **这是「保留筛选」那条统一规则的唯一例外，而且应当保持例外。**
    // 四页统一的做法是：保留用户设的筛选 + 重拉 + 确认新记录确实不在列表里才提示。
    // 那条规则的前提是「筛选是用户精心设的，清掉不可撤销」。
    // `?focus=` 不满足这个前提：它不是用户设的筛选，是从别处跳过来的**一次性定位**
    // （URL 参数，界面上有一个「点 × 看全部」的标签明说它是临时的），
    // 而且它按 id 过滤 —— 新建的单**永远**不可能满足它，走统一规则只能得到
    // 一条「新单不在当前筛选内」的提示，然后用户还得自己点 ×。
    // 所以这里清掉定位、回到全量列表。改成走统一规则会让这一页比别的页更难用。
    if (focusId.value) {
      clearFocus()             // 会触发 load()，新单自然出现在全量列表里
      done?.(true)
      return created
    }
    await afterCreate(created, { rows, total, page, filters, load, pageSize, sumJpy, unconverted })
    done?.(true)
    return created
  } catch (e) {
    // 超时/断网 = **结果未知**（请求已经发出去了，可能已经落库）。
    // 交给 NotionTable 说那句正确的话：「先别重复提交——刷新看看是不是已经存上了」。
    // 草稿照旧留着：万一真没存上，用户不用重敲。
    done?.(false, outcomeIsUnknown(e))
    // 409 被 http 拦截器刻意跳过（留给页面处理）→ 这里对「订单号+来源」重复给明确提示
    if (e.response?.status === 409) {
      handled(e)
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
    await ElMessageBox.confirm(`删除订单「${row.order_no || row.id}」？`, '删除订单',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch (_) { return }
  try {
    await ordersApi.remove(row.id)
    ElMessage.success('已删除')
    await afterDelete({ rows, page, load })
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
})
</script>

<style scoped>
/* OCR 上传：工具栏里的点选按钮（拖拽走整窗覆盖层，这里只负责点击选图）。 */


.focus-chip { font-weight: 500; }
.focus-empty { color: var(--txt-2); font-size: 13px; padding: 16px; text-align: center; }
.auto-txt { color: var(--txt-3); font-style: italic; }   /* 列表「物品」格：自动生成(名=标题)时灰显 */
/* 集运点选：内嵌无边框，像格子里的选择 */
.ship-pick { width: 100%; }
.ship-pick :deep(.el-select__wrapper),
.ship-pick :deep(.el-input__wrapper) { box-shadow: none !important; background: transparent; }
/* 隐藏下拉箭头/清除叉，避免误触；清除改放到下拉列表里 */
.ship-pick :deep(.el-select__suffix) { display: none; }
.ship-clear { color: var(--txt-2); font-size: 12px; }
.ship-opt { display: flex; flex-direction: column; gap: 3px; line-height: 1.3; }
.ship-opt-top { display: flex; align-items: center; gap: 8px; }
.ship-meta { color: var(--txt-3); font-size: 11px; }
.ship-ck { margin-left: auto; color: var(--ok); font-size: 14px; }
/* 单元格里显示所选集运单：单号 + 状态标签 */
.ship-sel { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.ship-sel b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
