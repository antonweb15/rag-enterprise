// Admin SPA — точка входа (каркас Этапа A).
//
// Реальный UI (fastapi-users auth, RBAC, файл-менеджер, §5A управление доступом)
// появится на Этапе I (core/admin_panel.md). Здесь — минимальный типобезопасный
// bootstrap, достаточный для сборки Vite → dist/ и проверки `tsc --noEmit`.

function mount(root: HTMLElement): void {
  root.textContent = "Enterprise RAG Admin — каркас (Этап A). UI будет реализован на Этапе I.";
}

const root = document.getElementById("app");
if (root) {
  mount(root);
}

// `isolatedModules`: файл обязан быть ES-модулем.
export {};
