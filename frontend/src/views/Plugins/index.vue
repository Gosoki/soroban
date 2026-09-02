<template>
  <div>
    <PageHeader>
      soroban 扫 plugins/ 下 soroban-plugin-* 目录作为插件。这里加账号、授权、启停、定时。
      抓取类插件抓到的单进「暂存订单」待处理。
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
      </template>
    </PageHeader>

    <!-- **三分支，顺序要紧**：失败 → 真空 → 卡片列表。
         「plugins/ 下没有目录」是全应用唯一一句**断言用户磁盘**的空态，而它原先在请求失败时
         照样显示——一个配好账号、授权、定时的人，首次进页面就被告知插件目录是空的，
         自然反应是去查目录、重拷插件、重建 venv（重下 Playwright 浏览器）。
         而这条路径特别容易撞上：GET /api/plugins 允许每个插件跑 30-60 秒的子进程探测，
         客户端 axios 超时只有 15 秒，装完依赖后的第一次刷新几乎必然超时。 -->
    <el-empty v-if="loadFailed" :description="MSG_LOAD_FAILED" />
    <el-empty v-else-if="!loading && !plugins.length"
              description="未发现插件（plugins/ 下没有 soroban-plugin-* 目录）" />

    <el-card v-for="p in plugins" :key="p.id" class="plugin" v-loading="p._busy">
      <template #header>
        <div class="head">
          <!-- 一个开关就是「启用/停用」：停用后定时不跑、命令按钮也点不动。
               原先叫「启用定时」，但它其实是这个插件的总开关——名字比实际管得窄，
               会让人以为「停用了还能手动点一下」。 -->
          <el-switch v-if="!p.missing" v-model="p._form.enabled" :disabled="!p.installed" @change="saveConfig(p)"
                     :title="p._form.enabled ? '已启用（定时与手动执行都可用）' : '已停用（定时不跑，命令也点不动）'" />
          <span class="pname" :class="{ off: !p._form.enabled }">{{ p.name }}</span>
          <span class="pver">v{{ p.version || '?' }}</span>
          <el-tag :style="typeStyle(installTagType(p))">{{ installTagText(p) }}</el-tag>
          <el-tag v-if="p.last_run.outcome" :style="typeStyle(runTagType(p.last_run.outcome))"
                  :title="p.last_run.summary + (p.last_run.at ? ' · ' + fmtTime(p.last_run.at) : '')">
            {{ { ok: '成功', warn: '有警告', failed: '失败', running: '执行中' }[p.last_run.outcome] || p.last_run.outcome }}
          </el-tag>
          <el-tag v-if="pendingGrants(p).length" :style="typeStyle('warning')"
                  :title="`还没授权：${pendingGrants(p).join('、')}——展开卡片勾选`">需要授权</el-tag>
          <!-- 「上次**成功**抓取」是与「上次跑过」不同的一件事，也是这套系统里最安静的
               那类故障唯一会露头的地方：爬虫的登录会话过期之后，每次定时都照跑、照失败、
               `last_run.at` 一直很新，没有任何一处会变红——而暂存里已经两周没进新单了。
               超过阈值就把这个标签变黄，它是唯一会主动说「不对劲」的东西。 -->
          <el-tag v-if="p.last_run.ok_at" :style="typeStyle(staleOk(p) ? 'warning' : 'info')"
                  :title="`上次成功抓取：${fmtTime(p.last_run.ok_at)}`">
            成功 {{ fmtAgo(p.last_run.ok_at) }}
          </el-tag>
          <el-tag v-else-if="p.installed && p.last_run.outcome" :style="typeStyle('warning')"
                  title="这个插件跑过，但一次都没成功过。展开卡片看看它最后说了什么。">
            从未成功
          </el-tag>
          <span v-if="p.last_run.summary" class="lastsum">{{ p.last_run.summary }}</span>

          <div class="grow" />
          <!-- 命令按钮留在卡片头：它们是每天要点的，不该藏进折叠里 -->
          <el-button v-for="c in (p.missing ? [] : p.commands)" :key="c.name"
                     :type="c.primary ? 'primary' : 'default'"
                     :disabled="!p.installed || !p._form.enabled || !!c.blocked.length
                                || (c.per === 'account' && !enabledCount(p))"
                     :title="cmdTitle(p, c)" @click.stop="doRun(p, c)">
            {{ c.label }}<template v-if="c.per === 'account'">（{{ enabledCount(p) }}）</template>
          </el-button>
          <!-- 目录已不在、库里还留着配置的插件：不给开关与命令，只给一个清理入口。
               它带着用户当初给的授权——不显示的话，以后放一个同 id 的插件进来会静默继承。 -->
          <el-button v-if="p.missing" type="danger" plain @click="doForget(p)">
            清理残留配置
          </el-button>
          <el-button v-else link :icon="p._open ? ArrowUp : ArrowDown"
                     :title="p._open ? '收起' : '展开设置'" @click="p._open = !p._open" />
        </div>
      </template>

      <!-- 缺依赖：说清缺什么 + 一键补齐。不列出来的话，用户只看到一片灰按钮而无从下手 -->
      <el-alert v-if="p.needs && p.needs.length" type="warning" show-icon :closable="false" class="needs">
        <template #title>
          <span v-if="p.install && p.install.running">正在安装：{{ p.install.step }}…</span>
          <span v-else>插件还缺以下依赖，装好后才能添加账号与抓取</span>
        </template>
        <ul class="needlist">
          <li v-for="n in p.needs" :key="n.key"><b>{{ n.label }}</b> —— {{ n.hint }}</li>
        </ul>
        <div class="needact">
          <el-button type="primary" :loading="!!(p.install && p.install.running)"
                     @click="doInstall(p)">
            {{ p.install && p.install.running ? '安装中…' : '一键安装' }}
          </el-button>
          <el-checkbox v-model="p._withBrowser" :disabled="!!(p.install && p.install.running)">
            一并下载浏览器内核（约 150MB，仅首次）
          </el-checkbox>
        </div>
        <div v-if="p.install && p.install.error" class="neederr">{{ p.install.error }}</div>
      </el-alert>

      <!-- 清单坏了也要列出来并说明原因，而不是让插件从界面上消失 -->
      <el-alert v-if="p.manifest_error" :type="p.missing ? 'warning' : 'error'" show-icon :closable="false" class="needs"
                :title="p.missing
                  ? `插件目录已不在，库里还留着它的配置${p.scopes.granted.length ? '（含授权：' + p.scopes.granted.join('、') + '）' : ''}`
                  : `plugin.toml 有问题：${p.manifest_error}`" />

      <!-- 折叠区：这三块都是「配一次就不动」的。卡片上每天要看的只有「上次结果」
           与命令按钮，它们留在外面；其余收起来，卡片才不会长得像配置文件。
           `v-model` 记在 _form.open 里，刷新（每 3 秒轮询安装进度）不会把用户展开的合上。 -->
      <div v-show="p._open" class="body">
      <div class="field">
        <label class="flabel">定时执行（分钟，0=关闭）</label>
        <el-input-number v-model="p._form.schedule_minutes" :min="0" :step="30" />
        <el-button type="primary" @click="saveConfig(p)">保存</el-button>
        <span class="sub">{{ p.config.last_run_at ? '上次触发 ' + fmtTime(p.config.last_run_at) : '尚未执行' }}</span>
      </div>

      <!-- 参数：插件私有的，核心不理解其含义，只按类型渲染控件 -->
      <template v-if="p.params.length">
        <div class="subsect">参数</div>
        <div v-for="pa in p.params" :key="pa.key" class="field">
          <label class="flabel">
            {{ pa.label }}
            <el-tooltip v-if="pa.hint" :content="pa.hint" placement="top" popper-class="wrap-tip">
              <el-icon class="help"><QuestionFilled /></el-icon>
            </el-tooltip>
          </label>
          <el-switch v-if="pa.type === 'bool'" v-model="p._form.params[pa.key]" />
          <el-input-number v-else-if="pa.type === 'int'" v-model="p._form.params[pa.key]"
                           :min="pa.min" :max="pa.max" :controls="false" style="width: 120px" />
          <el-select v-else-if="pa.type === 'select'" v-model="p._form.params[pa.key]" style="width: 160px">
            <el-option v-for="o in pa.choices" :key="o" :label="o" :value="o" />
          </el-select>
          <el-input v-else v-model="p._form.params[pa.key]" style="width: 220px"
                    :type="pa.secret ? 'password' : 'text'" :show-password="pa.secret"
                    :placeholder="pa.secret && pa.value === '__set__' ? '已设置（留空保持不变）' : String(pa.default ?? '')" />
        </div>
        <div class="field">
          <el-button type="primary" @click="saveParams(p)">保存参数</el-button>
          <span class="sub">保存后下一次执行即按新值跑</span>
        </div>
      </template>
      <div v-if="p.accounts_enabled" class="sect">账号（{{ enabledCount(p) }} 启用 / {{ p.accounts.length }}）</div>
      <!-- 账号列表 -->
      <div v-if="p.accounts_enabled && !p.accounts.length" class="sub">还没有账号——用下面「添加账号」加一个。</div>
      <div v-for="a in (p.accounts_enabled ? p.accounts : [])" :key="a.account" class="acct" :class="{ dim: !a.enabled }">
        <span class="c-sw">
          <el-switch v-if="a.configured" v-model="a.enabled" :disabled="!p.installed"
                     title="停用后定时与「抓取全部账号」都跳过它" @change="(v) => doToggle(p, a, v)" />
        </span>
        <span class="aname" :title="a.account">{{ a.account }}</span>
        <span class="c-plat">
          <el-tag v-if="a.platform" :style="platformTagStyle(a.platform)">{{ a.platform }}</el-tag>
        </span>
        <span class="c-auth">
          <el-tag :style="typeStyle(a.authorized ? 'success' : 'warning')">
            {{ a.authorized ? '已授权' : '未授权' }}
          </el-tag>
        </span>
        <span class="c-state">
          <el-tag v-if="!a.configured" :style="typeStyle('info')"
                  title="磁盘上有此账号的登录会话，但没作为账号添加。想纳管就用上面「添加账号」加同名账号。">未添加</el-tag>
          <el-tag v-else-if="!a.enabled" :style="typeStyle('info')">未启用</el-tag>
        </span>

        <!-- 账号级命令**按清单渲染**，判据与卡片头那排同源。
             原先这里写死了 login / fetch 两个动词，三个后果：
               · 不判「插件已停用」也不判「缺权限」⇒ 点了拿 409，
                 而 `manifest.Command.needs` 的定义原话是「缺了就不给点（而不是点了 403）」；
               · 清单里的 label / hint / confirm 在这条路径上全被忽略
                 （插件把 fetch 的 label 写成「同步订单」，用户看到的仍是「抓这个号」）；
               · 第二个账号型插件（动词叫 sync / auth）在账号行上没有任何执行入口。
             按钮文案直接用清单的 label——「已授权 / 未授权」左边那个标签已经在说状态了，
             不必再让按钮名去兼这个职（那正是原先要靠动词名特判的原因）。 -->
        <el-button v-for="c in accountCommands(p)" :key="c.name"
                   link :type="c.primary ? 'primary' : 'default'"
                   :disabled="!p.installed || !p._form.enabled || !!c.blocked.length
                              || (c.needs_session && !a.authorized)"
                   :title="accountCmdTitle(p, c, a)" @click="doRun(p, c, a.account)">
          {{ c.label }}
        </el-button>
        <el-button link @click="doRenameAccount(p, a.account)">改名</el-button>
        <el-button link type="danger" @click="doDeleteAccountStaging(p, a.account)">删暂存单</el-button>
        <el-button link type="danger" @click="doDeleteAccountOrders(p, a.account)">删账本单</el-button>
        <div class="grow" />
        <el-button link type="danger" @click="doDeleteAccount(p, a.account)">删除</el-button>
      </div>
      <!-- 账号：只有清单声明了 accounts 的插件才有这个维度。
           汇率、快递查询这类插件卡片上不该出现「添加账号」——那是纯噪音，
           而且会让人以为是自己漏配了什么。 -->
      <div v-if="p.accounts_enabled" class="subsect">添加账号</div>
      <div v-if="p.accounts_enabled" class="field">
        <el-input v-model="p._add.name" placeholder="账号昵称" style="width: 160px"
                  @keyup.enter="doAddAccount(p)" />
        <!-- 清单声明了 `account_platforms` ⇒ 只能从它里面选（后端也按它校验，
             打错一个字就是一个新平台，而平台是账本活跃唯一键的一半）。
             没声明 ⇒ 沿用旧行为：全站标签候选 + 可自由新建。 -->
        <el-select v-model="p._add.platform"
                   :filterable="!p.account_platforms?.length"
                   :allow-create="!p.account_platforms?.length" default-first-option
                   placeholder="导入平台" style="width: 140px">
          <el-option v-for="o in (p.account_platforms?.length ? p.account_platforms : platformOpts)"
                     :key="o" :label="o" :value="o" />
        </el-select>
        <el-button type="primary" :disabled="!p.installed" @click="doAddAccount(p)">添加</el-button>
        <span class="sub">平台加时确定、之后不可改（改名只改昵称）</span>
      </div>

      <!-- 权限：所有插件一律排在卡片**最末**，且自成一个折叠段。
           它是「配一次就不动」里最不常动的那一块，排在参数和账号之前只会把
           每天真要用的东西往下顶；每张卡片位置一致，扫的时候不用重新找。
           比值只数**你能勾的那些**：baseline 项勾选框里根本没有，把它算进分子
           会让「一项都没勾」显示成 1/1。它单独列在下面一行，明说默认持有。 -->
      <div class="sect grantsect" :class="{ open: p._grants }" @click="p._grants = !p._grants">
        <el-icon class="caret"><ArrowRight /></el-icon>
        <span>权限（已授权 {{ grantedCount(p) }} / 声明 {{ p.scopes.declared.length }}）</span>
        <el-tag v-if="pendingGrants(p).length" :style="typeStyle('warning')"
                title="插件更新后新要了权限，需要你确认">需要新授权</el-tag>
        <!-- 收起时也得看得出「有没有高风险项被授出去」——那正是折叠最容易藏掉的东西 -->
        <el-tag v-if="!p._grants && riskyGranted(p).length" :style="typeStyle('danger')"
                :title="`已授出高风险权限：${riskyGranted(p).join('、')}`">含高风险</el-tag>
      </div>
      <el-collapse-transition>
        <div v-show="p._grants" class="grants">
          <!-- 一行只放「勾选框 + 短名 + 风险标 + ? 」。说明放进 tooltip：
               整句塞在行内会把每一行撑成一根长条，而且长短不一、扫不出重点。 -->
          <label v-for="k in p.scopes.declared" :key="k" class="grant">
            <el-checkbox :model-value="p._form.granted.includes(k)"
                         @change="(v) => toggleGrant(p, k, v)" />
            <span class="g-name">{{ scopeMeta(p, k).label || k }}</span>
            <el-tag v-if="scopeMeta(p, k).risk === 'high'" :style="typeStyle('danger')">高风险</el-tag>
            <el-tag v-else-if="scopeMeta(p, k).risk === 'medium'" :style="typeStyle('warning')">留意</el-tag>
            <el-tooltip v-if="scopeMeta(p, k).hint" :content="scopeMeta(p, k).hint"
                        placement="top" popper-class="wrap-tip">
              <el-icon class="help"><QuestionFilled /></el-icon>
            </el-tooltip>
            <span class="g-key">{{ k }}</span>
          </label>
          <div v-if="!p.scopes.declared.length" class="sub">本插件没有声明任何需要授权的权限。</div>
          <!-- 基础权限：勾选框里没有它，所以必须在这里说出来。
               用户看到「已授权 0 / 声明 1」时会问「那它现在到底能干什么」，
               答案就是这一行；藏起来只会让人怀疑还有别的没写出来的。 -->
          <div v-for="b in (p.scopes.baseline || [])" :key="b.key" class="grant base">
            <el-checkbox :model-value="true" disabled />
            <span class="g-name">{{ b.label }}</span>
            <el-tag :style="typeStyle('info')" title="每个插件默认持有，不需要也无法单独授权">默认</el-tag>
            <el-tooltip v-if="b.hint" :content="b.hint" placement="top" popper-class="wrap-tip">
              <el-icon class="help"><QuestionFilled /></el-icon>
            </el-tooltip>
            <span class="g-key">{{ b.key }}</span>
          </div>
          <div class="sub">
            没勾的权限插件一个都用不了（默认全拒）。插件更新后自己多写一项权限<b>不会</b>自动生效——
            <b>`git pull` 不该悄悄扩大它能碰的范围</b>。
          </div>
        </div>
      </el-collapse-transition>

      </div>
    </el-card>
  </div>
