<template>
  <div>
    <PageHeader>
    一个账号昵称下的所有订单都放这里（一单可多物），逐单点「导入」才进入账本。（将来爬虫自动灌入）
    </PageHeader>

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

      <template #toolbar-right>
        <OcrButton ref="ocrUpload" :pending="ocrPending" @pick="onOcrPick">
          点「OCR」选图，或把图<b>拖到页面任意位置</b>松手（支持一次多张）。
          识别到的单<b>先落在这里</b>，不直接进账本——跟爬虫抓回来的走同一条路，
          你在这张表上核对/改完，再逐单点「导入」。
          <br>解析规则是照<b>闲鱼</b>的版式写的：淘宝/京东的截图也能建，
          但成交价、商品名多半认不出来（它们靠「成交价」这个闲鱼独有的标签定位），
          需要你在这里补。认不出是哪个平台时，<b>来源会留空</b>，不会瞎猜一个。
        </OcrButton>
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
import PageHeader from '@/components/PageHeader.vue'
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Camera, Delete, Plus } from '@element-plus/icons-vue'
import { applyRowUpdate } from '@/utils/orderWrites'
import { ordersApi, stagingApi, tagsApi } from '@/api'
import { handled } from '@/api/http'
import { ORDER_SOURCES, PRICE_HELP, IMPORT_STATUS, PURCHASE_STATUS, canAdvancePurchase, importStatusStyle } from '@/constants'
import { fmtDate } from '@/utils/datetime'
import { afterCreate, afterDelete } from '@/utils/listRows'
import NotionTable from '@/components/NotionTable.vue'
import OcrButton from '@/components/OcrButton.vue'
import { checkImageSize } from '@/utils/imageGate'
import { useWindowFileDrop } from '@/utils/windowFileDrop'

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
  // 有这一列才谈得上「在暂存里核对完再导入」：OCR 的快递公司是按坐标就近猜的，
  // 认错很常见。没有这一列时它会静默落库、导入后才在订单页第一次露面——
  // 而那时已经过了唯一一道人工确认关卡，反过来也没法在这儿手填。
  { key: 'express_company', label: '快递公司', type: 'text', width: COL_W, placeholder: '快递公司' },
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
      handled(e)
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
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
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
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '数据已变，已刷新'); load() }
  }
}

async function addRow(data = {}, done) {
  try {
    const created = await stagingApi.create({ ...data })
    await afterCreate(created, { rows, total, page, filters, load, dateKey: 'order_date' })
    done?.(true)
    // **必须把新建的行回传**：OCR 那条路径靠返回值判成败（`if (!created) 记失败`）。
    // 不回传的话建成功了也拿到 undefined，整批统计会把每一张都记成「失败」，
    // 而行其实好好地建出来了——数字和事实相反，比不报还糟。
    return created
  } catch (e) {
    done?.(false)   // 失败时保留幽灵行里的草稿，让用户就地改
    // 409 被 http 拦截器刻意跳过（留给页面处理）。撞订单号唯一约束时若这里也不提示，
    // 页面就是「什么都没发生」——连幽灵行里刚敲的单号都被 commitNew 清空了。
    if (e.response?.status === 409) {
      handled(e)
      const who = data.order_no ? `订单号「${data.order_no}」` : '该记录'
      ElMessage.warning(`${who} 已存在，未添加`)
    }
    return null
  }
}

async function doImport(row) {
  try {
    await stagingApi.import(row.id)
    ElMessage.success('已导入到商品订单账本')
    load()
  } catch (e) {
    if (e.response?.status === 409) {
      handled(e)
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
    await afterDelete({ rows, page, load })
  } catch (_) { /* 拦截器已提示 */ }
}

// —— OCR：识别结果**先落暂存**，与爬虫抓回来的走同一条路 ——————————————
// 为什么不直接进账本（原先就是）：OCR 是两个生产者里更不可靠的那个——
// 来源可能标错、成交价在非闲鱼版式上根本认不出来。而爬虫抓的都要人工确认才进账本
// （插件权限里刻意不给 staging:promote 就是为了让那句话是真的）。
// 更不可靠的那个反而直接写正式账本，是说不通的。现在两者同一条路：先落暂存，人点导入。
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
// 整窗拖图。**必须用共享的那个**：这套东西有五个部分（判据/四个处理函数/
// dragover 的 preventDefault/注册/反注册），少任何一个都是静默故障——
// 这一页就漏过「注册」，表现是拖图进来浏览器直接把 SPA 换成那张图片。
const { dragActive } = useWindowFileDrop(enqueueOcr)

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

// 这一批的统计。**按批而不是按张**：一次拖十几张图，每张弹一条 toast 会刷屏，
// 而真正要回答的问题只有一个——「这十几张里，几张真进去了、几张早就有了」。
// `wrongPlatform` 记的是「本批是否已经就非闲鱼截图问过一次」：
// null=还没问，true=用户说继续，false=用户说算了（后续同类一律跳过，不再打扰）。
let ocrTally = null

function newTally() {
  return { created: 0, merged: 0, nochange: 0, inLedger: 0, unread: 0, failed: 0,
           offPlatform: 0, offHint: '' }
}

async function pumpOcr() {
  if (ocrRunning) return          // 已有 worker 在跑；新入队的图会被同一循环取走
  ocrRunning = true
  ocrTally = newTally()
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
    const t = ocrTally
    ocrTally = null
    await reportOcr(t)
    load()                      // 表里多了几行，拉一次让它们真的显示出来
  }
}

