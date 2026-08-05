<template>
  <div class="gtn">
    <div v-if="$slots.toolbar" class="gtn-toolbar"><slot name="toolbar" /></div>

    <div class="gtn-scroll" v-loading="loading">
      <table class="gtn-table" :style="{ width: totalWidth + 'px' }">
        <colgroup>
          <col :style="{ width: ID_COL_W + 'px' }" />
          <col v-if="hasActions" :style="{ width: actionsWidth + 'px' }" />
          <col v-if="expandable" :style="{ width: EXPAND_COL_W + 'px' }" />
          <col v-for="col in cols" :key="col.key" :style="{ width: (col.width || col.minWidth || DEFAULT_COL_W) + 'px' }" />
        </colgroup>

        <thead>
          <tr>
            <th class="gtn-th gtn-th-id">{{ hideId ? '' : 'ID' }}</th>
            <th v-if="hasActions" class="gtn-th gtn-th-act">操作</th>
            <th v-if="expandable" class="gtn-th"></th>
            <th v-for="col in cols" :key="col.key" class="gtn-th"
                :class="{ drag: !!tableName, dragging: dragKey === col.key, dragover: overKey === col.key }"
                :draggable="!!tableName && layoutReady"
                @dragstart="onDragStart($event, col)" @dragover.prevent="onDragOver(col)"
                @dragleave="onDragLeave(col)" @drop="onDrop(col)" @dragend="onDragEnd">
              <div class="gtn-th-inner">
                <span class="gtn-col-name">{{ col.label }}</span>
                <el-popover v-if="col.type === 'tag'" trigger="click" :width="260" placement="bottom-start" @show="onTagShow(col.field)">
                  <template #reference>
                    <el-icon class="gtn-tagcfg" title="管理标签" @click.stop @mousedown.stop @dragstart.prevent.stop><Setting /></el-icon>
                  </template>
                  <div class="gtn-tagmgr" @mousedown.stop @click.stop>
                    <div class="gtn-tagmgr-title">{{ col.label }} · 标签（列头管理）</div>
                    <div class="gtn-tag-list">
                      <span v-for="v in (tagOptions[col.field] || [])" :key="v" class="gtn-tag-item">
                        <el-tag size="small"
                                :style="tagStyleAt(tagMeta[col.field]?.[v]?.color ?? -1, v)"
                                :closable="!(tagMeta[col.field]?.[v]?.in_use)"
                                :title="tagMeta[col.field]?.[v]?.in_use ? '使用中，不可删除' : ''"
                                @close="removeTag(col.field, v)">{{ v }}</el-tag>
                        <el-icon class="gtn-tag-edit" title="改色" @click="togglePalette(col.field, v)"><Brush /></el-icon>
                        <el-icon class="gtn-tag-edit" title="改名" @click="renameTag(col.field, v)"><Edit /></el-icon>
                      </span>
                      <span v-if="!(tagOptions[col.field] || []).length" class="gtn-tag-empty">暂无标签</span>
                    </div>
                    <div v-if="paletteFor[col.field]" class="gtn-palette-bar">
                      <span class="gtn-palette-label">「{{ paletteFor[col.field] }}」配色</span>
                      <span v-for="i in TAG_PALETTE.length" :key="i" class="gtn-swatch"
                            :class="{ active: tagMeta[col.field]?.[paletteFor[col.field]]?.color === i - 1 }"
                            :style="tagStyleAt(i - 1, '')"
                            @click="setColor(col.field, paletteFor[col.field], i - 1)" />
                    </div>
                    <div v-if="Object.values(tagMeta[col.field] || {}).some((t) => t.in_use)" class="gtn-tag-hint">
                      使用中的标签不可删除
                    </div>
                    <div class="gtn-tag-add">
                      <el-input v-model="newTag[col.field]" size="small" placeholder="新标签名" @keyup.enter="addTag(col.field)" />
                      <el-button size="small" type="primary" @click="addTag(col.field)">添加</el-button>
                    </div>
                  </div>
                </el-popover>
                <div v-if="tableName && layoutReady" class="gtn-resize" title="拖动改列宽"
                     @mousedown.stop.prevent="startResize($event, col)" @dragstart.prevent.stop />
              </div>
            </th>
          </tr>
        </thead>

        <tbody>
          <!-- 幽灵新建行：放最上，与「最新在上」一致；任意格输入即建行 -->
          <tr v-if="addable" class="gtn-new">
            <td class="gtn-td-id gtn-new-num" title="新建一行" @click="commitNew">
              <el-icon><component :is="hasNew ? Check : Plus" /></el-icon>
            </td>
            <td v-if="hasActions"></td>
            <td v-if="expandable"></td>
            <td v-for="col in cols" :key="'n-' + col.key" class="gtn-td">
              <GotionCell v-if="!col.readonly && !$slots['cell-' + col.key]" new-row
                          :model-value="newRow[col.key]" :col="cellCol(col)" @change="(v) => onNew(col, v)" />
              <!-- 只读/自定义列在新建行显其占位提示（如「集运订单」显「选择」）；无占位则留空 -->
              <div v-else-if="col.placeholder" class="gtn-new-ph">{{ col.placeholder }}</div>
            </td>
          </tr>

          <template v-for="row in rows" :key="row.id">
            <tr class="gtn-row">
              <td class="gtn-td-id">
                <span v-if="!hideId" class="num">{{ row.id }}</span>
                <el-icon v-if="deletable" class="del" title="删除此行" @click="$emit('delete', row)"><Delete /></el-icon>
              </td>
              <td v-if="hasActions" class="gtn-td gtn-td-act"><slot name="actions" :row="row" /></td>
              <td v-if="expandable" class="gtn-td-exp" @click="toggle(row.id)">
                <el-icon class="chev" :class="{ open: open.has(row.id) }"><ArrowRight /></el-icon>
              </td>
              <td v-for="col in cols" :key="col.key" class="gtn-td"
                  :class="{ 'gtn-td-clickexp': col.expand && expandable }"
                  @click="col.expand && expandable ? toggle(row.id) : null">
                <div v-if="$slots['cell-' + col.key]" class="gtn-slot"><slot :name="'cell-' + col.key" :row="row" /></div>
                <GotionCell v-else :model-value="row[col.key]" :col="cellCol(col)" @change="(v) => $emit('save', row, col.key, v)" />
              </td>
            </tr>
            <tr v-if="expandable && open.has(row.id)" class="gtn-exp-row">
              <td :colspan="colspan"><slot name="expand" :row="row" /></td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, useSlots, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowRight, Brush, Check, Delete, Edit, Plus, Setting } from '@element-plus/icons-vue'
