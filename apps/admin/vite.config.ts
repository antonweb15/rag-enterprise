import { defineConfig } from "vite";

// Admin SPA -> dist/, which admin-api serves as StaticFiles
// (core/admin_panel.md, core/eval_and_ops.md §3.6).
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
