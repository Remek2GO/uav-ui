import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const backend = "http://127.0.0.1:8090";

export default defineConfig({
  plugins: [react()],
  build: { chunkSizeWarningLimit: 1500 },
  server: {
    proxy: {
      "/api": backend,
      "/tiles": backend,
      "/video": backend,
      "/plugin-assets": backend,
      "/ws": { target: backend, ws: true },
    },
  },
});
