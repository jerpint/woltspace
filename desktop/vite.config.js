import { defineConfig } from "vite";

// Tauri expects the dev server on a fixed port (see devUrl in tauri.conf.json).
const host = process.env.TAURI_DEV_HOST;

export default defineConfig({
  // Prevent Vite from clobbering Tauri's own CLI output.
  clearScreen: false,
  server: {
    host: host || "127.0.0.1",
    port: 1420,
    strictPort: true,
    hmr: host ? { protocol: "ws", host, port: 1421 } : undefined,
    // Tauri watches the Rust source itself; don't double-watch it here.
    watch: { ignored: ["**/src-tauri/**"] },
  },
});
