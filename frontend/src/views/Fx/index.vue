<template>
  <div class="fx-page" v-loading="loading">
    <h2 class="title">日元汇率</h2>
    <p class="lead">
      每抓一次记一条，<b>一天可以有多条</b>。建单与补录都按<b>那一天</b>的汇率折算——
      爬虫抓回前几天买的东西时，用的是当天的值而不是今天的。
      当天有多条时取<b>手填的那条</b>，没有手填就取<b>当天最后抓到的</b>。
    </p>

    <!-- 工具栏 + 表格装进卡片：全站其余页面（订单/集运/插件/设置/数据库）都是
         「内容装在 el-card 里」，只有这一页是裸的一张表贴在页底色上。 -->
    <el-card shadow="never">
    <div class="bar">
      <el-radio-group v-model="days" @change="load">
        <el-radio-button v-for="d in [7, 30, 90, 365]" :key="d" :value="d">近 {{ d }} 天</el-radio-button>
      </el-radio-group>
      <span class="sub">共 {{ rows.length }} 天有记录</span>
      <div class="grow" />
      <router-link to="/plugins" class="sub">汇率由插件抓取 →</router-link>
    </div>

    <el-empty v-if="!loading && !rows.length" description="还没有任何汇率记录。装上汇率插件并授权，或去设置页手填一个。" />

    <table v-else class="tbl">
      <thead>
        <tr>
          <!-- 百分比而不是像素：页宽放开之后，固定像素列会把多出来的宽度**全部**
               丢给最后一列，数据挤在左边、右边空 700px。按比例分配才跟着页宽走。 -->
          <th style="width: 16%">日期</th>
          <th style="width: 12%">采用</th>
          <th style="width: 14%">来源</th>
          <th style="width: 18%">当天区间</th>
          <th style="width: 8%">条数</th>
          <th style="width: 32%"></th>
        </tr>
      </thead>
      <tbody>
        <template v-for="r in rows" :key="r.date">
          <tr class="day" :class="{ open: openDay === r.date }" @click="toggle(r)">
            <td>
              <el-icon class="caret"><component :is="openDay === r.date ? ArrowDown : ArrowRight" /></el-icon>
              {{ r.date }}
            </td>
            <td class="num">{{ r.used ?? '—' }}</td>
            <td>
              <el-tag :style="typeStyle(r.used_source === 'manual' ? 'info' : 'success')">
                {{ FX_SOURCE_NAMES[r.used_source] || r.used_source }}
              </el-tag>
            </td>
            <!-- 只有一条时不显示区间：`23.36 ~ 23.36` 是噪音 -->
            <td class="num sub">{{ r.count > 1 ? `${r.low} ~ ${r.high}` : '' }}</td>
            <td class="sub">{{ r.count }}</td>
            <td class="sub">{{ r.count > 1 ? '点开看当天各次' : '' }}</td>
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
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ArrowDown, ArrowRight } from '@element-plus/icons-vue'
import { fxApi } from '@/api'
import { FX_SOURCE_NAMES, typeStyle } from '@/constants'

const loading = ref(false)
const days = ref(30)
const rows = ref([])
const openDay = ref('')
const detail = ref([])

// 请求序号门：迟到的响应不许覆盖新结果。与 Orders/Items/Shipment/Staging/Misc
// 六处同一个写法。少了它，连点「近 7 天/近 30 天」或快速换行展开时，
// A 天的明细会画在 B 天下面——没有任何提示，看上去就是数据错了。
let loadSeq = 0
let daySeq = 0

async function load() {
  const my = ++loadSeq
  loading.value = true
  openDay.value = ''
  try {
    const res = await fxApi.history(days.value)
    if (my === loadSeq) rows.value = res.items
  } catch (_) { /* 拦截器已提示 */ } finally {
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

onMounted(load)
</script>

<style scoped>
/* 页宽不再自己设上限：这一页主体就是一张**表**，900px 下右边白掉 450px。
   全站统一「内容装在卡片里、卡片占满宽度」。 */
.title { margin: 0 0 8px; font-size: 20px; }
.lead { margin: 0 0 16px; color: var(--txt-3); font-size: 12px; line-height: 1.9; }
.bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.grow { flex: 1; }
.sub { color: var(--txt-3); font-size: 12px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th { text-align: left; font-weight: 600; color: var(--txt-2); padding: 8px 10px;
          border-bottom: 1px solid var(--border); }
.tbl td { padding: 8px 10px; border-bottom: 1px solid var(--border-dim); }
.day { cursor: pointer; }
.day:hover { background: var(--bg-row-hover); }
.day.open { background: var(--bg-hover); }
.caret { font-size: 12px; color: var(--txt-3); margin-right: 4px; vertical-align: -1px; }
.num { font-variant-numeric: tabular-nums; }
.detail td { background: var(--bg-head); }
.hit { display: flex; align-items: center; gap: 10px; padding: 4px 0 4px 26px; font-size: 13px; }
.hit .t { color: var(--txt-3); width: 150px; font-variant-numeric: tabular-nums; }
.hit .v { width: 90px; font-variant-numeric: tabular-nums; color: var(--txt-1); }
.hit.used .v { font-weight: 700; }
</style>
