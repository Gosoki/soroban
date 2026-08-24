<template>
  <div>
    <PageHeader>
    <b>杂项支出</b>：不属于商品也不属于集运的花销（工具、耗材、手续费…）。
    与另外两张表一样计入看板合计。
    </PageHeader>

    <NotionTable :columns="columns" :rows="rows" :loading="loading" :load-failed="loadFailed" table-name="misc" :empty-text="loadFailed ? MSG_LOAD_FAILED : '没有符合条件的记录'"
                 @save="saveCell" @add="addRow" @delete="delRow" @reload="load" @tags-changed="onTagsChanged">
      <template #toolbar>
        <el-input v-model="filters.q" placeholder="搜名称" clearable style="width: 200px" @change="applyFilters" />
        <el-select v-model="filters.category" placeholder="分类" clearable filterable style="width: 120px" @change="applyFilters">
          <el-option v-for="c in categoryOptions" :key="c" :label="c" :value="c" />
        </el-select>
        <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                        start-placeholder="起" end-placeholder="止" @change="applyFilters" />
      </template>
      <template #toolbar-right>
        <!-- 导出当前筛选的全部行（不是这一页）。放 toolbar-right 与集运页的 OCR 按钮同侧：
             它们都是「对整张表做一件事」，而左边那排是筛选。 -->
        <el-button :loading="exporting" @click="doExport">导出 CSV</el-button>
      </template>
      <template #footer>
        <TableFooterSum :total="total" :sum-jpy="sumJpy" :unconverted="unconverted" />
      </template>
    </NotionTable>

    <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                   :page-size="pageSize" :current-page="page" @current-change="onPage" />
    </div>
</template>

<script setup>
import { outcomeIsUnknown } from '@/api/retry'
import PageHeader from '@/components/PageHeader.vue'
import TableFooterSum from '@/components/TableFooterSum.vue'
import { onMounted, reactive, ref } from 'vue'
import { exportCsv } from '@/utils/exportCsv'
import { queueRowWrite } from '@/utils/rowWrites'
import { ElMessage, ElMessageBox } from 'element-plus'
import { miscApi, tagsApi } from '@/api'
import { handled } from '@/api/http'
import { today } from '@/utils/datetime'
import { MSG_FILTER_CLEARED, MSG_LOAD_FAILED, MSG_NOTHING_TO_EXPORT, MSG_STALE_RELOADED, PAGE_SIZE } from '@/constants'
import { afterCreate, afterDelete } from '@/utils/listRows'
import NotionTable from '@/components/NotionTable.vue'


const columns = [
  { key: 'date', label: '日期', type: 'date', width: 140, clearable: false },
  { key: 'name', label: '名称', type: 'text', minWidth: 150, placeholder: '名称', clearable: false },
  // 标签列而不是纯文本：别处的分类列都点一下就能选，只有这里要手打，
  // 于是库里会同时存在「手续费」和「手续费 」两个值，筛选也就永远对不齐。
  { key: 'category', label: '分类', type: 'tag', field: 'category', width: 120 },
  { key: 'price_cny', label: '人民币（元）', type: 'decimal', format: 'cny', width: 110, placeholder: '实付人民币' },
  { key: 'fx_rate', label: '汇率', type: 'decimal', width: 90, placeholder: '当天汇率' },
  { key: 'jpy_override', label: '覆盖（円）', type: 'int', format: 'jpy', width: 110, placeholder: '实付日元' },
  // `jpy_auto` = 按汇率算出来的日元；`jpy_settled` = 覆盖值优先的最终结算额。
  // 两者并列才看得出「这一行的钱是被手工覆盖过的」——只显示结算额时，
  // 一个填了覆盖值的行和一个正常折算的行长得一模一样。只读，不参与编辑。
  { key: 'jpy_auto', label: '折算（円）', format: 'jpy', readonly: true, width: 110 },
  { key: 'jpy_settled', label: '结算（円）', format: 'jpy', readonly: true, width: 120 },
  // 覆盖原因：填了 `jpy_override` 却不说为什么，过三个月自己也想不起来。
  { key: 'override_note', label: '覆盖原因', type: 'text', long: true, minWidth: 140 },
  // 备注：`note` 一直在 `LedgerBase` 上、每次响应都在回，只是前端列里没有它。
  // 库里就存着「日本空运-广东直飞EMS」这样的值——渠道没有蒸发，是没人显示。
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
const filters = reactive({ range: null, q: '', category: '' })
const categoryOptions = ref([])   // 分类下拉候选（标签接口，与其它页的标签列同一套）

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
      fetchPage: (limit, offset) => miscApi.list({ ...filterParams(), limit, offset }),
      columns,
      name: 'misc',
    })
    if (!n) ElMessage.info(MSG_NOTHING_TO_EXPORT)
    else ElMessage.success(`已导出 ${n} 条杂项支出`)
  } catch (_) { /* 拦截器已提示 */ } finally { exporting.value = false }
}

