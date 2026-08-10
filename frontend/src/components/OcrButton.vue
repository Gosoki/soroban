<!-- 工具栏右侧的 OCR 入口：相机 + 一句话的虚线投放区 + 一个「?」。

     形态是**虚线投放区**（不是普通按钮）：它既能点、又是整窗拖图的落点提示，
     虚线边就是「这里可以扔东西进来」的通用语言，实心按钮读不出这层意思。
     取值与改版前逐字相同：`1px dashed var(--border-strong)` + `var(--brand-soft)`，
     悬停转 `--brand` 实线感 + `--brand-weak` 底。

     两页原先各写了一份，尺寸和文案已经开始不一样——收进这一个组件共用。
     长说明（认哪些平台、拿错截图会怎样）进右边的「?」，正文只留一句够用的。

     **拖拽入口一点没变**：整窗拖图仍然可用（各页自己的 window 监听），
     那句话现在写在「?」里，不是取消了功能。 -->
<template>
  <span class="ocr-entry">
    <el-upload ref="up" class="ocr-up" multiple :show-file-list="false" :auto-upload="false"
               accept="image/*" :on-change="(file, list) => emit('pick', file, list)">
      <div class="ocr-drop" :class="{ busy: !!pending }">
        <el-icon class="ocr-ic"><Camera /></el-icon>
        <span>{{ pending ? `后台识别中 ${pending} 张…` : label }}</span>
      </div>
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
  // 后台还在识别几张。>0 时正文换成张数——这是唯一会变的状态，
  // 不显示的话连点几张图之后完全看不出有没有在跑。
  pending: { type: Number, default: 0 },
  // 正文。两页要说的不是一件事（订单是「识别建单」、集运是「识别成品包裹页」），
  // 所以由页面给；组件只管长相一致。
  label: { type: String, required: true },
})
const emit = defineEmits(['pick'])

const up = ref(null)
// 各页在队列排空后要清 el-upload 的内部列表，否则同一张图选第二次不触发 on-change。
// 组件把它转出去，页面不必知道内部包着一个 el-upload。
defineExpose({ clearFiles: () => up.value?.clearFiles?.() })
</script>

<style scoped>
.ocr-entry { display: inline-flex; align-items: center; gap: 6px; }
/* el-upload 默认是块级，塞进 flex 工具栏会把它撑歪 */
.ocr-up :deep(.el-upload) { display: inline-flex; }
/* 取值与改版前逐字相同，只有两处按新的工具栏调整：
   · height 跟着工具栏的尺寸变量走（原来写死 32px，而筛选栏现在是 30px，
     写死就会比旁边高出 2px——正是上一轮刚统一掉的那种参差）；
   · padding 14 → 18，配上更完整的一句话，整块更舒展。 */
.ocr-drop {
  display: inline-flex; align-items: center; gap: 6px;
  height: var(--el-component-size-small, 30px); padding: 0 18px;
  border: 1px dashed var(--border-strong); border-radius: var(--r-sm);
  color: var(--brand-soft); font-size: 13px; white-space: nowrap; cursor: pointer;
}
.ocr-drop:hover { border-color: var(--brand); background: var(--brand-weak); }
.ocr-drop.busy { color: var(--txt-3); }
.ocr-ic { font-size: 15px; }
.ocr-help { color: var(--txt-3); cursor: pointer; font-size: 15px; }
.ocr-help:hover { color: var(--brand); }
</style>
