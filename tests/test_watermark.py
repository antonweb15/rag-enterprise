"""Тесты системы скрытых меток (fingerprint / watermark / canary / build / registry)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_core.watermark import (
    BuildStamp,
    FingerprintRegistry,
    HeartbeatBeacon,
    LicenseIdentity,
    canary_token,
    derive_fingerprint,
    embed_linguistic,
    embed_watermark,
    extract_watermark,
    is_canary_hit,
    make_build_stamp,
    make_canaries,
    make_canary,
    make_heartbeat,
    strip_watermark,
    verify_build_stamp,
    verify_fingerprint,
    verify_heartbeat,
    verify_linguistic,
)

VENDOR_KEY = b"vendor-secret-key-not-license-signing-key"
OTHER_KEY = b"another-vendor-secret-key"


def _identity(client: str = "company_abc") -> LicenseIdentity:
    return LicenseIdentity(
        license_id="lic_987fbc82",
        client_id=client,
        build_id="enterprise-v1.2",
    )


# --------------------------------------------------------------------------- #
# fingerprint
# --------------------------------------------------------------------------- #
def test_fingerprint_is_deterministic() -> None:
    fp1 = derive_fingerprint(VENDOR_KEY, _identity())
    fp2 = derive_fingerprint(VENDOR_KEY, _identity())
    assert fp1 == fp2
    assert len(fp1) == 64  # SHA-256 hex


def test_fingerprint_differs_per_client_and_key() -> None:
    base = derive_fingerprint(VENDOR_KEY, _identity())
    assert base != derive_fingerprint(VENDOR_KEY, _identity("company_xyz"))
    assert base != derive_fingerprint(OTHER_KEY, _identity())


def test_verify_fingerprint() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    assert verify_fingerprint(VENDOR_KEY, _identity(), fp)
    assert not verify_fingerprint(OTHER_KEY, _identity(), fp)


def test_empty_vendor_key_rejected() -> None:
    with pytest.raises(ValueError):
        derive_fingerprint(b"", _identity())


# --------------------------------------------------------------------------- #
# zero-width watermark
# --------------------------------------------------------------------------- #
def test_watermark_roundtrip_and_invisibility() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    text = "Это обычный ответ RAG-системы на запрос пользователя."
    marked = embed_watermark(text, fp)

    assert strip_watermark(marked) == text  # визуально неотличимо
    assert marked != text
    assert extract_watermark(marked) == fp


def test_watermark_absent_returns_none() -> None:
    assert extract_watermark("Текст без меток") is None


def test_watermark_survives_surrounding_text() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    marked = embed_watermark("ядро ответа", fp)
    leaked = "Префикс. " + marked + " Суффикс скопированного текста."
    assert extract_watermark(leaked) == fp


def test_watermark_identifies_source_via_registry() -> None:
    registry = FingerprintRegistry(VENDOR_KEY)
    rec_a = registry.register(_identity("company_abc"))
    registry.register(_identity("company_xyz"))

    leaked = embed_watermark("утёкший ответ", rec_a.fingerprint)
    found = extract_watermark(leaked)
    assert found is not None
    assert registry.identify(found) == rec_a
    assert registry.identify(found).client_id == "company_abc"


# --------------------------------------------------------------------------- #
# canary
# --------------------------------------------------------------------------- #
def test_canary_tokens_are_deterministic_and_unique() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    assert canary_token(fp, 0) == canary_token(fp, 0)
    assert canary_token(fp, 0) != canary_token(fp, 1)

    other_fp = derive_fingerprint(VENDOR_KEY, _identity("company_xyz"))
    assert canary_token(fp, 0) != canary_token(other_fp, 0)


def test_make_canaries_and_hit_detection() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    docs = make_canaries(fp, 3)
    assert len(docs) == 3
    assert len({d.doc_id for d in docs}) == 3

    doc = make_canary(fp, 0)
    assert doc.token in doc.content
    assert is_canary_hit(f"...где-то всплыл термин {doc.token}...", fp)
    assert not is_canary_hit("чистый текст конкурента", fp)


# --------------------------------------------------------------------------- #
# build stamp
# --------------------------------------------------------------------------- #
def test_build_stamp_roundtrip_and_verification() -> None:
    stamp = make_build_stamp(VENDOR_KEY, _identity())
    restored = BuildStamp.from_json(stamp.to_json())
    assert restored == stamp

    assert verify_build_stamp(VENDOR_KEY, _identity(), stamp)
    assert not verify_build_stamp(OTHER_KEY, _identity(), stamp)
    assert not verify_build_stamp(VENDOR_KEY, _identity("company_xyz"), stamp)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_identify_unknown_returns_none() -> None:
    registry = FingerprintRegistry(VENDOR_KEY)
    registry.register(_identity())
    assert registry.identify("deadbeef" * 8) is None


def test_registry_persistence_roundtrip(tmp_path: Path) -> None:
    registry = FingerprintRegistry(VENDOR_KEY)
    rec = registry.register(_identity())
    path = tmp_path / "registry.json"
    registry.save(path)

    loaded = FingerprintRegistry(VENDOR_KEY)
    loaded.load(path)
    assert len(loaded) == 1
    assert loaded.identify(rec.fingerprint) == rec


# --------------------------------------------------------------------------- #
# linguistic watermark
# --------------------------------------------------------------------------- #
_LING_TEXT = (
    "Система поддерживает поиск. Кроме того, например, она устойчива. "
    "Поэтому, однако, таким образом результат надёжен. "
    "В частности, также обычно это работает."
)


def test_linguistic_is_invisible_in_meaning_but_marks_source() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    marked = embed_linguistic(_LING_TEXT, fp)
    # свой отпечаток полностью совпадает
    assert verify_linguistic(marked, fp) == 1.0


def test_linguistic_distinguishes_foreign_fingerprint() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    other = derive_fingerprint(VENDOR_KEY, _identity("company_xyz"))
    marked = embed_linguistic(_LING_TEXT, fp)
    assert verify_linguistic(marked, fp) > verify_linguistic(marked, other)


def test_linguistic_survives_zero_width_stripping() -> None:
    fp = derive_fingerprint(VENDOR_KEY, _identity())
    marked = embed_linguistic(_LING_TEXT, fp)
    # очистка невидимых символов не затрагивает синонимический слой
    assert verify_linguistic(strip_watermark(marked), fp) == 1.0


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def test_heartbeat_roundtrip_and_signature() -> None:
    beacon = make_heartbeat(VENDOR_KEY, _identity(), "hw-123", "2026-11-05T00:00:00Z")
    restored = HeartbeatBeacon.from_json(beacon.to_json())
    assert restored == beacon
    assert beacon.fingerprint == derive_fingerprint(VENDOR_KEY, _identity())
    assert verify_heartbeat(VENDOR_KEY, beacon)
    assert not verify_heartbeat(OTHER_KEY, beacon)


def test_heartbeat_detects_clone_on_foreign_hardware() -> None:
    a = make_heartbeat(VENDOR_KEY, _identity(), "hw-aaa", "2026-11-05T00:00:00Z")
    b = make_heartbeat(VENDOR_KEY, _identity(), "hw-bbb", "2026-11-05T00:00:00Z")
    # один license_id/fingerprint, но разные hardware_id → признак клонирования
    assert a.license_id == b.license_id
    assert a.fingerprint == b.fingerprint
    assert a.hardware_id != b.hardware_id


def test_heartbeat_tamper_is_rejected() -> None:
    beacon = make_heartbeat(VENDOR_KEY, _identity(), "hw-123", "2026-11-05T00:00:00Z")
    tampered = HeartbeatBeacon(
        license_id=beacon.license_id,
        hardware_id="hw-999",
        fingerprint=beacon.fingerprint,
        sent_at=beacon.sent_at,
        signature=beacon.signature,
    )
    assert not verify_heartbeat(VENDOR_KEY, tampered)
