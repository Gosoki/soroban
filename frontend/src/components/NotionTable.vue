<template>
  <div class="gtn">

    <div v-if="$slots.toolbar || $slots['toolbar-right']" class="gtn-toolbar">
      <slot name="toolbar" />
      <!-- 右侧入口（OCR 之类）：靠 flex 间隔顶过去，而不是让每一页自己拼一个
           `<div class="grow" />`——那样每页的写法和间距都会各长各的。 -->
      <div class="gtn-tb-gap" />
      <slot name="toolbar-right" />
    </div>

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
                <!-- 列级说明：任何列声明 help 就长出一个「?」，点开显示。给「口径不直观」的列用
                     （如派生金额为什么和平台显示差几分），免得靠用户去翻文档或猜。 -->
                <el-popover v-if="col.help" trigger="click" :width="340" placement="bottom-start">
                  <template #reference>
                    <el-icon class="gtn-help" title="说明" @click.stop @mousedown.stop
                             @dragstart.prevent.stop><QuestionFilled /></el-icon>
                  </template>
                  <div class="gtn-help-body" @mousedown.stop @click.stop>
                    <div class="gtn-help-title">{{ col.label }}</div>
                    <div class="gtn-help-text">{{ col.help }}</div>
                  </div>
                </el-popover>
                <el-popover v-if="col.type === 'tag'" trigger="click" :width="260" placement="bottom-start" @show="onTagShow(col.field)">
                  <template #reference>
                    <el-icon class="gtn-tagcfg" title="管理标签" @click.stop @mousedown.stop @dragstart.prevent.stop><Setting /></el-icon>
                  </template>
                  <div class="gtn-tagmgr" @mousedown.stop @click.stop>
                    <div class="gtn-tagmgr-title">{{ col.label }} · 标签（列头管理）</div>
                    <div class="gtn-tag-list">
                      <span v-for="v in (tagOptions[col.field] || [])" :key="v" class="gtn-tag-item">
                        <el-tag
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
                      <el-input v-model="newTag[col.field]" placeholder="新标签名" @keyup.enter="addTag(col.field)" />
                      <el-button type="primary" @click="addTag(col.field)">添加</el-button>
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
          <!-- 幽灵新建行：填格子只攒草稿，按回车或点左侧 ✓ 才落库（见 commitNew） -->
          <tr v-if="addable" class="gtn-new">
            <td class="gtn-td-id gtn-new-num" :class="{ ready: hasNew, busy: committing }"
                :title="committing ? '正在提交…'
                  : hasNew ? '提交新建这一行（或在格子里按回车）' : '在右侧格子里填内容即可新建一行'"
                @click="commitNew">
              <el-icon :class="{ spin: committing }">
                <component :is="committing ? Loading : hasNew ? Check : Plus" />
              </el-icon>
            </td>
            <td v-if="hasActions"></td>
            <td v-if="expandable"></td>
            <td v-for="col in cols" :key="'n-' + col.key" class="gtn-td">
              <!-- newEditable：数据行只读、**新建行可填**。用于「派生值，但新建时需要一个种子」
                   的列——如订单的人民币：平时由物品单价×数量派生、不许直接改，
                   但新建一单时总得有个地方把金额填进去。 -->
              <GotionCell v-if="(!col.readonly || col.newEditable) && !$slots['cell-' + col.key]" new-row
                          :model-value="newRow[col.key]" :col="newCol(col)"
                          @change="(v) => onNew(col, v)" @submit="commitNew" />
              <!-- 只读/自定义列在新建行显其占位提示（如「集运订单」显「选择」）；无占位则留空 -->
              <div v-else-if="col.placeholder" class="gtn-new-ph">{{ col.placeholder }}</div>
            </td>
          </tr>

          <!-- 零结果：给一行文案，而不是一个带边框、有列头、里面什么都没有的方块。
               「加载中 / 没有数据 / 筛没了」三种状态在界面上得分得开——
               Items 页 `addable=false`，筛空时 tbody 会完全为空，最像坏了。 -->
          <tr v-if="!loading && !rows.length" class="gtn-empty">
            <td :colspan="emptyColspan">{{ emptyText }}</td>
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
                  :class="{ 'gtn-td-clickexp': col.expand && expandable, 'gtn-td-locked': isLocked(col, row) }"
                  :title="isLocked(col, row) ? (col.lockHint || '') : null"
                  @click="col.expand && expandable ? toggle(row.id) : null">
                <div v-if="$slots['cell-' + col.key]" class="gtn-slot"><slot :name="'cell-' + col.key" :row="row" /></div>
                <GotionCell v-else :model-value="cellValue(col, row)" :col="cellCol(col)"
                            :locked="isLocked(col, row)" @change="(v) => $emit('save', row, col.key, v)" />
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
import { ArrowRight, Brush, Check, Delete, Edit, Loading, Plus, QuestionFilled, Setting } from '@element-plus/icons-vue'
import GotionCell from './GotionCell.vue'
import { layoutApi, tagsApi } from '@/api'
import { handled } from '@/api/http'
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
  emptyText: { type: String, default: '没有符合条件的记录' },   // 零结果时的一行文案
})
const emit = defineEmits(['save', 'add', 'delete', 'reload', 'tags-changed'])
const slots = useSlots()

