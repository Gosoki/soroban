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
        <tr v-for="(it, i) in (order.items || [])" :key="i" :class="{ 'item-auto': isDerived(it) }"
            :title="derivedWhy(it)">
          <td><el-input v-model="it.name" placeholder="物品名" @change="onItemEdit(it)" /></td>
          <td><el-input-number v-model="it.quantity" :min="1" :controls="false" @change="onItemEdit(it)" /></td>
          <td><el-input-number v-model="it.unit_price_cny" :min="0" :precision="2" :controls="false"
                               placeholder="单价" @change="onItemEdit(it)" /></td>
          <td class="c-act"><el-button link type="danger" :icon="Delete" tabindex="-1" @click="removeItem(i)" /></td>
        </tr>
        <!-- 末尾草稿行：三个格子只攒草稿，按回车或点右侧 ✓ 才落库。
             **不能挂 @change**：原生 change 在失焦时就触发，用户按自然顺序从左往右填
             （名 → 数量 → 单价）时，刚离开名字框就已经以「数量 1、无单价」提交并清空草稿，
             随后填的数量与单价留在孤儿 draft 里，永远进不了库。与 NotionTable 的幽灵新建行同一范式。 -->
        <tr class="draft-row" @keyup.enter="commitDraft">
          <td><el-input v-model="draft.name" placeholder="+ 新物品名，填完按回车" /></td>
          <td><el-input-number v-model="draft.quantity" :min="1" :controls="false" /></td>
          <td><el-input-number v-model="draft.price" :min="0" :precision="2" :controls="false" placeholder="单价" /></td>
          <td class="c-act">
            <el-button link :type="draftReady ? 'success' : 'info'" :icon="draftReady ? Check : Plus"
                       :disabled="!draftReady || committingDraft" tabindex="-1"
                       :title="draftReady ? '添加这条物品（或按回车）' : '先填物品名'"
                       @click="commitDraft" />
          </td>
        </tr>
      </tbody>
    </table>
    <div class="postage-row">
      <span class="postage-lb">货款（元）</span>
      <el-input-number v-model="goodsInput" :min="0" :precision="2" :controls="false"
                       :disabled="!isSingleUnitItem" :placeholder="isSingleUnitItem ? '直接填金额' : '由明细算出'"
                       style="width: 130px" @change="applyGoods" />
      <el-tooltip placement="top" :content="GOODS_HINT" popper-class="wrap-tip">
        <el-icon class="lb-help"><QuestionFilled /></el-icon>
      </el-tooltip>
      <span class="postage-lb pl">邮费（元）</span>
      <el-input-number v-model="order.postage_cny" :min="0" :precision="2" :controls="false"
                       placeholder="包邮" style="width: 130px" @change="savePostage" />
      <span class="postage-hint">不填 = 包邮</span>
    </div>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watchEffect } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Delete, Plus, QuestionFilled } from '@element-plus/icons-vue'
import { ordersApi } from '@/api'
import { handled } from '@/api/http'
import { applyRowUpdate, queueOrderWrite } from '@/utils/orderWrites'

// order 必须含 id / version / items / postage_cny / title（订单页的行 或 ordersApi.get 的结果都满足）
const props = defineProps({ order: { type: Object, required: true } })
const emit = defineEmits(['saved', 'conflict'])

const draft = reactive({ name: '', quantity: 1, price: null })
const committingDraft = ref(false)
const draftReady = computed(() => !!(draft.name && draft.name.trim()))

const GOODS_HINT =
  '懒人入口：不想拆物品明细时，直接填这一单的货款（不含邮费）。'
  + '会折成一条物品：名称取商品标题、数量 1、单价 = 该金额。\n'
  + '拆过明细（多条物品，或数量不为 1）之后这里就只读了——'
  + '那时候金额由明细算出来，反过来改它会把你拆好的明细冲掉。'

// 已经拆过明细就不许再从这里覆盖：多于一条、或唯一那条数量不是 1，都算拆过。
const isSingleUnitItem = computed(() => {
  const its = props.order.items || []
  return its.length <= 1 && (its.length === 0 || (Number(its[0].quantity) || 1) === 1)
})
// 显示值 = 货款 = 订单价 − 邮费（订单价本身是派生的，见列头「?」）
const goodsInput = ref(null)
watchEffect(() => {
  const its = props.order.items || []
  if (isSingleUnitItem.value) {           // 可编辑：就是那条物品的单价
    const v = its.length ? its[0].unit_price_cny : null
    goodsInput.value = (v === null || v === undefined) ? null : Number(v)
    return
  }
  // 拆过明细：只读显示派生出来的货款 Σ(单价×数量)，让人看得见总数、但改不了
  goodsInput.value = its.reduce(
    (sum, it) => sum + (Number(it.unit_price_cny) || 0) * (Number(it.quantity) || 1), 0)
})

