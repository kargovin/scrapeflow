import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/admin/',
  server: {
    proxy: {
      // Proxy API calls to the local FastAPI server during development.
      // Run the API separately: docker compose exec api uv run uvicorn app.main:app ...
      '/admin/users': { target: 'http://localhost:8000', changeOrigin: true },
      '/admin/jobs': { target: 'http://localhost:8000', changeOrigin: true },
      '/admin/stats': { target: 'http://localhost:8000', changeOrigin: true },
      '/jobs': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
