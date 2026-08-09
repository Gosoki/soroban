<template>
  <div>
    <div class="bar">
      <span class="hint">一个账号昵称下的所有订单都放这里（一单可多物），逐单点「导入」才进入账本。（将来爬虫自动灌入）</span>
    </div>

    <el-card>
      <NotionTable :columns="columns" :rows="rows" :loading="loading" expandable
                   table-name="staging" :actions-width="128" @save="saveCell" @add="addRow" @delete="doDelete" @reload="load">
        <template #toolbar>
          <el-input v-model="filters.q" placeholder="搜物品/商品/单号/快递号" clearable style="width: 200px" @change="reload" />
          <el-select v-model="filters.platform" placeholder="来源" clearable style="width: 120px" @change="reload">
            <el-option v-for="p in ORDER_SOURCES" :key="p" :label="p" :value="p" />
          </el-select>
          <el-select v-model="filters.importStatus" placeholder="全部状态" clearable style="width: 120px" @change="reload">
            <el-option v-for="s in IMPORT_STATUS" :key="s" :label="s" :value="s" />
          </el-select>
          <el-select v-model="filters.platform_account" placeholder="账号昵称" clearable filterable style="width: 120px" @change="reload">
            <el-option v-for="a in accountOptions" :key="a" :label="a" :value="a" />
          </el-select>
          <el-date-picker v-model="filters.range" type="daterange" value-format="YYYY-MM-DD" class="flt-date"
                          start-placeholder="起" end-placeholder="止" @change="reload" />
        </template>

        <template #cell-scraped_at="{ row }">
          <span :class="row.scraped_at ? '' : 'ph'">{{ fmtDate(row.scraped_at) }}</span>
        </template>
        <template #cell-items="{ row }">
          <span :class="{ ph: !(row.items && row.items.length), 'auto-txt': allTitleItems(row) }">{{ itemSummary(row) }}</span>
        </template>
        <template #cell-import_status="{ row }">
          <el-tag :style="importStatusStyle(row.import_status)">{{ row.import_status }}</el-tag>
        </template>

        <template #expand="{ row }">
          <div class="expand">
            <div class="ex-title">物品明细（一单多物）· 单价×数量汇总为订单价</div>
            <div v-for="(it, i) in row.items" :key="i" class="item-row" :class="{ 'item-auto': isTitleItem(row, it) }"
                 :title="isTitleItem(row, it) ? '物品名与商品标题相同（无独立物品详情）；改成真实物品名即正常' : ''">
              <el-input v-model="it.name" placeholder="物品名" style="width: 180px" @change="it.auto = false" />
              <el-input-number v-model="it.quantity" :min="1" :controls="false" style="width: 80px" @change="it.auto = false" />
              <el-input-number v-model="it.unit_price_cny" :min="0" :precision="2" :controls="false"
                               style="width: 110px" placeholder="单价" @change="it.auto = false" />
              <el-button link type="danger" :icon="Delete" @click="row.items.splice(i, 1)" />
            </div>
            <div>
              <el-button :icon="Plus" @click="ensureItems(row).push({ name: '', quantity: 1, unit_price_cny: null, auto: false })">加物品</el-button>
              <el-button type="primary" @click="saveItems(row)">保存物品</el-button>
            </div>
            <div class="postage-row">
              <span class="postage-lb">邮费（元）</span>
              <el-input-number v-model="row.postage_cny" :min="0" :precision="2" :controls="false"
                               placeholder="包邮" style="width: 130px" @change="savePostage(row)" />
              <span class="postage-hint">不填 = 包邮（订单价 = Σ单价×数量 + 邮费）</span>
            </div>
          </div>
        </template>

        <template #actions="{ row }">
          <template v-if="row.imported_order_id">
            <el-tag :style="importStatusStyle('已导入')">已导入 #{{ row.imported_order_id }}</el-tag>
          </template>
          <template v-else>
            <el-button type="primary" @click="doImport(row)">导入</el-button>
            <el-button v-if="row.import_status !== '已忽略'" link @click="doIgnore(row)">忽略</el-button>
          </template>
        </template>
      </NotionTable>

      <el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                     :page-size="pageSize" :current-page="page" @current-change="onPage" />
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Plus } from '@element-plus/icons-vue'
import { applyRowUpdate } from '@/utils/orderWrites'
import { stagingApi, tagsApi } from '@/api'
import { ORDER_SOURCES, PRICE_HELP, IMPORT_STATUS, PURCHASE_STATUS, importStatusStyle } from '@/constants'
import { fmtDate } from '@/utils/datetime'
import NotionTable from '@/components/NotionTable.vue'