import GotionCell from './GotionCell.vue'
import { layoutApi, tagsApi } from '@/api'
import { TAG_PALETTE, tagStyleAt } from '@/constants'

const props = defineProps({
  columns: { type: Array, required: true },
  rows: { type: Array, required: true },
  loading: Boolean,
  expandable: Boolean,
  addable: { type: Boolean, default: true },
  deletable: { type: Boolean, default: true },   // false = 隐藏每行删除图标（只读列表用）
  actionsWidth: { type: [Number, String], default: 72 },
  tableName: { type: String, default: '' },   // 有则启用列拖拽/拖宽 + 后端持久化
  openId: { type: [Number, String], default: null },   // 设置后自动展开该 id 的行（供跨页跳转定位）
  hideId: { type: Boolean, default: false },   // 隐藏最左「ID」列的编号与表头（仍保留该窄列的新建/删除操作）
})
const emit = defineEmits(['save', 'add', 'delete', 'reload'])
const slots = useSlots()

const DEFAULT_COL_W = 160   // 无显式 width 的列默认宽
const ID_COL_W = 56         // 最左 ID/删除/新建 列
const EXPAND_COL_W = 30     // 展开箭头列
const layoutReady = ref(false)   // 列布局拉取完成前禁用拖拽/拖宽，避免用默认序覆盖已存布局

const open = reactive(new Set())
const newRow = reactive({})
const cols = ref([])
let savedLayout = []
const tagOptions = reactive({})   // { field: [值...] } 标签下拉集（供 select 选项）
const tagMeta = reactive({})      // { field: { 值: {color, in_use} } } 每标签的颜色序号 + 是否在用
const newTag = reactive({})       // { field: 输入中的新标签名 }
const paletteFor = reactive({})   // { field: 正在改色的标签值 | null }（列头调色盘一次开一个）

