<template>
  <!-- 商品订单「整单编辑」面板：全部字段可改 + 内嵌物品/邮费编辑器。供物品列表编辑弹窗用。
       写入统一走 ordersApi.update（字段级即存，与订单页格子同一 PATCH、同一乐观锁），
       派生列（人民币/结算日元）只读展示。就地改 order 并 emit('saved')；409 emit('conflict')。 -->
  <div class="oep">
    <div class="oep-sum">
      <span>人民币 <b>{{ fmtCNY(order.price_cny) }}</b></span>
      <span>结算 <b>{{ fmtJPY(order.jpy_settled) }}</b></span>
    </div>

    <div class="oep-fields">
      <label class="f"><span>下单日期</span>
        <el-date-picker v-model="order.date" type="date" value-format="YYYY-MM-DD"
                        :clearable="false" @change="saveField('date', order.date)" /></label>
      <label class="f"><span>状态</span>
        <el-select v-model="order.purchase_status" @change="saveField('purchase_status', order.purchase_status)">
          <el-option v-for="s in PURCHASE_STATUS" :key="s" :label="s" :value="s" />
        </el-select></label>
      <label class="f"><span>来源</span>
        <el-select v-model="order.platform" clearable placeholder="来源" @change="saveField('platform', order.platform)">
          <el-option v-for="p in ORDER_SOURCES" :key="p" :label="p" :value="p" />
        </el-select></label>
      <label class="f"><span>账号昵称</span>
        <el-select v-model="order.platform_account" clearable filterable allow-create default-first-option
                   placeholder="账号昵称" @change="saveField('platform_account', order.platform_account)">
          <el-option v-for="a in accounts" :key="a" :label="a" :value="a" />
        </el-select></label>
      <label class="f"><span>所属集运</span>
        <el-select :persistent="false" v-model="order.shipment_order_id" clearable filterable placeholder="未集运"
                   remote :remote-method="searchShipment" :loading="shipSearching"
                   remote-show-suffix reserve-keyword
                   no-data-text="没有匹配的集运单"
                   @visible-change="onShipDropdown"
                   @change="saveField('shipment_order_id', order.shipment_order_id)">
          <!-- 当前挂靠的那张若不在前 200 里，补一条它自己，否则选框会显示成空 -->
          <el-option v-if="attachedMissing" :key="order.shipment_order_id"
                     :label="(order.shipment_no || ('#' + order.shipment_order_id)) + ' · ' + (order.fulfillment_status || '')"
                     :value="order.shipment_order_id" />
          <el-option v-for="j in sortedShipments" :key="j.id"
                     :label="(j.shipment_no || ('#' + j.id)) + ' · ' + j.shipment_status" :value="j.id" />
        </el-select></label>
      <label class="f"><span>订单号</span>
        <el-input v-model="order.order_no" placeholder="订单号" @change="saveField('order_no', order.order_no)" /></label>
      <label class="f"><span>快递公司</span>
        <el-input v-model="order.express_company" placeholder="快递公司" @change="saveField('express_company', order.express_company)" /></label>
      <label class="f"><span>快递号</span>
        <el-input v-model="order.express_no" placeholder="快递号" @change="saveField('express_no', order.express_no)" /></label>
      <label class="f"><span>汇率</span>
        <el-input v-model="order.fx_rate" placeholder="当天汇率" @change="saveField('fx_rate', order.fx_rate)" /></label>
      <label class="f"><span>覆盖日元（円）</span>
        <el-input-number v-model="order.jpy_override" :controls="false" placeholder="实付日元"
                         class="fnum" @change="saveField('jpy_override', order.jpy_override)" /></label>
      <label class="f"><span>分类</span>
        <el-input v-model="order.category" placeholder="分类" @change="saveField('category', order.category)" /></label>
      <label class="f"><span>商品标题</span>
        <el-input v-model="order.title" placeholder="商品标题" @change="saveField('title', order.title)" /></label>
      <label class="f f-wide"><span>商品链接</span>
        <el-input v-model="order.url" placeholder="商品链接" @change="saveField('url', order.url)">
          <!-- 爬虫一直在灌这个字段，而它此前只是个**点不开**的输入框。
               只认 http(s)：这一格的值来自爬虫与手输，不是可信输入。 -->
          <template v-if="openableUrl(order.url)" #append>
            <el-link type="primary" :underline="false" :href="order.url"
                     target="_blank" rel="noopener noreferrer" title="在新标签页打开">打开</el-link>
          </template>
        </el-input></label>
      <label class="f f-wide"><span>备注</span>
        <el-input v-model="order.note" type="textarea" :rows="2" placeholder="备注" @change="saveField('note', order.note)" /></label>
    </div>

    <div class="oep-subtitle">物品明细 · 单价×数量 + 邮费</div>
    <OrderItemsEditor :order="order" @saved="$emit('saved')" @conflict="$emit('conflict')" />
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ordersApi, shipmentApi } from '@/api'
import { handled } from '@/api/http'
import { MSG_STALE_RELOADED, ORDER_SOURCES, PURCHASE_STATUS } from '@/constants'
import { fmtCNY, fmtJPY } from '@/utils/money'
import { applyRowUpdate, queueRowWrite } from '@/utils/rowWrites'
import OrderItemsEditor from '@/components/OrderItemsEditor.vue'

