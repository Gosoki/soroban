import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 本地开发：前端默认 8621，代理 /api → 后端默认 8620（免 CORS）。端口可用环境变量
// FRONTEND_PORT / BACKEND_PORT 覆盖（start.sh 会 export 它们，保持前后端一致）。
const BACKEND_PORT = process.env.BACKEND_PORT || '8620'
const FRONTEND_PORT = Number(process.env.FRONTEND_PORT) || 8621

// 监听地址与后端共用**同一个**旋钮 HOST（backend/run.py 也读它，默认 127.0.0.1）。
// 这里绝不能默认监听全网卡：dev server 把 /api 反代到后端，一旦它对外，
// 后端那句 --host 127.0.0.1 就形同虚设——局域网任何设备同源打到全部 API。
// 要暴露就 `HOST=0.0.0.0 ./start.sh`，前后端一起对外，不可能再单边敞开。
// 默认取 'localhost' 而非 '127.0.0.1'：vite 的 resolveHostname 对 localhost 做了
// DNS 顺序处理（有些机器把它解析成 ::1），写死 v4 字面量会让浏览器连不上。
const HOST = process.env.HOST || 'localhost'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  server: {
    host: HOST,
    port: FRONTEND_PORT,
    proxy: {
      '/api': { target: `http://127.0.0.1:${BACKEND_PORT}`, changeOrigin: true }
    }
  }
})
