import { crx } from '@crxjs/vite-plugin'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import manifest from './manifest.config'

export default defineConfig({
  plugins: [react(), tailwindcss(), crx({ manifest })],
  build: {
    target: 'esnext',
    // Vite's preload helper would add <link rel=modulepreload href="/assets/..."> to the *job
    // site's* document when the content script lazy-loads the filler — a 404 on the wrong origin.
    modulePreload: { polyfill: false, resolveDependencies: () => [] },
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5174,
    strictPort: true,
    hmr: { port: 5174 },
  },
})
