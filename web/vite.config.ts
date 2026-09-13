// VFP: Build and dev-server setup of the interface; in dev, API and live socket are proxied to the local terminal.
// Changes when: the terminal port, build output or bundling needs change.
// Anti-goal:
// 1. Assets from a CDN — the terminal works fully offline from the network except for exchanges.

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const terminal = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": terminal,
      "/ws": { target: terminal, ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