// 手填货款 → 绑成「订单名称 × 1」的单条物品。auto=true 保持灰显，提示这是自动折算的、待复核。
async function applyGoods(v) {
  if (!isSingleUnitItem.value) return               // 拆过明细的不该走到这（输入框已禁用），双保险
  const price = itemPrice(v)
  const name = (props.order.title || '').trim() || '未命名物品'
  props.order.items = [{ name, quantity: 1, unit_price_cny: price, auto: true }]
  await saveItems()
}

function itemPrice(v) { return (v === '' || v === null || v === undefined) ? null : Number(v) }
// 灰显 = 物品名与商品标题相同（无独立物品详情，多为自动占位）；有真实物品名即正常
function isTitleItem(it) { return !!it.name && (it.name || '').trim() === (props.order.title || '').trim() }
// 灰显 = 「这一行不是你亲手确认的」。两种来源，都该灰：
//   · 物品名与商品标题相同 —— 手填货款时自动绑出来的占位行，没有独立物品详情；
//   · `auto=true` —— 服务端派生的行，最典型的是「金额尾差」那条
//     （单价除不尽时补出来保证总价守恒）。
// 原先只认第一种，于是「金额尾差」行是正常色：它看起来和用户自己敲的一模一样，
// 而它其实是派生的、会随下一次改单被重算/替换。任一格被编辑过就转成正常色
// （onItemEdit 会置 auto=false），这条语义两种来源共用。
function isDerived(it) { return !!it.auto || isTitleItem(it) }
function derivedWhy(it) {
  if (it.auto && !isTitleItem(it)) return '这一行由系统派生（如金额尾差），改任一格即转为你确认的物品'
  if (isTitleItem(it)) return '物品名与商品标题相同（无独立物品详情）；改成真实物品名即变正常色'
  return ''
}
function ensureItems() { if (!props.order.items) props.order.items = []; return props.order.items }

async function saveItems() {
  const all = props.order.items || []
  // **绝不静默丢弃没名字的行**。原先这里是 `.filter(有名字)`，于是「清空名字准备重打」
  // 期间的任何一次保存（改数量、改单价、改另一条物品）都会把那条连同它的钱一起删掉，
  // 订单金额随之缩水——无确认、无撤销、无提示。
  // `onItemEdit` 里那道守卫只挡住了「改这一条本身」，挡不住「改别的东西时顺带整体保存」。
  // 守卫必须放在**所有入口的必经之路**上，也就是这里。
  // 真正的删除只走 removeItem（有二次确认）。
  const blank = all.filter((it) => !it.name || !it.name.trim())
  if (blank.length) {
    ElMessage.warning(`有 ${blank.length} 条物品还没填名字——先填上，或点右侧 🗑 删掉`)
    return false
  }
  const items = all.map((it) => ({ name: it.name.trim(), quantity: Number(it.quantity) || 1,
                                   unit_price_cny: itemPrice(it.unit_price_cny), auto: !!it.auto }))
  try {
    // 整个「读 version→PATCH→回写」入队串行，避免与同订单的其它保存并发撞 version 互相 409
    await queueOrderWrite(props.order.id, async () => {
      const patch = { version: props.order.version, items }
      const updated = await ordersApi.update(props.order.id, patch)
      applyRowUpdate(props.order, patch, updated)   // 送了 items → 整体采纳（含后端重折算的单价）
      emit('saved', updated)
    })
    return true
  } catch (e) {
    // 分工固定：**组件只管 409**（拦截器刻意放行它），其余一律交给拦截器。
    // 这里再弹一次的话会出现两条提示，而且 detail 是 FastAPI 的校验数组时，
    // 这条会把 JSON 原样打进提示框（拦截器那边是展平过的）。
    if (e.response?.status === 409) { handled(e); ElMessage.warning('数据已变，已刷新'); emit('conflict') }
    return false
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
      const patch = { version: props.order.version, postage_cny: itemPrice(props.order.postage_cny) }
      const updated = await ordersApi.update(props.order.id, patch)
      applyRowUpdate(props.order, patch, updated)
      emit('saved', updated)
    })
    return true
  } catch (e) {
    // 分工固定：**组件只管 409**（拦截器刻意放行它），其余一律交给拦截器。
    // 这里再弹一次的话会出现两条提示，而且 detail 是 FastAPI 的校验数组时，
    // 这条会把 JSON 原样打进提示框（拦截器那边是展平过的）。
    if (e.response?.status === 409) { handled(e); ElMessage.warning('数据已变，已刷新'); emit('conflict') }
    return false
  }
}

