<template>
  <!-- 商品订单的「物品(单价×数量) + 邮费」编辑器。订单页展开面板与物品列表编辑弹窗共用同一份，
       写入统一走 ordersApi.update（后端 sync_from_items+compute_money+镜像暂存+version+1），
       杜绝「两个入口两套写逻辑」的账不一致。就地改动传入的 order 对象并 emit('saved')。 -->
  <div class="oie">
    <table class="item-tbl">
      <colgroup>
        <col class="c-name" /><col class="c-qty" /><col class="c-price" /><col class="c-act" />
      </colgroup>
      <thead>
        <tr><th>物品名</th><th>数量</th><th>单价（元）</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="(it, i) in (order.items || [])" :key="i" :class="{ 'item-auto': isTitleItem(it) }"
            :title="isTitleItem(it) ? '物品名与商品标题相同（无独立物品详情）；改成真实物品名即变正常色' : ''">
          <td><el-input v-model="it.name" size="small" placeholder="物品名" @change="onItemEdit(it)" /></td>
          <td><el-input-number v-model="it.quantity" :min="1" :controls="false" size="small" @change="onItemEdit(it)" /></td>
          <td><el-input-number v-model="it.unit_price_cny" :min="0" :precision="2" :controls="false" size="small"
                               placeholder="单价" @change="onItemEdit(it)" /></td>
          <td class="c-act"><el-button link type="danger" :icon="Delete" tabindex="-1" @click="removeItem(i)" /></td>
        </tr>
        <!-- 末尾草稿行：输入名称并失焦/回车即成为新物品并自动写库 -->
        <tr class="draft-row">
          <td><el-input v-model="draft.name" size="small" placeholder="+ 新物品名，输入后自动保存"
                        @change="commitDraft" @keyup.enter="commitDraft" /></td>
          <td><el-input-number v-model="draft.quantity" :min="1" :controls="false" size="small" /></td>
          <td><el-input-number v-model="draft.price" :min="0" :precision="2" :controls="false"
                               size="small" placeholder="单价" @keyup.enter="commitDraft" /></td>
          <td class="c-act"></td>
        </tr>
      </tbody>
    </table>
    <div class="postage-row">
      <span class="postage-lb">邮费（元）</span>
      <el-input-number v-model="order.postage_cny" :min="0" :precision="2" :controls="false" size="small"
                       placeholder="包邮" style="width: 130px" @change="savePostage" />
      <span class="postage-hint">不填 = 包邮</span>
    </div>
  </div>
</template>

