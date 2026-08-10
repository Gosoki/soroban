<!-- 工具栏右侧的 OCR 入口：一个相机小按钮 + 一个「?」。

     原先是一块 266~344px 宽、带虚线边的投放区，里面写着一整句
     「点击选图 OCR识别（或拖图到页面）」。问题有三个：
       · 它是筛选栏里**最宽**的东西，而筛选才是每天要用的；
       · 那句话第一次看有用、之后每天都在占地方；
       · 订单页和集运页各写了一份，文案与尺寸已经不一样了。
     收成按钮 + 说明进「?」，两页共用这一个组件。

     **拖拽入口一点没变**：整窗拖图仍然可用（各页自己的 window 监听），
     那句话现在写在「?」里，不是取消了功能。 -->
<template>
  <span class="ocr-entry">
    <el-upload ref="up" class="ocr-up" multiple :show-file-list="false" :auto-upload="false"
               accept="image/*" :on-change="(file, list) => emit('pick', file, list)">
      <el-button :icon="pending ? undefined : Camera" :loading="!!pending">
        {{ pending ? `识别中 ${pending}` : 'OCR' }}
      </el-button>
    </el-upload>

    <el-popover trigger="click" :width="400" placement="bottom-end">
      <template #reference>
        <el-icon class="ocr-help" title="OCR 怎么用"><QuestionFilled /></el-icon>
      </template>
      <div class="page-help-text"><slot /></div>
    </el-popover>
  </span>
</template>

<script setup>
import { ref } from 'vue'
import { Camera, QuestionFilled } from '@element-plus/icons-vue'

defineProps({
  // 后台还在识别几张。>0 时按钮转圈并显示张数——这是唯一会变的状态，
  // 不显示的话连点几张图之后完全看不出有没有在跑。
  pending: { type: Number, default: 0 },
})
const emit = defineEmits(['pick'])

const up = ref(null)
// 各页在队列排空后要清 el-upload 的内部列表，否则同一张图选第二次不触发 on-change。
// 组件把它转出去，页面不必知道内部包着一个 el-upload。
defineExpose({ clearFiles: () => up.value?.clearFiles?.() })
</script>

<style scoped>
.ocr-entry { display: inline-flex; align-items: center; gap: 6px; }
/* el-upload 默认是块级，塞进 flex 工具栏会把按钮撑歪 */
.ocr-up :deep(.el-upload) { display: inline-flex; }
.ocr-help { color: var(--txt-3); cursor: pointer; font-size: 15px; }
.ocr-help:hover { color: var(--brand); }
</style>
