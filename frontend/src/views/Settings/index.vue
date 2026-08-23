<template>
  <div class="set-page" v-loading="loading">
    <PageHeader>
      这里改的是<b>业务偏好</b>，存在数据库里、即时生效。密钥、监听地址、数据库连接串那些属于部署配置，
      在 <code>backend/.env</code> 里改、要重启，不在这一页。
    </PageHeader>

    <!-- 卡片按后端注册表的 group 自动分组，控件由 kind 决定。
         页面**不写死**渲染哪些键——加一项设置只改 backend/app/services/prefs.py 的 SPECS，
         这里自动出现。原先是硬编码 ['fx.attempts', ...] 那种列表，这次加两项就漏渲染了两项。 -->
    <el-card v-for="g in groups" :key="g.name" shadow="never" class="card">
      <div class="card-hd">
        <span>{{ g.name }}</span>
        <span class="cur" v-if="g.name === '汇率'">
          <template v-if="fx.rate">
            当前：1元 = {{ fx.rate }}円
            <el-tag :style="typeStyle(fx.expired ? 'danger' : fx.source === 'manual' ? 'info' : 'success')">
              {{ fxSourceName(fx) }}
            </el-tag>
            <el-tag v-if="fx.expired" :style="typeStyle('danger')">已过期 {{ ageText }}</el-tag>
          </template>
          <span v-else class="sub">库里还没有汇率</span>
          <!-- 这条**必须在 v-if="fx.rate" 之外**：一条汇率都没有的时候，恰恰最需要告诉用户
               汇率从哪来。放进去过一次，结果「没有汇率」时链接反而不见了。
               而且要**按事实说**：插件被删掉之后还写着「由插件负责」，点过去是个空页面。 -->
          <router-link v-if="fx.auto_provider" to="/plugins" class="sub">
            自动获取由「{{ fx.auto_provider }}」插件负责 →
          </router-link>
          <!-- 第三态：装了汇率插件、但它跑不起来（停用 / 没授权 / 缺环境）。
               原先这种情况显示的是上面那句「由它负责」——而它永远不会跑，
               汇率停更时账本会继续用兜底值建单，用户却以为一切正常。
               也不能退回下面那句「没有能自动取汇率的插件」：对「明明装了」的用户同样是假话。 -->
          <router-link v-else-if="fx.auto_blocked" to="/plugins" class="sub warn">
            {{ fx.auto_blocked }}，汇率不会自动更新 →
          </router-link>
          <router-link v-else to="/plugins" class="sub warn">
            没有能自动取汇率的插件，现在只用手填值 →
          </router-link>
        </span>
      </div>

      <div class="field-grid">
      <div v-for="sp in g.items" :key="sp.key" class="field">
        <label class="flabel">
          {{ sp.label }}
          <el-tooltip v-if="sp.hint" :content="sp.hint" placement="top" popper-class="wrap-tip">
            <el-icon class="help"><QuestionFilled /></el-icon>
          </el-tooltip>
        </label>

        <el-input-number v-if="sp.kind === 'int'" v-model="draft[sp.key]"
                         :min="sp.min" :max="sp.max" :controls="false"
                         style="width: 130px" />

        <el-select v-else-if="sp.choices" v-model="draft[sp.key]"
                   style="width: 160px">
          <el-option v-for="c in sp.choices" :key="c" :label="c" :value="c" />
        </el-select>

        <el-input v-else v-model="draft[sp.key]" style="width: 160px"
                  :placeholder="String(sp.default || '')" />

        <span class="sub">{{ extraHint(sp) }}</span>
      </div>
      </div>
    </el-card>

    <!-- 界面偏好**单独成卡**，且卡头就写明「只对这个浏览器生效」。
         这一页开篇那句是「这里改的是业务偏好，存在数据库里、即时生效」——
         把一个存 localStorage 的开关混进上面那些卡片里，那句话就成了假话，
         而用户换台电脑发现设置没跟过去时，是不会回来读这段注释的。
         也刻意**不受下面那个「保存 / 撤销改动」管**：它拨一下就生效，
         和需要提交的业务偏好不是一种东西，放进同一个 dirty 流程只会让人以为没保存上。 -->
    <el-card shadow="never" class="card">
      <div class="card-hd">
        <span>界面</span>
        <span class="cur">只对<b>这个浏览器</b>生效，不存进数据库、不跟着账号走</span>
      </div>
      <div class="field-grid">
        <div class="field">
          <label class="flabel">
            隐藏页面标题
            <el-tooltip placement="top" popper-class="wrap-tip"
                        content="每页顶部那行大标题（「商品订单」「日元汇率」…）。&#10;左侧导航已经写着页面名，是同一句话说两遍；&#10;关掉能给表格多让出一行。说明那个「?」也跟着一起收起来。">
              <el-icon class="help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </label>
          <el-switch v-model="hidePageTitle" />
          <span class="sub">{{ hidePageTitle ? '已隐藏（拨一下立即生效）' : '显示中' }}</span>
        </div>
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
import PageHeader from '@/components/PageHeader.vue'
import { hidePageTitle } from '@/utils/uiPrefs'
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
    // **别拿服务端的整包值盖掉「在途期间又改过」的那些格子。**
    // 保存按钮转圈时输入框并没有禁用，用户完全可能接着改别的项——
    // 而原先这一句 `draft.value = 深拷贝(r.values)` 会把那些改动当场抹掉：
    // 值跳回旧的、`dirty` 变 false（保存/撤销一起置灰）、同时弹「已保存，即时生效」。
    // 用户看到的是「两项都存了」，**实际第二项一个字都没提交，界面上也不再有未保存的痕迹**。
    //
    // 判据与幽灵新建行那条一致（utils/rowWrites.js 的 keysToClearAfterCreate）：
    // 只接受**这次送出去的那些键**的服务端值，其余保留 draft 现有的。
    const fresh = JSON.parse(JSON.stringify(r.values))
    for (const k of Object.keys(fresh)) {
      // 这次没送的键：如果 draft 已被改过（与提交前的 saved 不同），保留用户改的那份
      const untouched = !(k in patch) && JSON.stringify(draft.value[k]) === JSON.stringify(fresh[k])
      if ((k in patch) || untouched) draft.value[k] = fresh[k]
    }
    specs.value = r.specs
    // 还有没保存的改动就说清楚，别让「已保存」把它盖过去
    ElMessage.success(dirty.value ? '已保存；下面还有未保存的改动' : '已保存，即时生效')
    // **这一句只是刷新展示，它失败不能改口说「没保存成功」。**
    // 原先它排在 success 之前、又共用外层那个 catch：`PUT /api/settings` 已经 200、
    // 值已落库、`saved.value` 已回写，紧接着这句 `fxApi.get()` 挂了（后端在重启、
    // 局域网抖动、503 用完两次重试），于是「已保存」永远弹不出来，屏幕上只剩一条红色报错。
    // 更糟的是此刻 `saved.value` 已等于服务端新值 ⇒ `dirty` 为 false ⇒ 模板里
    // `:disabled="!dirty"` 的「保存」和「撤销改动」**同时置灰**，「有未保存的改动」
    // 那句提示也消失。用户的结论只能是「这次没存上」——而它已经生效了；
    // 他想再点一次都点不动，只有刷新整页才会发现原来存住了。
    //
    // 判据与 `utils/listRows.js::afterCreate` 一致：**一件事成没成，只看它自己那一步**，
    // 与随后那次刷新拿没拿到无关。
    try {
      fx.value = await fxApi.get()        // 手填汇率保存后可能立刻生效，刷新展示
    } catch (_) { /* 拦截器已提示。保存本身是成功的，不因为展示没刷新就改口 */ }
  } catch (_) { /* 拦截器已提示（422 会显示后端的具体原因） */ } finally { saving.value = false }
}


