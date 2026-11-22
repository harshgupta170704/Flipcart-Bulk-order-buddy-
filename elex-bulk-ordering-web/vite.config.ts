import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The browser only ever talks to this origin. In dev Vite proxies to the
// backend; in prod nginx does the same thing (see frontend/nginx.conf).
//   /api/*  -> backend with the /api prefix STRIPPED
//   /ws/*   -> backend with the /ws prefix PRESERVED
// `vite preview` does NOT inherit server.proxy, so the same config is applied
// to both. Without this, a production build served by `npm run preview` would
// render fine while every API call 404s.
const proxy = {
  "/api": {
    target: "http://127.0.0.1:8000",
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ""),
  },
  "/ws": {
    target: "ws://127.0.0.1:8000",
    ws: true,
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // ".devtunnels.ms": the shareable Dev Tunnels link (devtunnel host -p 5173).
  server: { port: 5173, proxy, allowedHosts: [".devtunnels.ms"] },
  preview: { port: 4173, proxy },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
