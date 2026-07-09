import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Proxy every backend path same-origin in dev so API calls never hit CORS,
// regardless of which port the dev server lands on.
const backend = process.env.VITE_DEV_API ?? 'http://localhost:8000'
const port = process.env.PORT ? Number(process.env.PORT) : 5173

export default defineConfig({
  plugins: [react()],
  server: {
    port,
    proxy: {
      '/demo': backend,
      '/decisions': backend,
      '/data': backend,
      '/learning': backend,
      '/health': backend,
      '/readiness': backend,
      '/inference': backend,
      '/products': backend,
    },
  },
})
