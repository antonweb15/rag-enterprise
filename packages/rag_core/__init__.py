"""`rag_core` - the clean core of Enterprise RAG (contracts, abstractions, domain logic).

The canonical name of the core package is `rag_core` (uniform across all `core/*.md` and the
code; the forms `packages/core`/`core/` are considered deprecated). The package is
pip-installable (`pip install -e .`) and does not depend on the transport layer
(`services/api`) or the frontend.

In Stage A only the skeleton is fixed here: Pydantic V2 models and `Base*` interfaces will
appear in Stage C (`core/modules_spec.md`).
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Core package version. Synchronized with `[project].version` in `pyproject.toml`
#: (a single SemVer for Box and Cloud, see IMPLEMENTATION_PLAN.md §5).
__version__ = "0.1.0"
