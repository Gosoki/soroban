<template>
  <div v-loading="loading">
    <PageHeader>
      每抓一次记一条，<b>一天可以有多条</b>。建单与补录都按<b>那一天</b>的汇率折算——
      爬虫抓回前几天买的东西时，用的是当天的值而不是今天的。
      当天有多条时取<b>手填的那条</b>，没有手填就取<b>当天最后抓到的</b>。
      <template #actions>
        <router-link to="/plugins" class="sub">汇率由插件抓取 →</router-link>
      </template>
    </PageHeader>

    <div class="bar">
      <el-radio-group v-model="days" @change="load">
        <el-radio-button v-for="d in [7, 30, 90, 365]" :key="d" :value="d">近 {{ d }} 天</el-radio-button>
      </el-radio-group>
      <span v-if="!loadFailed" class="sub">共 {{ rows.length }} 天有记录</span>
      <span class="grow"></span>
      <!-- 手填某一天的汇率。放在这一页而不是设置页：设置页那个是「没有任何汇率时的起点值」，
           记的永远是今天；这里填的是**具体某一天**，用来补历史。两者写的是同一张表，
           但解决的是两个问题，混在一处只会让人填错地方。 -->
      <el-date-picker v-model="mf.date" type="date" value-format="YYYY-MM-DD"
                      placeholder="补填哪一天" :disabled-date="isFuture" class="mf-date" />
      <el-input v-model="mf.rate" placeholder="汇率" class="mf-rate">
        <template #prepend>1 元 =</template>
        <template #append>円</template>
      </el-input>
      <el-button type="primary" :disabled="!mf.date || !mf.rate" :loading="saving"
                 @click="saveManual">手填这一天</el-button>
    </div>

    <!-- **三分支，顺序要紧**：失败 → 真空 → 表格。
         原先只有「真空」一支：5xx / 503 / axios 15s 超时（ECONNABORTED 不被 isNetworkError 拦）
         之后 rows 保持 []，页面就断言「你还没有任何汇率记录，去装插件或手填一个」。
         照做去手填的话更贵——手填汇率在 pick_used 里**优先于抓来的**，
         此后建单按这个估值折算日元，账本里就是错的钱。 -->
    <el-empty v-if="loadFailed" description="加载失败——请检查网络或后端，然后重试" />
    <el-empty v-else-if="!loading && !rows.length"
              description="还没有任何汇率记录。装上汇率插件并授权，或去设置页手填一个。" />

    <!-- .fx-scroll 与 NotionTable 的 .gtn-scroll 是同一件东西：1px 外框 + 8px 圆角 +
         自身的面。表格页不套 el-card，这圈框自己就是容器。 -->
    <div v-else class="fx-scroll">
    <table class="fx-tbl">
      <thead>
        <tr>
          <!-- 箭头独占一列，宽 30px（NotionTable 的 EXPAND_COL_W）。表头留空，
               与 `<th v-if="expandable" class="gtn-th"></th>` 一致。
               独立成列之后，所有数据列的左缘落在同一条竖线上——原先箭头内联在
               日期格里，日期的文字被顶开而其余列没有，第一列的文字左缘对不齐。
               30px 是像素、其余是百分比：table-layout: fixed 下两者可以混用，
               固定列先扣掉，剩余宽度再按百分比分。 -->
          <th style="width: 30px"></th>
          <th style="width: 24%">日期</th>
          <th style="width: 16%">采用</th>
          <th style="width: 18%">来源</th>
          <th style="width: 24%">当天区间</th>
          <th style="width: 18%">条数</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="r in rows" :key="r.date">
          <tr class="day" :class="{ open: openDay === r.date }" @click="toggle(r)">
            <!-- 与 .gtn-td-exp 同款：独立一格、居中、弱色。
                 图标本身也同款——**同一个图标转 90°**，不是换一个图标；
                 换图标没有中间态，展开/收起是「跳」过去的。 -->
            <td class="c-exp">
              <el-icon class="chev" :class="{ open: openDay === r.date }"><ArrowRight /></el-icon>
            </td>
            <td>{{ r.date }}</td>
            <td>{{ r.used ?? '—' }}</td>
            <td>
              <el-tag :style="typeStyle(r.used_source === 'manual' ? 'info' : 'success')">
                {{ FX_SOURCE_NAMES[r.used_source] || r.used_source }}
              </el-tag>
            </td>
            <!-- 只有一条时不显示区间：`23.36 ~ 23.36` 是噪音 -->
            <td class="dim">{{ r.count > 1 ? `${r.low} ~ ${r.high}` : '' }}</td>
            <!-- 提示并进「条数」而不是单占一列：加了纵向网格线之后，一个只有
                 count>1 才有字的列会常年空着，而空列在网格里是看得见的。 -->
            <td class="dim">{{ r.count }}<span v-if="r.count > 1" class="tip"> · 点开看各次</span></td>
          </tr>
          <tr v-if="openDay === r.date" class="detail">
            <td colspan="6">
              <div v-if="!detail.length" class="sub">加载中…</div>
              <div v-for="x in detail" :key="x.id" class="hit" :class="{ used: x.used }">
                <!-- 用后端给的 JST 时刻，不用浏览器本地时区：date 是 JST 日期，
                     两者不同源的话，非 JST 机器上会出现「8-07 那天写着 8/6 22:00」 -->
                <span class="t">{{ x.at || '—' }}</span>
                <span class="v">{{ x.rate }}</span>
                <el-tag :style="typeStyle(x.source === 'manual' ? 'info' : 'success')">
                  {{ FX_SOURCE_NAMES[x.source] || x.source }}
                </el-tag>
                <el-tag v-if="x.used" :style="typeStyle('warning')"
                        title="当天按这一条折算（手填优先，其次最后抓到的）">采用</el-tag>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
    </div>
  </div>
