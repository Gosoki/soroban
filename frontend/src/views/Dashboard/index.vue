<template>
  <div>
    <el-row :gutter="16" v-loading="loading">
      <el-col :xs="12" :sm="12" :md="6" v-for="c in cards" :key="c.label">
        <div class="stat" :style="{ borderTopColor: c.color }">
          <div class="stat-label">{{ c.label }}</div>
          <div class="stat-value">{{ fmtJPY(c.value) }}</div>
          <div class="stat-sub">{{ c.sub }}</div>
        </div>
      </el-col>
    </el-row>

    <el-card class="months" style="margin-top: 16px">
      <template #header>按月支出（结算日元）· 点行看占比</template>

      <div v-if="!data.by_month.length" class="m-empty">暂无数据</div>

      <div v-for="m in data.by_month" :key="m.month" class="mrow" :class="{ cur: m.month === curMonth, open: open.has(m.month) }"
           @click="toggle(m.month)">
        <div class="mrow-top">
          <div class="mrow-left">
            <el-icon class="chev" :class="{ open: open.has(m.month) }"><ArrowRight /></el-icon>
            <span class="mrow-month">{{ m.month }}</span>
            <el-tag v-if="m.month === curMonth" size="small" effect="plain" round>本月</el-tag>
          </div>
          <div class="mrow-total">{{ fmtJPY(m.jpy) }}</div>
        </div>

        <el-collapse-transition>
          <div v-show="open.has(m.month)" class="mdetail" @click.stop>
            <div class="donut-box">
              <div class="donut" :style="donutStyle(m)" />
              <div class="donut-center"><span class="dc-cap">合计</span>{{ fmtJPY(m.jpy) }}</div>
            </div>
            <div class="dlegend">
              <div class="dl-row"><i class="dot tb" /><span class="dl-k">商品（含快递）</span><span class="dl-p">{{ pct(m.order_jpy, m.jpy) }}</span><span class="dl-v">{{ fmtJPY(m.order_jpy) }}</span><span class="dl-c">{{ m.order_count }} 单</span></div>
              <div class="dl-row"><i class="dot sp" /><span class="dl-k">集运运费</span><span class="dl-p">{{ pct(m.shipment_jpy, m.jpy) }}</span><span class="dl-v">{{ fmtJPY(m.shipment_jpy) }}</span><span class="dl-c">{{ m.shipment_count }} 单</span></div>
              <div class="dl-row"><i class="dot mc" /><span class="dl-k">杂项</span><span class="dl-p">{{ pct(m.misc_jpy, m.jpy) }}</span><span class="dl-v">{{ fmtJPY(m.misc_jpy) }}</span><span class="dl-c">{{ m.misc_count }} 笔</span></div>
            </div>
          </div>
        </el-collapse-transition>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import { dashboardApi } from '@/api'
import { fmtJPY } from '@/utils/money'

const loading = ref(false)
const data = reactive({
  total_jpy: 0, order_jpy: 0, shipment_jpy: 0, misc_jpy: 0,
  order_count: 0, shipment_count: 0, misc_count: 0, by_month: [], fx_rate: null,
})

// 当前年月（按本地=JST）；本月行浅色底强调
const curMonth = (() => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
})()
const open = reactive(new Set())
function toggle(month) { open.has(month) ? open.delete(month) : open.add(month) }
function pct(v, total) { return total > 0 ? `${(v / total * 100).toFixed(1)}%` : '0%' }
// 环状比例图：按各类目占比生成扇区（商品绿/集运橙/杂项红）
function donutStyle(m) {
  const t = m.jpy || 0
  if (t <= 0) return { background: '#1c2740' }
  const a = (m.order_jpy / t) * 100
  const b = a + (m.shipment_jpy / t) * 100
  return { background: `conic-gradient(var(--ok) 0 ${a}%, var(--warn) ${a}% ${b}%, var(--danger) ${b}% 100%)` }
}

const cards = computed(() => [
  { label: '总支出', value: data.total_jpy, color: '#1890ff', sub: `汇率 1元≈${data.fx_rate ?? '—'}円` },
  { label: '商品（含快递）', value: data.order_jpy, color: 'var(--ok)', sub: `${data.order_count} 单` },
  { label: '集运运费', value: data.shipment_jpy, color: 'var(--warn)', sub: `${data.shipment_count} 单` },
  { label: '杂项', value: data.misc_jpy, color: 'var(--danger)', sub: `${data.misc_count} 项` },
])

async function load() {
  loading.value = true
  try {
    Object.assign(data, await dashboardApi.get())
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<style scoped>
.stat {
  background: var(--bg-card); border: 1px solid var(--border); border-top: 3px solid var(--brand);
  border-radius: 8px; padding: 16px; margin-bottom: 16px;
}
.stat-label { color: var(--txt-2); font-size: 13px; }
.stat-value { color: var(--txt-1); font-size: 26px; font-weight: 700; margin: 6px 0; }
.stat-sub { color: var(--txt-3); font-size: 12px; }

/* —— 按月支出：点行展开环状比例图 —— */
.dot { width: 8px; height: 8px; border-radius: 2px; display: inline-block; flex-shrink: 0; }
.dot.tb { background: var(--ok); }
.dot.sp { background: var(--warn); }
.dot.mc { background: var(--danger); }
.m-empty { color: var(--txt-3); text-align: center; padding: 24px; font-size: 13px; }

.mrow {
  padding: 12px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid transparent; transition: background .15s, border-color .15s;
}
.mrow + .mrow { margin-top: 6px; }
.mrow:hover { background: #172236; }
.mrow.cur { background: rgba(24, 144, 255, 0.07); border: 1px dashed rgba(24, 144, 255, 0.5); }
.mrow-top { display: flex; align-items: center; justify-content: space-between; }
.mrow-left { display: flex; align-items: center; gap: 8px; }
.chev { color: var(--txt-3); transition: transform .18s; }
.chev.open { transform: rotate(90deg); }
.mrow-month { color: var(--txt-1); font-size: 15px; font-weight: 600; letter-spacing: .3px; }
.mrow-total { color: var(--txt-1); font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }

/* 展开：左环图 + 右图例 */
.mdetail { display: flex; align-items: center; gap: 22px; padding: 14px 6px 6px 26px; flex-wrap: wrap; }
.donut-box { position: relative; width: 108px; height: 108px; flex-shrink: 0; }
.donut {
  width: 100%; height: 100%; border-radius: 50%;
  -webkit-mask: radial-gradient(transparent 56%, #000 57%);
  mask: radial-gradient(transparent 56%, #000 57%);
}
.donut-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center;
  color: var(--txt-1); font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums; }
.dc-cap { color: var(--txt-3); font-size: 11px; font-weight: 400; margin-bottom: 1px; }

.dlegend { flex: 1; min-width: 220px; }
.dl-row { display: flex; align-items: center; gap: 10px; padding: 5px 0; font-size: 13px; border-bottom: 1px solid var(--border-dim); }
.dl-row:last-child { border-bottom: none; }
.dl-k { color: var(--txt-body); flex: 1; }
.dl-p { color: var(--txt-2); width: 56px; text-align: right; font-variant-numeric: tabular-nums; }
.dl-v { color: var(--txt-1); width: 104px; text-align: right; font-variant-numeric: tabular-nums; }
.dl-c { color: var(--txt-3); font-size: 12px; width: 48px; text-align: right; }
</style>