// 整批跑完报一次账。用 alert 而不是 toast：toast 3 秒就没了，
// 而「12 张里 9 张早就有了」这种结论用户往往要回头核对。
//
async function reportOcr(t) {
  if (!t) return
  const total = t.created + t.merged + t.nochange + t.inLedger + t.unread + t.failed
  if (!total) return
  const lines = [
    `<b>放进暂存 ${t.created} 单</b>`,
    t.merged ? `补全 ${t.merged} 单：订单号已在暂存里，把这次多认出来的字段补了进去` : '',
    t.nochange ? `${t.nochange} 单没有新信息（订单号已有，这次没认出更多东西）` : '',
    t.inLedger ? `${t.inLedger} 单的订单号已经在账本里了，没有重复放进暂存` : '',
    t.unread ? `未识别出订单信息 ${t.unread} 张` : '',
    t.failed ? `失败 ${t.failed} 张` : '',
    t.offPlatform
      ? `<br><b>其中 ${t.offPlatform} 张不是闲鱼版式</b>：${t.offHint}。`
        + '<br>这类截图上<b>成交价和商品名多半认不出来</b>（它们靠「成交价」这个闲鱼独有的标签定位），请在表里补。'
      : '',
    '<br>核对无误后，在这张表上逐单点「导入」才进账本。',
  ].filter(Boolean)
  ElMessageBox.alert(lines.join('<br>'), `OCR 完成 · 共 ${total} 张`,
                     { dangerouslyUseHTMLString: true, confirmButtonText: '知道了' })
}


// 一张截图 → 一条暂存行。与订单页原来那套的区别只有落点，判据一模一样。
async function processOcr(file) {
  const tooBig = await checkImageSize(file)
  if (tooBig) { ElMessage.warning(tooBig); if (ocrTally) ocrTally.failed++; return }
  try {
    // 用的是 orders 那个端点，但它**只识别、不写任何东西**（见 routers/orders.ocr_order）。
    // 为暂存再造一个返回完全相同数据的端点，只会多出一处要同步维护的地方。
    const res = await ordersApi.ocr(file)
    // 后端认出「这不是闲鱼版式」时会给一句 platform_warning（外加 platform_plugin：
    // 这台机器上有没有插件在管那个平台）。**必须说出来**——解析规则是照闲鱼写的，
    // 非闲鱼版式上成交价和商品名多半是空的，而用户不知道该去核对什么。
    // 这一条曾经有：订单页那版 `if (res.reject_reason) ElMessage.warning(...)`，
    // 搬到暂存页时漏掉了，于是变成静默建一行没有金额的单。
    if (res.platform_warning && ocrTally) {
      ocrTally.offPlatform++
      if (!ocrTally.offHint) {
        ocrTally.offHint = res.platform_warning
          + (res.platform_plugin ? `；而「${res.platform_plugin}」插件正管着这个平台，用它抓更准` : '')
      }
    } else if (res.platform_warning) {
      ElMessage.warning(res.platform_warning)
    }
    const data = {}
    if (res.order_date) data.order_date = res.order_date
    if (res.platform) data.platform = res.platform            // 认不出就留空，不瞎猜
    if (res.product) data.title = res.product
    if (res.express_company) data.express_company = res.express_company
    if (res.express_no) data.express_no = res.express_no
    if (res.order_no) data.order_no = res.order_no
    if (res.purchase_status) data.purchase_status = res.purchase_status
    // 成交价当**种子价**下发：暂存的 price_cny 是由物品单价派生的（sync_from_items），
    // 直接塞顶层会被后端忽略——与爬虫回灌走同一条路（见 soroban_client 的降级兜底）。
    const seed = (res.price_cny != null && res.price_cny !== '') ? res.price_cny : null

    // platform / purchase_status 恒有值，故按「实质字段」判断是不是真认出了东西
    const recognized = data.order_no || data.express_no || data.order_date || data.title || seed
    if (!recognized) { if (ocrTally) ocrTally.unread++; return }

    if (data.order_no) {
      // ① 暂存里已经有这个单号 → **只补空格**（情报更多才更新），不新建
      const dup = await findStagingByOrderNo(data.order_no)
      if (dup) { await mergeIntoStaging(dup, data, seed); return }
      // ② 暂存里没有、但账本里有（手工建的单从没进过暂存）→ 不建
      //    建了也导不进去：账本对 order_no + COALESCE(platform,'') 有唯一索引，
      //    点导入只会撞一个「数据完整性冲突」，而那时用户已经不知道这行是哪来的了。
      if (await existsInLedger(data.order_no)) {
        if (ocrTally) ocrTally.inLedger++
        else ElMessage.info(`订单号 ${data.order_no} 已经在账本里了`)
        return
      }
    }
    const created = await addRow({ ...data, ...(seed ? { price_cny: seed } : {}) })
    if (!created) { if (ocrTally) ocrTally.failed++; return }
    if (ocrTally) ocrTally.created++
    else ElMessage.success(`已放进暂存 · ${data.title || data.order_no || file.name}`)
  } catch (_) {
    if (ocrTally) ocrTally.failed++
  }
}