const props = defineProps({
  order: { type: Object, required: true },
  shipments: { type: Array, default: () => [] },
  accounts: { type: Array, default: () => [] },   // 账号昵称候选（已登记的），下拉用；仍可 allow-create 新建
})
const emit = defineEmits(['saved', 'conflict'])

// 打包中的集运单置顶（最常挂），其余按原顺序（日期倒序）。
// 搜索态下展示命中结果，否则展示父组件传来的默认那批。
// null = 不在搜索态；数组 = 本次命中（可能为空数组 = 零命中）。理由同 Orders 页：
// 用 .length 判搜索态会让「零命中」静默回退成全量列表。
const shipHits = ref(null)
const shipSearching = ref(false)
const sortedShipments = computed(() => {
  const base = shipHits.value ?? props.shipments
  return [...base].sort((a, b) => (b.shipment_status === '打包中' ? 1 : 0) - (a.shipment_status === '打包中' ? 1 : 0))
})
// 当前挂靠的那张不在候选里 → 得自己补一条，否则 el-select 找不到匹配 option 会显示空白，
// 看起来像「这单没挂集运」，一旦误存就把挂靠关系抹了。
const attachedMissing = computed(() => {
  // 搜索态下不补：搜索结果里没有当前挂靠的那张是**正常的**（它就是没命中）。
  // 不排除的话零命中时它恒为真，会把当前挂靠单当成唯一「命中结果」摆出来，
  // 用户以为搜到了、点下去等于什么都没改。
  if (shipHits.value) return false
  const id = props.order?.shipment_order_id
  return !!id && !sortedShipments.value.some((j) => j.id === id)
})

let shipSeq = 0
async function searchShipment(kw) {
  const q = (kw || '').trim()
  if (!q) { shipHits.value = null; shipSearching.value = false; return }
  const my = ++shipSeq
  shipSearching.value = true
  try {
    const res = await shipmentApi.list({ q, limit: 50, brief: true })
    if (my === shipSeq) shipHits.value = res.items      // 迟到的响应不覆盖新结果
  } catch (_) { /* 拦截器已提示 */ } finally {
    if (my === shipSeq) shipSearching.value = false
  }
}
function onShipDropdown(visible) {
  if (!visible) { shipHits.value = null; shipSeq++ }
}

// 字段级即存：与订单页格子同一 PATCH。空串归一为 null（清空）。不回传 items，免踩面板里的物品数组。
// 只有 http(s) 链接才给「打开」。`javascript:` / `data:` 一律不给——
// 这一格的值来自爬虫与手输，而 target=_blank 打开的新页面能拿到 window.opener，
// 所以 rel="noopener noreferrer" 一并带上。
function openableUrl(u) { return /^https?:\/\//i.test(String(u || '').trim()) }

async function saveField(key, value) {
  const v = value === '' ? null : value
  try {
    // 入队串行：面板里连改多个字段（或与内嵌物品编辑器并发）不会各读旧 version 互相 409
    await queueRowWrite(`order:${props.order.id}`, async () => {
      // 请求前照一张：面板里每个字段都 v-model 直接绑在这个共享对象上，
      // 响应回来时用户很可能正在**另一格**里敲字（见 applyRowUpdate 的 `before`）。
      const before = { ...props.order }
      const patch = { version: props.order.version, [key]: v }
      const updated = await ordersApi.update(props.order.id, patch)
      applyRowUpdate(props.order, patch, updated, { before })
      emit('saved', updated)
    })
  } catch (e) {
    if (e.response?.status === 409) { handled(e); ElMessage.warning(MSG_STALE_RELOADED); emit('conflict') }
    // 其它（如 422 校验失败）：拦截器已提示，保留用户输入待修正重试
  }
}
</script>

<style scoped>
.oep-sum { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; padding: 4px 20px 10px; font-size: 13px; color: var(--txt-body); }
.oep-sum b { color: var(--txt-1); }
.oep-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 16px; padding: 4px 20px 8px; }
.f { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.f > span { color: var(--txt-2); font-size: 12px; }
.f-wide { grid-column: 1 / -1; }
.f :deep(.el-select), .f :deep(.el-input), .f :deep(.el-date-editor) { width: 100%; }
.fnum { width: 100% !important; }
.fnum :deep(.el-input__inner) { text-align: left; }
.oep-subtitle { padding: 8px 20px 0; color: var(--txt-3); font-size: 12px; border-top: 1px solid var(--border-soft); margin-top: 6px; }
</style>