const hasActions = computed(() => !!slots.actions)
const colspan = computed(() => 1 + (props.expandable ? 1 : 0) + cols.value.length + (hasActions.value ? 1 : 0))
const totalWidth = computed(() =>
  ID_COL_W + (props.expandable ? EXPAND_COL_W : 0)
  + cols.value.reduce((s, c) => s + (c.width || c.minWidth || DEFAULT_COL_W), 0)
  + (hasActions.value ? Number(props.actionsWidth) : 0),
)
const hasNew = computed(() =>
  Object.keys(newRow).some((k) => { const v = newRow[k]; return v !== null && v !== undefined && v !== '' }),
)

// —— 用后端保存的顺序/宽度重排代码里定义的列 ——
function buildCols() {
  const base = props.columns
  if (!savedLayout.length) { cols.value = base.map((c) => ({ ...c })); return }
  const order = savedLayout.map((l) => l.key)
  const wmap = Object.fromEntries(savedLayout.map((l) => [l.key, l.width]))
  const byKey = Object.fromEntries(base.map((c) => [c.key, c]))
  const out = []
  order.forEach((k) => { if (byKey[k]) out.push({ ...byKey[k], width: wmap[k] ?? byKey[k].width }) })
  base.forEach((c) => { if (!order.includes(c.key)) out.push({ ...c }) })   // 新增列附加在后
  cols.value = out
}
watch(() => props.columns, buildCols)
onMounted(async () => {
  buildCols()
  loadTags()
  if (props.tableName) {
    try {
      // 拉取期间不可能有用户改动：拖拽/拖宽都由 layoutReady 门控，而它到本函数末尾才置 true。
      savedLayout = (await layoutApi.get(props.tableName)).columns || []
      buildCols()
    } catch (_) { /* 忽略：用默认列 */ }
  }
  layoutReady.value = true   // 布局就绪，开放拖拽/拖宽
})
function saveLayout() {
  if (!props.tableName || !layoutReady.value) return   // 布局未就绪不保存，避免用默认序覆盖已存布局
  // 只有 minWidth、没有 width 的列：持久化其 minWidth（而非默认 160），否则一拖动排序就把
  // 这些列的宽度悄悄写成 160、并永久盖掉原本的 minWidth。
  savedLayout = cols.value.map((c) => ({ key: c.key, width: Math.round(c.width || c.minWidth || DEFAULT_COL_W) }))
  layoutApi.save(props.tableName, savedLayout).catch(() => {})
}

// —— 标签列：渲染成 select，选项 + 每标签颜色/是否在用 来自可管理的标签集 ——
function cellCol(col) {
  return col.type === 'tag'
    ? { ...col, type: 'select', options: tagOptions[col.field] || [], tagMeta: tagMeta[col.field] || {}, tagColored: true }
    : col
}
function applyTags(field, list) {   // list: [{value, color, in_use}]
  tagOptions[field] = list.map((t) => t.value)
  tagMeta[field] = Object.fromEntries(list.map((t) => [t.value, t]))
}
async function loadTag(field) {   // 单字段拉取；失败保留旧值/置空，供列头弹窗 @show 重试
  try { applyTags(field, await tagsApi.list(field)) } catch (_) { if (!tagOptions[field]) { tagOptions[field] = []; tagMeta[field] = {} } }
}
function onTagShow(field) { paletteFor[field] = null; loadTag(field) }   // 每次开弹窗：收起调色盘 + 拉最新标签
function togglePalette(field, value) { paletteFor[field] = paletteFor[field] === value ? null : value }
async function setColor(field, value, color) {   // 改某标签的颜色（调色盘 10 色之一），即时刷新
  try { applyTags(field, await tagsApi.setColor(field, value, color)); paletteFor[field] = null } catch (_) { /* 拦截器已提示 */ }
}
async function loadTags() {
  const fields = [...new Set(props.columns.filter((c) => c.type === 'tag').map((c) => c.field))]
  await Promise.all(fields.map((f) => loadTag(f)))
}
async function addTag(field) {
  const v = (newTag[field] || '').trim()
  if (!v) return
  try { applyTags(field, await tagsApi.add(field, v)); newTag[field] = '' } catch (_) { /* 拦截器已提示 */ }
}
async function removeTag(field, value) {
  try {
    applyTags(field, await tagsApi.remove(field, value))
  } catch (e) {
    // 409（使用中不可删）被拦截器刻意跳过，这里自行提示；其余错误拦截器已提示
    if (e.response?.status === 409) ElMessage.warning(e.response?.data?.detail || '该标签使用中，不能删除')
  }
}
async function renameTag(field, oldVal) {
  let value
  try {
    const r = await ElMessageBox.prompt(
      `把标签「${oldVal}」改成新名字。会一并把用到它的订单迁到新名字、并保留颜色。`,
      '标签改名',
      { confirmButtonText: '改名', cancelButtonText: '取消', inputValue: oldVal,
        inputValidator: (v) => (!!v && !!v.trim()) || '名字不能为空' },
    )
    value = r.value.trim()
  } catch (_) { return }   // 取消
  if (!value || value === oldVal) return
  try {
    const res = await tagsApi.rename(field, oldVal, value)
    if (res && res.warning) ElMessage.warning(res.warning)
    else ElMessage.success(`已改名为「${value}」`)
    await loadTag(field)   // 刷新下拉集（颜色/在用状态）
    emit('reload')         // 通知父页重拉行数据，让已用到该标签的单元格显示新值
  } catch (e) {
    // 409（新名占用）被拦截器刻意跳过，这里自行提示；其余错误拦截器已提示
    if (e.response?.status === 409) ElMessage.warning(e.response?.data?.detail || '新名字已被占用')
  }
}

