import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const BACKEND = "http://127.0.0.1:8787";

// Dev proxy: /api and the /app data/stream/ws sub-resources go to the Python
// backend. A bare document navigation to /app/<handle> is served by the SPA
// (client-side routing), so React renders the Driver instead of the backend's
// legacy HTML page. Everything under /app/<handle>/... (dom, menu, windows,
// icon, events, stream, ws, stream_ws) is proxied, WebSockets included.
export default defineConfig({
  base: "/",
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true, ws: true },
      "/app": {
        target: BACKEND,
        changeOrigin: true,
        ws: true,
        bypass(req) {
          const accept = req.headers.accept || "";
          const url = req.url || "";
          const isDoc = req.method === "GET" && accept.includes("text/html");
          const isBare = /^\/app\/[^/]+(\?.*)?$/.test(url);
          if (isDoc && isBare) return "/index.html";
          return undefined;
        },
      },
    },
  },
});
