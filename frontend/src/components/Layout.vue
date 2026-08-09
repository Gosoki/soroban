<template>
  <div class="layout">
    <!-- 手机：菜单按钮 + 遮罩挂到 body（避免父级 transform 破坏 fixed 定位） -->
    <Teleport to="body">
      <button v-if="isMobile && !drawerOpen" type="button" class="sb-fab" aria-label="打开菜单" @click="drawerOpen = true">
        <el-icon :size="22"><Fold /></el-icon>
      </button>
    </Teleport>
    <Teleport to="body">
      <div v-if="isMobile && drawerOpen" class="sb-mask" @click="drawerOpen = false" />
    </Teleport>

    <!-- 侧边栏：电脑常驻；手机变成从左滑入的抽屉（teleport 到 body） -->
    <Teleport to="body" :disabled="!isMobile">
      <aside class="sidebar" :class="{ 'sidebar--mobile': isMobile, 'sidebar--open': isMobile && drawerOpen }">
        <div class="brand">
          <span class="brand-logo">算</span>
          <div class="brand-text">
            <div class="brand-title">算盤 soroban</div>
            <div class="brand-sub">代购集运记账</div>
          </div>
          <el-button v-if="isMobile" text class="brand-close" aria-label="关闭菜单" @click="drawerOpen = false">
            <el-icon :size="18"><Close /></el-icon>
          </el-button>
        </div>

        <el-menu router :default-active="$route.path" class="menu" @select="onSelect">
          <el-menu-item v-for="m in nav" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <span>{{ m.title }}</span>
          </el-menu-item>
        </el-menu>

        <!-- 迷你计算器：放在「管理员」上方；手机/侧栏收起时不显示 -->
        <SidebarCalc v-if="!isMobile" />

        <div class="foot">
          <div class="fx" v-if="fx.rate" @click="loadFx" title="点一下刷新汇率">
            1元 = {{ fx.rate }}円
            <!-- 「旧」= 不是今天的（日粒度）；「已过期」= 超过设置里的上限、这个值已经不该信了。
                 分两级是因为前者很常见（凌晨还没刷新），后者意味着取汇率的链路真的断了：
                 建单会继续用它（有值好过没有，且逐行存下来可审计），但必须看得见。 -->
            <el-tag v-if="fx.expired" :style="typeStyle('danger')"
                    :title="`已过期 ${fx.ageText}，取汇率的链路可能断了。建单仍会用它，但金额可能不准——去设置页手填一个，或检查汇率插件`">
              已过期 {{ fx.ageText }}
            </el-tag>
            <el-tag v-else-if="fx.notToday" :style="typeStyle('warning')">旧</el-tag>
            <!-- 「手填」= 这条汇率是你在设置页填的，不是自动取到的。
                 原先这里是「备用」（不是链上首选源），但「谁是首选」现在是插件的私有参数，
                 核心看不到——留一个恒 False 的标签等于持续输出假信息。 -->
            <el-tag v-if="fx.source === 'manual'" :style="typeStyle('info')"
                    title="这是你在设置页手填的汇率，不是自动取到的。装上汇率插件并授权后会自动更新">手填</el-tag>
          </div>
          <div class="user">
            <el-icon><User /></el-icon><span>{{ userName }}</span>
          </div>
          <div class="foot-btns">
            <el-button text bg @click="pwd.open = true">改密码</el-button>
            <el-button text bg @click="logout">退出登录</el-button>
          </div>
        </div>
      </aside>
    </Teleport>

    <main class="content">
      <router-view />
    </main>

    <el-dialog v-model="pwd.open" title="修改密码" width="360px" append-to-body @closed="resetPwd">
      <el-form label-width="76px" @submit.prevent>
        <el-form-item label="原密码"><el-input v-model="pwd.old" type="password" show-password /></el-form-item>
        <el-form-item label="新密码"><el-input v-model="pwd.neo" type="password" show-password placeholder="至少 6 位" /></el-form-item>
        <el-form-item label="确认新密码"><el-input v-model="pwd.confirm" type="password" show-password @keyup.enter="submitPwd" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwd.open = false">取消</el-button>
        <el-button type="primary" :loading="pwd.saving" @click="submitPwd">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authApi, fxApi } from '@/api'