</template>

<script setup>
import PageHeader from '@/components/PageHeader.vue'
import { onMounted, reactive, ref } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { fxApi } from '@/api'
import { FX_SOURCE_NAMES, typeStyle } from '@/constants'

const loading = ref(false)
const saving = ref(false)
const days = ref(30)
// 手填表单。**只追加一条，不覆盖**任何已有行；也不会改动已经折算过的旧单
// （那些行盖的是成交当时的汇率，事后改汇率去动它们是篡改账目而不是修复）。
const mf = reactive({ date: '', rate: '' })
const rows = ref([])
const openDay = ref('')
const detail = ref([])

// 请求序号门：迟到的响应不许覆盖新结果。与 Orders/Items/Shipment/Staging/Misc
// 六处同一个写法。少了它，连点「近 7 天/近 30 天」或快速换行展开时，
// A 天的明细会画在 B 天下面——没有任何提示，看上去就是数据错了。
let loadSeq = 0
// 上一次加载是否失败：空态文案据此说实话。
// 这一页的空态**在断言用户的系统状态**（「装上汇率插件」/「plugins 下没有目录」），
// 而请求失败时它照样会显示——那不只是没信息，是给了一条错误的行动指令。
const loadFailed = ref(false)
let daySeq = 0

async function load() {
  const my = ++loadSeq
  loading.value = true
  openDay.value = ''
  try {
    const res = await fxApi.history(days.value)
    if (my === loadSeq) { rows.value = res.items; loadFailed.value = false }
  } catch (_) {
    // 拦截器已提示原因；这里负责让**页面本身**留下痕迹，否则空态在说假话。
    if (my === loadSeq) loadFailed.value = true
  } finally {
    if (my === loadSeq) loading.value = false
  }
}

async function toggle(r) {
  if (openDay.value === r.date) { openDay.value = ''; daySeq++; return }
  const my = ++daySeq
  openDay.value = r.date
  detail.value = []
  try {
    const res = await fxApi.historyDay(r.date)
    if (my === daySeq) detail.value = res.items
  } catch (_) {
    // 只收自己那一次的展开态：否则 A 的失败响应会把用户刚展开的 B 收掉
    if (my === daySeq) openDay.value = ''
  }
}

// 未来的汇率不存在。前端先挡一道，后端仍会 422——两边都拦是有意的：
// 日期选择器挡的是「点不到」，后端挡的是「绕过界面直接调接口」。
function isFuture(d) {
  const today = new Date()
  today.setHours(23, 59, 59, 999)
  return d > today
}

async function saveManual() {
  saving.value = true
  try {
    const r = await fxApi.setManual(mf.date, mf.rate)
    ElMessage.success(`已记下 ${r.date} 的手填汇率 1 元 = ${r.rate} 円`)
    mf.rate = ''
    await load()
  } catch (_) { /* 拦截器已提示 */ } finally { saving.value = false }
}

onMounted(load)
</script>

<style scoped>
/* 页宽不再自己设上限：这一页主体就是一张**表**，900px 下右边白掉 450px。
   全站统一「内容装在卡片里、卡片占满宽度」。 */
.bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.grow { flex: 1; }
/* 两个输入框都给固定宽度：不给的话 el-input 会把 .bar 撑到整行，
   把左边的「近 N 天」挤成两行。与 Database 页 .field-grid 同样的意图——
   一组相关的输入横着排，但不许某一个独吞剩余宽度。 */
.mf-date { width: 160px; }
.mf-rate { width: 220px; }
/* ↓ 以下逐条对齐 components/NotionTable.vue 的取值。
   这张表是手写的（只读 + 按天展开，用不上 NotionTable 的列宽持久化/单元格编辑/
   幽灵新建行），但**看上去必须是同一个应用里的表**。差异原先有九处，
   最扎眼的是：没有外框、表头没有底色、没有纵向网格线。 */
