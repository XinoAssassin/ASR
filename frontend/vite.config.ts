import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, type Plugin, type ResolvedConfig } from 'vite'

/**
 * Copy `public/` into the build output with read + write. Vite's own copy uses
 * fs.copyFileSync, which also copies the file mode; WSL drvfs mounts (/mnt/<drive>) reject that
 * with EPERM. Behaviour is otherwise identical, so this is used on every platform.
 */
function copyPublicDir(): Plugin {
  let config: ResolvedConfig
  const copyDir = (src: string, dst: string): void => {
    mkdirSync(dst, { recursive: true })
    for (const entry of readdirSync(src, { withFileTypes: true })) {
      const from = join(src, entry.name)
      const to = join(dst, entry.name)
      if (entry.isDirectory()) copyDir(from, to)
      else writeFileSync(to, readFileSync(from))
    }
  }
  return {
    name: 'asr:copy-public-dir',
    apply: 'build',
    configResolved(resolved) {
      config = resolved
    },
    writeBundle() {
      if (config.publicDir) copyDir(config.publicDir, resolve(config.root, config.build.outDir))
    },
  }
}

// Dev: the SPA calls same-origin `/api/...`; Vite proxies it to the backend on :8000.
// Prod: `/config.js` (public/config.js, rendered by deploy/frontend-entrypoint.sh in Docker)
// supplies the backend URL.
export default defineConfig({
  plugins: [vue(), copyPublicDir()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    copyPublicDir: false, // done by copyPublicDir() above
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: false },
    },
  },
  preview: {
    port: 4173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: false },
    },
  },
})
