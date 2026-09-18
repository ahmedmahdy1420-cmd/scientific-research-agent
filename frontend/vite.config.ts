import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    // Vite rejects requests whose Host header it does not recognise (a DNS
    // rebinding defence). Inside compose the browser may address the dev
    // server by its service name, so that has to be allowed explicitly.
    allowedHosts: (process.env.VITE_ALLOWED_HOSTS ?? "localhost,127.0.0.1,frontend")
      .split(",")
      .map((host) => host.trim())
      .filter(Boolean),
    // Proxy the API so the browser makes same-origin requests: no CORS
    // preflight in development, and VITE_API_BASE_URL can stay empty.
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
