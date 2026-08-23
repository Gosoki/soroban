<template>
  <div class="db-page" v-loading="loadingStatus">
    <PageHeader>
      两个库：<b>控制库</b>恒为本地 SQLite（存「当前用哪个后端」与加密的连接串），
      <b>数据库</b>存全部业务数据、可在这里运行期热切换。
      「迁移」是把当前库的数据整表覆盖到目标；「切换」只改指向、不动任何数据。
    </PageHeader>

    <!-- 当前后端 -->
    <el-card shadow="never" class="card">
      <div class="card-hd">
        <span>当前使用</span>
        <el-tag v-if="status.active.backend"
                :style="typeStyle(status.active.backend === 'mysql' ? 'success' : 'info')">
          {{ activeLabel }}
        </el-tag>
      </div>
      <!-- 静默降级必须在**最显眼**的地方说出来：配置里写着 MySQL、连接串却解不开时，
           应用会退回本地 SQLite，而用户看到的现象是「账本全空了」。
           最容易踩到的路径是升级换目录忘了搬 .env（SECRET_KEY 一变就解不开）。 -->
      <el-alert v-if="status.active.degraded" type="error" show-icon :closable="false" class="degraded">
        <template #title>数据库已降级到本地 SQLite</template>
        {{ status.active.degraded }}
      </el-alert>
      <div class="hint">
        切换只改变「连接指向」，不迁移、不删除任何数据；如需目标数据最新，切换前先「迁移到此库」。
      </div>
    </el-card>

    <!-- 已保存 / 连接过的数据库 -->
    <el-card shadow="never" class="card">
      <div class="card-hd"><span>连接过的数据库</span></div>
      <el-table :data="rows" style="width: 100%">
        <el-table-column label="数据库" min-width="150">
          <template #default="{ row }">
            <el-icon class="row-ic"><Coin v-if="row.kind === 'mysql'" /><Files v-else /></el-icon>
            {{ row.label }}
          </template>
        </el-table-column>
        <el-table-column prop="desc" label="地址" min-width="200" />
        <el-table-column label="状态" width="76" align="center">
          <template #default="{ row }">
            <el-tag v-if="isActive(row)" :style="typeStyle('success')">当前</el-tag>
            <el-tag v-else-if="row.locked" :style="typeStyle('warning')">密钥已变</el-tag>
            <span v-else class="ph">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" align="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="!!busy || isActive(row) || row.locked"
                       :title="row.locked ? LOCKED_WHY : ''"
                       @click="doMigrate(targetOf(row), row.label)">迁移到此库</el-button>
            <el-button link type="warning" :disabled="!!busy || isActive(row) || row.locked"
                       :title="row.locked ? LOCKED_WHY : ''"
                       @click="doSwitch(targetOf(row), row.label)">切换</el-button>
            <el-button v-if="row.kind === 'mysql'" link type="danger"
                       :disabled="!!busy || isActive(row)" @click="doDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 连接新的 MySQL -->
    <el-card shadow="never" class="card">
      <div class="card-hd"><span>连接新的 MySQL</span></div>
      <el-form :model="form" label-width="92px" class="form" @submit.prevent>
        <!-- 连接参数按可用宽度自动分列（窄屏仍是一列）。一个连接串的五个字段
             本来就是一组，横着排比竖着排一长条更好扫，也不至于让输入框拉到 1300px 宽。 -->
        <div class="field-grid">
        <el-form-item label="主机"><el-input v-model="form.host" placeholder="127.0.0.1" /></el-form-item>
        <el-form-item label="端口">
          <el-input v-model.number="form.port" placeholder="3306" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="form.user" name="soroban-mysql-user"
                    autocomplete="off" :readonly="ro.user" @focus="ro.user = false" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" show-password
                    name="soroban-mysql-pass" autocomplete="new-password"
                    :readonly="ro.pass" @focus="ro.pass = false" />
        </el-form-item>
        <el-form-item label="数据库名"><el-input v-model="form.database" placeholder="soroban" /></el-form-item>
        </div>
        <el-form-item>
          <!-- 任一操作进行中(busy)全部禁用：与表格行按钮同规则，杜绝迁移中又点切换的并发操作 -->
          <el-button :disabled="!!busy" :loading="busy === 'test'" @click="onTest">测试连接并记住</el-button>
          <el-button type="primary" :disabled="!!busy" :loading="busy === 'migrate'" @click="doMigrate(formTarget(), form.database)">迁移到此库</el-button>
          <el-button type="warning" :disabled="!!busy" :loading="busy === 'switch'" @click="doSwitch(formTarget(), form.database)">切换到此库</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 备份 -->
    <el-card shadow="never" class="card">
      <div class="card-hd">
        <span>备份</span>
        <el-tag v-if="backups.length" :style="typeStyle('info')">{{ backups.length }} 份</el-tag>
      </div>
      <div class="hint">
        把<b>当前正在用的那个库</b>整本拷成一个独立的 SQLite 文件（换成 MySQL 之后也一样）。
        拷贝那几秒会短暂只读，别人的保存会自动重试。
        恢复不在这里——那是唯一一条能一键清空账本的操作，要到服务器上执行
        <code>python -m tools.backup_db --restore &lt;文件&gt;</code> 并手敲一次确认。
      </div>
      <div class="bk-actions">
        <el-button type="primary" :disabled="!!busy" :loading="busy === 'backup'"
                   @click="doBackup">立刻备份</el-button>
        <span v-if="backupDir" class="ph">存放在 {{ backupDir }}</span>
      </div>
      <el-table v-if="backups.length" :data="backups" style="width: 100%">
        <el-table-column prop="name" label="文件" min-width="200" />
        <el-table-column prop="when" label="时间" width="180" />
        <el-table-column prop="size" label="大小" width="110" align="right" />
      </el-table>
      <!-- **空态不许说假话。** 请求失败时 `backupList` 也是空的，
           照着说「还没有备份」会让人以为备份从来没跑过——而实际可能是接口挂了，
           备份好好躺在目录里。这一页的其它地方（降级提示）也是这个口径。 -->
      <div v-else class="hint">
        {{ backupsFailed ? MSG_LOAD_FAILED : '还没有备份。' }}
      </div>
    </el-card>

    <!-- 迁移结果 -->
    <el-card v-if="result" shadow="never" class="card">
      <div class="card-hd"><span>迁移完成</span><el-tag :style="typeStyle('success')">共 {{ result.total }} 行</el-tag></div>
      <el-table :data="resultRows" style="width: 100%">
        <el-table-column prop="table" label="表" />
        <el-table-column prop="rows" label="行数" width="120" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import PageHeader from '@/components/PageHeader.vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Coin, Files } from '@element-plus/icons-vue'
