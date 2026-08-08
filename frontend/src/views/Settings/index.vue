<template>
  <div class="set-page" v-loading="loading">
    <h2 class="title">设置</h2>
    <p class="lead">
      这里改的是<b>业务偏好</b>，存在数据库里、即时生效。密钥、监听地址、数据库连接串那些属于部署配置，
      在 <code>backend/.env</code> 里改、要重启，不在这一页。
    </p>

    <!-- 卡片按后端注册表的 group 自动分组，控件由 kind 决定。
         页面**不写死**渲染哪些键——加一项设置只改 backend/app/services/prefs.py 的 SPECS，
         这里自动出现。原先是硬编码 ['fx.attempts', ...] 那种列表，这次加两项就漏渲染了两项。 -->
    <el-card v-for="g in groups" :key="g.name" shadow="never" class="card">
      <div class="card-hd">
        <span>{{ g.name }}</span>
        <span class="cur" v-if="g.name === '汇率' && fx.rate">
          当前：1元 = {{ fx.rate }}円
          <el-tag size="small" :style="typeStyle(fx.expired ? 'danger' : fx.fallback ? 'info' : 'success')">
            {{ fx.source_label || fx.source }}
          </el-tag>
          <el-tag v-if="fx.expired" size="small" :style="typeStyle('danger')">已过期 {{ ageText }}</el-tag>
          <el-button link type="primary" size="small" :loading="refreshing" @click="doRefresh">
            立即刷新
          </el-button>
        </span>
      </div>

      <div v-for="sp in g.items" :key="sp.key" class="field" :class="{ col: sp.kind === 'list[str]' }">
        <label class="flabel">
          {{ sp.label }}
          <el-tooltip v-if="sp.hint" :content="sp.hint" placement="top">
            <el-icon class="help"><QuestionFilled /></el-icon>
          </el-tooltip>
        </label>

        <!-- 有序列表（目前只有汇率源优先级）：上下移比拖拽更准，也不引第三方库 -->
        <div v-if="sp.kind === 'list[str]'" class="chain">
          <div v-for="(s, i) in draft[sp.key] || []" :key="s" class="chain-row">
            <span class="ord">{{ i + 1 }}</span>
            <span class="sname">{{ SOURCE_NAMES[s] || s }}</span>
            <el-tag v-if="i === 0" size="small" :style="typeStyle('success')">首选</el-tag>
            <span class="sdesc">{{ SOURCE_DESC[s] }}</span>
            <div class="grow" />
            <el-button link :icon="Top" :disabled="i === 0" title="上移" @click="move(sp.key, i, -1)" />
            <el-button link :icon="Bottom" :disabled="i === (draft[sp.key] || []).length - 1"
                       title="下移" @click="move(sp.key, i, 1)" />
            <el-button link type="danger" :icon="Delete" :disabled="(draft[sp.key] || []).length <= 1"
                       title="不使用这个源" @click="drop(sp.key, i)" />
          </div>
          <div v-if="unused(sp).length" class="chain-add">
            未启用：
            <el-button v-for="s in unused(sp)" :key="s" size="small" :icon="Plus" @click="add(sp.key, s)">
              {{ SOURCE_NAMES[s] || s }}
            </el-button>
          </div>
        </div>

        <el-input-number v-else-if="sp.kind === 'int'" v-model="draft[sp.key]"
                         :min="sp.min" :max="sp.max" :controls="false" size="small"
                         style="width: 130px" :disabled="blocked(sp)" />

        <el-select v-else-if="sp.choices" v-model="draft[sp.key]" size="small"
                   style="width: 160px" :disabled="blocked(sp)">
          <el-option v-for="c in sp.choices" :key="c" :label="CHOICE_NAMES[c] || c" :value="c" />
        </el-select>

        <el-input v-else v-model="draft[sp.key]" size="small" style="width: 160px"
                  :placeholder="String(sp.default || '')" :disabled="blocked(sp)" />

        <span class="sub">{{ extraHint(sp) }}</span>
      </div>
    </el-card>

    <div class="acts">
      <el-button type="primary" :disabled="!dirty" :loading="saving" @click="save">保存</el-button>
      <el-button :disabled="!dirty" @click="reset">撤销改动</el-button>
      <span v-if="dirty" class="sub warn">有未保存的改动</span>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Bottom, Delete, Plus, QuestionFilled, Top } from '@element-plus/icons-vue'
import { fxApi, settingsApi } from '@/api'
import { typeStyle } from '@/constants'