</template>

<script setup>
import PageHeader from '@/components/PageHeader.vue'
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowRight, ArrowUp, Refresh, QuestionFilled } from '@element-plus/icons-vue'
import { pluginsApi, tagsApi } from '@/api'
import { handled } from '@/api/http'
import { MSG_LOAD_FAILED, longToast, tagStyleAt, typeStyle } from '@/constants'
import { daysSince, fmtAgo, fmtDateTime } from '@/utils/datetime'

const plugins = ref([])
let loadSeq = 0
// 轮询连续失败多少次才放弃。**不是 1**：一次 503 / 一次 WiFi 抖动就永久停表，
// 而卡片会一直停在「执行中…」或「安装中…」对状态说假话。
const _POLL_MAX_FAILS = 3
let installFails = 0
let runFails = 0
// 上一次加载是否失败：空态文案据此说实话。这一页的空态在**断言用户的磁盘**，
// 请求失败时照样显示的话，不只是没信息，是给了一条错误的行动指令（去重装插件）。
const loadFailed = ref(false)
const loading = ref(false)
const platformTags = ref([])   // [{value,color}] 来源平台标签集（下拉选项 + 上色）

const platformOpts = computed(() => platformTags.value.map((t) => t.value))
const platformColor = computed(() => Object.fromEntries(platformTags.value.map((t) => [t.value, t.color])))
// 取用户在标签管理里配的颜色（不是 constants 里那套写死的语义色，别混用）
function platformTagStyle(v) { return tagStyleAt(platformColor.value[v] ?? -1, v) }

