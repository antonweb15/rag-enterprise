// Web-виджет — точка входа (каркас Этапа A).
//
// Реальная реализация (Shadow DOM, Origin allowlist, короткоживущий JWT handshake,
// режимы Cloud/Box, auth_mode) появится на Этапе G (core/channels.md). Здесь —
// минимальный типобезопасный bootstrap, достаточный для сборки Vite → dist/ и `tsc --noEmit`.

function mount(root: HTMLElement): void {
  const shadow = root.attachShadow({ mode: "open" });
  const container = document.createElement("div");
  container.textContent = "Enterprise RAG Widget — каркас (Этап A). UI будет реализован на Этапе G.";
  shadow.appendChild(container);
}

const root = document.getElementById("rag-widget-root");
if (root) {
  mount(root);
}

// `isolatedModules`: файл обязан быть ES-модулем.
export {};
