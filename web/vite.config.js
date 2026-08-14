import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  server: {
    port: 5173,
    proxy: {
      // 开发环境: /api 代理到 FastAPI 后端(8001)
      '/api': { target: 'http://localhost:8001', changeOrigin: true },
      // 开发环境: 报告产物(/static/reports/... 下的 PDF/PNG/MD)也由后端提供,
      // 否则报告中的图表图片 <img src="/static/reports/..."> 在 5173 下 404 无法显示
      '/static': { target: 'http://localhost:8001', changeOrigin: true }
    }
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        manualChunks: {
          vue: ['vue', 'vue-router', 'pinia'],
          element: ['element-plus', '@element-plus/icons-vue'],
          echarts: ['echarts']
        }
      }
    }
  }
})
