"""Smoke tests for the monorepo skeleton (Stage A - foundation: monorepo, tooling, CI).

Verify that the package skeleton imports and that the CI pipeline contains no "stubs" that mask
failures (IMPLEMENTATION_PLAN.md §2 Stage A, eval_and_ops.md §4). The tests use stdlib only,
without external dependencies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Single product SemVer (§5): one source - `[project].version` in pyproject.toml,
# all other artifacts must match.
_PACKAGE_JSON_PATHS = (
    "package.json",
    "apps/admin/package.json",
    "apps/widget/package.json",
)


def _pyproject_version() -> str:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, flags=re.MULTILINE)
    assert match is not None, "version line not found in pyproject.toml"
    return match.group(1)


def test_rag_core_importable_and_versioned() -> None:
    """The canonical core `rag_core` imports and exports its version."""
    import rag_core

    assert rag_core.__version__ == "0.1.0"


def test_services_api_skeleton_importable() -> None:
    """The transport-layer skeleton `services.api` imports (DI layer on top of the core)."""
    import services.api

    assert services.api.__doc__ is not None


def test_versions_are_single_sourced() -> None:
    """Single SemVer (§5): `rag_core.__version__` and all package.json == `[project].version`."""
    import rag_core

    version = _pyproject_version()
    assert rag_core.__version__ == version, "rag_core.__version__ out of sync with pyproject"
    for rel in _PACKAGE_JSON_PATHS:
        data = json.loads((PROJECT_ROOT / rel).read_text(encoding="utf-8"))
        assert data["version"] == version, f"{rel}: version != {version}"


def test_monorepo_structure_present() -> None:
    """Monorepo structure (eval_and_ops.md §3.6): apps, services/api, packages/rag_core."""
    expected = [
        "packages/rag_core/__init__.py",
        "packages/rag_core/py.typed",
        "services/api/__init__.py",
        "apps/admin/package.json",
        "apps/admin/tsconfig.json",
        "apps/admin/vite.config.ts",
        "apps/admin/index.html",
        "apps/admin/src/main.ts",
        "apps/widget/package.json",
        "apps/widget/tsconfig.json",
        "apps/widget/vite.config.ts",
        "apps/widget/index.html",
        "apps/widget/src/main.ts",
        "pnpm-workspace.yaml",
        ".python-version",
        ".env.example",
    ]
    for rel in expected:
        assert (PROJECT_ROOT / rel).is_file(), f"Missing monorepo skeleton element: {rel}"


def test_frontend_scripts_are_real_not_echo_stubs() -> None:
    """Frontend `lint`/`build` are real checks (tsc/vite), not echo stubs.

    The plan explicitly forbids stubs that mask failures (IMPLEMENTATION_PLAN.md §2 Stage A).
    """
    for rel in ("apps/admin/package.json", "apps/widget/package.json"):
        data = json.loads((PROJECT_ROOT / rel).read_text(encoding="utf-8"))
        scripts = data["scripts"]
        for name in ("lint", "build"):
            cmd = scripts[name]
            assert not cmd.strip().startswith("echo"), f"{rel}: script `{name}` is an echo stub"
            assert "tsc" in cmd or "vite" in cmd, f"{rel}: script `{name}` does not call tsc/vite"
        # Toolchain dependencies are actually declared (otherwise the workspace will not build).
        dev_deps = data.get("devDependencies", {})
        assert "vite" in dev_deps and "typescript" in dev_deps, f"{rel}: no vite/typescript"


def test_ci_has_no_bypass_stubs() -> None:
    """CI self-check: steps fail on error, no `|| echo ok` (eval_and_ops.md §4).

    Also asserts the presence of the mandatory blocking quality steps.
    """
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # Check only executable steps (`run:`), not explanatory comments.
    run_lines = [ln for ln in ci.splitlines() if "run:" in ln and not ln.lstrip().startswith("#")]
    for ln in run_lines:
        assert "|| echo" not in ln, "CI must not mask step failures (`|| echo ok`)"
    assert "ruff check ." in ci
    assert "ruff format --check ." in ci
    assert "mypy" in ci
    assert "pytest" in ci
    # Two pipelines (back / front) per core/eval_and_ops.md §3.6.
    assert "backend:" in ci and "frontend:" in ci


def test_env_example_covers_canonical_settings() -> None:
    """`.env.example` has the key variables from the §4 registry (names = `Settings` fields)."""
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    required = [
        # Deployment / Licensing
        "DEPLOYMENT_MODE",
        "LICENSE_PUBLIC_KEY",
        "LICENSE_SERVER_URL",
        # Postgres / Supabase
        "POSTGRES_DSN",
        "POSTGRES_APP_DSN",
        "SUPABASE_URL",
        # S3
        "S3_DEFAULT_BUCKET",
        "S3_REGION_NAME",
        # LLM & Embedder (runtime fields with defaults)
        "GROQ_API_KEY",
        "EMBEDDING_MODEL",
        "EMBEDDING_BATCH_SIZE",
        # RAG parameters
        "RETRIEVAL_TOP_CANDIDATES",
        "RRF_K",
        "RRF_ALPHA",
        "RRF_BETA",
        "CONFIDENCE_THRESHOLD",
        "MAX_HISTORY_TURNS",
        # Admin & rate-limit
        "ADMIN_HOST",
        "ADMIN_PORT",
        "ADMIN_STATIC_DIR",
        "JWT_SECRET",
        "ADMIN_RATE_LIMIT_PER_MINUTE",
        "WIDGET_RATE_LIMIT_PER_MINUTE",
        # Stores
        "VECTOR_STORE_PROVIDER",
        "OBJECT_STORAGE_PROVIDER",
        "OBJECT_STORAGE_LOCAL_ROOT",
        # Observability
        "LANGFUSE_HOST",
        "TRACE_SAMPLE_RATE",
        "TRACE_RETENTION_DAYS",
        "OBSERVABILITY_FORWARD_ENABLED",
        # SSO
        "OIDC_ENABLED",
        "OIDC_GROUPS_CLAIM",
    ]
    for name in required:
        assert f"{name}=" in env_example, f".env.example is missing variable {name}"
    # Deprecated forward-telemetry name is not allowed (§5).
    assert "TELEMETRY_FORWARD_ENABLED=" not in env_example