// 当前筛选 → 查询参数。**列表与导出共用这一份**（理由见 Orders 页同名函数）。
function filterParams() {
  const params = {}
  if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
  if (filters.q) params.q = filters.q
  if (filters.category) params.category = filters.category
  return params
}

async function load() {
  const my = ++loadSeq
  loading.value = true
  try {
    const res = await miscApi.list({ ...filterParams(), limit: pageSize,
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

async function saveCell(row, key, value) {
  try {
    // 入队串行：连改同一行的两个格子时，第二次会在第一次回写 version 之后才发出。
    // 不串的话两次都带着同一个旧 version 出去，后一次必 409 →「数据已变，已刷新」→
    // 用户刚敲的那一格被悄悄丢掉。订单页与暂存页早就这么做了，这两页原先漏了。
    await queueRowWrite(`misc:${row.id}`, async () => {
      const updated = await miscApi.update(row.id, { version: row.version, [key]: value })
      Object.assign(row, updated)
    })
  } catch (e) {
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || MSG_STALE_RELOADED); load() }
  }
}

async function addRow(data = {}, done) {
  try {
    const created = await miscApi.create({ date: today(), name: '', ...data })
    await afterCreate(created, { rows, total, page, filters, load, pageSize })
    done?.(true)
  } catch (e) {
    // 超时/断网 = **结果未知**（请求已经发出去了，可能已经落库）。
    // 交给 NotionTable 说那句正确的话：「先别重复提交——刷新看看是不是已经存上了」。
    // 草稿照旧留着：万一真没存上，用户不用重敲。
    done?.(false, outcomeIsUnknown(e))
  }
}

async function delRow(row) {
  try {
    await ElMessageBox.confirm(`删除杂项「${row.name || row.id}」？`, '删除杂项支出',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
  } catch (_) { return }
  try {
    await miscApi.remove(row.id)
    ElMessage.success('已删除')
    await afterDelete({ rows, page, load })
  } catch (_) { /* 拦截器已提示 */ }
}

// 与订单页/集运页同一套：标签改名后清掉停在旧名的筛选值，并说一句。
function onTagsChanged({ field, values }) {
  if (field === 'category') categoryOptions.value = values

  // **通用地清掉停在旧值上的筛选**（口径与订单页/暂存页/物品页逐字相同）：
  // 标签改名后库里再没有旧值，拿它精确匹配会查回 0 行，
  // 空态显示「没有符合条件的记录」——用户刚改完名就看到「东西没了」。
  // 写成通用的而不是按字段 if：按字段枚举正是「来源(platform)」被漏掉四轮的原因。
  if (filters[field] && !values.includes(filters[field])) {
    filters[field] = ''
    ElMessage.info(MSG_FILTER_CLEARED)
    applyFilters()
  }
}

async function loadCategories() {
  try { categoryOptions.value = (await tagsApi.list('category')).map((t) => t.value) } catch (_) { /* 已提示 */ }
}

onMounted(() => { load(); loadCategories() })
</script>

<style scoped>
</style>
