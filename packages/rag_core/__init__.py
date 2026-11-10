"""`rag_core` - the clean core of Enterprise RAG (contracts, abstractions, domain logic).

The canonical name of the core package is `rag_core` (uniform across all `core/*.md` and the
code; the forms `packages/core`/`core/` are considered deprecated). The package is
pip-installable (`pip install -e .`) and does not depend on the transport layer
(`services/api`) or the frontend.

Stage C adds the core contracts (`core/modules_spec.md`): the Pydantic V2 domain models and
enumerations, the `Settings` object and the abstract `Base*` interfaces live in the `contracts`
subpackage; the `di` subpackage provides the DI container that binds the abstractions to
providers.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Core package version. Synchronized with `[project].version` in `pyproject.toml`
#: (a single SemVer for Box and Cloud, see IMPLEMENTATION_PLAN.md §5).
__version__ = "0.1.0"
