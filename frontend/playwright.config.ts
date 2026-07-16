import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, devices } from '@playwright/test'

const FRONTEND_PORT = 5173
const BACKEND_PORT = 8000

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..')
const srcDir = path.join(repoRoot, 'src')

// Prefer this repo's own virtualenv (created by local dev setup); CI has no .venv and
// installs dependencies onto the system interpreter instead, so fall back to whatever
// "python"/"python3" the environment already resolves.
function resolvePythonExecutable(): string {
  const override = process.env.PLAYWRIGHT_PYTHON
  if (override) return override
  const windowsVenv = path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
  const posixVenv = path.join(repoRoot, '.venv', 'bin', 'python')
  if (existsSync(windowsVenv)) return windowsVenv
  if (existsSync(posixVenv)) return posixVenv
  return process.platform === 'win32' ? 'python' : 'python3'
}

const pythonExecutable = resolvePythonExecutable()

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: [
    {
      command:
        `"${pythonExecutable}" -m uvicorn shelfwise_backend.app:app --host 127.0.0.1 --port ` +
        BACKEND_PORT +
        ` --app-dir "${srcDir}"`,
      url: `http://127.0.0.1:${BACKEND_PORT}/health`,
      reuseExistingServer: !process.env.CI,
      env: {
        SHELFWISE_STORE_BACKEND: 'memory',
        SHELFWISE_TENANT_ID: 'sa_retail_demo',
      },
      timeout: 60_000,
    },
    {
      // vite.config.ts proxies API paths to VITE_DEV_API (defaults to localhost:8000,
      // matching BACKEND_PORT above) so no explicit env override is needed here.
      command: 'npm run dev -- --host 127.0.0.1 --port ' + FRONTEND_PORT,
      url: `http://127.0.0.1:${FRONTEND_PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
})
