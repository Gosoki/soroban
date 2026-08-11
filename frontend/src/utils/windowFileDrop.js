/**
 * 整窗拖图：把图片拖到浏览器任意位置松手即入队。
 *
 * **为什么必须是共享的**：这套东西有五个必须同时存在的部分——
 *   ① `isFileDrag` 判据（列头换序拖的是 text/plain，不判就会被误触）
 *   ② 四个处理函数
 *   ③ `dragover` 里的 preventDefault（**不写就根本不触发 drop**）
 *   ④ onMounted 注册
 *   ⑤ onBeforeUnmount 反注册
 * 少任何一个都是**静默**故障，而且失败方式一个比一个隐蔽：
 *   · 少 ④：浏览器按默认行为把当前页**导航到那张图片**——整个 SPA 被顶掉，
 *     暂存表里没保存的编辑全丢，用户只能按后退键回来，全程零报错。
 *     （这一条真发生过：OCR 从订单页搬到暂存页时，四个函数搬了、④⑤ 没跟着搬，
 *      而 OcrButton 的说明里还写着「把图拖到页面任意位置松手」。）
 *   · 少 ⑤：离开页面后监听还挂在 window 上，在别的页面拖文件会去调已卸载组件的回调。
 *   · 少 ③：拖上去有反应、松手什么也不发生。
 *
 * 让每个页面自己抄一遍，就是让每个页面各有一次机会漏掉其中一样。
 *
 * @param {(files: File[]) => void} onFiles 松手时拿到的文件列表
 * @returns {{ dragActive: import('vue').Ref<boolean> }} 拖拽中（页面据此浮出提示层）
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

export function useWindowFileDrop(onFiles) {
  const dragActive = ref(false)
  // dragenter/leave 会因子元素冒泡多次触发，用计数判断是否真的离开了窗口
  let depth = 0

  const isFileDrag = (e) =>
    !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')

  const enter = (e) => {
    if (!isFileDrag(e)) return        // 列头换序拖的是 text/plain，不会误触
    e.preventDefault(); depth++; dragActive.value = true
  }
  const over = (e) => {
    if (!isFileDrag(e)) return
    e.preventDefault()                // 必须 preventDefault，否则不触发 drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  }
  const leave = (e) => {
    if (!isFileDrag(e)) return
    depth = Math.max(0, depth - 1)
    if (depth === 0) dragActive.value = false
  }
  const drop = (e) => {
    if (!isFileDrag(e)) return
    e.preventDefault(); depth = 0; dragActive.value = false
    onFiles(Array.from(e.dataTransfer.files || []))
  }

  const pairs = [['dragenter', enter], ['dragover', over], ['dragleave', leave], ['drop', drop]]
  onMounted(() => pairs.forEach(([k, fn]) => window.addEventListener(k, fn)))
  onBeforeUnmount(() => pairs.forEach(([k, fn]) => window.removeEventListener(k, fn)))

  return { dragActive }
}