function toggle(id) { open.has(id) ? open.delete(id) : open.add(id) }
// 跨页跳转定位：openId 一变就标记展开；行数据到达后（v-if open.has）即渲染为展开态，天然处理时序。
// 换/清定位时收回上一个「自动展开」（只收自己加的那个，不动用户手动展开的行），避免越积越多。
watch(() => props.openId, (id, prev) => {
  if (prev !== null && prev !== undefined) open.delete(prev)
  if (id !== null && id !== undefined) open.add(id)
}, { immediate: true })

function onNew(col, value) {
  newRow[col.key] = value
  if (hasNew.value) commitNew()
}
function commitNew() {
  if (!hasNew.value) return
  const data = {}
  Object.keys(newRow).forEach((k) => {
    if (newRow[k] !== null && newRow[k] !== '') data[k] = newRow[k]
    delete newRow[k]
  })
  emit('add', data)
}

// —— 列拖拽换位 ——
const dragKey = ref(null)
const overKey = ref(null)
let resizing = false
function onDragStart(e, col) {
  if (resizing) { e.preventDefault(); return }
  dragKey.value = col.key
  e.dataTransfer.effectAllowed = 'move'
  e.dataTransfer.setData('text/plain', col.key)
}
function onDragOver(col) { if (dragKey.value && dragKey.value !== col.key) overKey.value = col.key }
function onDragLeave(col) { if (overKey.value === col.key) overKey.value = null }
function onDrop(target) {
  const from = cols.value.findIndex((c) => c.key === dragKey.value)
  const to = cols.value.findIndex((c) => c.key === target.key)
  if (from < 0 || to < 0 || from === to) return
  const arr = [...cols.value]
  const [moved] = arr.splice(from, 1)
  arr.splice(to, 0, moved)
  cols.value = arr
  saveLayout()
}
function onDragEnd() { dragKey.value = null; overKey.value = null }

// —— 列拖宽 ——
let rzCol = null
let rzX = 0
let rzW = 0
function startResize(e, col) {
  resizing = true
  rzCol = col
  rzX = e.clientX
  rzW = col.width || col.minWidth || DEFAULT_COL_W   // 从实际渲染宽度起拖（minWidth-only 列否则会先跳到 160）
  window.addEventListener('mousemove', onResize)
  window.addEventListener('mouseup', stopResize)
}
function onResize(e) {
  if (rzCol) rzCol.width = Math.max(60, rzW + e.clientX - rzX)
}
function stopResize() {
  if (rzCol) saveLayout()
  rzCol = null
  resizing = false
  window.removeEventListener('mousemove', onResize)
  window.removeEventListener('mouseup', stopResize)
}
</script>

