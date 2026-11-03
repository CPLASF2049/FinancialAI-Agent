import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 产物直接落到 FastAPI 的静态目录，服务端不用额外配置托管
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "../src/finagent/web/static/ui",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      // dev 模式下把 API 打到后端，避免跨域
      "/api": "http://127.0.0.1:8000",
    },
  },
});