import { fmtDateTime } from '@/utils/datetime'
import { dbApi } from '@/api'
import { handled } from '@/api/http'
import { MSG_LOAD_FAILED, longToast, typeStyle } from '@/constants'

// active 初值刻意留空：给 `{ backend: 'sqlite' }` 的话，首帧必然渲染成
// 「SQLite（本地文件）」并给本地那行挂上「当前」标签——实际连着 MySQL 时，
// 用户先看到一个**错误的结论**再被纠正。宁可空一瞬，也不要先说错话。
const status = reactive({ active: {}, connections: [] })
const loadingStatus = ref(false)
const form = reactive({ host: '', port: 3306, user: '', password: '', database: 'soroban' })
// 初始 readonly，聚焦解除 → 阻止浏览器自动填充登录账号
const ro = reactive({ user: true, pass: true })
const busy = ref(null)          // null | 'test' | 'migrate' | 'switch' | 'backup'（单操作串行，期间禁用按钮）
const result = ref(null)

const activeLabel = computed(() => {
  const a = status.active
  if (a.backend !== 'mysql') return 'SQLite（本地文件）'
  return `MySQL · ${a.user}@${a.host}:${a.port}/${a.database}`
})

// 列表行：内置本地 SQLite 恒在最前，后接已保存的 MySQL
const rows = computed(() => [
  { id: 'local', kind: 'sqlite', label: '本地 SQLite', desc: 'soroban.db（文件）' },
  ...status.connections.map((c) => ({
    id: c.id, kind: 'mysql', label: c.label,
    desc: `${c.user}@${c.host}:${c.port}/${c.database}`,
    host: c.host, port: c.port, user: c.user, database: c.database,
    // SECRET_KEY 变过之后这条的 DSN 就再也解不开了。列表照列（跳过等于「记录凭空消失」），
    // 但迁移/切换都会拿到 404「连接不存在或无法解密」——「明明列在这里」却说「不存在」，
    // 是一条读不懂的死路。所以在这里就标出来并禁掉那两个按钮，删除保持可用（那是出口）。
    locked: c.decryptable === false,
  })),
])
const LOCKED_WHY = 'SECRET_KEY 变过，这条连接的密码解不开了。请在下面「连接新的 MySQL」重填一次，或直接删掉这条。'
const resultRows = computed(() =>
  result.value ? Object.entries(result.value.counts).map(([table, rows]) => ({ table, rows })) : [])

