<template>
  <div class="fx-page" v-loading="loading">
    <h2 class="title">日元汇率</h2>
    <p class="lead">
      每抓一次记一条，<b>一天可以有多条</b>。建单与补录都按<b>那一天</b>的汇率折算——
      爬虫抓回前几天买的东西时，用的是当天的值而不是今天的。
      当天有多条时取<b>手填的那条</b>，没有手填就取<b>当天最后抓到的</b>。
    </p>

    <div class="bar">
      <el-radio-group v-model="days" size="small" @change="load">
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
          <th style="width: 120px">日期</th>
          <th style="width: 120px">采用</th>
          <th style="width: 90px">来源</th>
          <th style="width: 140px">当天区间</th>
          <th style="width: 70px">条数</th>
          <th></th>
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
              <el-tag size="small" :style="typeStyle(r.used_source === 'manual' ? 'info' : 'success')">
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
                <el-tag size="small" :style="typeStyle(x.source === 'manual' ? 'info' : 'success')">
                  {{ FX_SOURCE_NAMES[x.source] || x.source }}
                </el-tag>
                <el-tag v-if="x.used" size="small" :style="typeStyle('warning')"
                        title="当天按这一条折算（手填优先，其次最后抓到的）">采用</el-tag>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
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

async function load() {
  loading.value = true
  openDay.value = ''
  try {
    rows.value = (await fxApi.history(days.value)).items
  } catch (_) { /* 拦截器已提示 */ } finally { loading.value = false }
}

async function toggle(r) {
  if (openDay.value === r.date) { openDay.value = ''; return }
  openDay.value = r.date
  detail.value = []
  try {
    detail.value = (await fxApi.historyDay(r.date)).items
  } catch (_) { openDay.value = '' }
}

onMounted(load)
</script>

<style scoped>
.fx-page { max-width: 900px; }
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