// 源的展示名与一句话说明。键必须与后端 prefs.FX_SOURCE_CHOICES 对齐
// （backend/tests/test_consistency.py 钉着这份对应关系）。
// 只列**能排进优先级链**的源——「手填」不在链上，它的展示名由后端随 FxRead.source_label 下发。
const SOURCE_NAMES = { boc: '中国银行', google: '谷歌财经', erapi: '通用汇率 API' }
const SOURCE_DESC = {
  boc: '官方牌价，国内可执行的口径；只有今日、无历史',
  google: '更新快、覆盖广，但是中间价',
  erapi: '免费公开接口，中间价，约每日更新一次',
}
// 枚举型设置的取值展示名（目前只有中行口径）。键即后端 Spec.choices 里的值。
const CHOICE_NAMES = {
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

// 按 group 分卡片，保持注册表里的声明顺序（Object 插入序）
const groups = computed(() => {
  const out = []
  for (const sp of specs.value) {
    let g = out.find((x) => x.name === (sp.group || '通用'))
    if (!g) { g = { name: sp.group || '通用', items: [] }; out.push(g) }
    g.items.push(sp)
  }
  return out
})
// 深比较：源顺序是数组，`!==` 恒为真会让「保存」按钮一直亮着
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(saved.value))
const ageText = computed(() => {
  const h = fx.value.age_hours || 0
  return h >= 48 ? `${Math.floor(h / 24)} 天` : `${Math.round(h)} 小时`
})

// spec.requires = 「依赖某个源被启用」。没启用就置灰——比如中行取价口径只在启用了中行时有意义。
function blocked(sp) {
  if (!sp.requires) return false
  const chain = specs.value.find((x) => x.kind === 'list[str]')
  return !(chain && (draft.value[chain.key] || []).includes(sp.requires))
}
function unused(sp) {
  const cur = draft.value[sp.key] || []
  return (sp.choices || []).filter((s) => !cur.includes(s))
}
function extraHint(sp) {
  const v = draft.value[sp.key]
  if (sp.requires && blocked(sp)) return `仅在启用「${SOURCE_NAMES[sp.requires] || sp.requires}」时有用`
  if (sp.key === 'fx.fallback_hours' && v === 0) return '一失败就立刻用下一个源'
  if (sp.unit === '秒' && v) return `${sp.unit} · ≈ ${(v / 3600).toFixed(1)} 小时`
  if (sp.unit === '小时' && v >= 48) return `${sp.unit} · ≈ ${(v / 24).toFixed(1)} 天`
  return sp.unit || ''
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

function move(key, i, d) {
  const arr = draft.value[key]
  const j = i + d
  if (!arr || j < 0 || j >= arr.length) return
  ;[arr[i], arr[j]] = [arr[j], arr[i]]
}
function drop(key, i) { draft.value[key]?.splice(i, 1) }
function add(key, s) { draft.value[key]?.push(s) }
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
    specs.value = r.specs
    fx.value = await fxApi.get()          // 手填汇率保存后可能立刻生效，刷新展示
    ElMessage.success('已保存，下一轮刷新即按新设置执行')
  } catch (_) { /* 拦截器已提示（422 会显示后端的具体原因） */ } finally { saving.value = false }
}

async function doRefresh() {
  refreshing.value = true
  try {
    fx.value = await fxApi.refresh()
    ElMessage.success(`已按当前设置取到：1元 = ${fx.value.rate}円（${fx.value.source_label}）`)
  } catch (e) {
    // 「拦截器已提示」只对 axios 抛的错成立。这里曾因 fxApi.refresh 压根没导出而抛
    // TypeError，被空 catch 吞掉 → 点了完全静默、按钮像坏的。留一行痕。
    console.error('[fx refresh]', e)
  } finally { refreshing.value = false }
}

onMounted(load)
</script>

<style scoped>
.set-page { max-width: 820px; }
.title { margin: 0 0 8px; font-size: 20px; }
.lead { margin: 0 0 16px; color: var(--txt-3); font-size: 12px; line-height: 1.8; }
.lead code { background: var(--el-fill-color-light); padding: 1px 5px; border-radius: 3px; }
.card { margin-bottom: 16px; }
.card-hd { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-weight: 600; margin-bottom: 12px; }
.cur { font-weight: 400; font-size: 13px; color: var(--txt-2); display: flex; align-items: center; gap: 8px; }
.field { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.field.col { align-items: flex-start; flex-direction: column; gap: 8px; }
.flabel { width: 148px; flex: none; font-size: 13px; color: var(--txt-2); display: flex; align-items: center; gap: 4px; }
.field.col .flabel { width: auto; }
.help { color: var(--txt-3); cursor: help; font-size: 13px; }
.sub { color: var(--txt-3); font-size: 12px; }
.sub.warn { color: var(--el-color-warning); }
.chain { width: 100%; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; overflow: hidden; }
.chain-row { display: flex; align-items: center; gap: 8px; padding: 8px 12px; font-size: 13px; }
.chain-row + .chain-row { border-top: 1px solid var(--el-border-color-lighter); }
.ord { width: 18px; color: var(--txt-3); font-size: 12px; }
.sname { font-weight: 600; }
.sdesc { color: var(--txt-3); font-size: 12px; }
.grow { flex: 1; }
.chain-add { padding: 8px 12px; border-top: 1px dashed var(--el-border-color-lighter); font-size: 12px; color: var(--txt-3); display: flex; align-items: center; gap: 8px; }
.acts { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
</style>
