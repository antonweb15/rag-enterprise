"""Tests for the per-build leak-attribution watermark (Stage M).

Covers `core/licensing_and_distribution.md` §4a: the deterministic fingerprint derivation
``HMAC-SHA256(vendor_key, license_id | client_id | build_id)``, stamp (de)serialization,
silent runtime reading and vendor-side leak attribution.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from rag_core.watermark import (
    FINGERPRINT_SEPARATOR,
    STAMP_ENV_VAR,
    BuildStamp,
    attribute_leak,
    compute_fingerprint,
    current_stamp,
    load_stamp,
    write_stamp,
)

VENDOR_KEY = "vendor-secret-not-the-license-key"
LICENSE_ID = "987fbc82-9a1b-4c3d-8e5f-0123456789ab"
CLIENT_ID = "company_abc"
BUILD_ID = "enterprise-v1.4.0"


def test_fingerprint_matches_documented_formula() -> None:
    """The fingerprint is exactly HMAC-SHA256(vendor_key, license|client|build) (§4a)."""
    message = FINGERPRINT_SEPARATOR.join((LICENSE_ID, CLIENT_ID, BUILD_ID))
    expected = hmac.new(
        VENDOR_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert compute_fingerprint(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID) == expected


def test_fingerprint_is_deterministic_and_accepts_bytes_key() -> None:
    """Same inputs -> same mark; a str and bytes vendor key are equivalent."""
    a = compute_fingerprint(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID)
    b = compute_fingerprint(VENDOR_KEY.encode("utf-8"), LICENSE_ID, CLIENT_ID, BUILD_ID)
    assert a == b == compute_fingerprint(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID)


def test_different_clients_get_different_marks() -> None:
    """Different clients = different hashes (attribution, §4a 'per-build stamp')."""
    one = compute_fingerprint(VENDOR_KEY, LICENSE_ID, "client_one", BUILD_ID)
    two = compute_fingerprint(VENDOR_KEY, LICENSE_ID, "client_two", BUILD_ID)
    assert one != two


def test_wrong_vendor_key_cannot_reproduce_mark() -> None:
    """A client without the vendor secret cannot forge someone else's fingerprint (§4a)."""
    genuine = compute_fingerprint(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID)
    forged = compute_fingerprint("attacker-guess", LICENSE_ID, CLIENT_ID, BUILD_ID)
    assert genuine != forged


def test_build_stamp_create_and_verify() -> None:
    """A created stamp verifies with the vendor key and fails with a wrong one."""
    stamp = BuildStamp.create(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID)
    assert stamp.fingerprint == compute_fingerprint(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID)
    assert stamp.verify(VENDOR_KEY) is True
    assert stamp.verify("wrong-key") is False


def test_build_stamp_json_roundtrip() -> None:
    """JSON (de)serialization preserves every field (artifact baking / registry)."""
    created = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    stamp = BuildStamp.create(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID, created_at=created)
    restored = BuildStamp.from_json(stamp.to_json())
    assert restored == stamp
    assert restored.created_at == "2026-10-26T10:00:00+00:00"


def test_write_and_load_stamp(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The stamp written next to the core round-trips through the filesystem."""
    stamp = BuildStamp.create(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID)
    path = str(tmp_path / "build_stamp.json")
    write_stamp(path, stamp)
    assert load_stamp(path) == stamp


def test_current_stamp_reads_env_and_is_silent_on_garbage() -> None:
    """current_stamp parses $RAG_BUILD_STAMP and never raises on missing/garbage (§4a rule 3)."""
    stamp = BuildStamp.create(VENDOR_KEY, LICENSE_ID, CLIENT_ID, BUILD_ID)
    assert current_stamp({STAMP_ENV_VAR: stamp.to_json()}) == stamp
    assert current_stamp({}) is None
    assert current_stamp({STAMP_ENV_VAR: "not-json"}) is None
    assert current_stamp({STAMP_ENV_VAR: '{"license_id": "x"}'}) is None


def test_attribute_leak_finds_the_source() -> None:
    """A leaked fingerprint is attributed back to its registered build (the registry layer)."""
    registry = [
        BuildStamp.create(VENDOR_KEY, "lic-1", "client_one", BUILD_ID),
        BuildStamp.create(VENDOR_KEY, "lic-2", "client_two", BUILD_ID),
    ]
    leaked = registry[1].fingerprint
    found = attribute_leak(VENDOR_KEY, leaked, registry)
    assert found is not None and found.client_id == "client_two"


def test_attribute_leak_unknown_returns_none() -> None:
    """An unknown fingerprint does not get falsely attributed."""
    registry = [BuildStamp.create(VENDOR_KEY, "lic-1", "client_one", BUILD_ID)]
    assert attribute_leak(VENDOR_KEY, "deadbeef", registry) is None