.fx-scroll { overflow: auto; border: 1px solid var(--border); border-radius: var(--r-md);
             background: var(--bg-card); }   /* 理由同 .gtn-scroll */
.fx-tbl { width: 100%; border-collapse: collapse; font-size: 13px; color: var(--txt-body);
          table-layout: fixed; }
/* 表头：底色 + **2px** 下边框 + 12px/500 —— 与 .gtn-th 逐项相同。
   ⚠️ `position: sticky` 与 .gtn-th 一样带着，但**两边现在都不生效**：
   sticky 相对最近的滚动祖先定位，而 .fx-scroll / .gtn-scroll 只有 overflow:auto、
   没有任何高度约束，永远不产生纵向滚动——真正在滚的是 Layout.vue 的 .content。
   实测：订单页滚到底时表头 top 从 120 变成 -560，整条滚出屏幕。
   留着是为了与 NotionTable 保持逐字一致（哪天给滚动框加了 max-height，两边一起生效），
   但**别再写「能吸住」这种注释**——那正是这条注释上一版说的话。 */
.fx-tbl th { background: var(--bg-hover); border-bottom: 2px solid var(--border);
             border-right: 1px solid var(--border); padding: 8px; text-align: left;
             font-weight: 500; font-size: 12px; color: var(--txt-body); white-space: nowrap;
             position: sticky; top: 0; z-index: 2; }
/* 单元格：36px 行高 + 横线 --border-soft + **纵向网格线** --border。
   原先横线用的是 --border-dim（那是侧栏那一档），且完全没有竖线。
   token 的注释写得很清楚：横线「刻意比竖线浅」——只有两条都在，那句话才成立。 */
/* padding 横向 8px：NotionTable 的 padding 不在 td 上（那里是 0），而在内层
   .gtn-disp / .gtn-slot 的 `padding: 0 8px`。数值要对齐的是那一层。
   overflow/ellipsis/nowrap 三件套是 36px 行高的**前提**：没有它们，
   窄屏下「当天区间」一折行，那一行就变成两行高，同屏出现两种行高。 */
.fx-tbl td { height: 36px; padding: 0 8px; border-bottom: 1px solid var(--border-soft);
             border-right: 1px solid var(--border);
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fx-tbl th:last-child, .fx-tbl td:last-child { border-right: none; }
.day { cursor: pointer; }
.day:hover td { background: var(--bg-row-hover); }
/* 「已展开」原先用 --bg-hover(#18233a)，比悬停色 --bg-row-hover(#1b2942) **还淡**——
   而 tokens.css 自己写着「悬停必须比选中轻，否则分不出选没选上」，方向是反的。
   底色维持不变，改用一条 2px 的品牌色竖条来标记，与 .gtn-th.dragover 的
   `inset 2px 0 0 var(--brand)` 同一种「这个是选中的」语言——
   它和悬停不是同一个维度，所以悬停一个已展开的行时两者都还在。 */
.day.open td { background: var(--bg-hover); }
.day.open td:first-child { box-shadow: inset 2px 0 0 var(--brand); }
/* 箭头列：与 .gtn-td-exp 逐项相同（居中、弱色、可点）。
   padding 归零由 text-align: center 定位；原先内联在日期格里时靠
   `margin-right` + `vertical-align: -1px` 微调，进了居中格之后那两个都成了净负分。 */
.fx-tbl td.c-exp { text-align: center; padding: 0; color: var(--txt-3); }
.chev { transition: transform 0.15s; }
.chev.open { transform: rotate(90deg); }
/* 弱化只改颜色、**不改字号**：NotionTable 整表统一 13px，没有任何单元格改字号。
   原先「当天区间/条数」挂的是全局 .sub（12px），6 列里 3 列小一号，同一行里两种字号。 */
.fx-tbl td.dim { color: var(--txt-3); }
.tip { color: var(--txt-3); font-size: 12px; }
/* 展开区：与 .gtn-exp-row 同一个面（--bg-sunken）与同一条下边框。
   原先用的是 --bg-head——比卡片**浅**，读起来像浮起来的一层，方向正好反了。 */
/* 12px/20px 与三个列表页的展开区（Shipment/Staging 的 .expand、OrderItemsEditor 的 .oie）
   逐字相同。原先是 td 的 10px 再加 .hit 的 22px = 32px，比它们多缩 12px。
   这里还要把 td 的三件套解掉：展开区是自由排版，不能被省略号和 36px 行高管着。 */
.detail td { background: var(--bg-sunken); border-bottom: 1px solid var(--border);
             height: auto; padding: 12px 20px; white-space: normal; overflow: visible; }
.hit { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 13px; }
.hit .t { color: var(--txt-3); width: 150px; }
.hit .v { width: 90px; color: var(--txt-1); }
.hit.used .v { font-weight: 700; }
</style>
