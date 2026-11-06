import { defineConfig } from "vite";

// Web-виджет → dist/ (встраиваемый бандл, Shadow DOM; core/channels.md,
// core/eval_and_ops.md §3.6).
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
