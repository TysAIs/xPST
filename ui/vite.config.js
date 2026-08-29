import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";

// Dev proxy: the FastAPI engine (xpst dashboard / `xpst ui`) runs on
// 127.0.0.1:8080. Every engine route is reachable from the dev server so
// the Svelte app can call /api/*, /health, /state, /bio, /metrics during
// development exactly as it will against the mounted production engine.
const ENGINE = "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [tailwindcss(), svelte()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: ENGINE, changeOrigin: true },
      "/health": { target: ENGINE, changeOrigin: true },
      "/state": { target: ENGINE, changeOrigin: true },
      "/bio": { target: ENGINE, changeOrigin: true },
      "/metrics": { target: ENGINE, changeOrigin: true },
      "/oauth/callback": { target: ENGINE, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
