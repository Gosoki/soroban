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
        <el-date-picker v-model="order.date" type="date" value-format="YYYY-MM-DD" size="small"
                        :clearable="false" @change="saveField('date', order.date)" /></label>
      <label class="f"><span>状态</span>
        <el-select v-model="order.status" size="small" @change="saveField('status', order.status)">
          <el-option v-for="s in ORDER_STATUS" :key="s" :label="s" :value="s" />
        </el-select></label>
      <label class="f"><span>来源</span>
        <el-select v-model="order.platform" size="small" clearable placeholder="来源" @change="saveField('platform', order.platform)">
          <el-option v-for="p in ORDER_SOURCES" :key="p" :label="p" :value="p" />
        </el-select></label>
      <label class="f"><span>账号昵称</span>
        <el-select v-model="order.platform_account" size="small" clearable filterable allow-create default-first-option
                   placeholder="账号昵称" @change="saveField('platform_account', order.platform_account)">
          <el-option v-for="a in accounts" :key="a" :label="a" :value="a" />
        </el-select></label>
      <label class="f"><span>所属集运</span>
        <el-select :persistent="false" v-model="order.shipment_order_id" size="small" clearable filterable placeholder="未集运"
                   @change="saveField('shipment_order_id', order.shipment_order_id)">
          <el-option v-for="j in sortedShipments" :key="j.id"
                     :label="(j.shipment_no || ('#' + j.id)) + ' · ' + j.status" :value="j.id" />
        </el-select></label>
      <label class="f"><span>订单号</span>
        <el-input v-model="order.order_no" size="small" placeholder="订单号" @change="saveField('order_no', order.order_no)" /></label>
      <label class="f"><span>快递公司</span>
        <el-input v-model="order.express_company" size="small" placeholder="快递公司" @change="saveField('express_company', order.express_company)" /></label>
      <label class="f"><span>快递号</span>
        <el-input v-model="order.express_no" size="small" placeholder="快递号" @change="saveField('express_no', order.express_no)" /></label>
      <label class="f"><span>汇率</span>
        <el-input v-model="order.fx_rate" size="small" placeholder="当天汇率" @change="saveField('fx_rate', order.fx_rate)" /></label>
      <label class="f"><span>覆盖日元（円）</span>
        <el-input-number v-model="order.jpy_override" :controls="false" size="small" placeholder="实付日元"
                         class="fnum" @change="saveField('jpy_override', order.jpy_override)" /></label>
      <label class="f"><span>分类</span>
        <el-input v-model="order.category" size="small" placeholder="分类" @change="saveField('category', order.category)" /></label>
      <label class="f"><span>商品标题</span>
        <el-input v-model="order.title" size="small" placeholder="商品标题" @change="saveField('title', order.title)" /></label>
      <label class="f f-wide"><span>商品链接</span>
        <el-input v-model="order.url" size="small" placeholder="商品链接" @change="saveField('url', order.url)" /></label>
      <label class="f f-wide"><span>备注</span>
        <el-input v-model="order.note" type="textarea" :rows="2" size="small" placeholder="备注" @change="saveField('note', order.note)" /></label>
    </div>

    <div class="oep-subtitle">物品明细 · 单价×数量 + 邮费</div>
    <OrderItemsEditor :order="order" @saved="$emit('saved')" @conflict="$emit('conflict')" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { ordersApi } from '@/api'
import { ORDER_SOURCES, ORDER_STATUS } from '@/constants'
import { fmtCNY, fmtJPY } from '@/utils/money'
import { queueOrderWrite } from '@/utils/orderWrites'
import OrderItemsEditor from '@/components/OrderItemsEditor.vue'

const props = defineProps({
  order: { type: Object, required: true },
  shipments: { type: Array, default: () => [] },
  accounts: { type: Array, default: () => [] },   // 账号昵称候选（已登记的），下拉用；仍可 allow-create 新建
})
const emit = defineEmits(['saved', 'conflict'])

// 打包中的集运单置顶（最常挂），其余按原顺序（日期倒序）
const sortedShipments = computed(() =>
  [...props.shipments].sort((a, b) => (b.status === '打包中' ? 1 : 0) - (a.status === '打包中' ? 1 : 0)),
)

// 字段级即存：与订单页格子同一 PATCH。空串归一为 null（清空）。不回传 items，免踩面板里的物品数组。
async function saveField(key, value) {
  const v = value === '' ? null : value
  try {
    // 入队串行：面板里连改多个字段（或与内嵌物品编辑器并发）不会各读旧 version 互相 409
    await queueOrderWrite(props.order.id, async () => {
      const updated = await ordersApi.update(props.order.id, { version: props.order.version, [key]: v })
      const { items, ...rest } = updated
      Object.assign(props.order, rest)
      emit('saved', updated)
    })
  } catch (e) {
    if (e.response?.status === 409) { ElMessage.warning('数据已变，已刷新'); emit('conflict') }
    // 其它（如 422 校验失败）：拦截器已提示，保留用户输入待修正重试
  }
}
</script>

<style scoped>
.oep-sum { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; padding: 4px 20px 10px; font-size: 13px; color: #c7d2e6; }
.oep-sum b { color: #e6edf7; }
.oep-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 16px; padding: 4px 20px 8px; }
.f { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.f > span { color: #9ba8bf; font-size: 12px; }
.f-wide { grid-column: 1 / -1; }
.f :deep(.el-select), .f :deep(.el-input), .f :deep(.el-date-editor) { width: 100%; }
.fnum { width: 100% !important; }
.fnum :deep(.el-input__inner) { text-align: left; }
.oep-subtitle { padding: 8px 20px 0; color: #7d8aa3; font-size: 12px; border-top: 1px solid #202c44; margin-top: 6px; }
</style>
