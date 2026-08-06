<template>
  <div class="db-page">
    <h2 class="title">数据库</h2>

    <!-- 当前后端 -->
    <el-card shadow="never" class="card">
      <div class="card-hd">
        <span>当前使用</span>
        <el-tag :type="status.active.backend === 'mysql' ? 'success' : 'info'" effect="dark">
          {{ activeLabel }}
        </el-tag>
      </div>
      <div class="hint">
        切换只改变「连接指向」，不迁移、不删除任何数据；如需目标数据最新，切换前先「迁移到此库」。
      </div>
    </el-card>

    <!-- 已保存 / 连接过的数据库 -->
    <el-card shadow="never" class="card">
      <div class="card-hd"><span>连接过的数据库</span></div>
      <el-table :data="rows" size="small" style="width: 100%">
        <el-table-column label="数据库" min-width="150">
          <template #default="{ row }">
            <el-icon class="row-ic"><Coin v-if="row.kind === 'mysql'" /><Files v-else /></el-icon>
            {{ row.label }}
          </template>
        </el-table-column>
        <el-table-column prop="desc" label="地址" min-width="200" />
        <el-table-column label="状态" width="76" align="center">
          <template #default="{ row }">
            <el-tag v-if="isActive(row)" type="success" size="small">当前</el-tag>
            <span v-else class="dash">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" align="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="!!busy || isActive(row)"
                       @click="doMigrate(targetOf(row), row.label)">迁移到此库</el-button>
            <el-button link type="warning" :disabled="!!busy || isActive(row)"
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
        <el-form-item label="主机"><el-input v-model="form.host" placeholder="127.0.0.1" /></el-form-item>
        <el-form-item label="端口">
          <el-input v-model.number="form.port" placeholder="3306" style="max-width: 160px" />
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
        <el-form-item>
          <!-- 任一操作进行中(busy)全部禁用：与表格行按钮同规则，杜绝迁移中又点切换的并发操作 -->
          <el-button :disabled="!!busy" :loading="busy === 'test'" @click="onTest">测试连接并记住</el-button>
          <el-button type="primary" :disabled="!!busy" :loading="busy === 'migrate'" @click="doMigrate(formTarget(), form.database)">迁移到此库</el-button>
          <el-button type="warning" :disabled="!!busy" :loading="busy === 'switch'" @click="doSwitch(formTarget(), form.database)">切换到此库</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 迁移结果 -->
    <el-card v-if="result" shadow="never" class="card">
      <div class="card-hd"><span>迁移完成</span><el-tag type="success">共 {{ result.total }} 行</el-tag></div>
      <el-table :data="resultRows" size="small" style="width: 100%">
        <el-table-column prop="table" label="表" />
        <el-table-column prop="rows" label="行数" width="120" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Coin, Files } from '@element-plus/icons-vue'
import { dbApi } from '@/api'

const status = reactive({ active: { backend: 'sqlite' }, connections: [] })
const form = reactive({ host: '', port: 3306, user: '', password: '', database: 'soroban' })
// 初始 readonly，聚焦解除 → 阻止浏览器自动填充登录账号
const ro = reactive({ user: true, pass: true })
const busy = ref(null)          // null | 'test' | 'migrate' | 'switch'（单操作串行，期间禁用按钮）
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
  })),
])
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

async function loadStatus() {
  try {
    const s = await dbApi.status()
    status.active = s.active || { backend: 'sqlite' }
    status.connections = s.connections || []
  } catch (_) { /* 拦截器已提示 */ }
}

async function onTest() {
  busy.value = 'test'
  try {
    const r = await dbApi.test(formTarget())
    ElMessage.success(r.note ? r.note : `连接成功，MySQL ${r.version}`)
    await loadStatus()          // 已记住 → 刷新列表，可一键切换
  } catch (_) { /* 拦截器已提示 */ } finally { busy.value = null }
}

async function doMigrate(target, name) {
  try {
    await ElMessageBox.confirm(
      `将建库/建表，并把【当前数据库】的数据整表覆盖到【${name}】（会覆盖目标同名表）。此步不切换、不改动当前库。确认继续？`,
      '迁移数据库', { type: 'warning', confirmButtonText: '开始迁移', cancelButtonText: '取消' },
    )
  } catch (_) { return }
  busy.value = 'migrate'
  result.value = null
  try {
    const r = await dbApi.migrate(target)
    result.value = r
    ElMessage.success(`迁移完成，共 ${r.total} 行。确认无误后可「切换」到该库`)
    await loadStatus()
  } catch (_) { /* 拦截器已提示 */ } finally { busy.value = null }
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
      await dbApi.migrate(target)            // 先补一次迁移，把新改动带过去
      await dbApi.switch(target)
      ElMessage.success(`已重新迁移并切换到 ${name}`)
    }
    await loadStatus()
  } catch (_) { /* 拦截器已提示 */ } finally { busy.value = null }
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

onMounted(loadStatus)
</script>

<style scoped>
.db-page { max-width: 760px; }
.title { margin: 0 0 16px; font-size: 20px; }
.card { margin-bottom: 16px; }
.card-hd { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-weight: 600; margin-bottom: 10px; }
.hint { color: #909399; font-size: 12px; }
.dash { color: #c0c4cc; }
.row-ic { margin-right: 4px; vertical-align: -2px; }
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
