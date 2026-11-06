"""Smoke-тесты целостности спецификации (Фаза 1-2: прикладного кода ещё нет).

Пока нет production-кода, CI должен проверять хотя бы инварианты документов-ядра,
иначе "зелёный CI" ничего не ловит. Тесты — только stdlib, без внешних зависимостей.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORE = PROJECT_ROOT / "core"

# Единый канонический реестр feature-флагов лицензии (enum LicenseFeature, modules_spec.md).
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
    """Все ключевые документы-ядра на месте и не пустые."""
    for name in EXPECTED_CORE_DOCS:
        doc = CORE / name
        assert doc.is_file(), f"Отсутствует документ ядра: core/{name}"
        assert doc.stat().st_size > 0, f"Пустой документ ядра: core/{name}"


def test_license_feature_enum_matches_ddl_check() -> None:
    """Набор LicenseFeature в modules_spec.md совпадает с CHECK-констрейнтом в storage_schema.md.

    Защита от рассинхрона реестра feature-флагов между Pydantic-enum и DDL.
    """
    modules_spec = (CORE / "modules_spec.md").read_text(encoding="utf-8")
    storage_schema = (CORE / "storage_schema.md").read_text(encoding="utf-8")
    for feature in CANONICAL_LICENSE_FEATURES:
        assert f'"{feature}"' in modules_spec, f"Фича {feature} не описана в modules_spec.md"
        assert f'"{feature}"' in storage_schema, f"Фича {feature} не в CHECK storage_schema.md"


def test_deprecated_telemetry_alias_not_used_as_canonical() -> None:
    """Устаревшее имя telemetry_forward_enabled допустимо только как явно запрещённый alias."""
    modules_spec = (CORE / "modules_spec.md").read_text(encoding="utf-8")
    for line in modules_spec.splitlines():
        if "telemetry_forward_enabled" in line:
            assert ("устаревш" in line.lower()) or ("alias" in line.lower()), (
                "telemetry_forward_enabled должен упоминаться только как устаревший alias"
            )


def test_rls_three_role_model_declared() -> None:
    """В storage_schema.md зафиксирована трёхролевая RLS-модель (app_user член authenticated)."""
    storage_schema = (CORE / "storage_schema.md").read_text(encoding="utf-8")
    assert "GRANT authenticated TO app_user" in storage_schema
    assert "BYPASSRLS" in storage_schema
