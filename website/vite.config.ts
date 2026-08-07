import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { defineConfig } from "vite";

export default defineConfig({
 plugins: [react(), tailwindcss()],
 resolve: {
 alias: {
 "@": path.resolve(import.meta.dirname, "client", "src"),
 "@assets": path.resolve(import.meta.dirname, "attached_assets"),
 },
 },
 envDir: path.resolve(import.meta.dirname),
 root: path.resolve(import.meta.dirname, "client"),
 build: {
 outDir: path.resolve(import.meta.dirname, "dist/public"),
 emptyOutDir: true,
 },
 server: {
 port: 3000,
 strictPort: false,
 host: true,
 // Leading dot = wildcard for any subdomain (Vite syntax). Covers
 // lab.zspan.org + any future *.zspan.org tunnel host without re-
 // debugging the same "blocked request" host-allowlist error.
 allowedHosts: ["localhost", "127.0.0.1", ".zspan.org"],
 fs: {
 strict: true,
 deny: ["**/.*"],
 },
 },
});