// 走**精确**参数，不是模糊 q。第一版写的是 `{ q: orderNo, limit: 20 }` 再在这里按 === 过滤：
// q 是跨 订单号/标题/快递号/物品名 的模糊搜，命中数超过 20 时真正那条可能排在后面，
// 于是「没找到」→ 新建一条重复的暂存行，而且是**静默**的（它要等到点导入才撞唯一约束）。
// q 还是大小写不敏感的，与这里的 === 口径也对不上。
async function findStagingByOrderNo(orderNo) {
  try {
    const res = await stagingApi.list({ order_no: orderNo, limit: 1 })
    return (res.items || [])[0] || null
  } catch (_) { return null }
}

async function existsInLedger(orderNo) {
  try {
    const res = await ordersApi.list({ order_no: orderNo, limit: 1 })
    return !!(res.items || []).length
  } catch (_) { return false }          // 查不到就当没有：宁可多建一行，也别because网络抖动把单丢了
}

// 「情报更多就更新」：**只填空格，不覆盖已填的**。
// 用户可能已经在暂存里手工改过标题/价格，第二张截图不该把它盖回去；
// 而状态只按 canAdvancePurchase 往前推（同订单页的合并口径）。
async function mergeIntoStaging(row, data, seed) {
  const patch = { version: row.version }
  for (const k of ['order_date', 'platform', 'title', 'express_company', 'express_no']) {
    const cur = row[k]
    if (data[k] != null && data[k] !== '' && (cur == null || cur === '')) patch[k] = data[k]
  }
  if (data.purchase_status && canAdvancePurchase(row.purchase_status, data.purchase_status)) {
    patch.purchase_status = data.purchase_status
  }
  // 价：只有原来一分钱都没有时才补，且同样走「种子价 + 无单价物品」那条路
  const noPrice = row.price_cny == null || Number(row.price_cny) === 0
  if (seed && noPrice) {
    patch.price_cny = seed
    patch.items = (row.items || []).length
      ? row.items.map((it) => ({ name: it.name, quantity: it.quantity, unit_price_cny: null, auto: true }))
      : [{ name: patch.title || row.title || '未命名物品', quantity: 1, unit_price_cny: null, auto: true }]
  }
  if (Object.keys(patch).length <= 1) {          // 只有 version → 这次没认出更多东西
    if (ocrTally) ocrTally.nochange++
    else ElMessage.info(`订单号 ${data.order_no} 已在暂存里，没有新信息`)
    return
  }
  try {
    await stagingApi.update(row.id, patch)
    if (ocrTally) ocrTally.merged++
    else ElMessage.success(`已补全暂存单 ${data.order_no}`)
  } catch (_) {
    if (ocrTally) ocrTally.failed++
  }
}

onMounted(() => { loadAccounts(); load() })
</script>

<style scoped>
/* 整窗拖拽覆盖层：居中的上传提示框，不拦截拖拽事件 */
.ocr-overlay {
  position: fixed; inset: 0; z-index: 9000; pointer-events: none;
  display: flex; align-items: center; justify-content: center;
  background: rgba(6, 12, 24, 0.72);
}
.ocr-overlay-box {
  width: min(460px, 76vw); padding: 40px 32px; text-align: center;
  border: 2px dashed var(--brand); border-radius: 16px;
  background: rgba(16, 25, 44, 0.92); box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
}
.ocr-overlay-ic { font-size: 44px; color: #6ea8ff; margin-bottom: 12px; }
.ocr-overlay-title { font-size: 18px; font-weight: 600; color: #eaf1ff; }
.ocr-overlay-sub { margin-top: 6px; font-size: 13px; color: #8a9ab8; }
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
