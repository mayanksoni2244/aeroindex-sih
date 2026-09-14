import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /v1 to the FastAPI app so the browser sees one origin
// and CORS never enters the picture during development. In a real deployment
// the same job is done by a reverse proxy in front of both.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': {
        target: process.env.APIX_API_URL || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