import { typeStyle } from '@/constants'
import SidebarCalc from '@/components/SidebarCalc.vue'

const router = useRouter()
const route = useRoute()
// 侧栏菜单**从路由表生成**，不再手写一份。
// 这里曾经是一个硬编码数组：加了新路由却忘了往这加一行，页面就进不去——
// 而路由 meta 里本来就写着 title/icon，抄第二遍纯属自找漏。
// 顺序按 ORDER 排（业务上的先后≠路由声明顺序）；没列进 ORDER 的新页面自动排在最后，
// 也就是说以后加页面只要写路由，菜单自己会长出来。
// 顺序：账目页 → 支撑数据 → 工具页。**漏一个就排到最后**（下面的 999 兜底），
// `/fx` 就这么在「设置」后面待过一阵子——菜单是从路由表生成的，加页面时很容易
// 只记得写路由。`tests/test_consistency.py` 有一条守卫钉住「ORDER 必须覆盖全部页面」。
const ORDER = ['/dashboard', '/orders', '/items', '/shipment', '/misc',
               '/staging', '/fx', '/plugins', '/database', '/settings']
const nav = router.getRoutes()
  .filter((r) => r.meta?.title && r.path.startsWith('/') && r.path !== '/login')
  .map((r) => ({ path: r.path, title: r.meta.title, icon: r.meta.icon || 'Document' }))
  .sort((a, b) => {
    const ia = ORDER.indexOf(a.path); const ib = ORDER.indexOf(b.path)
    return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib)
  })

// —— 响应式：≤768px 视为手机，侧栏收成抽屉 ——
const isMobile = ref(false)
const drawerOpen = ref(false)
let mq = null
function syncMobile() {
  if (!mq) return
  isMobile.value = mq.matches
  if (!mq.matches) drawerOpen.value = false   // 回到电脑宽度：强制关抽屉，避免残留态
}
function onSelect() { if (isMobile.value) drawerOpen.value = false }   // 手机点菜单即导航即关抽屉
watch(() => route.path, () => { if (isMobile.value) drawerOpen.value = false })   // 任意方式导航都关

const userName = ref('用户')
try {
  const u = JSON.parse(localStorage.getItem('auth_user') || '{}')
  userName.value = u.display_name || u.username || '用户'
} catch (_) { /* ignore */ }

const fx = reactive({ rate: null, notToday: false, source: '', expired: false, ageText: '' })

// 侧栏汇率原先只在 onMounted 取一次。Layout 是父级路由组件、切页不卸载，
// 于是那一行是**登录那一刻的快照**：红色的「已过期」标签正劝用户去设置页手填一个，
// 照做之后标签却不会消失——用户会以为手填没生效。
// 给它一个点击重取（最廉价的形态；为这个引事件总线/provide-inject 不划算）。
async function loadFx() {
  try {
    const r = await fxApi.get()
    fx.rate = r.rate
    fx.notToday = r.not_today
    fx.source = r.source || ''
    fx.expired = !!r.expired
    const h = r.age_hours || 0
    fx.ageText = h >= 48 ? `${Math.floor(h / 24)} 天` : `${Math.round(h)} 小时`
  } catch (_) { /* 拦截器已提示 */ }
}

onMounted(async () => {
  mq = window.matchMedia('(max-width: 768px)')
  syncMobile()
  mq.addEventListener('change', syncMobile)
  await loadFx()
})
onUnmounted(() => { mq?.removeEventListener('change', syncMobile) })

function logout() {
  localStorage.removeItem('auth_token')
  localStorage.removeItem('auth_user')
  router.push('/login')
}

