<!-- 每一页顶部那一行：H1 + 一个「?」。

     **标题不接受 prop，从 `route.meta.title` 取。** router/index.js 里每条路由都写了
     title（左侧导航、浏览器标签页标题也都用它），那就是唯一来源——加一个页面，
     它的 H1 自动就有、且与导航里那一项逐字相同。写成 prop 的话就是第四份手抄清单，
     而这个项目已经在「三份手抄清单漂移」上栽过。

     说明文字收进「?」：原先各页在标题下面挂一段小字（.lead / .hint），
     每页都占掉两三行、字号还比正文小。那段话是「第一次来的人要看一遍」的东西，
     不是每天都要看的——放进点开才显示的气泡里，页面顶部就只剩一行。 -->
<template>
  <div v-if="!hidePageTitle" class="page-hd">
    <h1 class="page-title">{{ title }}</h1>

    <el-popover v-if="$slots.default" trigger="click" :width="440" placement="bottom-start">
      <template #reference>
        <el-icon class="page-help" title="这一页是干什么的"><QuestionFilled /></el-icon>
      </template>
      <div class="page-help-text"><slot /></div>
    </el-popover>

    <!-- 页首右侧的入口（如汇率页的「汇率由插件抓取 →」）。用具名插槽而不是让各页
         自己在标题旁边拼 flex——那样每页的间距和对齐都会各长各的。 -->
    <div class="grow" />
    <slot name="actions" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { QuestionFilled } from '@element-plus/icons-vue'
import { hidePageTitle } from '@/utils/uiPrefs'

const route = useRoute()
const title = computed(() => route.meta?.title || '')
</script>

<style scoped>
.page-hd { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
/* 20px：与原先各页手写的 h2.title 同号——改的是层级语义（h1）与位置统一，
   不是把字放大。左侧导航已经写着页面名，这里再来一个大号标题是双重强调。 */
.page-title { margin: 0; font-size: 20px; font-weight: 600; color: var(--txt-1); line-height: 1.3; }
/* 与 NotionTable 列头那个 .gtn-help 同款：同一种「点开看说明」的东西，同一个样子 */
.page-help { color: var(--txt-3); cursor: pointer; font-size: 15px; }
.page-help:hover { color: var(--brand); }
.grow { flex: 1; }
</style>

<style>
/* popover 挂在 body 上，scoped 够不着。与 .gtn-help-text（列级说明）同一套排版：
   同样是「点开才看的一段解释」，没有理由长得不一样。 */
.page-help-text { font-size: 12px; line-height: 1.9; color: var(--txt-2); }
.page-help-text b { color: var(--txt-1); }
/* 说明里引路径/命令：原先只有设置页有这条（.lead code），随着 .lead 一起作废。
   放这儿之后任何一页的说明都能用。 */
.page-help-text code { background: var(--el-fill-color-light); padding: 1px 5px;
                       border-radius: var(--r-sm); font-size: 11px; }
</style>