<style scoped>
.gtn { display: flex; flex-direction: column; }
.gtn-toolbar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.gtn-scroll { overflow: auto; border: 1px solid #28354a; border-radius: 8px; }
.gtn-table { border-collapse: collapse; font-size: 13px; color: #d6deea; table-layout: fixed; }

.gtn-th {
  background: #18233a; border-bottom: 2px solid #28354a; border-right: 1px solid #28354a;
  padding: 0; text-align: left; font-weight: 500; font-size: 12px; color: #c7d2e6;
  position: sticky; top: 0; z-index: 2; white-space: nowrap;
}
.gtn-th-act { text-align: center; padding: 8px 10px; }
.gtn-th-inner { display: flex; align-items: center; padding: 8px 10px; position: relative; }
.gtn-col-name { flex: 1; overflow: hidden; text-overflow: ellipsis; }
.gtn-th.drag { cursor: grab; }
.gtn-th.drag:active { cursor: grabbing; }
.gtn-th.dragging { opacity: 0.4; }
.gtn-th.dragover { box-shadow: inset 2px 0 0 #1890ff; background: #1d2a45; }
.gtn-resize { position: absolute; right: 0; top: 0; bottom: 0; width: 6px; cursor: col-resize; z-index: 3; }
.gtn-resize:hover { background: #3a4a6b; }

.gtn-row:hover td { background: #1b2942; }
.gtn-th-id { text-align: center; padding: 8px 6px; }
.gtn-td-id {
  text-align: center; color: #7a8aa3; font-size: 11px;
  border-bottom: 1px solid #202c44; border-right: 1px solid #28354a; position: relative;
}
.gtn-td-id .del { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); display: none; color: #f56c6c; cursor: pointer; }
.gtn-td-id:hover .num { opacity: 0; }
.gtn-td-id:hover .del { display: inline-flex; }
.gtn-td-exp { text-align: center; border-bottom: 1px solid #202c44; border-right: 1px solid #28354a; cursor: pointer; color: #7d8aa3; }
.gtn-td-exp .chev { transition: transform 0.15s; }
.gtn-td-exp .chev.open { transform: rotate(90deg); }
.gtn-td { height: 36px; padding: 0; border-bottom: 1px solid #202c44; border-right: 1px solid #28354a; max-width: 0; overflow: hidden; }
.gtn-td-act { overflow: visible; text-align: center; }
.gtn-td-clickexp { cursor: pointer; }
.gtn-slot { height: 36px; padding: 0 8px; display: flex; align-items: center; overflow: hidden; white-space: nowrap; }

.gtn-exp-row > td { background: #10192c; border-bottom: 1px solid #28354a; }
.gtn-new td { background: #10192c; border-bottom: 1px solid #202c44; border-right: 1px solid #28354a; }
.gtn-new-num { color: #5c6b85; cursor: pointer; }
.gtn-new-num:hover { color: #67c23a; background: rgba(103, 194, 58, 0.1); }
.gtn-new-ph { height: 36px; padding: 0 8px; display: flex; align-items: center; color: #5b6880; font-size: 13px; }

.gtn-tagcfg { margin-left: 4px; color: #6b7a93; cursor: pointer; font-size: 13px; }
.gtn-tagcfg:hover { color: #67c23a; }
.gtn-tagmgr-title { color: #9ba8bf; font-size: 12px; margin-bottom: 8px; }
.gtn-tag-list { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.gtn-tag-item { display: inline-flex; align-items: center; gap: 2px; }
.gtn-tag-edit { color: #6b7a93; font-size: 12px; cursor: pointer; }
.gtn-tag-edit:hover { color: #8ab4ff; }
.gtn-tag-empty { color: #5b6880; font-size: 12px; }
.gtn-palette-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.gtn-palette-label { color: #9ba8bf; font-size: 12px; margin-right: 2px; }
.gtn-swatch { width: 18px; height: 18px; border-radius: 4px; border: 1px solid; cursor: pointer; box-sizing: border-box; transition: transform .1s; }
.gtn-swatch:hover { transform: scale(1.15); }
.gtn-swatch.active { outline: 2px solid #8ab4ff; outline-offset: 1px; }
.gtn-tag-hint { color: #6b7a93; font-size: 11px; margin-bottom: 8px; }
.gtn-tag-add { display: flex; gap: 6px; }
</style>
