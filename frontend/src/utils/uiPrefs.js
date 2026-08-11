/**
 * 界面偏好：**只存在这个浏览器里**，不进数据库。
 *
 * 与设置页那些「业务偏好」是两回事，别混：
 *   · 业务偏好（手填汇率、过期上限…）存库、跨设备一致、影响算出来的钱；
 *   · 界面偏好只影响「这台机器上这个人怎么看」，换台电脑就该重来。
 * 设置页开篇写着「这里改的是业务偏好，存在数据库里、即时生效」——
 * 把界面偏好塞进同一张卡片会让那句话变成假话，所以它在设置页里单独成组、并明说。
 *
 * 为什么是模块级 ref 而不是各组件自己读 localStorage：
 * localStorage 不是响应式的。各读各的话，在设置页拨动开关之后，
 * 已经渲染出来的页首不会有任何反应——要切一次路由或刷新才生效，
 * 而用户会以为开关坏了。ref 是**跨组件共享的同一个**，拨一下所有页首当场跟着变。
 */
import { ref, watch } from 'vue'

const KEY = 'ui.hidePageTitle'

function read() {
  try {
    return localStorage.getItem(KEY) === '1'
  } catch (_) {
    // 隐私模式 / 禁用存储：读不到就当没设过。界面偏好读失败不该让页面起不来。
    return false
  }
}

/** 隐藏每页顶部那行大标题（左侧导航已经写着页面名，是重复信息）。 */
export const hidePageTitle = ref(read())

watch(hidePageTitle, (v) => {
  try {
    if (v) localStorage.setItem(KEY, '1')
    else localStorage.removeItem(KEY)
  } catch (_) { /* 同上：存不住也只是下次打开恢复默认，不影响本次 */ }
})


// —— OCR 上次选的来源 ——
// 与上面那个开关同一套写法（模块级 ref + try/catch 包 localStorage + watch 回写），
// 只是它不在设置页露面：它是「上次怎么选的」这种顺手记住的东西，不是一项配置。
// 记住它是为了常见情形——一个人一批一批地传，多半连着几批都是同一个平台。
const SRC_KEY = 'ui.lastOcrPlatform'

function readSrc() {
  try {
    return localStorage.getItem(SRC_KEY) || ''
  } catch (_) {
    return ''
  }
}

/** 上一次在「这批截图是什么平台的」里选的值，空串 = 没选过。 */
export const lastOcrPlatform = ref(readSrc())

watch(lastOcrPlatform, (v) => {
  try {
    if (v) localStorage.setItem(SRC_KEY, v)
    else localStorage.removeItem(SRC_KEY)
  } catch (_) { /* 同上 */ }
})
