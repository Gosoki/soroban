<template>
  <div>
    <div class="bar">
      <span class="hint">soroban 扫 scraper/ 下 soroban-scraper-* 目录作为插件。这里加账号、授权、启停、定时；插件只管抓取，抓到的单进「暂存订单」待处理。</span>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-empty v-if="!loading && !plugins.length" description="未发现插件（scraper/ 下没有 soroban-scraper-* 目录）" />

    <el-card v-for="p in plugins" :key="p.id" class="plugin" v-loading="p._busy">
      <template #header>
        <div class="head">
          <span class="pname">{{ p.name }}</span>
          <span class="pver">v{{ p.version || '?' }}</span>
          <el-tag size="small" :style="typeStyle(installTagType(p))">
            {{ installTagText(p) }}
          </el-tag>
          <div class="grow" />
          <span class="sw-label">启用定时</span>
          <el-switch v-model="p._form.enabled" :disabled="!p.installed" @change="saveConfig(p)" />
        </div>
      </template>

      <!-- 缺依赖：说清缺什么 + 一键补齐。不列出来的话，用户只看到一片灰按钮而无从下手 -->
      <el-alert v-if="p.needs && p.needs.length" type="warning" :closable="false" class="needs">
        <template #title>
          <span v-if="p.install && p.install.running">正在安装：{{ p.install.step }}…</span>
          <span v-else>插件还缺以下依赖，装好后才能添加账号与抓取</span>
        </template>
        <ul class="needlist">
          <li v-for="n in p.needs" :key="n.key"><b>{{ n.label }}</b> —— {{ n.hint }}</li>
        </ul>
        <div class="needact">
          <el-button type="primary" size="small" :loading="!!(p.install && p.install.running)"
                     @click="doInstall(p)">
            {{ p.install && p.install.running ? '安装中…' : '一键安装' }}
          </el-button>
          <el-checkbox v-model="p._withBrowser" :disabled="!!(p.install && p.install.running)" size="small">
            一并下载浏览器内核（约 150MB，仅首次）
          </el-checkbox>
        </div>
        <div v-if="p.install && p.install.error" class="neederr">{{ p.install.error }}</div>
      </el-alert>

      <!-- 插件设置：定时抓取 -->
      <div class="field">
        <label class="flabel">定时抓取（分钟，0=关闭）</label>
        <el-input-number v-model="p._form.schedule_minutes" :min="0" :step="30" size="small" />
        <el-button type="primary" size="small" @click="saveConfig(p)">保存</el-button>
        <span class="sub">{{ p.config.last_run_at ? '上次：' + fmtTime(p.config.last_run_at) : '尚未抓取' }}</span>
      </div>

      <!-- 添加账号 -->
      <div class="sect">添加账号</div>
      <div class="field">
        <el-input v-model="p._add.name" size="small" placeholder="账号昵称" style="width: 160px"
                  @keyup.enter="doAddAccount(p)" />
        <el-select v-model="p._add.platform" size="small" filterable allow-create default-first-option
                   placeholder="导入平台" style="width: 140px">
          <el-option v-for="o in platformOpts" :key="o" :label="o" :value="o" />
        </el-select>
        <el-button type="primary" size="small" :disabled="!p.installed" @click="doAddAccount(p)">添加</el-button>
        <span class="sub">平台加时确定、之后不可改（改名只改昵称）</span>
      </div>

      <!-- 账号列表 -->
      <div class="sect">账号（{{ p.accounts.length }}）</div>
      <div v-if="!p.accounts.length" class="sub">还没有账号——用上面「添加账号」加一个。</div>
      <div v-for="a in p.accounts" :key="a.account" class="acct" :class="{ dim: !a.enabled }">
        <span class="c-sw">
          <el-switch v-if="a.configured" v-model="a.enabled" size="small" :disabled="!p.installed"
                     title="停用后定时与「抓取全部账号」都跳过它" @change="(v) => doToggle(p, a, v)" />
        </span>
        <span class="aname" :title="a.account">{{ a.account }}</span>
        <span class="c-plat">
          <el-tag v-if="a.platform" size="small" :style="platformTagStyle(a.platform)">{{ a.platform }}</el-tag>
        </span>
        <span class="c-auth">
          <el-tag size="small" :style="typeStyle(a.authorized ? 'success' : 'warning')">
            {{ a.authorized ? '已授权' : '未授权' }}
          </el-tag>
        </span>
        <span class="c-state">
          <el-tag v-if="!a.configured" size="small" :style="typeStyle('info')"
                  title="磁盘上有此账号的登录会话，但没作为账号添加。想纳管就用上面「添加账号」加同名账号。">未添加</el-tag>
          <el-tag v-else-if="!a.enabled" size="small" :style="typeStyle('info')">未启用</el-tag>
        </span>

        <el-button size="small" link type="primary" :disabled="!p.installed" @click="doLogin(p, a.account)">
          {{ a.authorized ? '重新授权' : '授权登录' }}
        </el-button>
        <el-button size="small" link :disabled="!p.installed || !a.authorized" @click="doFetch(p, a.account)">抓这个号</el-button>
        <el-button size="small" link @click="doRenameAccount(p, a.account)">改名</el-button>
        <el-button size="small" link type="danger" @click="doDeleteAccountStaging(p, a.account)">删暂存单</el-button>
        <el-button size="small" link type="danger" @click="doDeleteAccountOrders(p, a.account)">删账本单</el-button>
        <div class="grow" />
        <el-button size="small" link type="danger" @click="doDeleteAccount(p, a.account)">删除</el-button>
      </div>

      <div class="field" style="margin-top: 12px">
        <el-button size="small" :icon="Download" :disabled="!p.installed || !enabledCount(p)" @click="doFetch(p)">
          抓取全部账号（{{ enabledCount(p) }}）
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Download } from '@element-plus/icons-vue'
import { pluginsApi, tagsApi } from '@/api'
import { tagStyleAt, typeStyle } from '@/constants'
import { fmtDateTime } from '@/utils/datetime'