// 删除某物品：二次确认后再移除并写库（删到 0 件时后端会自动补一条占位物品，与订单页一致）
async function removeItem(i) {
  const it = props.order.items?.[i]
  try {
    await ElMessageBox.confirm(`删除物品「${it?.name || '未命名'}」？`, '确认', { type: 'warning' })
  } catch (_) { return }
  // **先删本地、失败要放回去**。不放回去的话本地数组已经少了这条，
  // 下一次任何成功的保存都会把「不含它的 items」整体覆盖上去——
  // 这条物品和它的钱就被静默删掉了，而用户看到的只是「刚才那次删除失败了」。
  // 同一个文件里的 commitDraft 已经这么做了，这里漏了。
  const removed = props.order.items.splice(i, 1)
  if (!(await saveItems())) props.order.items.splice(i, 0, ...removed)
}

// 末尾草稿录入完成：转为正式物品(auto=false)、写库、**成功后**才清空草稿
async function commitDraft() {
  if (!draftReady.value || committingDraft.value) return
  const row = { name: draft.name.trim(), quantity: Number(draft.quantity) || 1,
                unit_price_cny: itemPrice(draft.price), auto: false }
  const items = ensureItems()
  items.push(row)
  committingDraft.value = true
  try {
    // 成功才清空草稿：失败（409 等）时把用户刚敲的内容留在格子里让他改，而不是连人带字一起吞掉。
    // 同理失败要把乐观塞进去的那条也撤回，否则界面上会留一条其实没入库的物品。
    if (await saveItems()) {
      draft.name = ''; draft.quantity = 1; draft.price = null
    } else {
      const i = items.indexOf(row)
      if (i !== -1) items.splice(i, 1)
    }
  } finally {
    committingDraft.value = false
  }
}
</script>

<style scoped>
.lb-help { color: var(--txt-3); cursor: help; font-size: 13px; margin-left: 2px; }
.postage-lb.pl { margin-left: 16px; }
.oie { padding: 12px 20px; }
/* 二级子表格：视觉与一级列表(NotionTable)一致——同样的边框、行高与悬停；无表头填充 */
.item-tbl { border-collapse: collapse; font-size: 13px; color: var(--txt-body); table-layout: fixed; }
.item-tbl col.c-name { width: 240px; }
.item-tbl col.c-qty { width: 90px; }
.item-tbl col.c-price { width: 120px; }
.item-tbl col.c-act { width: 56px; }
.item-tbl thead th { height: 30px; font-weight: 500; color: var(--txt-3); text-align: left; padding: 0 10px; border-bottom: 1px solid var(--border); }
.item-tbl td { height: 36px; padding: 0; border-bottom: 1px solid var(--border-soft); border-right: 1px solid var(--border); }
.item-tbl tbody tr:hover td { background: var(--bg-row-hover); }
.item-tbl td.c-act { text-align: center; }
/* 灰显：系统自动生成/自动定价的物品（编辑即去灰） */
.item-tbl tr.item-auto :deep(.el-input__inner) { color: var(--txt-3); font-style: italic; }
.postage-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.postage-lb { color: var(--txt-2); font-size: 13px; }
.postage-hint { color: var(--txt-3); font-size: 12px; }
/* 单元格内输入做成无边框，贴合一级列表的扁平格子观感 */
.item-tbl :deep(.el-input__wrapper),
.item-tbl :deep(.el-input-number .el-input__wrapper) {
  box-shadow: none !important; background: transparent; padding: 0 10px; height: 36px;
}
.item-tbl :deep(.el-input-number) { width: 100%; line-height: normal; }
.item-tbl :deep(.el-input-number .el-input__inner) { text-align: left; }
</style>