const fmtTime = fmtDateTime   // 后端存 naive UTC，必须补 Z 再解析，见 utils/datetime.js

// 多久没成功算「太久」：取这个插件自己的定时间隔的 3 倍，没设定时的按 3 天。
// **刻意不写死一个天数**：一个每 10 分钟跑一次的插件，停 1 天就已经很不对劲；
// 而一个每周手动点一次的插件，3 天前成功过完全正常。用它自己的节奏当基准。
function staleOk(p) {
  const days = daysSince(p.last_run?.ok_at)
  if (days === null) return false
  const every = Number(p._form?.schedule_minutes) || 0
  return days > (every > 0 ? (every * 3) / 1440 : 3)
}
// 权限元信息由后端随列表下发（catalog），前端不写第二份说明文案——
// 那份必然与后端漂移，而漂移的方向通常是「界面上写的比实际权限小」。
// warn = 插件正常退出、但自己在结果里报了 error（部分成功 / 软跳过）。
// 没有这一档时只能二选一：算成功 → 绿字，用户不会再点开摘要看那句话；
// 算失败 → 把插件作者刻意的软跳过（淘宝的 already_running 就是 return 0）刷成红色。
// 「执行中」用蓝色而不是黄：它和 warn 同屏出现，两个黄标签分不出哪个要人处理。
function runTagType(outcome) {
  return { ok: 'success', warn: 'warning', failed: 'danger', running: 'primary' }[outcome] || 'info'
}
// 按钮为什么点不了，鼠标悬停时说清楚——比一个灰按钮强
function cmdTitle(p, c) {
  if (!p.installed) return '插件未安装'
  if (!p._form.enabled) return '插件已停用——打开左上角的开关'
  if (c.blocked.length) return `缺权限：${c.blocked.join('、')}——先在下面勾选授权`
  if (c.per === 'account' && !enabledCount(p)) return '没有启用的账号'
  return c.hint || ''
}
async function doForget(p) {
  // 文案里要点名**插件私有存储**：它跟授权/定时不一样，是插件自己写进去的业务数据
  // （`data:own` / PluginRecord）。不说的话，用户以为只是清掉几项配置，
  // 而实际上那些数据也一并没了——这一步是不可逆的。
  // 用 ElMessageBox 而不是 window.confirm：全站只有暗色一套皮（tokens.css 里 main.js
  // 无条件加 .dark，没有切换入口），原生对话框那块白底每次都是异物——而它承担的
  // 恰好是全站唯一一处不可逆删业务数据的操作，同一张卡片往下的「删除账号」用的就是 ElMessageBox。
  // 还有一个更实际的理由：浏览器对反复弹原生对话框会给「阻止此页面创建更多对话框」，
  // 勾上之后 window.confirm 直接返回 false——按钮变死键，无 toast、无报错。
  // **不用 dangerouslyUseHTMLString**：下面 doRun 那处的文案来自第三方 plugin.toml，
  // 这里也插值了 p.id（清单缺 id 时会退回目录名，无格式校验）。要分行就用 h()。
  try {
    await ElMessageBox.confirm(
      h('div', { style: 'white-space: pre-line' },
        `会删掉它的授权、定时、账号、上次结果，以及它写入的插件私有存储。\n`
        + `这一步不可逆。\n\n`
        + `留着的话，以后放一个同 id 的插件进来会直接继承这份授权与私有存储。`),
      `清理「${p.id}」的残留配置？`,
      { type: 'warning', confirmButtonText: '清理', cancelButtonText: '取消' })
  } catch (_) { return }
  p._busy = true
  try {
    const r = await pluginsApi.forget(p.id)
    // 把「顺带删了多少条私有数据」说出来：不说的话，那条删除对用户是完全不可见的，
    // 而它恰恰是这个操作里唯一会丢业务数据的部分。
    ElMessage.success(r?.records_removed
      ? `已清理（含插件私有存储 ${r.records_removed} 条）`
      : '已清理')
    await load()
  } catch (_) { /* 拦截器已提示 */ } finally { p._busy = false }
}
async function doRun(p, c, account = null) {
  // 这句文案来自插件自己的 plugin.toml，是**第三方自由文本**——只能当纯文本渲染。
  if (c.confirm) {
    try {
      await ElMessageBox.confirm(h('div', { style: 'white-space: pre-line' }, c.confirm),
                                 `执行「${c.label}」`,
                                 { type: 'warning', confirmButtonText: '继续', cancelButtonText: '取消' })
    } catch (_) { return }
  }
  p._busy = true
  try {
    const r = await pluginsApi.run(p.id, c.name, account)
    // 被互斥挡掉的目标要说出来。只报「已触发」而其中几个根本没起来，是半句假话。
    const skipped = r?.skipped_running || []
    ElMessage.success(
      `已触发「${c.label}」${r.targets?.length ? '：' + r.targets.join('、') : ''}`
      + (skipped.length ? `；${skipped.join('、')} 上一次还在跑，本次跳过` : ''))
    // 卡片立刻进入「执行中」，然后**一直盯到它收尾**。
    // 原先只是 3 秒后刷一次：抓取动辄几分钟，那一刷必然还在跑，之后再没有人来问，
    // 于是「执行中」就一直挂着——看起来和真的卡死一模一样，而它其实早就跑完了。
    // **保留 ok_at**：这次点击只改变了「正在跑什么」，没有改变「上次成功是什么时候」。
    // 原先整体替换掉 last_run，新对象里没有 ok_at ⇒ 模板的 `v-if="p.last_run.ok_at"`
    // 判假 ⇒ 落到下一支渲染出黄色的【从未成功】——而这个插件可能两小时前刚成功过。
    // 那个标签是全站**唯一**会主动说「不对劲」的东西（staleOk 那套），
    // 让它在最正常的操作上误报，等于把它训练成噪音：以后真的报警也没人当回事。
    p.last_run = { ...(p.last_run || {}), outcome: 'running',
                   summary: `${c.label}${account ? ' ' + account : ''} 执行中…`, at: null }
    scheduleRunPoll()
  } catch (e) {
    // 不能空 catch：这里要把「哪个命令没起来」也说出来（后端的 detail 只说原因，
    // 不知道用户点的是哪个按钮）。带上命令名之后就比拦截器那条兜底更有用，
    // 所以取消兜底，避免两条提示叠在一起。
    handled(e)
    ElMessage.warning(e.response?.data?.detail || `「${c.label}」没能启动`)
  } finally { p._busy = false }
}
async function saveParams(p) {
  p._busy = true
  try {
    // 空的 secret = 「保持不变」，不提交（提交空串会把已存的密钥清掉）
    const patch = {}
    for (const pa of p.params) {
      const v = p._form.params[pa.key]
      if (pa.secret && (v === '' || v == null)) continue
      patch[pa.key] = v
    }
    const r = await pluginsApi.saveParams(p.id, patch)
    p.params = r.params
    // **把服务端规范化后的值回灌到输入框。** 只写 `p.params` 是不够的——
    // 输入框绑的是 `p._form.params`，两者会当场分叉：
    // 清空一个整数参数（el-input-number 对 isNil 提前 return，`:min` 钳制根本走不到）
    // 会提交 null，而后端 `params._coerce` 对非 str/secret/select 类型遇 None **折回默认值**
    // ⇒ 屏幕上是空框、库里是 3，还配一句「参数已保存，下一次执行即按新值跑」。
    // 刷新能自愈、落的值也正是默认值，所以今天不丢数据；
    // **真正的理由是前瞻**：以后只要给参数加任何服务端规范化（trim、单位换算、区间钳制），
    // 屏幕与库里就会各说各话，而这一句能让所有这类改动天然生效。
    // secret 例外：后端只回 `'__set__'` 占位，填回输入框会把密钥真改成这个字符串。
    p._form.params = Object.fromEntries(
      (r.params || []).map((pa) => [pa.key, pa.value === '__set__' ? '' : pa.value]))
    ElMessage.success('参数已保存，下一次执行即按新值跑')
  } catch (_) { /* 422 会显示后端的具体原因 */ } finally { p._busy = false }
}