// 空态那一行要横跨整表。列数 = ID 列 + 可选展开列 + 数据列 + 操作列，
// 少算一列的话空态文案会挤在左边、右边留一段空表格，比没有还难看。
// **操作列是可选的**（没有 `#actions` 插槽的页面就没有这一列）——原先这里无条件 +1，
// 于是 Orders / Shipment / Misc 三页恒多算一列。浏览器会把越界的 colspan 截断，
// 所以看不出来，但这个值是被当成「要算准」的东西写的。
// 数的是 `props.columns` 而不是 `cols.value`：后者要等列布局拉回来才有值，
// 而空态在那之前就可能渲染。下面的 `colspan` 用于布局已就绪之后，故用 cols.value。
const emptyColspan = computed(
  () => 1 + (props.expandable ? 1 : 0) + props.columns.length + (hasActions.value ? 1 : 0))

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

// 按**行**锁定某一格：col.lock(row) 返回真即锁。列级只读仍用 col.readonly。
// 用于「这格的值由别处决定」——如订单挂上集运单后状态跟随那张单，本格不该再能改。
function isLocked(col, row) { return typeof col.lock === 'function' && !!col.lock(row) }
// 展示值可以不等于 row[col.key]：col.display(row) 给一个派生值（如状态显示继承来的集运状态），
// 但**写回仍走 col.key**——显示与存储各归各的，别把派生值写进原字段。
function cellValue(col, row) {
  return typeof col.display === 'function' ? col.display(row) : row[col.key]
}

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
// 新建行用的列配置：把 readonly 摘掉（否则 GotionCell 直接渲染成只读展示）
function newCol(col) {
  const c = cellCol(col)
  return col.newEditable ? { ...c, readonly: false } : c
}
// 标签列的派生配置**必须缓存**：`cellCol` 写在模板里（`:col="cellCol(col)"`），
// 而它对标签列每次调用都返回一个**新对象** ⇒ 这些 `GotionCell` 的 props 在父组件
// 每次重渲时都判定为「变了」（Vue 的 props diff 是浅比较）。
// 商品订单页 2 个标签列 × 30 行 = 60 个格子：点一下展开箭头、改一个格子、翻一页
// 都会让它们全部重渲并重算颜色；拖列宽时更是**每帧一次**。
// 用 computed 建一张按列 key 的表：只要 tagOptions/tagMeta 没变，对象身份就稳定。
const tagColCache = computed(() => {
  const out = {}
  for (const c of props.columns) {
    if (c.type === 'tag') {
      out[c.key] = { ...c, type: 'select', options: tagOptions[c.field] || [],
                     tagMeta: tagMeta[c.field] || {}, tagColored: true }
    }
  }
  return out
})
function cellCol(col) {
  return col.type === 'tag' ? (tagColCache.value[col.key] || col) : col
}
function applyTags(field, list) {   // list: [{value, color, in_use}]
  tagOptions[field] = list.map((t) => t.value)
  tagMeta[field] = Object.fromEntries(list.map((t) => [t.value, t]))
  // **所有标签变更的唯一汇合点，事件就发在这里。**
  // 表格自己那份 `tagOptions` 刷新了，但父页往往还各存一份同样的候选集
  // （工具栏的筛选下拉、编辑面板的 `:accounts`）——那些不会自动跟着变。
  // 后果分两种，都很难自己想明白：
  //   · 改名：`tags.py` 会 `UPDATE ... SET col=新 WHERE col=旧`，**旧名在库里彻底消失**。
  //     若工具栏筛选此刻正停在旧名，紧接着的 `load()` 拿一个不存在的值精确匹配 → 0 行 →
  //     空态显示「没有符合条件的记录」——**刚改完名就像把单子改没了**。
  //   · 新增/删除/改色：原先**一个事件都不发**，新加的账号在工具栏筛选和编辑面板里
  //     永远选不到，直到组件重挂载（切页回来才自愈）。
  // 只发 `reload` 不够——它只让父页重拉**行**，既不刷新候选集、也不管仍停在旧名的筛选值。
  emit('tags-changed', { field, values: tagOptions[field] })
}
async function loadTag(field) {   // 单字段拉取；失败**保持未拉到**（见下），供列头弹窗 @show 重试
  // 原先失败时会把候选集**固化成空数组**，而 `loadTags()` 只在 onMounted 跑一次、
  // 此后没有任何自动重试：后端刚起（或迁移期返 503）时进页面，用户看到的是
  // 「我的账号标签全没了，但旧数据还照常带色显示」——实际只是那一次 GET 挂了。
  // 留 `undefined` 表示「还没拿到」：渲染侧对 undefined 与 [] 处理相同
  // （`tagOptions[col.field] || []`），所以不置空没有副作用，而 `loadTags` 能据此重试。
  try {
    applyTags(field, await tagsApi.list(field))
    return true
  } catch (_) {
    return false
  }
}
function onTagShow(field) { paletteFor[field] = null; loadTag(field) }   // 每次开弹窗：收起调色盘 + 拉最新标签
function togglePalette(field, value) { paletteFor[field] = paletteFor[field] === value ? null : value }
async function setColor(field, value, color) {   // 改某标签的颜色（调色盘 10 色之一），即时刷新
  try { applyTags(field, await tagsApi.setColor(field, value, color)); paletteFor[field] = null } catch (_) { /* 拦截器已提示 */ }
}
async function loadTags() {
  const fields = [...new Set(props.columns.filter((c) => c.type === 'tag').map((c) => c.field))]
  const ok = await Promise.all(fields.map((f) => loadTag(f)))
  // **失败重试一次。** 这一路只在 onMounted 跑，而它最常见的失败恰恰是「后端刚起来」
  // 或迁移期的 503 ——两者都是几秒内自愈的。不重试的话，用户要么点列头的 ⚙、
  // 要么切页再回来，两条自愈路径都不显眼。只重试一次：真的连不上时，
  // 拦截器那条全屏离线提示才是该出场的东西，不该由这里反复刷。
  const failed = fields.filter((_, i) => !ok[i])
  if (failed.length) setTimeout(() => failed.forEach(loadTag), 3000)
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
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '该标签使用中，不能删除') }
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
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '新名字已被占用') }
  }
}