onMounted(load)
</script>

<style scoped>
/* 页宽不再自己设上限：全站统一「卡片占满宽度、卡片内字段自动分列」（见 tokens.css 的
   .field-grid）。原先这里是 820px，而汇率页 900、数据库页 760——同一个应用四种页宽。 */
.card { margin-bottom: 16px; }
/* 刻意**不用** space-between：「汇率」与它右边那句「当前：1元 = …」是同一件事的标题和值，
   页宽放开后 space-between 会把它们推到相隔一千多像素的两端，读起来不像一组。
   与插件页卡片头同一套做法：相关的挤在左边，真正的「操作」才用 .grow 顶到右边。 */
.card-hd { display: flex; align-items: center; gap: 12px; font-weight: 600; margin-bottom: 12px; }
.cur { font-weight: 400; font-size: 13px; color: var(--txt-2); display: flex; align-items: center; gap: 8px; }
.field { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
/* 180px 与插件页的 .flabel 同宽：两页长得一模一样的字段行，没有理由用两个数。
   148px 时「手填汇率（1元 = ？円)」这种标签会折成两行，行高跟着变，整列对不齐。 */
.flabel { width: 180px; flex: none; font-size: 13px; color: var(--txt-2); display: flex; align-items: center; gap: 4px; }
.help { color: var(--txt-3); cursor: help; font-size: 13px; }
.sub { color: var(--txt-3); font-size: 12px; }
.sub.warn { color: var(--el-color-warning); }
.acts { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
</style>
