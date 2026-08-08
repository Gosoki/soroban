import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login/index.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/components/Layout.vue'),
    children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'Dashboard', component: () => import('@/views/Dashboard/index.vue'), meta: { title: '看板', icon: 'Odometer' } },
      { path: 'staging', name: 'Staging', component: () => import('@/views/Staging/index.vue'), meta: { title: '暂存订单', icon: 'Tickets' } },
      { path: 'orders', name: 'Orders', component: () => import('@/views/Orders/index.vue'), meta: { title: '商品订单', icon: 'ShoppingCart' } },
      { path: 'items', name: 'Items', component: () => import('@/views/Items/index.vue'), meta: { title: '物品列表', icon: 'Grid' } },
      { path: 'shipment', name: 'Shipment', component: () => import('@/views/Shipment/index.vue'), meta: { title: '集运订单', icon: 'Ship' } },
      { path: 'misc', name: 'Misc', component: () => import('@/views/Misc/index.vue'), meta: { title: '杂项支出', icon: 'Money' } },
      { path: 'fx', name: 'Fx', component: () => import('@/views/Fx/index.vue'), meta: { title: '日元汇率', icon: 'Coin' } },
      { path: 'plugins', name: 'Plugins', component: () => import('@/views/Plugins/index.vue'), meta: { title: '插件管理', icon: 'Connection' } },
      { path: 'database', name: 'Database', component: () => import('@/views/Database/index.vue'), meta: { title: '数据库', icon: 'Coin' } },
      { path: 'settings', name: 'Settings', component: () => import('@/views/Settings/index.vue'), meta: { title: '设置', icon: 'Setting' } },
    ],
  },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('auth_token')
  if (!to.meta?.public && !token) return next('/login')
  if (to.path === '/login' && token) return next('/dashboard')
  next()
})

// 页面标题：Soroban - 页面名（无页面名时只显示 Soroban）
router.afterEach((to) => {
  document.title = to.meta?.title ? `Soroban - ${to.meta.title}` : 'Soroban'
})

export default router