// 默认列顺序 + 统一列宽（≈ 刚好显示日期，取整多留一点 = 110）；用户可拖动改序/改宽，改动持久化
const COL_W = 110
const columns = [
  { key: 'order_date', label: '下单日期', type: 'date', width: COL_W },
  { key: 'platform_account', label: '账号昵称', type: 'tag', field: 'platform_account', width: COL_W },
  { key: 'platform', label: '来源', type: 'tag', field: 'platform', width: COL_W, placeholder: '来源' },
  { key: 'title', label: '商品', type: 'text', long: true, width: COL_W },   // 标题长：点开弹宽框看全
  { key: 'price_cny', label: '人民币（元）', format: 'cny', readonly: true, width: COL_W, help: PRICE_HELP },   // 由物品单价×数量派生
  { key: 'purchase_status', label: '交易状态', type: 'select', options: PURCHASE_STATUS, width: COL_W },
  { key: 'items', label: '物品', readonly: true, width: COL_W, expand: true },
  { key: 'order_no', label: '订单号', type: 'text', width: COL_W, placeholder: '订单号' },
  { key: 'express_no', label: '快递号', type: 'text', width: COL_W, placeholder: '快递号' },
  { key: 'scraped_at', label: '入库日期', readonly: true, width: COL_W },   // 写进库的日期，方便按批次筛选
  { key: 'fx_rate', label: '汇率', type: 'decimal', width: COL_W, placeholder: '当天汇率' },
  { key: 'import_status', label: '导入状态', readonly: true, width: COL_W },
]

const rows = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = 30
// 默认只看「待处理」（抓进来待逐单导入的）；清空筛选或切状态即可看全部/已导入/已忽略
const filters = reactive({ q: '', platform: '', importStatus: '待处理', platform_account: '', range: null })
const accountOptions = ref([])   // 账号昵称下拉候选（标签接口）
async function loadAccounts() {
  try { accountOptions.value = (await tagsApi.list('platform_account')).map((t) => t.value) } catch (_) { /* 已提示 */ }
}

function itemSummary(row) {
  if (!row.items || !row.items.length) return '—'
  return row.items.map((it) => `（${it.quantity}x）${it.name}`).join('，')
}
// 灰显 = 物品名与商品标题相同（无独立物品详情）；有真实物品名即正常
function isTitleItem(row, it) {
  return !!it.name && (it.name || '').trim() === (row.title || '').trim()
}
// 列表「物品」格：全是标题占位（自动生成）时整格灰显
function allTitleItems(row) {
  return !!(row.items && row.items.length) && row.items.every((it) => isTitleItem(row, it))
}
function ensureItems(row) {
  if (!row.items) row.items = []
  return row.items
}

// 请求序号：筛选/翻页可以在上一次响应回来前再发一次，慢的那次后到会把新数据整个覆盖掉
// （表现为「清了筛选却只剩一部分」「内容是第2页、页码高亮第3页」）。只认最后一次发出的请求。
let loadSeq = 0
async function load() {
  const my = ++loadSeq
  loading.value = true
  try {
    const params = { limit: pageSize, offset: (page.value - 1) * pageSize }
    if (filters.q) params.q = filters.q
    if (filters.platform) params.platform = filters.platform
    if (filters.importStatus) params.import_status = filters.importStatus
    if (filters.platform_account) params.platform_account = filters.platform_account
    if (filters.range) { params.date_from = filters.range[0]; params.date_to = filters.range[1] }
    const res = await stagingApi.list(params)
    if (my !== loadSeq) return          // 已有更新的请求发出，丢弃这次的结果
    rows.value = res.items
    total.value = res.total
  } finally {
    if (my === loadSeq) loading.value = false
  }
}
function reload() { page.value = 1; load() }
function onPage(p) { page.value = p; load() }