function scopeMeta(p, key) {
  return (p.scopes?.catalog || []).find((x) => x.key === key) || {}
}
// 插件声明了、但你还没勾的那些 = 「需要新授权」。插件升级后自己多加一项时用它提示。
function pendingGrants(p) {
  return (p.scopes?.declared || []).filter((k) => !(p.scopes?.granted || []).includes(k))
}
// 「已授权 X / 声明 Y」的 X。**必须与 Y 取自同一个集合**：分子曾经用的是
// scopes.effective（= 声明 ∩ 授权 ∩ 已知 ∪ baseline），而 baseline 不在 declared 里，
// 于是一项都没勾的插件显示成「1/1」——读起来正是「全都授权了」。
// 库里存着、但插件已经不再声明的旧授权也不该计入：它不在分母里，进了分子同样跑偏。
function grantedCount(p) {
  return (p.scopes?.declared || []).filter((k) => (p.scopes?.granted || []).includes(k)).length
}
// 折叠之后还必须一眼看得出的东西：已经授出去的高风险权限。
// 折叠最容易藏掉的恰恰是这一类——「处置暂存单」能让暂存行直接消失。
function riskyGranted(p) {
  return (p.scopes?.declared || [])
    .filter((k) => (p.scopes?.granted || []).includes(k) && scopeMeta(p, k).risk === 'high')
    .map((k) => scopeMeta(p, k).label || k)
}
async function toggleGrant(p, key, on) {
  const next = on ? [...p._form.granted, key] : p._form.granted.filter((k) => k !== key)
  p._form.granted = next
  p._busy = true
  try {
    const r = await pluginsApi.saveGrants(p.id, next)
    p.scopes.granted = r.granted
    // 刻意**不**顺手更新 scopes.effective：它是「令牌实际带的权限」（含 baseline），
    // 与 granted 不是一回事。写成 `effective = r.granted` 只是把界面上的数字凑对，
    // 而下一次 load() 就会露馅。卡片上的比值现在只数 declared ∩ granted。
    // 就地重算每条命令还缺哪些权限。不重算的话按钮仍停在「缺权限」禁用态，
    // 要手动刷新整页才活过来——用户会以为刚才那一勾没生效。
    // **baseline 也算「有」。** 后端的判据是 `blocked = needs - effective`，
    // 而 effective = 令牌实际带的权限，**含 baseline**（`meta:read` 那类默认给的）。
    // 这里原先只减 `r.granted`，于是一条声明了 `needs = ["meta:read", "fx:write"]`
    // 的命令，在用户勾上 fx:write 的**那一刻**反而被算成「缺 meta:read」而灰掉——
    // 而权限区里 meta:read 恰恰是那一行不可点的「默认」标记，他没有任何操作能解锁。
    // 刷新整页又好了（后端判据是对的），于是这看起来像「界面偶尔抽风」，
    // 而不是一个判据错误——那种 bug 最不容易被报上来。
    const held = new Set([...r.granted, ...(p.scopes.baseline || []).map((b) => b.key)])
    for (const c of p.commands) c.blocked = (c.needs || []).filter((k) => !held.has(k))
    ElMessage.success(on ? `已授予「${scopeMeta(p, key).label || key}」`
                         : `已收回「${scopeMeta(p, key).label || key}」`)
  } catch (_) { p._form.granted = [...(p.scopes?.granted || [])] } finally { p._busy = false }
}

