<template>
  <div>
    <PageHeader>
      三张账本表（商品 / 集运 / 杂项）的合计与按月分布。
      合计只数<b>计入</b>的行：软删、以及「取消 / 退款」这类状态不算。
      点某个月可以看它的构成占比。
    </PageHeader>
    <!-- **失败不能长得像「这个账本是空的」**。原先 load() 只有 try/finally：
         接口挂了 data 保持初值全 0，整屏渲染成「总支出 ¥0、0 单、暂无数据」，
         而拦截器那句 toast 3 秒后就没了——此后与「真的还没记过账」完全无法区分。
         对一个记账工具，「你的账全没了」是最不该让人误会的一句话。 -->
    <!-- **两种失败得说两句不同的话。** 原先共用一句「下面显示的不是你的账本，只是初值」：
         首次就失败时它是准的；但**加载成功过之后再失败**，页面上留着的恰恰**就是**用户的
         账本（只是旧的），那句话就成了假话——用户会以为数据没了、或者以为这些数字是垃圾。
         这条修复的初衷是「别让失败长得像空账本」，而这半句是反过来的同一类错误：
         让「有点旧的真数据」长得像「不是你的数据」。记账工具在这两个方向上都不该说错。 -->
    <el-alert v-if="loadFailed" type="error" show-icon :closable="false" class="load-failed">
      <template #title>看板数据加载失败</template>
      <template v-if="loadedAt">
        下面是<b>{{ loadedAt }}</b> 那次加载的数据，可能已经过时；本次刷新没成功。
      </template>
      <template v-else>
        下面显示的<b>不是</b>你的账本，只是初值。请检查网络或后端后重试。
      </template>
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>
    <!-- 有货款、却因为缺汇率算不出日元的行：SUM 会把 NULL 直接跳过，
         于是**金额被吞、笔数照数**——合计变小而单数不变，看板上没有任何异常信号。
         记账不该因为断网就记不了，所以写入侧照旧放行；代价是这里必须说出来。 -->
    <el-alert v-if="data.uncounted_count" type="warning" show-icon :closable="false" class="load-failed">
      <template #title>有 {{ data.uncounted_count }} 笔没算进下面的合计</template>
      这些行填了货款（合计 ¥{{ data.uncounted_cny }}）但当时<b>没有汇率</b>，折不出日元。
      下面的总支出因此偏小，而单数是全的。
      <el-link type="primary" :underline="false" @click="$router.push('/fx')">去汇率页补一条</el-link>，然后在对应行重填一次货款即可重算。
    </el-alert>
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
            <el-tag v-if="m.month === curMonth" :style="typeStyle('info')" round>本月</el-tag>
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
import PageHeader from '@/components/PageHeader.vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'
import { dashboardApi } from '@/api'
import { fmtJPY } from '@/utils/money'
import { typeStyle } from '@/constants'

const loading = ref(false)
const data = reactive({
  total_jpy: 0, order_jpy: 0, shipment_jpy: 0, misc_jpy: 0,
  order_count: 0, shipment_count: 0, misc_count: 0, by_month: [], fx_rate: null,
  // 初值必须是 0：接口挂掉时 loadFailed 那条 alert 已经在说话了，
  // 这里若初值非 0，会再叠一条「有 N 笔没算进合计」的假警报。
  uncounted_count: 0, uncounted_cny: 0,
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
  { label: '总支出', value: data.total_jpy, color: 'var(--brand)', sub: `汇率 1元≈${data.fx_rate ?? '—'}円` },
  { label: '商品（含快递）', value: data.order_jpy, color: 'var(--ok)', sub: `${data.order_count} 单` },
  { label: '集运运费', value: data.shipment_jpy, color: 'var(--warn)', sub: `${data.shipment_count} 单` },
  { label: '杂项', value: data.misc_jpy, color: 'var(--danger)', sub: `${data.misc_count} 项` },
])

const loadFailed = ref(false)   // 上一次加载是否失败：整屏据此说实话，见模板顶部
// 上一次**成功**加载的时刻（空 = 从来没成功过）。失败时的文案二选一全靠它：
// 有值 ⇒ 屏幕上是真数据只是旧了；没有 ⇒ 屏幕上是全 0 的初值。
const loadedAt = ref('')

async function load() {
  loading.value = true
  try {
    Object.assign(data, await dashboardApi.get())
    loadFailed.value = false
    loadedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e) {
    loadFailed.value = true     // 拦截器已弹过 toast；这里负责让**页面本身**留下痕迹
    // 再往控制台记一条：toast 三秒就没了、alert 只说结论不说原因，
    // 事后要查「到底是网络断了还是后端 500」就只剩这一条。
    console.error('[看板] 数据加载失败', { hadDataBefore: !!loadedAt.value, error: e })
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
.load-failed { margin-bottom: 12px; }
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
