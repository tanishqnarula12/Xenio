/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built interface is served by XENIO itself (python -m app.main --ui, http://127.0.0.1:8765).
// `npm run dev` proxies the API to that running XENIO; start XENIO with --ui-dev-origin http://localhost:5173.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: true } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
