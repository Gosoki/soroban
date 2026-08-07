<template>
  <div class="set-page" v-loading="loading">
    <h2 class="title">设置</h2>
    <p class="lead">
      这里改的是**业务偏好**，存在数据库里、即时生效。密钥、监听地址、数据库连接串那些属于部署配置，
      在 <code>backend/.env</code> 里改、要重启，不在这一页。
    </p>

    <!-- 汇率 -->
    <el-card shadow="never" class="card">
      <div class="card-hd">
        <span>汇率</span>
        <span class="cur" v-if="fx.rate">
          当前：1元 = {{ fx.rate }}円
          <el-tag size="small" :style="typeStyle(fx.fallback ? 'info' : 'success')">
            {{ fx.source_label || fx.source }}
          </el-tag>
          <el-button link type="primary" size="small" :loading="refreshing" @click="doRefresh">
            立即刷新
          </el-button>
        </span>
      </div>

      <!-- 源优先级 -->
      <div class="field col">
        <label class="flabel">
          {{ spec('fx.sources').label }}
          <el-tooltip :content="spec('fx.sources').hint" placement="top">
            <el-icon class="help"><QuestionFilled /></el-icon>
          </el-tooltip>
        </label>
        <div class="chain">
          <div v-for="(s, i) in draft['fx.sources']" :key="s" class="chain-row">
            <span class="ord">{{ i + 1 }}</span>
            <span class="sname">{{ SOURCE_NAMES[s] || s }}</span>
            <el-tag v-if="i === 0" size="small" :style="typeStyle('success')">首选</el-tag>
            <span class="sdesc">{{ SOURCE_DESC[s] }}</span>
            <div class="grow" />
            <el-button link :icon="Top" :disabled="i === 0" title="上移" @click="move(i, -1)" />
            <el-button link :icon="Bottom" :disabled="i === draft['fx.sources'].length - 1"
                       title="下移" @click="move(i, 1)" />
            <el-button link type="danger" :icon="Delete" :disabled="draft['fx.sources'].length <= 1"
                       title="不使用这个源" @click="drop(i)" />
          </div>
          <div v-if="unused.length" class="chain-add">
            未启用：
            <el-button v-for="s in unused" :key="s" size="small" :icon="Plus" @click="add(s)">
              {{ SOURCE_NAMES[s] || s }}
            </el-button>
          </div>
        </div>
      </div>

      <!-- 数值项 -->
      <div v-for="k in ['fx.attempts', 'fx.fallback_hours', 'fx.refresh_seconds']" :key="k" class="field">
        <label class="flabel">
          {{ spec(k).label }}
          <el-tooltip :content="spec(k).hint" placement="top">
            <el-icon class="help"><QuestionFilled /></el-icon>
          </el-tooltip>
        </label>
        <el-input-number v-model="draft[k]" :min="spec(k).min" :max="spec(k).max"
                         :controls="false" size="small" style="width: 130px" />
        <span class="sub">{{ extraHint(k) }}</span>
      </div>

      <!-- 中行口径 -->
      <div class="field">
        <label class="flabel">
          {{ spec('fx.boc_column').label }}
          <el-tooltip :content="spec('fx.boc_column').hint" placement="top">
            <el-icon class="help"><QuestionFilled /></el-icon>
          </el-tooltip>
        </label>
        <el-select v-model="draft['fx.boc_column']" size="small" style="width: 160px"
                   :disabled="!draft['fx.sources'].includes('boc')">
          <el-option v-for="c in spec('fx.boc_column').choices || []" :key="c"
                     :label="BOC_COLS[c] || c" :value="c" />
        </el-select>
        <span class="sub">仅在启用「中国银行」时有用</span>
      </div>

      <div class="acts">
        <el-button type="primary" :disabled="!dirty" :loading="saving" @click="save">保存</el-button>
        <el-button :disabled="!dirty" @click="reset">撤销改动</el-button>
        <span v-if="dirty" class="sub warn">有未保存的改动</span>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Bottom, Delete, Plus, QuestionFilled, Top } from '@element-plus/icons-vue'
import { fxApi, settingsApi } from '@/api'
import { typeStyle } from '@/constants'

// 源的展示名与一句话说明。键必须与后端 services/fx.py 的 SOURCE_* 对齐
// （后端 tests/test_consistency.py 钉着这份对应关系）。
const SOURCE_NAMES = { boc: '中国银行', google: '谷歌财经', erapi: '通用汇率 API' }
const SOURCE_DESC = {
  boc: '官方牌价，国内可执行的口径；只有今日、无历史',
  google: '更新快、覆盖广，但是中间价',
  erapi: '免费公开接口，中间价，约每日更新一次',
}
const BOC_COLS = {
  hmrj: '现汇买入价', cmrj: '现钞买入价', mcj: '现汇卖出价',
  cmcj: '现钞卖出价', zhzjj: '中行折算价',
}

