"""A minimal, type-safe DI container binding ports (ABCs) to providers.

The container maps an abstraction (typically a `Base*` ABC) to either a pre-built instance or a
zero-argument factory. Factory-backed bindings are resolved lazily and cached as singletons;
`override()` replaces a binding (used in tests to inject fakes). The implementation is dependency
free - it does not reach into the transport layer or any provider package.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class DependencyError(RuntimeError):
    """Raised when a requested port has no registered provider (or a double registration)."""


class Container:
    """A small singleton-scoped service container.

    Usage:
        container = Container()
        container.register(BaseEmbedder, lambda: MyEmbedder())   # factory (lazy singleton)
        container.register_instance(Settings, Settings())        # ready instance
        embedder = container.resolve(BaseEmbedder)
    """

    def __init__(self) -> None:
        self._factories: dict[type, Callable[[], object]] = {}
        self._instances: dict[type, object] = {}

    def register(self, port: type[T], factory: Callable[[], T]) -> None:
        """Binds a port to a zero-argument factory (resolved lazily, then cached as a singleton)."""
        if port in self._factories or port in self._instances:
            raise DependencyError(f"Provider for {port.__name__} is already registered")
        self._factories[port] = factory

    def register_instance(self, port: type[T], instance: T) -> None:
        """Binds a port to an already-constructed instance (eager singleton)."""
        if port in self._factories or port in self._instances:
            raise DependencyError(f"Provider for {port.__name__} is already registered")
        self._instances[port] = instance

    def override(self, port: type[T], provider: T | Callable[[], T]) -> None:
        """Replaces an existing binding (primarily for tests).

        Accepts either a ready instance or a factory; any cached singleton is discarded. A
        provider is treated as a factory ONLY if it is a plain function/lambda/method or a
        functools.partial (see `_looks_like_factory`); everything else - including a callable
        object with `__call__` or a class - is stored as an instance. This removes the earlier
        ambiguity where a callable instance was misclassified as a factory.
        """
        self._factories.pop(port, None)
        self._instances.pop(port, None)
        if _looks_like_factory(provider):
            self._factories[port] = provider  # type: ignore[assignment]
        else:
            self._instances[port] = provider

    def is_registered(self, port: type) -> bool:
        """Returns True if the port has a binding (instance or factory)."""
        return port in self._factories or port in self._instances

    def resolve(self, port: type[T]) -> T:
        """Returns the singleton instance for a port, building it from the factory on first use."""
        if port in self._instances:
            return self._instances[port]  # type: ignore[return-value]
        factory = self._factories.get(port)
        if factory is None:
            raise DependencyError(f"No provider registered for {port.__name__}")
        instance = factory()
        self._instances[port] = instance
        return instance  # type: ignore[return-value]


def _looks_like_factory(provider: object) -> bool:
    """Classifies a provider as a factory (vs a ready instance).

    A factory is a plain function, lambda, built-in or method, or a functools.partial - the only
    things `register()` is meant to accept. A callable object (an instance with `__call__`) or a
    class is intentionally NOT treated as a factory, so such providers are stored and returned
    as-is instead of being invoked.
    """
    return inspect.isroutine(provider) or isinstance(provider, functools.partial)
