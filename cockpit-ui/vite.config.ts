import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build lands INSIDE the Python package: cockpit/api.py mounts cockpit/static/
// at "/" and the built output is committed so a zero-Node clone runs (design doc
// docs/plans/2026-07-05-desktop-ui-design.md, "Technology"). The dev proxy points at
// a locally running backend: `python -m swing_screener.cockpit --browser --port 8901`.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/swing_screener/cockpit/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8901',
    },
  },
})