function enabledCount(p) { return p.accounts.filter((a) => a.configured && a.enabled).length }
// 账号行上该出现哪些按钮：清单里 per = "account" 的那些。核心不认识任何具体动词。
function accountCommands(p) { return (p.commands || []).filter((c) => c.per === 'account') }
function accountCmdTitle(p, c, a) {
  if (!p.installed) return '插件未安装'
  if (!p._form.enabled) return '插件已停用——打开左上角的开关'
  if (c.blocked.length) return `缺权限：${c.blocked.join('、')}——先在下面勾选授权`
  // 「要不要先有登录会话」由清单声明（needs_session），不由核心按动词名猜。
  if (c.needs_session && !a.authorized) return '这个账号还没授权——先点左边的登录命令'
  return c.hint || ''
}

async function load() {
  // **序号门**：这一页的 load 会被 onMounted、安装轮询、执行轮询三处并发调用，
  // 而 GET /api/plugins 每个插件允许 30-60 秒的子进程探测——先发的那次很可能后回来，
  // 用一份旧快照盖掉新的。与其它列表页同一口径（见 Items/index.vue）。
  const my = ++loadSeq
  loading.value = true
  try {
    const list = await pluginsApi.list()
    if (my !== loadSeq) return
    const prev = Object.fromEntries(plugins.value.map((x) => [x.id, x]))
    plugins.value = list.map((p) => ({
      ...p, _busy: false,
      // **永远默认收起**。曾经写成「有待授权就自动展开」，结果新装的插件全是展开的，
      // 反而比不折叠还乱。要人处理的事改成在卡片头挂一个标签——不展开也看得见。
      // 轮询刷新时沿用上一次的展开状态，别把用户展开的合上。
      _open: prev[p.id]?._open ?? false,
      // 权限段自己的折叠位。默认收起——它是全卡片最不常动的一块；
      // 但**有待授权时默认展开**：那是要人处理的事，藏在两层折叠底下等于没提示
      // （卡片头上那个「需要授权」标签只说明有事，说不清是哪一项）。
      _grants: prev[p.id]?._grants ?? !!(p.scopes?.declared || [])
        .filter((k) => !(p.scopes?.granted || []).includes(k)).length,
      // 刷新时保留用户勾过的选项与输了一半的账号昵称——
      // 安装轮询那段(scheduleInstallPoll)的注释早就写着「否则用户正在输入的账号名会被冲掉」，
      // 但整体刷新这条路径上它一直是被硬编码重置的。
      // `_form.params` 刻意**不**沿用：参数以服务端为权威，沿用会造出
      // 「插件升级改了默认值但表单显示旧值且看不出来」的新失败面。
      _withBrowser: prev[p.id]?._withBrowser ?? true,
      _form: { enabled: p.config.enabled, schedule_minutes: p.config.schedule_minutes || 0,
               granted: [...(p.scopes?.granted || [])],
               // secret 参数后端只回 '__set__'，不能把它当值填回输入框——
               // 那样一保存就会把密钥真的改成 '__set__' 这个字符串。
               params: Object.fromEntries((p.params || []).map(
                 (x) => [x.key, x.value === '__set__' ? '' : x.value])) },
      // 平台初值：清单声明了就取它的第一项，别再写死「淘宝」——
      // 那个默认值对**每个**插件生效，装一个京东插件不改它，抓回来的单就带着
      // platform="淘宝" 进账本（后端同一处默认值已一并去掉）。
      _add: prev[p.id]?._add ?? { name: '', platform: p.account_platforms?.[0] ?? '淘宝' },
    }))
    // 有安装在跑就继续盯着，装完自动刷新（按钮解禁、缺依赖提示消失）
    if (list.some((p) => p.install && p.install.running)) scheduleInstallPoll()
    // 定时触发的执行不经过本页面，进页面时可能已经在跑了——同样得盯到收尾，
    // 否则「执行中」要等用户自己想起来点刷新才会变。
    if (list.some((p) => p.last_run?.outcome === 'running')) scheduleRunPoll()
    try { platformTags.value = await tagsApi.list('platform') } catch (_) { /* 无所谓，下拉可自建 */ }
    loadFailed.value = false
  } catch (_) {
    // 拦截器已提示原因；这里负责让**页面本身**留下痕迹，否则空态在说假话。
    if (my === loadSeq) loadFailed.value = true
  } finally {
    if (my === loadSeq) loading.value = false
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
let runTimer = null
function scheduleInstallPoll() {
  if (installTimer) return                    // 单例：多张卡同时装也只有一个轮询
  // **重新开表就重新给满 _POLL_MAX_FAILS 次机会。** 不清零的话：连败 3 次停表 →
  // 用户点「刷新」→ 计时器是新建的，计数却还是 3 → 下一次抖动（第 4 次）当场又停，
  // 而且一次机会都不给。用户点第二下、第三下，每次都「刚点就又不动了」，
  // 看起来像刷新按钮坏了。
  installFails = 0
  installTimer = setInterval(async () => {
    try {
      const list = await pluginsApi.list()
      installFails = 0                          // 成功一次就把连败清零
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
    } catch (_) {
      // **不能错一次就永久停。** 这个 catch 吃的是**任何**错误，而最现实的是
      // `main.py` 那条连接池繁忙时的 503（它写出来就是为了应付繁忙时刻），
      // 以及 WiFi 抖动 / 睡眠唤醒——后者这个应用专门修过恢复路径
      // （离线遮罩 + 健康轮询），页面上除了这两个计时器之外的一切都能自愈，
      // 唯独它们停了就再也不回来：装完那次用来整体刷新的 load() 永不执行，按钮一直禁着。
      // 连续失败到上限才停，并说一句——否则用户只看到一个卡住的界面，毫无线索。
      if (++installFails >= _POLL_MAX_FAILS) {
        clearInterval(installTimer); installTimer = null
        ElMessage.warning('安装进度获取失败，已停止刷新——点「刷新」可继续查看')
      }
    }
  }, 2000)
}
// 有插件在跑就盯着，跑完自动把结果换上。**与安装轮询分开两个计时器**：
// 安装是分钟级、抓取可以是十几分钟，合成一个的话只要有一件在跑，另一件的节奏就被带偏。
// 只更新 last_run 与 last_run_at 两处，绝不整体替换——否则用户正在输入的账号昵称、
// 刚勾了一半的授权都会被冲掉（安装轮询那段的注释早就写着这一条）。
function scheduleRunPoll() {
  if (runTimer) return                        // 单例：多张卡同时在跑也只有一个轮询
  runFails = 0                                // 同上：重新开表 = 重新给满次数
  runTimer = setInterval(async () => {
    try {
      const list = await pluginsApi.list()
      runFails = 0                              // 成功一次就把连败清零
      for (const fresh of list) {
        const cur = plugins.value.find((x) => x.id === fresh.id)
        if (cur) { cur.last_run = fresh.last_run; cur.config.last_run_at = fresh.config.last_run_at }
      }
      if (!list.some((x) => x.last_run?.outcome === 'running')) {
        clearInterval(runTimer); runTimer = null
      }
    } catch (_) {
      // 同上。这条更要紧：它跑在「抓取可以是十几分钟」的最繁忙那一段，
      // 一次 503 就让卡片永久停在「执行中…」——而 run poll 连收尾的 load() 都没有。
      if (++runFails >= _POLL_MAX_FAILS) {
        clearInterval(runTimer); runTimer = null
        ElMessage.warning('执行状态获取失败，已停止刷新——点「刷新」可继续查看')
      }
    }
  }, 4000)
}
onBeforeUnmount(() => {
  if (installTimer) { clearInterval(installTimer); installTimer = null }
  if (runTimer) { clearInterval(runTimer); runTimer = null }
})

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
      handled(e)
      longToast(ElMessage, 'error', e.response?.data?.detail || '无法开始安装')
    }
    // 其余状态码拦截器已提示
  }
}

