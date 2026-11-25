"""Static contract tests for the Stage M protected-Box release pipeline (no Docker needed).

Verify the invariants of `IMPLEMENTATION_PLAN.md` Stage M / `core/eval_and_ops.md` §4 /
`core/licensing_and_distribution.md` §2 without building an image:

* the multi-stage ``Dockerfile`` compiles the protected core to ``.so``, strips the ``.py``
  sources, runs as non-root and declares a ``HEALTHCHECK``;
* ``.dockerignore`` keeps secrets (``.env``) and VCS/test cruft out of the image;
* the release workflow triggers on ``v*`` tags, blocks on CRITICAL CVEs (Trivy), emits a
  CycloneDX SBOM and publishes to the private GHCR;
* the compile helper's protected-module inventory matches the plan.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
COMPILE_SCRIPT = PROJECT_ROOT / "scripts" / "compile_core.py"


def _load_compile_core() -> ModuleType:
    """Loads ``scripts/compile_core.py`` as a module (it lives outside the import path)."""
    spec = importlib.util.spec_from_file_location("compile_core", COMPILE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path: Path) -> str:
    assert path.exists(), f"missing required file: {path}"
    return path.read_text(encoding="utf-8")


def test_dockerfile_is_multistage_and_compiles_protected_core() -> None:
    """The Dockerfile builds in a builder stage and cythonizes + strips the protected core."""
    src = _read(DOCKERFILE)
    assert "AS builder" in src
    assert "AS runtime" in src
    # The protected core is compiled to .so and the .py sources are stripped in the builder stage.
    assert "scripts/compile_core.py --build" in src
    assert "Cython" in src


def test_dockerfile_replaces_installed_core_and_asserts_stripped() -> None:
    """🔴1: the readable pip-installed core is REMOVED (not nested) and verified stripped.

    `cp -r SOURCE DEST` nests the tree when DEST exists; the fix is `rm -rf` then copy, plus an
    `--assert-stripped` guard that fails the build if any readable protected .py survived.
    """
    src = _read(DOCKERFILE)
    assert "rm -rf" in src and "site-packages" in src.lower() or "SITE_PACKAGES" in src
    assert "--assert-stripped" in src
    # No bare `cp -r packages/rag_core <dest>/rag_core` without a preceding rm (nesting bug).
    assert 'rm -rf "${SITE_PACKAGES}/rag_core"' in src


def test_dockerfile_pins_base_image_by_digest() -> None:
    """⚪10: the base image is pinned by @sha256 digest (reproducible scan==push artifact)."""
    src = _read(DOCKERFILE)
    assert "@sha256:" in src
    assert src.count("@sha256:") >= 2  # both builder and runtime stages pinned


def test_dockerfile_has_migration_entrypoint() -> None:
    """🟡6: the box image ships an entrypoint able to run alembic migrations on demand."""
    src = _read(DOCKERFILE)
    assert "docker-entrypoint.sh" in src
    assert "ENTRYPOINT" in src
    # 🟡6: the migrations dir must be COPYed into the image - setuptools does NOT ship it
    # (migrations/ has no __init__.py), so `alembic upgrade head` would otherwise fail.
    assert "packages/rag_core/db/migrations" in src
    entry = (PROJECT_ROOT / "scripts" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "alembic" in entry and "upgrade head" in entry
    assert "RUN_MIGRATIONS" in entry
    assert "/app/migrations" in entry


def test_entrypoint_backs_up_db_before_migrations() -> None:
    """Pre-upgrade hook: a pg_dump runs BEFORE alembic, and the upgrade aborts if it cannot."""
    entry = (PROJECT_ROOT / "scripts" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "pg_dump" in entry
    assert "BACKUP_DIR" in entry
    # The SQLAlchemy DSN (postgresql+psycopg://) is normalized to a libpq URI for pg_dump.
    assert "postgresql+psycopg://" in entry
    # The dump must happen BEFORE the migration is applied.
    assert entry.index("pg_dump") < entry.index("upgrade head")


def test_dockerfile_ships_pg_dump_for_backup_hook() -> None:
    """The runtime stage installs postgresql-client so the pg_dump hook can actually run."""
    src = _read(DOCKERFILE)
    assert "postgresql-client" in src


def test_dockerfile_runs_nonroot_with_healthcheck() -> None:
    """Supply-chain hardening: non-root USER + a HEALTHCHECK are present (Stage M)."""
    src = _read(DOCKERFILE)
    assert "USER rag" in src
    assert "HEALTHCHECK" in src
    # The healthcheck hits the real widget liveness endpoint (services/api/widget_app.py).
    assert "/healthz" in src


def test_dockerignore_excludes_secrets_and_cruft() -> None:
    """.dockerignore keeps .env secrets and VCS/test cruft out of the image context."""
    src = _read(DOCKERIGNORE)
    for pattern in (".env", ".git", "tests/", "__pycache__/", "node_modules/"):
        assert pattern in src, f".dockerignore must exclude {pattern}"


def test_dockerignore_keeps_the_admin_spa_bundle() -> None:
    """🔴2: the `dist/` exclusion is root-anchored so `apps/admin/dist` is NOT excluded."""
    lines = _read(DOCKERIGNORE).splitlines()
    assert "/dist/" in lines, "dist/ must be root-anchored (a bare `dist/` drops the admin SPA)"
    assert "dist/" not in lines, "bare `dist/` line would exclude apps/admin/dist from the context"


def test_release_workflow_triggers_on_version_tags() -> None:
    """The release pipeline runs on SemVer `v*` tags (IMPLEMENTATION_PLAN.md §5)."""
    src = _read(RELEASE_WORKFLOW)
    assert "tags:" in src
    assert '"v*"' in src


def test_release_workflow_blocks_on_critical_cves() -> None:
    """Trivy scans the image and fails (exit-code 1) on CRITICAL CVEs - release is blocked."""
    src = _read(RELEASE_WORKFLOW)
    assert "trivy-action" in src
    assert 'exit-code: "1"' in src
    assert "CRITICAL" in src


def test_release_workflow_emits_cyclonedx_sbom() -> None:
    """An SBOM (CycloneDX) is generated and attached to the release."""
    src = _read(RELEASE_WORKFLOW)
    assert "sbom-action" in src
    assert "cyclonedx" in src.lower()
    assert "sbom.cyclonedx.json" in src


def test_release_workflow_publishes_the_scanned_artifact_once() -> None:
    """🟠3: build ONCE (load), scan, then `docker push` the SAME tags (no second build)."""
    src = _read(RELEASE_WORKFLOW)
    assert "ghcr.io" in src
    assert "enterprise-v" in src  # the rag-core:enterprise-vX.Y tag scheme
    # Pushed via `docker push` of the loaded tags, not a second build-push-action with push:true.
    assert "docker push" in src
    assert "push: true" not in src
    assert src.count("load: true") == 1


def test_release_workflow_builds_the_frontend() -> None:
    """🔴2: the gitignored admin SPA is built in the workflow before the Docker build."""
    src = _read(RELEASE_WORKFLOW)
    assert "pnpm -r build" in src
    assert "setup-node" in src


def test_release_workflow_wires_per_client_watermark() -> None:
    """🟠4: a per-release stamp is derived from a real VENDOR_KEY secret (not one static blob)."""
    src = _read(RELEASE_WORKFLOW)
    assert "VENDOR_KEY" in src
    assert "BuildStamp" in src
    assert "RAG_BUILD_STAMP=${{ env.RAG_BUILD_STAMP }}" in src


def test_release_workflow_asserts_no_sources_and_smokes_image() -> None:
    """🟡6/7: the built image is smoke-tested and asserted free of readable protected .py."""
    src = _read(RELEASE_WORKFLOW)
    assert "--assert-stripped" in src
    # The smoke step imports the COMPILED core from inside the built image (deterministic).
    assert "compiled core imports OK" in src
    assert "rag_core.retrieval.pipeline" in src


def test_compile_script_check_lists_protected_modules() -> None:
    """`compile_core.py --check` reports the protected packages AND the per-module IP targets."""
    result = subprocess.run(
        [sys.executable, str(COMPILE_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout
    assert "rag_core/licensing [ok]" in out
    assert "rag_core/retrieval [ok]" in out
    # 🟠5: chunking + context-budgeting/prompt assembly are protected too (spec §2).
    assert "rag_core/ingestion/splitter.py [ok]" in out
    assert "rag_core/generation/prompt.py [ok]" in out
    assert "rag_core/generation/pipeline.py [ok]" in out
    # __init__.py must stay importable (not compiled away), concrete modules must be compiled.
    assert "rag_core/retrieval/pipeline.py" in out
    assert "licensing/__init__.py" not in out


def test_assert_stripped_passes_on_compiled_tree_and_fails_on_readable(tmp_path: Path) -> None:
    """`--assert-stripped` is green only when every protected module is a .so with no .py."""
    compile_core = _load_compile_core()

    rag_core = tmp_path / "rag_core"
    # Build a minimal fake INSTALLED tree: a .so per protected module, __init__.py kept.
    for src in compile_core.iter_protected_sources():
        rel = src.relative_to(compile_core.PACKAGES_ROOT / "rag_core")
        target_dir = rag_core / rel.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / f"{rel.stem}.cpython-311-x86_64-linux-gnu.so").write_bytes(b"\x00")
        (target_dir / "__init__.py").write_text("", encoding="utf-8")

    assert compile_core._assert_stripped(rag_core) == 0

    # Now plant a readable .py next to a .so -> the guard must fail (sources leaked into the image).
    leaked = rag_core / "retrieval" / "pipeline.py"
    leaked.write_text("secret = 1\n", encoding="utf-8")
    assert compile_core._assert_stripped(rag_core) == 1


def test_assert_stripped_fails_when_so_missing(tmp_path: Path) -> None:
    """A tree with the .py stripped but NO .so is also a failure (broken compile)."""
    compile_core = _load_compile_core()

    rag_core = tmp_path / "rag_core"
    rag_core.mkdir()
    assert compile_core._assert_stripped(rag_core) == 1
