import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发环境通过代理连接真实后端；生产构建由后端在 /ui/ 同源提供。
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  server: {
    port: 5173,
    host: true, // 同时监听 IPv4/IPv6，避免 localhost 解析差异导致 127.0.0.1 连不上
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
