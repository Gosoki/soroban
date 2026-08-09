import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/tokens.css'   // 必须排在 Element 之后：其中覆盖了 --el-color-*
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'

document.documentElement.classList.add('dark')

const app = createApp(App)
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}
app.use(router)
// size: 'small' 是**全站默认控件尺寸**。
// 之前没设，于是页面里 55 处显式写 size="small"、另外 32 处什么都不写（走 Element 的
// default，比 small 高一档）——同一行筛选栏里输入框和按钮不一样高，就是这么来的。
// 定在这里之后：新写的控件天生就对，不用记得加；登录页那种要刻意放大的仍可显式覆盖。
app.use(ElementPlus, { locale: zhCn, size: 'small' })
app.mount('#app')
