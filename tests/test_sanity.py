"""Smoke tests for specification integrity (Phase 1-2: no application code yet).

Until there is production code, CI must check at least the invariants of the core documents,
otherwise a "green CI" catches nothing. The tests use stdlib only, without external
dependencies.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE = PROJECT_ROOT / "core"

# Single canonical registry of license feature flags (enum LicenseFeature, modules_spec.md).
CANONICAL_LICENSE_FEATURES = [
    "web_widget",
    "telegram",
    "rest_sse",
    "cli",
    "enterprise_acl",
    "collections",
    "sso_oidc",
    "hybrid_search",
    "s3_connector",
    "flashrank_rerank",
    "observability_forward",
]

EXPECTED_CORE_DOCS = [
    "INDEX.md",
    "soul.md",
    "architecture.md",
    "modules_spec.md",
    "storage_schema.md",
    "channels.md",
    "admin_panel.md",
    "eval_and_ops.md",
    "licensing_and_distribution.md",
    "license_server.md",
    "vendor_backoffice.md",
]


def test_core_docs_present_and_non_empty() -> None:
    """All key core documents are present and non-empty."""
    for name in EXPECTED_CORE_DOCS:
        doc = CORE / name
        assert doc.is_file(), f"Missing core document: core/{name}"
        assert doc.stat().st_size > 0, f"Empty core document: core/{name}"


def test_license_feature_enum_matches_ddl_check() -> None:
    """The LicenseFeature set in modules_spec.md matches the CHECK constraint in storage_schema.md.

    Guards against desynchronization of the feature-flag registry between the Pydantic enum
    and the DDL.
    """
    modules_spec = (CORE / "modules_spec.md").read_text(encoding="utf-8")
    storage_schema = (CORE / "storage_schema.md").read_text(encoding="utf-8")
    for feature in CANONICAL_LICENSE_FEATURES:
        assert f'"{feature}"' in modules_spec, f"Feature {feature} not described in modules_spec.md"
        assert f'"{feature}"' in storage_schema, (
            f"Feature {feature} not in CHECK of storage_schema.md"
        )


def test_deprecated_telemetry_alias_not_used_as_canonical() -> None:
    """telemetry_forward_enabled is allowed only as an explicitly forbidden alias."""
    modules_spec = (CORE / "modules_spec.md").read_text(encoding="utf-8")
    for line in modules_spec.splitlines():
        if "telemetry_forward_enabled" in line:
            assert ("deprecated" in line.lower()) or ("alias" in line.lower()), (
                "telemetry_forward_enabled must be mentioned only as a deprecated alias"
            )


def test_rls_three_role_model_declared() -> None:
    """storage_schema.md fixes the three-role RLS model (app_user is a member of authenticated)."""
    storage_schema = (CORE / "storage_schema.md").read_text(encoding="utf-8")
    assert "GRANT authenticated TO app_user" in storage_schema
    assert "BYPASSRLS" in storage_schema
