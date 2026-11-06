import { defineConfig } from "vite";

// Admin SPA → dist/, который admin-api отдаёт как StaticFiles
// (core/admin_panel.md, core/eval_and_ops.md §3.6).
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
