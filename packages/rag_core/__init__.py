"""`rag_core` — чистое ядро Enterprise RAG (контракты, абстракции, доменная логика).

Каноническое имя пакета ядра — `rag_core` (единое по всем `core/*.md` и коду;
формы `packages/core`/`core/` считаются устаревшими). Пакет pip-installable
(`pip install -e .`) и не зависит от транспортного слоя (`services/api`) или фронтенда.

На Этапе A здесь зафиксирован лишь каркас: Pydantic V2-модели и `Base*`-интерфейсы
появятся на Этапе C (`core/modules_spec.md`).
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Версия пакета ядра. Синхронизируется с `[project].version` в `pyproject.toml`
#: (единый SemVer на Коробку и Облако, см. IMPLEMENTATION_PLAN.md §5).
__version__ = "0.1.0"