async function saveConfig(p) {
  p._busy = true
  try {
    // `params` 不在这个接口保存（走 PUT /{id}/params），后端已改成 extra=forbid，带上会 422
    await pluginsApi.saveConfig(p.id, { enabled: p._form.enabled, schedule_minutes: p._form.schedule_minutes })
    ElMessage.success('已保存')
    await load()
  } catch (_) {
    // 保存失败要把开关拨回去。停在用户拨的位置上，卡片说「已启用」而库里是停用，
    // 定时不跑、命令点不动，而界面上看不出任何异常。
    p._form.enabled = p.config.enabled
  } finally {
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
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '账号已存在') }
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

// （doLogin / doFetch 已删：它们是「把 login / fetch 两个动词写死在界面里」的最后两处。
//   账号行现在按清单渲染，统一走 doRun —— 连点保护、confirm、缺权限判据也随之统一。
//   `_busy` 那道连点闸没有丢：doRun 自己就设它。）

// 「改了哪几张表、各几行」的人话。后端 `moved` 是 {模型名: 行数}。
// 表名映射只在这里一处——加一张表时不用去改提示语的拼接。
const TABLE_LABEL = { OrderStaging: '暂存', Order: '账本', ShipmentOrder: '集运单',
                      MiscExpense: '杂项', TagOption: '标签' }