<script setup>
import { reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import { ordersApi } from '@/api'
import { queueOrderWrite } from '@/utils/orderWrites'

// order 必须含 id / version / items / postage_cny / title（订单页的行 或 ordersApi.get 的结果都满足）
const props = defineProps({ order: { type: Object, required: true } })
const emit = defineEmits(['saved', 'conflict'])

const draft = reactive({ name: '', quantity: 1, price: null })

function itemPrice(v) { return (v === '' || v === null || v === undefined) ? null : Number(v) }
// 灰显 = 物品名与商品标题相同（无独立物品详情，多为自动占位）；有真实物品名即正常
function isTitleItem(it) { return !!it.name && (it.name || '').trim() === (props.order.title || '').trim() }
function ensureItems() { if (!props.order.items) props.order.items = []; return props.order.items }

async function saveItems() {
  const items = (props.order.items || []).filter((it) => it.name && it.name.trim())
    .map((it) => ({ name: it.name.trim(), quantity: Number(it.quantity) || 1,
                    unit_price_cny: itemPrice(it.unit_price_cny), auto: !!it.auto }))
  try {
    // 整个「读 version→PATCH→回写」入队串行，避免与同订单的其它保存并发撞 version 互相 409
    await queueOrderWrite(props.order.id, async () => {
      const updated = await ordersApi.update(props.order.id, { version: props.order.version, items })
      Object.assign(props.order, updated)   // 同步 version + 派生订单价 + 物品（就地更新调用方持有的对象）
      emit('saved', updated)
    })
  } catch (e) {
    if (e.response?.status === 409) { ElMessage.warning('数据已变，已刷新'); emit('conflict') }
  }
}

// 编辑任一物品字段 → 该物品转为「已确认」(auto=false，去灰) 并写库
function onItemEdit(it) {
  it.auto = false
  // 名字被清空时**不落库**：saveItems 会把无名物品整条剔掉（连同它的单价），订单金额随之缩水。
  // 而「Ctrl+A 删掉名字准备重打」是最常见的改名姿势，失焦即触发 change —— 用户以为只是清了个
  // 输入框，实际是一次无确认、无撤销的删除。真正的删除只走 removeItem（有二次确认）。
  if (!it.name || !it.name.trim()) return
  saveItems()
}

// 邮费改动：写库并让订单价随之重算（不填=包邮）。不覆盖未保存的物品编辑
async function savePostage() {
  try {
    await queueOrderWrite(props.order.id, async () => {
      const updated = await ordersApi.update(props.order.id, { version: props.order.version, postage_cny: itemPrice(props.order.postage_cny) })
      const { items, ...rest } = updated
      Object.assign(props.order, rest)
      emit('saved', updated)
    })
  } catch (e) {
    if (e.response?.status === 409) { ElMessage.warning('数据已变，已刷新'); emit('conflict') }
  }
}

// 删除某物品：二次确认后再移除并写库（删到 0 件时后端会自动补一条占位物品，与订单页一致）
async function removeItem(i) {
  const it = props.order.items?.[i]
  try {
    await ElMessageBox.confirm(`删除物品「${it?.name || '未命名'}」？`, '确认', { type: 'warning' })
  } catch (_) { return }
  props.order.items.splice(i, 1)
  saveItems()
}

// 末尾草稿录入完成：转为正式物品(auto=false)、清空草稿、自动写库
async function commitDraft() {
  if (!draft.name || !draft.name.trim()) return
  ensureItems().push({ name: draft.name.trim(), quantity: Number(draft.quantity) || 1,
                       unit_price_cny: itemPrice(draft.price), auto: false })
  draft.name = ''; draft.quantity = 1; draft.price = null
  await saveItems()
}
</script>

<style scoped>
.oie { padding: 12px 20px; }
/* 二级子表格：视觉与一级列表(NotionTable)一致——同样的边框、行高与悬停；无表头填充 */
.item-tbl { border-collapse: collapse; font-size: 13px; color: #d6deea; table-layout: fixed; }
.item-tbl col.c-name { width: 240px; }
.item-tbl col.c-qty { width: 90px; }
.item-tbl col.c-price { width: 120px; }
.item-tbl col.c-act { width: 56px; }
.item-tbl thead th { height: 30px; font-weight: 500; color: #7d8aa3; text-align: left; padding: 0 10px; border-bottom: 1px solid #28354a; }
.item-tbl td { height: 36px; padding: 0; border-bottom: 1px solid #202c44; border-right: 1px solid #28354a; }
.item-tbl tbody tr:hover td { background: #1b2942; }
.item-tbl td.c-act { text-align: center; }
/* 灰显：系统自动生成/自动定价的物品（编辑即去灰） */
.item-tbl tr.item-auto :deep(.el-input__inner) { color: #6b7488; font-style: italic; }
.postage-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.postage-lb { color: #9ba8bf; font-size: 13px; }
.postage-hint { color: #6b7488; font-size: 12px; }
/* 单元格内输入做成无边框，贴合一级列表的扁平格子观感 */
.item-tbl :deep(.el-input__wrapper),
.item-tbl :deep(.el-input-number .el-input__wrapper) {
  box-shadow: none !important; background: transparent; padding: 0 10px; height: 36px;
}
.item-tbl :deep(.el-input-number) { width: 100%; line-height: normal; }
.item-tbl :deep(.el-input-number .el-input__inner) { text-align: left; }
</style>