function toggle(id) { open.has(id) ? open.delete(id) : open.add(id) }
// 跨页跳转定位：openId 一变就标记展开；行数据到达后（v-if open.has）即渲染为展开态，天然处理时序。
// 换/清定位时收回上一个「自动展开」（只收自己加的那个，不动用户手动展开的行），避免越积越多。
watch(() => props.openId, (id, prev) => {
  if (prev !== null && prev !== undefined) open.delete(prev)
  if (id !== null && id !== undefined) open.add(id)
}, { immediate: true })

// 幽灵新建行：填格子只是攒草稿，**不落库**。落库要么点左侧的 ✓、要么在格子里按回车。
//
// 原先是「任意一格 change 就立刻 emit('add') 并清空 newRow」，有两个后果：
// ① 横着填三格 = 连建三条各只有一个字段的记录（每条还带今天日期+默认状态），得逐条确认删掉；
// ② 表头那颗 hasNew ? Check : Plus 图标永远停在 Plus——为「多格填完再提交」设计的路径是死代码。
function onNew(col, value) {
  newRow[col.key] = value
}
const committing = ref(false)
function commitNew() {
  // committing 门闩：草稿要等 POST 回来才清空，在途这段时间 hasNew 仍为真，
  // 再点一次 ✓（或再按一次回车）就会把同一份草稿再 POST 一遍 → 两条重复订单。
  if (!hasNew.value || committing.value) return
  const data = {}
  Object.keys(newRow).forEach((k) => {
    if (newRow[k] !== null && newRow[k] !== '') data[k] = newRow[k]
  })
  committing.value = true
  let settled = false
  // done 在契约上是**可选**的（父页写的是 done?.(true)）。哪个父页将来早退没调它，
  // 只在 done 里解锁就会让幽灵行永久点不动——那比偶发多建一条更糟。故加超时兜底。
  //
  // **20s 是错的。** 原注释算的是「20s > api 层 15s 默认超时」，但父页的 `addRow` 在
  // `done` 之前还 `await afterCreate(...)`，而它在「有筛选 / 不在第 1 页」那一支里会
  // 再 `await load()` —— **两次**请求，上界 15+15=30s。
  // 早于上界触发的后果不是注释里说的「短暂锁一会儿」：`finish(false)` 会解锁并
  // **留下草稿**（`ok !== false` 才清），用户看着字还在、以为没存上，再按一次回车 ——
  // 而杂项支出**没有任何唯一约束**，同一笔钱就这么记两遍。
  // 所以时长抬到两次请求之上，并且超时那一支要**说一句话**：此刻结果是未知的，
  // 默默解锁等于邀请用户重复提交。
  const timer = setTimeout(() => finish(false, true), 35000)
  function finish(ok, timedOut = false) {
    if (settled) return
    settled = true
    clearTimeout(timer)
    committing.value = false
    if (timedOut) {
      ElMessage.warning('等了很久还没收到结果。先别重复提交——刷新看看是不是已经存上了')
    }
    // 成功才清空草稿：失败（如订单号撞唯一约束）时把用户刚敲的内容留在格子里让他改，
    // 而不是连人带字一起吞掉。
    if (ok !== false) Object.keys(newRow).forEach((k) => delete newRow[k])
  }
  emit('add', data, finish)
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
/* align-items: center 是**必需的**，不是审美：flex 默认 stretch，会把每个控件拉到
   与本行最高者等高。工具栏里混着输入框(24px)和日期范围选择器(32px)，于是搜索框被
   悄悄拉成 32px——同一行里两种高度，而且只在带日期筛选的页面上出现，很难联想到原因。 */
.gtn-empty td { padding: 22px 12px; text-align: center; color: var(--txt-3); font-size: 13px; }
/* 筛选栏的控件比表格里的高一档（24 → 30）。表格单元格要密，筛选栏是要**点**的，
   24px 在 1500px 宽的屏幕上又矮又挤。
   改 Element 的尺寸变量而不是各页去写 size="default"（那是 32px，跳得太多，
   而且要在五个页面的每一个控件上都写一遍）。
   ⚠️ select 必须单独补一条：Element 把它的 min-height 按尺寸档**写死**在类里，
   不读 --el-component-size-small——只设变量的话，输入框和日期变高了、下拉还是 24，
   一排控件参差不齐（实测：[30, 24, 24, 24, 30]）。 */
.gtn-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px;
               --el-component-size-small: 30px; }
