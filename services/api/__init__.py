"""`services.api` - the transport layer of Enterprise RAG (FastAPI apps and workers).

One codebase - two delivery modes (`saas_cloud` / `on_premise_box`), the difference being only
in the runtime config (`GET /api/v1/admin/config`); forks are forbidden
(`core/architecture.md` §1.1).

The layer depends on the `rag_core` abstractions (DI) and contains no core domain logic.
In Stage A this is only the package skeleton; the applications (`admin_app`, `widget_app`),
channels and Taskiq tasks will appear in later stages (`core/eval_and_ops.md` §3.6).
"""

from __future__ import annotations
