"""`services.api` — транспортный слой Enterprise RAG (FastAPI-приложения и воркеры).

Один код — два режима поставки (`saas_cloud` / `on_premise_box`), различие только в
runtime-конфиге (`GET /api/v1/admin/config`), форки запрещены
(`core/architecture.md` §1.1).

Слой зависит от абстракций `rag_core` (DI) и не содержит доменной логики ядра.
На Этапе A это лишь каркас пакета; приложения (`admin_app`, `widget_app`), каналы и
Taskiq-задачи появятся на последующих этапах (`core/eval_and_ops.md` §3.6).
"""

from __future__ import annotations
