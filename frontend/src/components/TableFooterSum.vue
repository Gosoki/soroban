<template>
  <div class="foot">
    <span>共 {{ total }} 条</span>
    <span class="sep">·</span>
    <!-- 「筛选合计」而不是「支出」：这是**你正在看的这些行**加起来多少，
         而看板的支出会排掉退款/关闭/已取消的单。两个数不一样是正常的，
         所以措辞上就要分开——同名不同值才是真正让人不信任数字的地方。 -->
    <span>{{ label }} <b>{{ fmtJPY(sumJpy) }}</b></span>
    <!-- 有钱、却缺汇率没折算的行。不说出来的话，合计静默变小而条数照旧，
         界面上没有任何异常——这是这一栏最危险的失败形态。 -->
    <el-tag v-if="unconverted > 0" :style="typeStyle('warning')" class="warn"
            :title="'这些行有货款、但缺当天汇率，没能折算成日元，因此没有计入上面的合计。去汇率页手填那一天的汇率，之后再编辑一次这些行即可自动补上。'">
      {{ unconverted }} 条未折算
    </el-tag>
  </div>
</template>

<script setup>
import { fmtJPY } from '@/utils/money'
import { typeStyle } from '@/constants'

defineProps({
  total: { type: Number, default: 0 },
  sumJpy: { type: [Number, String], default: null },
  unconverted: { type: Number, default: 0 },
  label: { type: String, default: '筛选合计' },
})
</script>

<style scoped>
/* 与工具栏、卡片头同一套：横排 + gap，不给单个元素挂 margin。 */
.foot { display: flex; align-items: center; gap: 8px; }
.sep { color: var(--txt-3); }
.foot b { color: var(--txt-body); font-variant-numeric: tabular-nums; }
.warn { margin-left: 4px; }
</style>
