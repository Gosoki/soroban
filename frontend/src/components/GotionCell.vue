<template>
  <!-- 只读展示（派生金额等） -->
  <div v-if="col.readonly" class="gtn-disp">
    <span v-if="disp !== null" :class="col.format ? 'derived' : ''">{{ disp }}</span>
    <span v-else class="ph">{{ emptyText }}</span>
  </div>

  <!-- select：tag + 弹出选项。locked 时只展示不可选——但**标签本身照常上色**：
       置灰的是格子（表示这格现在由别处决定），不是标签，否则一眼看不出是什么状态。 -->
  <el-popover v-else-if="col.type === 'select'" :visible="editing && !locked" :width="180" :offset="4"
              placement="bottom-start" @update:visible="(v) => !v && (editing = false)">
    <template #reference>
      <div class="gtn-disp sel" :class="{ locked }" @click="!locked && (editing = !editing)">
        <el-tag v-if="modelValue" v-bind="tagAttrs(modelValue)" size="small">{{ modelValue }}</el-tag>
        <span v-else class="ph">{{ emptyText }}</span>
      </div>
    </template>
    <div class="gtn-opts">
      <div v-for="o in col.options" :key="o" class="gtn-opt" :class="{ active: modelValue === o }" @click="choose(o)">
        <el-tag v-bind="tagAttrs(o)" size="small">{{ o }}</el-tag>
        <el-icon v-if="modelValue === o" class="gtn-ck"><Check /></el-icon>
      </div>
      <div v-if="modelValue && col.clearable !== false" class="gtn-opt clear" @click="choose(null)">清除</div>
    </div>
  </el-popover>

  <!-- date -->
  <div v-else-if="col.type === 'date'" class="gtn-disp" @click="!editing && start()">
    <!-- clearable 必须显式透传：el-date-picker 默认 true，编辑态一悬停就出 ✕，点下去发
         date=null 撞 NOT NULL 列 → 409「数据完整性冲突」→ 前端当成乐观锁冲突弹「数据已变」。 -->
    <el-date-picker v-if="editing" ref="inp" v-model="editVal" type="date" value-format="YYYY-MM-DD"
                    :clearable="col.clearable !== false"
                    size="small" class="gtn-in" @change="commit()" @visible-change="(v) => !v && close()" />
    <template v-else>
      <span v-if="disp !== null">{{ disp }}</span>
      <span v-else class="ph">{{ emptyText }}</span>
    </template>
  </div>

  <!-- long：长文本(如商品标题) → 点开弹宽 textarea 编辑（无悬停预览）。手动控制：只有点到弹窗+单元格
       以外才关闭并保存(commit 仅值变才 emit)；点框内/点单元格本身都不关；Esc 取消。固定 4 行防漂移。 -->
  <el-popover v-else-if="col.long" trigger="manual" :visible="editing" :width="380" :offset="4"
              placement="bottom-start">
    <template #reference>
      <div ref="refEl" class="gtn-disp" @click="!editing && start()">
        <span v-if="disp !== null">{{ disp }}</span>
        <span v-else class="ph">{{ emptyText }}</span>
      </div>
    </template>
    <el-input ref="inp" v-model="editVal" v-click-outside="onClickOutside" type="textarea"
              class="gtn-long-in" :rows="4" resize="none"
              @keydown.enter.exact.prevent="commit(true)" @keydown.esc="close" />
  </el-popover>

  <!-- text / decimal / int -->
  <div v-else class="gtn-disp" @click="!editing && start()">
    <el-input-number v-if="editing && col.type === 'int'" ref="inp" v-model="editVal" :controls="false"
                     size="small" class="gtn-in" @blur="commit()" @keydown.enter="commit(true)" @keydown.esc="close" />
    <el-input v-else-if="editing" ref="inp" v-model="editVal" size="small" class="gtn-in"
              @blur="commit()" @keydown.enter="commit(true)" @keydown.esc="close" />
    <template v-else>
      <span v-if="disp !== null" :class="col.format ? 'derived' : ''">{{ disp }}</span>
      <span v-else class="ph">{{ emptyText }}</span>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, ref } from 'vue'
import { ClickOutside as vClickOutside, ElMessage } from 'element-plus'
import { Check } from '@element-plus/icons-vue'
import { fmtCNY, fmtJPY } from '@/utils/money'
import { statusStyle, tagStyleAt } from '@/constants'

const props = defineProps({
  modelValue: { default: null },
  col: { type: Object, required: true },
  newRow: { type: Boolean, default: false },   // 幽灵新建行：空值显占位提示引导填写
  // 按**行**锁定（列级只读用 col.readonly）。用于「这一格的值由别处决定」的情形：
  // 例如订单挂上集运单后，状态跟随那张单，本格不该再能改。
  locked: { type: Boolean, default: false },
})
const emit = defineEmits(['change', 'submit'])

const editing = ref(false)
const editVal = ref(null)
const inp = ref(null)
const refEl = ref(null)   // long 单元格 reference，用于 click-outside 判断"点到单元格本身不算外面"

