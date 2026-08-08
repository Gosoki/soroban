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
        <span class="cur" v-if="g.name === '汇率'">
          <template v-if="fx.rate">
            当前：1元 = {{ fx.rate }}円
            <el-tag size="small" :style="typeStyle(fx.expired ? 'danger' : fx.source === 'manual' ? 'info' : 'success')">
              {{ fxSourceName(fx) }}
            </el-tag>
            <el-tag v-if="fx.expired" size="small" :style="typeStyle('danger')">已过期 {{ ageText }}</el-tag>
          </template>
          <span v-else class="sub">库里还没有汇率</span>
          <!-- 这条**必须在 v-if="fx.rate" 之外**：一条汇率都没有的时候，恰恰最需要告诉用户
               汇率从哪来。放进去过一次，结果「没有汇率」时链接反而不见了。
               而且要**按事实说**：插件被删掉之后还写着「由插件负责」，点过去是个空页面。 -->
          <router-link v-if="fx.auto_provider" to="/plugins" class="sub">
            自动获取由「{{ fx.auto_provider }}」插件负责 →
          </router-link>
          <router-link v-else to="/plugins" class="sub warn">
            没有能自动取汇率的插件，现在只用手填值 →
          </router-link>
        </span>
      </div>

      <div v-for="sp in g.items" :key="sp.key" class="field">
        <label class="flabel">
          {{ sp.label }}
          <el-tooltip v-if="sp.hint" :content="sp.hint" placement="top" popper-class="wrap-tip">
            <el-icon class="help"><QuestionFilled /></el-icon>
          </el-tooltip>
        </label>

        <el-input-number v-if="sp.kind === 'int'" v-model="draft[sp.key]"
                         :min="sp.min" :max="sp.max" :controls="false" size="small"
                         style="width: 130px" />

        <el-select v-else-if="sp.choices" v-model="draft[sp.key]" size="small"
                   style="width: 160px">
          <el-option v-for="c in sp.choices" :key="c" :label="c" :value="c" />
        </el-select>

        <el-input v-else v-model="draft[sp.key]" size="small" style="width: 160px"
                  :placeholder="String(sp.default || '')" />

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
import { QuestionFilled } from '@element-plus/icons-vue'
import { fxApi, settingsApi } from '@/api'
import { fxSourceName, typeStyle } from '@/constants'


const loading = ref(false)
const saving = ref(false)
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
// 深比较：值可能是任意类型，`!==` 对对象恒为真会让「保存」按钮一直亮着
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(saved.value))
const ageText = computed(() => {
  const h = fx.value.age_hours || 0
  return h >= 48 ? `${Math.floor(h / 24)} 天` : `${Math.round(h)} 小时`
})

function extraHint(sp) {
  const v = draft.value[sp.key]
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
    ElMessage.success('已保存，即时生效')
  } catch (_) { /* 拦截器已提示（422 会显示后端的具体原因） */ } finally { saving.value = false }
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
.flabel { width: 148px; flex: none; font-size: 13px; color: var(--txt-2); display: flex; align-items: center; gap: 4px; }
.help { color: var(--txt-3); cursor: help; font-size: 13px; }
.sub { color: var(--txt-3); font-size: 12px; }
.sub.warn { color: var(--el-color-warning); }
.chain-row + .acts { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
</style>