async function saveCell(row, key, value) {
  try {
    const patch = { version: row.version, [key]: value }
    const updated = await stagingApi.update(row.id, patch)
    applyRowUpdate(row, patch, updated)      // 没送 items → 不覆盖展开面板里未保存的物品编辑
  } catch (e) {
    if (e.response?.status === 409) {
      ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新')
      load()   // 冲突：刷新回退到服务器状态
    }
    // 非 409：拦截器已提示，单元格自动显示旧值，无需整页重拉
  }
}

async function saveItems(row) {
  const items = (row.items || []).filter((it) => it.name && it.name.trim())
    .map((it) => ({ name: it.name.trim(), quantity: Number(it.quantity) || 1,
                    unit_price_cny: (it.unit_price_cny === '' || it.unit_price_cny == null)
                      ? null : Number(it.unit_price_cny),
                    auto: !!it.auto }))
  try {
    const updated = await stagingApi.update(row.id, { version: row.version, items })
    Object.assign(row, updated)
    ElMessage.success('物品已保存')
  } catch (e) {
    // 仅 409（数据已变）才整表刷新；其它错误交拦截器提示，保留本地未保存编辑
    if (e.response?.status === 409) { ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
  }
}

// 邮费改动：写库并让暂存价随之重算（不填=包邮）。不覆盖未保存的物品编辑
async function savePostage(row) {
  const postage = (row.postage_cny === '' || row.postage_cny == null) ? null : Number(row.postage_cny)
  try {
    const patch = { version: row.version, postage_cny: postage }
    const updated = await stagingApi.update(row.id, patch)
    applyRowUpdate(row, patch, updated)
  } catch (e) {
    if (e.response?.status === 409) { ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
  }
}

async function addRow(data = {}, done) {
  try {
    const created = await stagingApi.create({ ...data })
    rows.value.unshift(created)
    total.value++
    done?.(true)
  } catch (e) {
    done?.(false)   // 失败时保留幽灵行里的草稿，让用户就地改
    // 409 被 http 拦截器刻意跳过（留给页面处理）。撞订单号唯一约束时若这里也不提示，
    // 页面就是「什么都没发生」——连幽灵行里刚敲的单号都被 commitNew 清空了。
    if (e.response?.status === 409) {
      const who = data.order_no ? `订单号「${data.order_no}」` : '该记录'
      ElMessage.warning(`${who} 已存在，未添加`)
    }
  }
}

async function doImport(row) {
  try {
    await stagingApi.import(row.id)
    ElMessage.success('已导入到商品订单账本')
    load()
  } catch (e) {
    if (e.response?.status === 409) {
      await ElMessageBox.alert(e.response?.data?.detail || '导入冲突', '导入失败', { type: 'warning' })
    }
  }
}
async function doIgnore(row) {
  try {
    await stagingApi.ignore(row.id)
    load()
  } catch (_) { /* 拦截器已提示 */ }
}
async function doDelete(row) {
  try {
    await ElMessageBox.confirm('删除这条暂存记录？', '确认', { type: 'warning' })
  } catch (_) { return }
  try {
    await stagingApi.remove(row.id)
    ElMessage.success('已删除')
    load()
  } catch (_) { /* 拦截器已提示 */ }
}

onMounted(() => { loadAccounts(); load() })
</script>

<style scoped>
.bar { margin-bottom: 10px; }
.hint { color: var(--txt-3); font-size: 12px; }
.auto-txt { color: var(--txt-3); font-style: italic; }   /* 列表「物品」格：自动生成(名=标题)时灰显 */
.expand { padding: 12px 20px; }
.ex-title { color: var(--txt-2); font-size: 13px; margin-bottom: 8px; }
.item-row { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
/* 灰显：系统自动生成/自动定价的物品（编辑即去灰） */
.item-row.item-auto :deep(.el-input__inner) { color: var(--txt-3); font-style: italic; }
.postage-row { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.postage-lb { color: var(--txt-2); font-size: 13px; }
.postage-hint { color: var(--txt-3); font-size: 12px; }
</style>