function isActive(row) {
  const a = status.active
  if (row.kind === 'sqlite') return a.backend === 'sqlite'
  return a.backend === 'mysql' && a.host === row.host && Number(a.port) === Number(row.port)
    && a.user === row.user && a.database === row.database
}
function targetOf(row) {
  return row.kind === 'sqlite' ? { backend: 'sqlite' } : { connection_id: row.id }
}
function formTarget() {
  return { backend: 'mysql', host: form.host, port: form.port, user: form.user,
    password: form.password, database: form.database }
}

const backupList = ref([])
// 上一次加载是否失败：空态文案据此说实话（与 Orders/Fx 等页同一口径）。
const backupsFailed = ref(false)
const backupDir = ref('')
const backups = computed(() => backupList.value.map((b) => ({
  name: b.name,
  when: fmtDateTime(b.mtime),      // 与全站其它时间戳走同一条管线（UTC → 本地）
  size: b.bytes >= 1048576 ? `${(b.bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b.bytes / 1024))} KB`,
})))

async function loadBackups() {
  try {
    const r = await dbApi.backups()
    backupList.value = r.items || []
    backupDir.value = r.dir || ''
    backupsFailed.value = false
  } catch (_) {
    // 拦截器已提示原因；这里负责让**页面本身**留下痕迹，否则空态在说假话。
    backupsFailed.value = true
  }
}

async function doBackup() {
  busy.value = 'backup'
  try {
    const r = await dbApi.createBackup()
    ElMessage.success(`已备份 ${r.total} 行 → ${r.file}`)
    await loadBackups()
  } catch (_) { /* 拦截器已提示；409「已有另一项维护操作在进行」也走这里 */ } finally { busy.value = null }
}

async function loadStatus() {
  loadingStatus.value = true
  try {
    const s = await dbApi.status()
    status.active = s.active || {}
    status.connections = s.connections || []
  } catch (_) { /* 拦截器已提示 */ } finally { loadingStatus.value = false }
}

async function onTest() {
  busy.value = 'test'
  try {
    const r = await dbApi.test(formTarget())
    ElMessage.success(r.note ? r.note : `连接成功，MySQL ${r.version}`)
    await loadStatus()          // 已记住 → 刷新列表，可一键切换
  } catch (_) { /* 拦截器已提示 */ } finally { busy.value = null }
}

