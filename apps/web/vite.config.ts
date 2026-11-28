import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// The agent serves dist/ on APP_PORT and forwards /api, /ws and /bootstrap to the Rust API, so the
// dev server proxies the same paths to a running app (APP_PORT, default 8000 when run from source).
const target = `http://127.0.0.1:${process.env.APP_PORT ?? "8000"}`;

export default defineConfig({
  plugins: [preact()],
  build: { target: "es2022", assetsInlineLimit: 0, chunkSizeWarningLimit: 400 },
  server: {
    proxy: {
      "/api": target,
      "/agent": target,
      "/bootstrap": target,
      "/ws": { target, ws: true },
    },
  },
  test: { environment: "jsdom", globals: false },
});
