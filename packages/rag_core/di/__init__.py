"""Dependency-injection layer of the core (`core/modules_spec.md` §2).

The DI container binds the abstract `Base*` ports to concrete providers. The clean core depends
only on the abstractions; the wiring (which provider implements which port) is assembled here, at
the application composition root. Providers appear in later stages (D-H); Stage C only fixes the
container contract and lets tests register fake implementations.
"""

from __future__ import annotations

from rag_core.di.container import Container, DependencyError

__all__ = ["Container", "DependencyError"]