const plugins = ref([])
const loading = ref(false)
const platformTags = ref([])   // [{value,color}] 来源平台标签集（下拉选项 + 上色）

const platformOpts = computed(() => platformTags.value.map((t) => t.value))
const platformColor = computed(() => Object.fromEntries(platformTags.value.map((t) => [t.value, t.color])))
// 取用户在标签管理里配的颜色（不是 constants 里那套写死的语义色，别混用）
function platformTagStyle(v) { return tagStyleAt(platformColor.value[v] ?? -1, v) }

const fmtTime = fmtDateTime   // 后端存 naive UTC，必须补 Z 再解析，见 utils/datetime.js
function enabledCount(p) { return p.accounts.filter((a) => a.configured && a.enabled).length }

async function load() {
  loading.value = true
  try {
    const list = await pluginsApi.list()
    const prev = Object.fromEntries(plugins.value.map((x) => [x.id, x]))
    plugins.value = list.map((p) => ({
      ...p, _busy: false,
      // 刷新时保留用户勾过的选项，别让轮询把复选框弹回默认
      _withBrowser: prev[p.id]?._withBrowser ?? true,
      _form: { enabled: p.config.enabled, schedule_minutes: p.config.schedule_minutes || 0 },
      _add: { name: '', platform: '淘宝' },
    }))
    // 有安装在跑就继续盯着，装完自动刷新（按钮解禁、缺依赖提示消失）
    if (list.some((p) => p.install && p.install.running)) scheduleInstallPoll()
    try { platformTags.value = await tagsApi.list('platform') } catch (_) { /* 无所谓，下拉可自建 */ }
  } catch (_) { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

// --- 依赖安装 ---------------------------------------------------------------

function installTagType(p) {
  if (p.install && p.install.running) return 'warning'
  return p.installed ? 'success' : 'danger'
}
function installTagText(p) {
  if (p.install && p.install.running) return '安装中…'
  if (p.installed) return '已就绪'
  const n = (p.needs || []).length
  return n ? `缺 ${n} 项依赖` : '未就绪'
}

let installTimer = null
function scheduleInstallPoll() {
  if (installTimer) return                    // 单例：多张卡同时装也只有一个轮询
  installTimer = setInterval(async () => {
    try {
      const list = await pluginsApi.list()
      if (!list.some((p) => p.install && p.install.running)) {
        clearInterval(installTimer); installTimer = null
        await load()                          // 装完整体刷新一次，状态与按钮一起就位
        return
      }
      // 安装中只更新进度字段，不整体替换 —— 否则用户正在输入的账号名会被冲掉
      for (const fresh of list) {
        const cur = plugins.value.find((x) => x.id === fresh.id)
        if (cur) { cur.install = fresh.install; cur.needs = fresh.needs; cur.installed = fresh.installed }
      }
    } catch (_) { clearInterval(installTimer); installTimer = null }
  }, 2000)
}
onBeforeUnmount(() => { if (installTimer) { clearInterval(installTimer); installTimer = null } })

async function doInstall(p) {
  const browserPart = p._withBrowser ? '，并下载 Playwright 浏览器内核（约 150MB）' : ''
  try {
    await ElMessageBox.confirm(
      `将为插件【${p.name}】建立独立 Python 环境并从 PyPI 安装依赖${browserPart}。`
      + '这一步需要联网，可能持续几分钟。确认继续？',
      '安装插件依赖', { type: 'warning', confirmButtonText: '开始安装', cancelButtonText: '取消' },
    )
  } catch (_) { return }
  try {
    await pluginsApi.install(p.id, p._withBrowser)
    p.install = { running: true, step: '准备', error: null }
    scheduleInstallPoll()
  } catch (e) {
    // 409 被 http 拦截器**刻意跳过**（留给页面自己处理），这里不显式弹就是点了毫无反馈。
    // 后端两条 409 分支里，「打包版内没有可用的 Python 解释器」是专门写给用户看的两句话操作指引，
    // 3 秒读不完 → 用 duration 8000（与同文件 doDeleteAccountStaging 的长文案一致）。
    // 不要写进 p.install.error：那是服务端字段，下一次 load()/轮询会用服务端的 {} 覆盖掉，
    // 表现成「错误提示自己消失」。
    if (e.response?.status === 409) {
      ElMessage({ type: 'error', duration: 8000, message: e.response?.data?.detail || '无法开始安装' })
    }
    // 其余状态码拦截器已提示
  }
}

async function saveConfig(p) {
  p._busy = true
  try {
    await pluginsApi.saveConfig(p.id, { enabled: p._form.enabled, params: {}, schedule_minutes: p._form.schedule_minutes })
    ElMessage.success('已保存')
    await load()
  } catch (_) { /* 拦截器已提示 */ } finally {
    p._busy = false
  }
}

async function doAddAccount(p) {
  const name = (p._add.name || '').trim()
  const platform = (p._add.platform || '淘宝').trim() || '淘宝'
  if (!name) { ElMessage.warning('请填账号昵称'); return }
  if (name.includes(',')) { ElMessage.warning('昵称不能含逗号'); return }
  p._busy = true
  try {
    await pluginsApi.addAccount(p.id, name, platform)
    ElMessage.success(`已添加账号「${name}」（${platform}）`)
    p._add.name = ''
    await load()
  } catch (e) {
    // 409（账号已存在）被 http 拦截器刻意跳过（留给页面处理），这里显式弹出后端 detail，否则静默无反馈
    if (e.response?.status === 409) ElMessage.error(e.response?.data?.detail || '账号已存在')
  } finally {
    p._busy = false
  }
}

async function doToggle(p, a, enabled) {
  try {
    await pluginsApi.setAccountEnabled(p.id, a.account, enabled)
  } catch (_) {
    a.enabled = !enabled   // 失败回滚开关
    await load()
  }
}

async function doLogin(p, account) {
  try {
    await pluginsApi.login(p.id, account)
    ElMessage.success(`已启动 ${account} 的登录，请在弹出的浏览器完成登录后点「刷新」`)
  } catch (_) { /* 拦截器已提示 */ }
}

async function doFetch(p, account) {
  try {
    await pluginsApi.fetch(p.id, account)
    ElMessage.success(account ? `已触发抓取：${account}` : '已触发抓取（全部启用账号）')
  } catch (_) { /* 拦截器已提示 */ }
}

async function doRenameAccount(p, account) {
  let value
  try {
    const r = await ElMessageBox.prompt(
      `给账号「${account}」改个名（只改昵称，平台不变）。会一并迁移它名下的暂存/账本订单、保留标签颜色、重命名本地登录会话。新名字须全新、不能含逗号。`,
      '账号改名',
      {
        confirmButtonText: '改名', cancelButtonText: '取消', inputValue: account,
        inputValidator: (v) => (!!v && !!v.trim() && !v.includes(',')) || '名字不能为空、且不能含逗号',
      },
    )
    value = r.value.trim()
  } catch (_) { return }   // 取消
  if (!value || value === account) return
  p._busy = true
  try {
    const res = await pluginsApi.renameAccount(p.id, account, value)
    if (res.warning) ElMessage.warning(res.warning)
    else ElMessage.success(`已改名为「${value}」（迁移订单：暂存 ${res.staging} / 账本 ${res.orders}）`)
    await load()
  } catch (e) {
    // 409（新名字已被占用）被 http 拦截器刻意跳过，这里显式弹出后端 detail，否则静默无反馈
    if (e.response?.status === 409) ElMessage.error(e.response?.data?.detail || '新名字已被占用')
  } finally {
    p._busy = false
  }
}

async function doDeleteAccount(p, account) {
  try {
    await ElMessageBox.confirm(
      `确定删除账号「${account}」？会删掉本地登录会话并从配置移除，之后需重新添加+扫码登录才能再抓。不动已抓进库的订单。`,
      '删除账号', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch (_) { return }   // 用户取消
  p._busy = true
  try {
    await pluginsApi.deleteAccount(p.id, account)
    ElMessage.success(`已删除账号 ${account}`)
    await load()
  } catch (_) { /* 拦截器已提示 */ } finally {
    p._busy = false
  }
}

async function doDeleteAccountStaging(p, account) {
  try {
    await ElMessageBox.confirm(
      `确定删除账号「${account}」在「暂存订单」里的全部暂存记录（含物品明细）？此操作不可恢复，且不影响已进账本的正式订单。`,
      '删除该账号的暂存单', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch (_) { return }
  p._busy = true
  try {
    const r = await pluginsApi.deleteAccountStaging(p.id, account)
    if (r.skipped) {
      ElMessage({
        type: 'warning', duration: 8000,
        message: `已删除 ${account} 的暂存单 ${r.deleted} 条；跳过 ${r.skipped} 条已导入的`
          + `（删了会在账本里留下导不回来的孤儿单，请先到「商品订单」页删掉对应订单）`,
      })
    } else {
      ElMessage.success(`已删除 ${account} 的暂存单 ${r.deleted} 条`)
    }
  } catch (_) { /* 拦截器已提示 */ } finally {
    p._busy = false
  }
}

async function doDeleteAccountOrders(p, account) {
  try {
    await ElMessageBox.confirm(
      `确定删除账号「${account}」名下的全部账本正式商品订单？将从账本移除（软删）。不影响暂存记录。`,
      '删除该账号的账本单', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch (_) { return }
  p._busy = true
  try {
    const r = await pluginsApi.deleteAccountOrders(p.id, account)
    ElMessage.success(`已删除 ${account} 的账本单 ${r.deleted} 条`)
  } catch (_) { /* 拦截器已提示 */ } finally {
    p._busy = false
  }
}

onMounted(load)
</script>

<style scoped>
.needs { margin-bottom: 12px; }
.needlist { margin: 6px 0 0; padding-left: 18px; font-size: 12px; line-height: 1.7; }
.needact { display: flex; align-items: center; gap: 12px; margin-top: 10px; }
.neederr { margin-top: 8px; font-size: 12px; color: var(--el-color-danger); white-space: pre-wrap; word-break: break-all; }
.bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.hint { color: #7d8aa3; font-size: 12px; flex: 1; }
.plugin { margin-bottom: 16px; }
.head { display: flex; align-items: center; gap: 10px; }
.pname { color: #e6edf7; font-size: 15px; font-weight: 600; }
.pver { color: #7d8aa3; font-size: 12px; }
.grow { flex: 1; }
.sw-label { color: #9ba8bf; font-size: 13px; }
.field { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.flabel { color: #9ba8bf; font-size: 13px; min-width: 180px; }
.sub { color: #7d8aa3; font-size: 12px; }
.sect { color: #c7d2e6; font-size: 13px; font-weight: 600; margin: 6px 0 10px; padding-top: 12px; border-top: 1px solid #1c2740; }
.acct { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.acct.dim .aname, .acct.dim .c-plat, .acct.dim .c-auth { opacity: 0.4; }   /* 只灰昵称/平台/授权，开关和按钮保持清晰可用 */
.c-sw { width: 40px; flex: none; display: inline-flex; }                   /* 固定列，孤儿无开关也占位，保证对齐 */
.aname { width: 104px; flex: none; color: #e6edf7; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.c-plat { min-width: 64px; flex: none; display: inline-flex; }
.c-auth { min-width: 58px; flex: none; display: inline-flex; }
.c-state { min-width: 56px; flex: none; display: inline-flex; }
</style>
