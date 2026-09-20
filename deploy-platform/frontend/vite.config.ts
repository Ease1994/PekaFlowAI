/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
  optimizeDeps: {
    include: ['swagger-ui-react'],
  },
  server: {
    host: true, // 监听所有网卡，虚拟机/局域网设备可访问
    port: 5173,
    proxy: {
      // 后端 API 代理，避免跨域
      '/api': {
        target: 'http://localhost:8080',
        changeOrigin: true,
        // 助手 SSE 会开几分钟；默认 2 分钟超时会把思考态掐掉。
        timeout: 300000,
        proxyTimeout: 300000,
      },
    },
  },
})
