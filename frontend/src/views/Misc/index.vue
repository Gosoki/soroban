<template>
  <div>
    <PageHeader>
    <b>杂项支出</b>：不属于商品也不属于集运的花销（工具、耗材、手续费…）。
    与另外两张表一样计入看板合计。
    </PageHeader>

    <NotionTable :columns="columns" :rows="rows" :loading="loading" table-name="misc" :empty-text="loadFailed ? '加载失败——请检查网络或后端，然后重试' : '没有符合条件的记录'"
                 @save="saveCell" @add="addRow" @delete="delRow">
      <template #toolbar>
        <el-input v-model="filters.q" placeholder="搜名称" clearable style="width: 200px" @change="reload" />
        <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                        start-placeholder="起" end-placeholder="止" @change="reload" />
      </template>
    </NotionTable>

    <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                   :page-size="pageSize" :current-page="page" @current-change="onPage" />
    </div>
</template>

<script setup>
import PageHeader from '@/components/PageHeader.vue'
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { miscApi } from '@/api'
import { handled } from '@/api/http'
import { today } from '@/utils/datetime'
import { afterCreate, afterDelete } from '@/utils/listRows'
import NotionTable from '@/components/NotionTable.vue'


const columns = [
  { key: 'date', label: '日期', type: 'date', width: 140, clearable: false },
  { key: 'name', label: '名称', type: 'text', minWidth: 150, placeholder: '名称', clearable: false },
  { key: 'category', label: '分类', type: 'text', width: 120 },
  { key: 'price_cny', label: '人民币（元）', type: 'decimal', format: 'cny', width: 110, placeholder: '实付人民币' },
  { key: 'fx_rate', label: '汇率', type: 'decimal', width: 90, placeholder: '当天汇率' },
  { key: 'jpy_override', label: '覆盖（円）', type: 'int', format: 'jpy', width: 110, placeholder: '实付日元' },
  { key: 'jpy_settled', label: '结算（円）', format: 'jpy', readonly: true, width: 120 },
]

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 30
const filters = reactive({ range: null, q: '' })

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
    if (filters.q) params.q = filters.q
    const res = await miscApi.list(params)
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
    const updated = await miscApi.update(row.id, { version: row.version, [key]: value })
    Object.assign(row, updated)
  } catch (e) {
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
  }
}

async function addRow(data = {}, done) {
  try {
    const created = await miscApi.create({ date: today(), name: '', ...data })
    await afterCreate(created, { rows, total, page, filters, load })
    done?.(true)
  } catch (_) {
    done?.(false)   // 失败时保留幽灵行里的草稿，让用户就地改，别把刚敲的内容一起吞掉
  }
}

async function delRow(row) {
  try {
    await ElMessageBox.confirm(`删除杂项 ${row.name || row.id}？`, '确认', { type: 'warning' })
  } catch (_) { return }
  try {
    await miscApi.remove(row.id)
    ElMessage.success('已删除')
    await afterDelete({ rows, page, load })
  } catch (_) { /* 拦截器已提示 */ }
}

onMounted(load)
</script>

<style scoped>
</style>
