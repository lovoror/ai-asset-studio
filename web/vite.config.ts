import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` proxies API calls to the running compose stack. Build output is served by the FastAPI container.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", assetsDir: "static", sourcemap: false, chunkSizeWarningLimit: 1500 },  // "static": the SPA route /assets/:id must not collide with the bundle dir
  server: {
    port: 5173,
    proxy: {
      "/v1": "http://127.0.0.1:8090",
      "/health": "http://127.0.0.1:8090",
      "/capabilities": "http://127.0.0.1:8090",
    },
  },
});
