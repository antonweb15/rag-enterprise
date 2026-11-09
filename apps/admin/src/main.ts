// Admin SPA - entry point (Stage A skeleton).
//
// The real UI (fastapi-users auth, RBAC, file manager, §5A access management)
// will appear in Stage I (core/admin_panel.md). Here is a minimal type-safe
// bootstrap, sufficient to build Vite -> dist/ and to pass `tsc --noEmit`.

function mount(root: HTMLElement): void {
  root.textContent = "Enterprise RAG Admin - skeleton (Stage A). The UI will be implemented in Stage I.";
}

const root = document.getElementById("app");
if (root) {
  mount(root);
}

// `isolatedModules`: the file must be an ES module.
export {};