// 迁移会**先删光目标库的业务表**再整表覆盖。后端在目标非空时返回 409，
// 并把「对面现在有什么」逐表列出来——用户要判断的是「值不值得覆盖」，
// 只说一句「会覆盖目标同名表」他没法判断。目标是空库时没有任何东西可丢，不打断。
//
// **两个入口都必须走这里**：按钮（doMigrate）与「当前库有未迁移的改动」弹窗里的
// 「重新迁移再切换」（onSourceChanged）。后者原先直接调 dbApi.migrate，一句确认都没有——
// 而它恰恰是那个弹窗的**默认按钮**，且那条路上目标库往往比当前库新得多
// （MySQL 断线 → --use-local-db 退回本地 → 本地补记几单 → 回来点切换 → 409 → 默认按钮）。
async function migrateWithOverwriteGuard(target) {
  try {
    return await dbApi.migrate(target)
  } catch (e) {
    // **只认「目标库里已经有数据」那一种 409。** `migrate` 还会因为
    // 「已有另一项维护操作在进行」返回 409（备份/另一次迁移在跑）——
    // 不区分的话，用户会在一条讲维护中的消息上点下「仍然覆盖」，
    // 而重试带上 `confirm_overwrite: true` 正好**跳过了这道闸本身**。
    // 那句话由后端的 `test_the_overwrite_409_is_recognisable` 钉住，不会漂。
    const detail = e.response?.data?.detail
    if (e.response?.status !== 409 || typeof detail !== 'string'
        || !detail.includes('目标库里已经有数据')) throw e
    handled(e)
    await ElMessageBox.confirm(
      e.response?.data?.detail || '目标库里已经有数据，迁移会把它们全部覆盖。',
      '目标库里已有数据',
      { type: 'warning', confirmButtonText: '仍然覆盖', cancelButtonText: '取消' },
    )
    return await dbApi.migrate({ ...target, confirm_overwrite: true })
  }
}

async function doMigrate(target, name) {
  try {
    await ElMessageBox.confirm(
      `将建库/建表，并把【当前数据库】的数据整表覆盖到【${name}】。此步不切换、不改动当前库。确认继续？`,
      '迁移数据库', { type: 'warning', confirmButtonText: '开始迁移', cancelButtonText: '取消' },
    )
  } catch (_) { return }
  busy.value = 'migrate'
  result.value = null
  try {
    const r = await migrateWithOverwriteGuard(target)
    result.value = r
    ElMessage.success(`迁移完成，共 ${r.total} 行。确认无误后可「切换」到该库`)
    await loadStatus()
  } catch (_) { /* 取消覆盖，或拦截器已提示 */ } finally { busy.value = null }
}

async function doSwitch(target, name) {
  try {
    await ElMessageBox.confirm(
      `将热切换到【${name}】（仅改变连接，不迁移、不删除任何数据）。请确保已先「迁移到此库」使其数据最新。确认继续？`,
      '切换数据库', { type: 'warning', confirmButtonText: '切换', cancelButtonText: '取消' },
    )
  } catch (_) { return }
  busy.value = 'switch'
  try {
    await dbApi.switch(target)
    ElMessage.success(`已切换到 ${name}`)
    await loadStatus()
  } catch (e) {
    // 409 = 后端发现「迁移之后当前库又被改过」。这些改动不在目标库里，切过去就静默没了。
    // 409 被 http 拦截器刻意跳过（留给页面处理），所以这里必须自己弹，否则用户什么都看不到。
    if (e.response?.status === 409) {
      handled(e)
      await onSourceChanged(target, name, e.response?.data?.detail)
    }
    // 其余错误拦截器已提示
  } finally { busy.value = null }
}