function movedText(res) {
  const parts = Object.entries(res.moved || {})
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${TABLE_LABEL[k] || k} ${n}`)
  return parts.length ? `迁移 ${parts.join(' / ')}` : '这个账号名下没有单'
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
    // **按后端实际改了哪几张表报数**，不写死「暂存 / 账本」两个。
    // 那两个键是 `platform_account` 这一列的源表；声明 `accounts_ledger_field = "recipient"`
    // 的插件（集运类的「账号」就是收货人）改完名会被告知「暂存 0 / 账本 0」——
    // 而他刚把整本集运单的收货人改掉了。后端现在多回一个 `moved`（模型名 → 行数）。
    else ElMessage.success(`已改名为「${value}」（${movedText(res)}）`)
    await load()
  } catch (e) {
    // 409（新名字已被占用）被 http 拦截器刻意跳过，这里显式弹出后端 detail，否则静默无反馈
    if (e.response?.status === 409) { handled(e); ElMessage.warning(e.response?.data?.detail || '新名字已被占用') }
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
      longToast(ElMessage, 'warning',
        `已删除 ${account} 的暂存单 ${r.deleted} 条；跳过 ${r.skipped} 条已导入的`
        + `（删了会在账本里留下导不回来的孤儿单）。要清掉这几条：先点上面的「删账本单」，`
        + `再回来点一次「删暂存单」——顺序反过来是没用的，这一步会把它们整批跳过。`)
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
      // 原文那句「不会动到暂存」是假话：`soft_delete_account_orders` 会把这些账本单对应的暂存行
      // `imported_order_id` 置 NULL、`import_status` 改回「待处理」。
      // **那个行为本身是对的**——全项目一致（单条 `delete_order` 一字不差地做同一件事，
      // `common.py` 的 `mirror_to_staging` docstring 明写这条设计，`test_plugins.py`
      // 有断言钉着），意思是「账本单没了，暂存那条就该能重新导入」。
      // 错的是这句话没说出来：用户以为删干净了，而那些行原封不动躺在暂存页、
      // 状态是「待处理」，任何人点一下「导入账本」就把刚删掉的单原样建回来，
      // 看板金额跟着涨回去——他不会想到去暂存页看一眼。
      `确定删除账号「${account}」名下的全部账本正式商品订单？将从账本移除（软删）。\n\n` +
      `已导入过的暂存行会退回「待处理」——它们还在暂存页，任何人点一下「导入账本」就会把它们建回来。\n` +
      `要彻底清掉：删完这一步，再点「删暂存单」——那时它们已经不是「已导入」，会被真正删除。`,
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
.hint { color: var(--txt-3); font-size: 12px; flex: 1; }
.plugin { margin-bottom: 16px; }
.head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pname { color: var(--txt-1); font-size: 15px; font-weight: 600; }
/* 停用的插件名字变淡：一眼能从一排卡片里看出哪个没在跑 */
.pname.off { color: var(--txt-3); font-weight: 500; }
/* 上次结果的摘要跟在标签后面，占满剩余宽度但不换行——卡片头要保持一行高 */
.lastsum { color: var(--txt-3); font-size: 12px; max-width: 40%; overflow: hidden;
           text-overflow: ellipsis; white-space: nowrap; }
.pver { color: var(--txt-3); font-size: 12px; }
.grow { flex: 1; }
.field { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.flabel { color: var(--txt-2); font-size: 13px; min-width: 180px; }
.sub { color: var(--txt-3); font-size: 12px; }
.body { padding-top: 4px; }
.sect { color: var(--txt-body); font-size: 13px; font-weight: 600; margin: 6px 0 10px; padding-top: 12px; border-top: 1px solid var(--border-dim); }
.acct { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.acct.dim .aname, .acct.dim .c-plat, .acct.dim .c-auth { opacity: 0.4; }   /* 只灰昵称/平台/授权，开关和按钮保持清晰可用 */
.c-sw { width: 40px; flex: none; display: inline-flex; }                   /* 固定列，孤儿无开关也占位，保证对齐 */
.aname { width: 104px; flex: none; color: var(--txt-1); font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.c-plat { min-width: 64px; flex: none; display: inline-flex; }
.c-auth { min-width: 58px; flex: none; display: inline-flex; }
.c-state { min-width: 56px; flex: none; display: inline-flex; }
/* 权限段的标题行就是折叠开关：整行可点（不是只有那个小箭头），
   命中区域和它在视觉上占的宽度一致。用 .sect 的排版，只加一个转向的箭头,
   刻意**不**换底色——换了就成了卡片里嵌一块深色板，而全站没有第二处这么做。 */
.grantsect { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
.grantsect:hover { color: var(--brand); }
.caret { color: var(--txt-3); font-size: 12px; transition: transform 0.2s; }
.grantsect.open .caret { transform: rotate(90deg); }
.grants { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
.grant { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }
/* 基础权限那一行：勾选框是禁用的，整行也不该表现得可点 */
.grant.base { cursor: default; }
.grant.base .g-name { color: var(--txt-2); }
.g-name { font-weight: 600; }
/* 只显示权限 key（fx:write 这种），给愿意深究的人看；说明在 ? 里 */
.g-key { color: var(--txt-3); font-size: 11px; font-family: ui-monospace, monospace; }
.help { color: var(--txt-3); cursor: help; font-size: 13px; }
.subsect { color: var(--txt-3); font-size: 12px; margin: 8px 0 6px; }
</style>
