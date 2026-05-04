import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import fs from 'fs'

// Read version from local VERSION file first, fallback to root VERSION
let majorVersion = '0.0.0'
for (const candidate of ['./VERSION', '../VERSION']) {
  try {
    majorVersion = fs.readFileSync(path.resolve(__dirname, candidate), 'utf-8').trim()
    break
  } catch {
    // try next candidate
  }
}
const now = new Date()
const buildTimestamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, '0')}${String(now.getDate()).padStart(2, '0')}.${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}`
const version = `${majorVersion}+${buildTimestamp}`

export default defineConfig(({ mode }) => {
    // Loads .env / .env.<mode> / .env.<mode>.local — empty prefix returns ALL keys
    // so VITE_DEV_API_TARGET (a server-side dev convenience, not a client-exposed
    // VITE_*) is reachable here. .env.development.local should hold the workstation
    // override; it is gitignored.
    const env = loadEnv(mode, __dirname, '')
    const devApiTarget = env.VITE_DEV_API_TARGET || 'http://localhost:8008'
    const devWsTarget = devApiTarget.replace(/^http/, 'ws')

    return {
        plugins: [react()],
        define: {
            __APP_VERSION__: JSON.stringify(version),
        },
        resolve: {
            alias: {
                '@': path.resolve(__dirname, './src'),
            },
        },
        server: {
            port: 3008,
            host: '0.0.0.0',
            proxy: {
                // VITE_DEV_API_TARGET lets a workstation point /api at a remote runtime
                // backend (e.g. https://app.isola.epic.dm) instead of the in-container
                // localhost:8008. Default preserves the in-container path.
                '/api': {
                    target: devApiTarget,
                    changeOrigin: true,
                },
                '/ws': {
                    target: devWsTarget,
                    ws: true,
                    changeOrigin: true,
                },
            },
        },
    }
})
