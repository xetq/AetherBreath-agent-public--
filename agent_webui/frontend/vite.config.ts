import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式：/api 全部代理到网关 8900（同源，免 CORS）。
// SSE 走同一代理：显式禁用响应缓冲，保证事件实时到达。
// 不依赖 @types/node：从 globalThis 上安全取 process.env
const ENV = (globalThis as unknown as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {}
const GATEWAY = ENV.AETHER_GATEWAY_URL || 'http://127.0.0.1:8900'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: GATEWAY,
        changeOrigin: true,
        ws: false,
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            const ct = String(proxyRes.headers['content-type'] || '')
            if (ct.includes('text/event-stream')) {
              proxyRes.headers['x-accel-buffering'] = 'no'
              proxyRes.headers['cache-control'] = 'no-cache, no-transform'
            }
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
  },
})