const loading = ref(false)
const saving = ref(false)
const refreshing = ref(false)
const specs = ref([])
const saved = ref({})        // 服务端当前值
const draft = ref({})        // 页面上的改动
const fx = ref({})

function spec(key) { return specs.value.find((s) => s.key === key) || {} }
const allSources = computed(() => spec('fx.sources').choices || [])
const unused = computed(() => allSources.value.filter((s) => !(draft.value['fx.sources'] || []).includes(s)))
// 深比较：源顺序是数组，`!==` 恒为真会让「保存」按钮一直亮着
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(saved.value))

function extraHint(key) {
  if (key === 'fx.fallback_hours') {
    const h = draft.value[key]
    return h === 0 ? '一失败就立刻用下一个源' : `约 ${(h / 24).toFixed(1)} 天`
  }
  if (key === 'fx.refresh_seconds') {
    return `约 ${(draft.value[key] / 3600).toFixed(1)} 小时一次`
  }
  return ''
}

async function load() {
  loading.value = true
  try {
    const r = await settingsApi.get()
    specs.value = r.specs
    saved.value = r.values
    draft.value = JSON.parse(JSON.stringify(r.values))
    fx.value = await fxApi.get()
  } catch (_) { /* 拦截器已提示 */ } finally { loading.value = false }
}

function move(i, d) {
  const arr = draft.value['fx.sources']
  const j = i + d
  if (j < 0 || j >= arr.length) return
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
}
function drop(i) { draft.value['fx.sources'].splice(i, 1) }
function add(s) { draft.value['fx.sources'].push(s) }
function reset() { draft.value = JSON.parse(JSON.stringify(saved.value)) }

async function save() {
  saving.value = true
  try {
    // 只提交真正变了的键：整包提交会把别人刚在另一个标签页改过的项一起盖回去
    const patch = {}
    for (const k of Object.keys(draft.value)) {
      if (JSON.stringify(draft.value[k]) !== JSON.stringify(saved.value[k])) patch[k] = draft.value[k]
    }
    const r = await settingsApi.save(patch)
    saved.value = r.values
    draft.value = JSON.parse(JSON.stringify(r.values))
    ElMessage.success('已保存，下一轮刷新即按新设置执行')
  } catch (_) { /* 拦截器已提示（422 会显示后端的具体原因） */ } finally { saving.value = false }
}

async function doRefresh() {
  refreshing.value = true
  try {
    fx.value = await fxApi.refresh()
    ElMessage.success(`已按当前设置取到：1元 = ${fx.value.rate}円（${fx.value.source_label}）`)
  } catch (_) { /* 拦截器已提示 */ } finally { refreshing.value = false }
}

onMounted(load)
</script>

<style scoped>
.set-page { max-width: 820px; }
.title { margin: 0 0 8px; font-size: 20px; }
.lead { margin: 0 0 16px; color: #909399; font-size: 12px; line-height: 1.8; }
.lead code { background: var(--el-fill-color-light); padding: 1px 5px; border-radius: 3px; }
.card { margin-bottom: 16px; }
.card-hd { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-weight: 600; margin-bottom: 12px; }
.cur { font-weight: 400; font-size: 13px; color: #606266; display: flex; align-items: center; gap: 8px; }
.field { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.field.col { align-items: flex-start; flex-direction: column; gap: 8px; }
.flabel { width: 132px; flex: none; font-size: 13px; color: #606266; display: flex; align-items: center; gap: 4px; }
.field.col .flabel { width: auto; }
.help { color: #909399; cursor: help; font-size: 13px; }
.sub { color: #909399; font-size: 12px; }
.sub.warn { color: var(--el-color-warning); }
.chain { width: 100%; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; overflow: hidden; }
.chain-row { display: flex; align-items: center; gap: 8px; padding: 8px 12px; font-size: 13px; }
.chain-row + .chain-row { border-top: 1px solid var(--el-border-color-lighter); }
.ord { width: 18px; color: #909399; font-size: 12px; }
.sname { font-weight: 600; }
.sdesc { color: #909399; font-size: 12px; }
.grow { flex: 1; }
.chain-add { padding: 8px 12px; border-top: 1px dashed var(--el-border-color-lighter); font-size: 12px; color: #909399; display: flex; align-items: center; gap: 8px; }
.acts { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
</style>
