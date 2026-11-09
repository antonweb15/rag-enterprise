import { defineConfig } from "vite";

// Web widget -> dist/ (embeddable bundle, Shadow DOM; core/channels.md,
// core/eval_and_ops.md §3.6).
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