// long 编辑：只有点到「弹窗 + 单元格」以外才关闭并保存（点框内、点单元格本身都不关）
function onClickOutside(e) {
  if (!editing.value) return
  if (refEl.value && refEl.value.contains(e.target)) return   // 点单元格本身不关（含刚打开那一下）
  commit()
}
// 统一「柔和底色」标签：标签列按值哈希取色，状态列按语义取色
function tagAttrs(v) {
  if (!props.col.tagColored) return { style: statusStyle(v) }
  // 标签列用后端持久化的颜色序号（稳定不撞色）；缺失则回退按值哈希
  const meta = props.col.tagMeta && props.col.tagMeta[v]
  return { style: tagStyleAt(meta ? meta.color : -1, v) }
}

const disp = computed(() => {
  const v = props.modelValue
  if (v === null || v === undefined || v === '') return null
  if (props.col.format === 'jpy') return fmtJPY(v)
  if (props.col.format === 'cny') return fmtCNY(v)
  return v
})

// 只有幽灵新建行才显占位提示（引导填写）；普通数据行空值一律「—」（select 新建行默认「选择」）
const emptyText = computed(() =>
  props.newRow ? (props.col.placeholder || (props.col.type === 'select' ? '选择' : '—')) : '—',
)

function start() {
  if (props.col.readonly || props.locked) return
  editVal.value = props.modelValue ?? null
  editing.value = true
  nextTick(() => inp.value?.focus?.())
}
function close() { editing.value = false }
function norm(v) {
  if (v === '' || v === undefined) return null
  if (props.col.type === 'int') return v === null ? null : Number(v)
  return v
}
// fromEnter：回车才算「这一格填完了、可以提交整行」。**必须显式传参**，不能靠事件冒泡猜——
// GotionCell 在 keydown 阶段就 commit 并把 editing 置 false，Vue 在 keydown 与 keyup 之间
// 已经把 <el-input v-if="editing"> 卸载、焦点回落 body，父层 <tr @keyup.enter> 再也收不到事件。
function commit(fromEnter = false) {
  if (!editing.value) return          // 防 @blur 与 @keydown.enter 重复提交 → 重复 PATCH
  const raw = editVal.value
  // 数值列基本校验：非法值不提交、保持编辑态（避免脏字符串发后端）
  if ((props.col.type === 'decimal' || props.col.type === 'int')
      && raw !== '' && raw !== null && raw !== undefined && Number.isNaN(Number(raw))) {
    ElMessage.warning('请输入数字')
    return
  }
  editing.value = false
  const v = norm(raw)
  // 必填列兜底：不管是哪条路径把值清空的（✕、全选删、粘贴空串），都不往后端发 null。
  // 后端也有同样的闸门并会返回可读的 422，这里只是不让用户白跑一趟。
  if (v === null && props.col.clearable === false) return
  if (String(v ?? '') !== String(props.modelValue ?? '')) emit('change', v)
  if (fromEnter) emit('submit')       // change 是同步派发，此刻草稿已经写进父层的 newRow
}
function choose(o) {
  editing.value = false
  if (o !== props.modelValue) emit('change', o)
}
</script>

<style scoped>
/* 锁定：格子发灰 + 不可点，但内部标签保持原色（见模板注释） */
.gtn-disp.sel.locked { cursor: not-allowed; opacity: .85; }
.gtn-disp { height: 36px; padding: 0 8px; display: flex; align-items: center; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gtn-disp.sel { cursor: pointer; }
.gtn-disp:hover { background: rgba(24, 144, 255, 0.06); }
.ph { color: #5b6880; }
.derived { color: #8fb8ff; }

.gtn-opts { display: flex; flex-direction: column; gap: 4px; }
.gtn-opt { display: flex; align-items: center; padding: 4px 6px; border-radius: 4px; cursor: pointer; }
.gtn-opt:hover { background: #1b2942; }
.gtn-opt.active { background: rgba(24, 144, 255, 0.15); }
.gtn-ck { margin-left: auto; color: #67c23a; font-size: 14px; }
.gtn-opt.clear { color: #9ba8bf; font-size: 12px; border-top: 1px solid #28354a; }
.gtn-opt.clear:hover { color: #f56c6c; }

/* 长文本弹窗编辑：宽 textarea，失焦自动保存 */
.gtn-long-in { width: 100% !important; }
.gtn-long-in :deep(.el-textarea__inner) { font-size: 13px; }

/* 无边框内嵌输入，像直接在格子里打字（对冲全局 .el-input{width:180px}） */
.gtn-in { width: 100% !important; }
.gtn-in :deep(.el-input__wrapper),
.gtn-in :deep(.el-input-number) { box-shadow: none !important; background: transparent; width: 100% !important; }
.gtn-in :deep(.el-input__inner) { height: 34px; font-size: 13px; color: inherit; text-align: left; }
</style>
