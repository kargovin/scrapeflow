import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/app/',
  server: {
    proxy: {
      // Proxy API calls to the local FastAPI server during development.
      // Run the API separately: docker compose exec api uv run uvicorn app.main:app ...
      '/admin': { target: 'http://localhost:8000', changeOrigin: true },
      '/users':  { target: 'http://localhost:8000', changeOrigin: true },
      '/jobs':   { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