// —— 改密码 ——
const pwd = reactive({ open: false, old: '', neo: '', confirm: '', saving: false })
function resetPwd() { pwd.old = ''; pwd.neo = ''; pwd.confirm = ''; pwd.saving = false }
async function submitPwd() {
  if (pwd.neo.length < 6) return ElMessage.warning('新密码至少 6 位')
  if (pwd.neo !== pwd.confirm) return ElMessage.warning('两次输入的新密码不一致')
  pwd.saving = true
  try {
    await authApi.changePassword(pwd.old, pwd.neo)
    ElMessage.success('密码已修改，下次登录用新密码')
    pwd.open = false
  } catch (_) {
    /* 错误提示已由 http 拦截器统一弹出（含后端 detail，如"原密码不正确"），这里不再重复弹 */
  } finally {
    pwd.saving = false
  }
}
</script>

<style scoped>
.layout { display: flex; height: 100vh; overflow: hidden; }
.sidebar {
  width: 220px; flex-shrink: 0; background: #0f1728;
  display: flex; flex-direction: column; border-right: 1px solid var(--border-dim);
}
.brand { display: flex; align-items: center; gap: 10px; padding: 18px 16px; }
.brand-logo {
  width: 40px; height: 40px; border-radius: 8px; background: var(--brand);
  color: #fff; font-size: 22px; font-weight: 700;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.brand-text { min-width: 0; }
.brand-title { font-size: 15px; color: var(--txt-1); font-weight: 600; }
.brand-sub { font-size: 12px; color: var(--txt-3); }
.brand-close { flex-shrink: 0; margin-left: auto; color: #a6adb4 !important; padding: 4px !important; }
.menu {
  /* 不用 background-color/text-color/active-text-color 三个 prop：Element 会去解析
     它们的颜色值来推导悬停色，喂 var() 会让它算不出来——实测菜单文字直接变成 active
     色，控制台还刷 `Maximum call stack size exceeded`。直接写它的 CSS 变量则安全。 */
  --el-menu-bg-color: var(--bg-side);
  --el-menu-text-color: var(--txt-2);
  --el-menu-active-color: var(--txt-on-active);
  --el-menu-hover-bg-color: var(--bg-hover);
  flex: 1; border-right: none; overflow-y: auto;
}
.menu :deep(.el-menu-item) { margin: 4px 8px; border-radius: 6px; }
.menu :deep(.el-menu-item.is-active) { background: var(--brand); }
.foot { padding: 12px 16px; border-top: 1px solid var(--border-dim); display: flex; flex-direction: column; gap: 8px; }
/* 页脚信息行统一：同色(var(--txt-body))、同字号(13)、常规字重 */
.fx { font-size: 13px; color: var(--txt-body); display: flex; align-items: center; gap: 6px; }
.user { display: flex; align-items: center; gap: 6px; color: var(--txt-body); font-size: 13px; }
.foot-btns { display: flex; gap: 8px; }
.foot-btns :deep(.el-button) { font-size: 13px; }   /* 与汇率/管理员/计算器统一 13px（el small 默认 12px） */
.content { flex: 1; overflow: auto; padding: 20px; min-width: 0; }

/* —— 手机抽屉：从左滑入 —— */
.sidebar--mobile {
  position: fixed; left: 0; top: 0; bottom: 0;
  width: min(220px, 84vw); z-index: 5010;
  transform: translate3d(-100%, 0, 0);
  transition: transform 0.28s cubic-bezier(0.32, 0.72, 0, 1);
  will-change: transform;
}
.sidebar--mobile.sidebar--open {
  transform: translate3d(0, 0, 0);
  box-shadow: 4px 0 24px rgba(0, 0, 0, 0.45);
}
@media (max-width: 768px) {
  .content { padding: 12px; padding-top: 52px; }
}
</style>

<!-- Teleport 到 body 的浮动按钮/遮罩：不用 scoped，单独挂类名 -->
<style>
.sb-fab {
  position: fixed;
  left: max(12px, env(safe-area-inset-left, 0px));
  top: max(12px, env(safe-area-inset-top, 0px));
  z-index: 5020; width: 44px; height: 44px; border-radius: 10px;
  background: #121b2e; border: 1px solid var(--border); color: #ecf2ff;
  display: flex; align-items: center; justify-content: center; cursor: pointer;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.45); padding: 0;
}
.sb-fab:active { opacity: 0.92; }
.sb-mask { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45); z-index: 5000; }
</style>