.gtn-toolbar :deep(.el-select__wrapper) { min-height: var(--el-component-size-small); }
/* 按钮同理：`.el-button--small` 的 height 也是写死的。不补这条，
   右侧的 OCR 按钮会是 24px，和左边一排 30px 的筛选框对不齐（实测 [30, 24]）。 */
.gtn-toolbar :deep(.el-button) { height: var(--el-component-size-small); }
.gtn-tb-gap { flex: 1; }
/* background 是**去掉外层 el-card 之后补的**：表格页现在不再套卡片，
   这圈边框自己就是容器，行底色必须由它给，否则整表掉到页面底 --bg-page（深一档），
   而表头（--bg-hover）还亮着，看起来像表头浮在一个洞上。 */
.gtn-scroll { overflow: auto; border: 1px solid var(--border); border-radius: 8px;
              background: var(--bg-card); }
.gtn-table { border-collapse: collapse; font-size: 13px; color: var(--txt-body); table-layout: fixed; }

.gtn-th {
  background: var(--bg-hover); border-bottom: 2px solid var(--border); border-right: 1px solid var(--border);
  padding: 0; text-align: left; font-weight: 500; font-size: 12px; color: var(--txt-body);
  position: sticky; top: 0; z-index: 2; white-space: nowrap;
}
.gtn-th-act { text-align: center; padding: 8px 10px; }
.gtn-th-inner { display: flex; align-items: center; padding: 8px 10px; position: relative; }
.gtn-col-name { flex: 1; overflow: hidden; text-overflow: ellipsis; }
.gtn-th.drag { cursor: grab; }
.gtn-th.drag:active { cursor: grabbing; }
.gtn-th.dragging { opacity: 0.4; }
.gtn-th.dragover { box-shadow: inset 2px 0 0 var(--brand); background: #1d2a45; }
.gtn-resize { position: absolute; right: 0; top: 0; bottom: 0; width: 6px; cursor: col-resize; z-index: 3; }
.gtn-resize:hover { background: var(--border-strong); }

.gtn-row:hover td { background: var(--bg-row-hover); }
.gtn-th-id { text-align: center; padding: 8px 6px; }
.gtn-td-id {
  text-align: center; color: #7a8aa3; font-size: 11px;
  border-bottom: 1px solid var(--border-soft); border-right: 1px solid var(--border); position: relative;
}
.gtn-td-id .del { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); display: none; color: var(--danger); cursor: pointer; }
.gtn-td-id:hover .num { opacity: 0; }
.gtn-td-id:hover .del { display: inline-flex; }
.gtn-td-exp { text-align: center; border-bottom: 1px solid var(--border-soft); border-right: 1px solid var(--border); cursor: pointer; color: var(--txt-3); }
.gtn-td-exp .chev { transition: transform 0.15s; }
.gtn-td-exp .chev.open { transform: rotate(90deg); }
.gtn-td { height: 36px; padding: 0; border-bottom: 1px solid var(--border-soft); border-right: 1px solid var(--border); max-width: 0; overflow: hidden; }
.gtn-td-act { overflow: visible; text-align: center; }
.gtn-td-clickexp { cursor: pointer; }
.gtn-slot { height: 36px; padding: 0 8px; display: flex; align-items: center; overflow: hidden; white-space: nowrap; }

