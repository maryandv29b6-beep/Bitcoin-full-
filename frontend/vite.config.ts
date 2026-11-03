import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes into the Python package so `python -m flowlens serve` serves the UI.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../backend/flowlens/static", emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", ws: true } },
  },
});