// 源库在迁移后又有改动：让用户在「重新迁移」与「放弃这些改动」之间明确选一个，
// 而不是替他决定。默认按钮是「重新迁移」——那才是不丢数据的那条路。
async function onSourceChanged(target, name, detail) {
  let choice
  try {
    choice = await ElMessageBox.confirm(
      detail || '迁移之后当前库又有改动，直接切换会丢失。',
      '当前库有未迁移的改动',
      {
        type: 'warning',
        distinguishCancelAndClose: true,
        confirmButtonText: '重新迁移再切换',
        cancelButtonText: '仍然切换（放弃这些改动）',
        // 全项目**唯一**一处「取消位不是退出键」。其余 17 处 cancelButtonText 都是「取消」，
        // 用户的肌肉记忆是「点左边 = 什么都没发生」——而这里点下去会不可逆地丢掉改动。
        // 染成 danger 让它在视觉上先自我否认一次：这个位置上的红按钮不是退出键。
        // 「什么都不做」由 × / Esc 承接（上面的 distinguishCancelAndClose 就是为此）。
        cancelButtonClass: 'el-button--danger',
      },
    )
  } catch (action) {
    if (action !== 'cancel') return          // 点 × 或按 Esc = 什么都不做
    choice = 'discard'
  }
  busy.value = 'switch'
  try {
    if (choice === 'discard') {
      await dbApi.switch({ ...target, confirm_changed: true })
      ElMessage.warning(`已切换到 ${name}（放弃了未迁移的改动）`)
    } else {
      // 先补一次迁移，把新改动带过去。走 guard：目标库非空时先说清楚要删掉什么。
      await migrateWithOverwriteGuard(target)
      await dbApi.switch(target)
      ElMessage.success(`已重新迁移并切换到 ${name}`)
    }
    await loadStatus()
  } catch (e) {
    // migrate 与 switch 都可能再返回 409，而 409 被 http 拦截器**刻意跳过**（留给页面处理）。
    // 这里原先是空 catch：弹窗关掉、loading 停掉、既无成功也无失败提示、loadStatus 也被跳过
    // —— 用户完全不知道到底切没切。上面 doSwitch 为同一件事写了处理，这条分支漏了。
    if (e.response?.status === 409) {
      handled(e)
      // 只提示不递归：迁移与切换之间源库又被写了（爬虫回灌 / 汇率刷新 / 另一个标签页）。
      // 递归重试会在爬虫逐单回灌时变成关不掉的弹窗循环。
      longToast(ElMessage, 'warning',
        (e.response?.data?.detail || '迁移与切换之间源库又有改动')
        + '——已取消切换，当前仍连着原来的库。请等抓取/刷新结束后再试一次。')
    }
    // 其余错误拦截器已提示
    await loadStatus()                       // 无论成败都把真实状态拉回来，别让界面停在猜测里
  } finally { busy.value = null }
}

async function doDelete(row) {
  try {
    await ElMessageBox.confirm(
      `删除连接记录【${row.label}】？仅删本地记录，不影响 MySQL 里的数据。`,
      '删除连接', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch (_) { return }
  try {
    await dbApi.removeConnection(row.id)
    ElMessage.success('已删除')
    await loadStatus()
  } catch (_) { /* 拦截器已提示 */ }
}

onMounted(() => { loadStatus(); loadBackups() })
</script>

<style scoped>
/* 页宽不再自己设上限：这一页有两张**表格**（连接过的库），760px 下 DSN 那列会折行，
   而右边空着 600px。统一成「占满宽度 + 字段自动分列」，见 tokens.css 的 .field-grid。 */
.card { margin-bottom: 16px; }
/* 刻意**不用** space-between：「当前使用」与它右边那个后端标签是标题和值，
   页宽放开后 space-between 会把它们推到相隔一千多像素的两端，读起来不像一组。
   与插件页卡片头同一套做法：相关的挤在左边，真正的「操作」才用 .grow 顶到右边。 */
.card-hd { display: flex; align-items: center; gap: 12px; font-weight: 600; margin-bottom: 10px; }
/* 与插件页的 .needs 同一档间距：三处 el-alert 都是「要人处理的异常态」，
   同一种组件不该有两种排版。 */
.degraded { margin-bottom: 12px; }
.hint { color: var(--txt-3); font-size: 12px; }
.row-ic { margin-right: 4px; vertical-align: -2px; }
/* 按钮与它右边那句说明是一组，走 gap 而不是给按钮挂 margin——与本页其它成组元素同一套。 */
.bk-actions { display: flex; align-items: center; gap: 12px; margin: 10px 0; }
.form { margin-top: 6px; }

/* 去掉浏览器自动填充（用户名/密码）留下的黄/蓝底色，保持与其它输入框一致 */
.form :deep(input:-webkit-autofill),
.form :deep(input:-webkit-autofill:hover),
.form :deep(input:-webkit-autofill:focus),
.form :deep(input:-webkit-autofill:active) {
  -webkit-box-shadow: 0 0 0 1000px var(--el-input-bg-color, transparent) inset !important;
  -webkit-text-fill-color: var(--el-input-text-color, inherit) !important;
  caret-color: var(--el-input-text-color, inherit);
  transition: background-color 99999s ease-out 0s !important;
}
</style>