.gtn-exp-row > td { background: var(--bg-sunken); border-bottom: 1px solid var(--border); }
.gtn-new td { background: var(--bg-sunken); border-bottom: 1px solid var(--border-soft); border-right: 1px solid var(--border); }
.gtn-new-num { color: #5c6b85; cursor: pointer; }
.gtn-new-num:hover { color: var(--ok); background: var(--ok-faint); }
/* 草稿已有内容 → ✓ 亮起，提示「可以提交了」 */
.gtn-new-num.ready { color: var(--ok); background: var(--ok-weak); }
/* 提交在途：点不动 + 转圈。在途零反馈正是用户第二次点击、建出重复单的成因 */
.gtn-new-num.busy { color: var(--txt-3); background: transparent; pointer-events: none; cursor: progress; }
.gtn-new-num .spin { animation: gtn-spin 1s linear infinite; }
@keyframes gtn-spin { to { transform: rotate(360deg); } }
.gtn-new-ph { height: 36px; padding: 0 8px; display: flex; align-items: center; color: var(--txt-3); font-size: 13px; }

.gtn-tagcfg { margin-left: 4px; color: var(--txt-3); cursor: pointer; font-size: 13px; }
/* 锁定格：整格发灰、点不动；内部标签仍是原色（见 GotionCell 注释） */
.gtn-td-locked { background: var(--el-fill-color-light); cursor: not-allowed; }
.gtn-td-locked .gtn-disp, .gtn-td-locked .gtn-slot { opacity: .72; pointer-events: none; }
.gtn-td-locked .el-tag { opacity: 1; }
.gtn-help { margin-left: 4px; color: var(--txt-3); cursor: help; font-size: 13px; }
.gtn-help:hover { color: var(--el-color-primary); }
.gtn-help-title { font-weight: 600; margin-bottom: 6px; }
.gtn-help-text { font-size: 12px; line-height: 1.75; color: var(--el-text-color-regular); white-space: pre-line; }
.gtn-tagcfg:hover { color: var(--ok); }
.gtn-tagmgr-title { color: var(--txt-2); font-size: 12px; margin-bottom: 8px; }
.gtn-tag-list { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.gtn-tag-item { display: inline-flex; align-items: center; gap: 2px; }
.gtn-tag-edit { color: var(--txt-3); font-size: 12px; cursor: pointer; }
.gtn-tag-edit:hover { color: #8ab4ff; }
.gtn-tag-empty { color: var(--txt-3); font-size: 12px; }
.gtn-palette-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.gtn-palette-label { color: var(--txt-2); font-size: 12px; margin-right: 2px; }
.gtn-swatch { width: 18px; height: 18px; border-radius: 4px; border: 1px solid; cursor: pointer; box-sizing: border-box; transition: transform .1s; }
.gtn-swatch:hover { transform: scale(1.15); }
.gtn-swatch.active { outline: 2px solid #8ab4ff; outline-offset: 1px; }
.gtn-tag-hint { color: var(--txt-3); font-size: 11px; margin-bottom: 8px; }
.gtn-tag-add { display: flex; gap: 6px; }
</style>
