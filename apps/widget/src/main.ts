// Web widget - entry point (Stage A skeleton).
//
// The real implementation (Shadow DOM, Origin allowlist, short-lived JWT handshake,
// Cloud/Box modes, auth_mode) will appear in Stage G (core/channels.md). Here is a
// minimal type-safe bootstrap, sufficient to build Vite -> dist/ and `tsc --noEmit`.

function mount(root: HTMLElement): void {
  const shadow = root.attachShadow({ mode: "open" });
  const container = document.createElement("div");
  container.textContent = "Enterprise RAG Widget - skeleton (Stage A). The UI will be implemented in Stage G.";
  shadow.appendChild(container);
}

const root = document.getElementById("rag-widget-root");
if (root) {
  mount(root);
}

// `isolatedModules`: the file must be an ES module.
export {};
