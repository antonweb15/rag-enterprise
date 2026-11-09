"""Docker availability detection + a marker to skip integration tests without it.

Moved to a separate module (not conftest) so it can be safely imported from test files
without depending on conftest autoload rules.

Policy: locally (no CI) the integration RLS tests are skipped when Docker is unavailable, so
a dev run stays green. In CI (`CI`/`GITHUB_ACTIONS`) Docker is mandatory: the tests are NOT
skipped, so a broken/absent Docker daemon surfaces as a hard failure instead of silently
dropping the security coverage while CI stays green.
"""

from __future__ import annotations

import os

import pytest


def in_ci() -> bool:
    """True when running inside a CI environment (GitHub Actions / generic `CI`)."""
    return os.environ.get("CI", "").lower() == "true" or bool(os.environ.get("GITHUB_ACTIONS"))


def docker_available() -> bool:
    """True if the local Docker daemon is available (otherwise testcontainers cannot start)."""
    try:
        import docker  # type: ignore[import-untyped]

        docker.from_env().ping()
        return True
    except Exception:
        return False


# Skip ONLY outside CI. In CI we let the tests run and fail loudly if Docker is missing,
# so the mandatory security contract is never silently skipped.
requires_docker = pytest.mark.skipif(
    not docker_available() and not in_ci(),
    reason="Docker unavailable - integration RLS tests (testcontainers) skipped (local only).",
)
