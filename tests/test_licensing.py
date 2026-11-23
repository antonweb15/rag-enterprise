"""Stage K - licensing client+server tests (`IMPLEMENTATION_PLAN.md` §2 Stage K).

Covers the acceptance checks listed under Stage K, over the UNIFIED codec (the canonical
``PASETO v4.public`` / ``JWT (EdDSA)`` tokens from ``rag_core.admin.license_crypto``, wrapped by
``rag_core.licensing``):

* Ed25519 signature validation / rejection (forgery, tamper, expiry, CRL, wrong key, key rotation).
* Grace Period -> Restricted Mode transitions.
* ``hardware_id`` binding and composite-fingerprint re-activation (audited, rate-limited).
* Heartbeats fail-closed for unknown ids / hardware mismatch; anonymized payload.
* Signed CRL verified before it is applied.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from rag_core.contracts.enums import LicenseFeature, LicenseMode, LicensePlan
from rag_core.contracts.models import (
    HeartbeatPayload,
    LicenseIssueRequest,
    LicensePayload,
    LicenseRevokeRequest,
)
from rag_core.licensing import (
    Ed25519KeyPair,
    Ed25519LicenseValidator,
    Ed25519TokenSigner,
    HardwareMismatchError,
    HeartbeatState,
    InMemoryLicenseServer,
    InvalidLicenseError,
    LicenseError,
    LicenseExpiredError,
    LicenseKeyring,
    LicenseRevokedError,
    LicenseState,
    QuotaExceededError,
    compute_hardware_id,
    fingerprint_similarity,
    generate_keypair,
    sign_license,
    verify_license,
)


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _payload(**overrides: object) -> LicensePayload:
    base: dict[str, object] = {
        "license_id": str(uuid4()),
        "client_id": "company_abc",
        "plan": LicensePlan.ENTERPRISE_BOX,
        "mode": LicenseMode.AIRGAP_ENTERPRISE,
        "issued_at": _now(),
        "expires_at": _now() + timedelta(days=365),
        "max_documents": 100,
        "max_users": 10,
    }
    base.update(overrides)
    return LicensePayload(**base)


@pytest.fixture
def keypair() -> Ed25519KeyPair:
    return generate_keypair("k1")


@pytest.fixture
def keyring(keypair: Ed25519KeyPair) -> LicenseKeyring:
    return LicenseKeyring({keypair.kid: keypair.public_key})


# --- token codec ---------------------------------------------------------------
def test_sign_verify_roundtrip_paseto(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    payload = _payload()
    token = sign_license(payload, keypair.private_seed, keypair.kid)
    assert token.startswith("v4.public.")  # canonical PASETO format
    decoded = verify_license(token, keyring.as_mapping())
    assert decoded.license_id == payload.license_id
    assert decoded.plan is LicensePlan.ENTERPRISE_BOX
    assert decoded.max_documents == 100


def test_sign_verify_roundtrip_jwt(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    token = sign_license(_payload(), keypair.private_seed, keypair.kid, format_type="jwt")
    assert len(token.split(".")) == 3  # compatible JWT EdDSA
    assert verify_license(token, keyring.as_mapping()).client_id == "company_abc"


def test_tampered_payload_rejected(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    token = sign_license(_payload(), keypair.private_seed, keypair.kid)
    # Flip the tail of the signed body -> signature must no longer verify.
    tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
    with pytest.raises(InvalidLicenseError):
        verify_license(tampered, keyring.as_mapping())


def test_foreign_key_signature_rejected(keyring: LicenseKeyring) -> None:
    # A token signed by a DIFFERENT (attacker) key cannot verify against the legit keyring.
    attacker = generate_keypair("k1")
    forged = sign_license(_payload(), attacker.private_seed, "k1")
    with pytest.raises(InvalidLicenseError):
        verify_license(forged, keyring.as_mapping())


def test_wrong_key_rejected(keypair: Ed25519KeyPair) -> None:
    token = sign_license(_payload(), keypair.private_seed, keypair.kid)
    other = generate_keypair("other")
    with pytest.raises(InvalidLicenseError):
        verify_license(token, {"other": other.public_key})


def test_malformed_token_rejected(keyring: LicenseKeyring) -> None:
    with pytest.raises(InvalidLicenseError):
        verify_license("not-a-token", keyring.as_mapping())


def test_forged_jwt_alg_tamper_rejected(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    # Craft a JWT whose header claims a different alg; without the real key the Ed25519 signature
    # check still fails (no "alg confusion" bypass).
    import base64
    import json

    hdr = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    claims = json.dumps(_payload().model_dump(mode="json")).encode()
    pld = base64.urlsafe_b64encode(claims).rstrip(b"=").decode()
    forged = f"{hdr}.{pld}.{base64.urlsafe_b64encode(b'0' * 64).rstrip(b'=').decode()}"
    with pytest.raises(InvalidLicenseError):
        verify_license(forged, keyring.as_mapping())


def test_key_rotation(keypair: Ed25519KeyPair) -> None:
    # A keyring holding two kids verifies tokens signed by either (rotation).
    kp2 = generate_keypair("k2")
    ring = LicenseKeyring({keypair.kid: keypair.public_key, kp2.kid: kp2.public_key})
    t1 = sign_license(_payload(), keypair.private_seed, keypair.kid)
    t2 = sign_license(_payload(), kp2.private_seed, kp2.kid)
    assert ring.verify(t1).client_id == "company_abc"
    assert ring.verify(t2).client_id == "company_abc"


# --- validator: offline checks -------------------------------------------------
def test_validate_token_ok(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    token = sign_license(_payload(), keypair.private_seed, keypair.kid)
    validator = Ed25519LicenseValidator(keyring, clock=_now)
    payload = validator.validate_token(token)
    assert payload.client_id == "company_abc"
    assert validator.state is LicenseState.ACTIVE


def test_validate_token_expired(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    expired = _payload(mode=LicenseMode.ONLINE_SUBSCRIPTION, expires_at=_now() - timedelta(days=1))
    token = sign_license(expired, keypair.private_seed, keypair.kid)
    validator = Ed25519LicenseValidator(keyring, clock=_now)
    with pytest.raises(LicenseExpiredError):
        validator.validate_token(token)
    assert validator.restricted


def test_validate_token_perpetual_airgap(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    perpetual = _payload(mode=LicenseMode.AIRGAP_ENTERPRISE, expires_at=None)
    token = sign_license(perpetual, keypair.private_seed, keypair.kid)
    validator = Ed25519LicenseValidator(keyring, clock=_now)
    validator.validate_token(token)
    assert validator.state is LicenseState.ACTIVE


def test_validate_token_revoked_crl(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    payload = _payload()
    token = sign_license(payload, keypair.private_seed, keypair.kid)
    validator = Ed25519LicenseValidator(keyring, clock=_now)
    validator.load_crl([UUID(payload.license_id)])
    with pytest.raises(LicenseRevokedError):
        validator.validate_token(token)


def test_hardware_binding_match_and_mismatch(
    keypair: Ed25519KeyPair, keyring: LicenseKeyring
) -> None:
    signals = {"machine_id": "abc", "machine": "arm64", "system": "Linux"}
    hw = compute_hardware_id(signals)
    token = sign_license(_payload(hardware_id=hw), keypair.private_seed, keypair.kid)

    ok = Ed25519LicenseValidator(keyring, hardware_signals=signals, clock=_now)
    assert ok.validate_token(token).hardware_id == hw

    bad = Ed25519LicenseValidator(keyring, hardware_signals={"machine_id": "zzz"}, clock=_now)
    with pytest.raises(HardwareMismatchError):
        bad.validate_token(token)


def test_fingerprint_similarity_threshold() -> None:
    a = {"mac": "1", "machine_id": "x", "system": "Linux", "machine": "arm64"}
    # MAC changed (container restart) but 3/4 signals still agree -> above the 0.6 threshold.
    b = {"mac": "2", "machine_id": "x", "system": "Linux", "machine": "arm64"}
    assert fingerprint_similarity(a, b) == pytest.approx(0.75)


# --- validator: signed CRL -----------------------------------------------------
async def test_signed_crl_verified_before_apply(
    keypair: Ed25519KeyPair, keyring: LicenseKeyring
) -> None:
    signer = Ed25519TokenSigner(keypair.private_seed, keypair.kid)
    server = InMemoryLicenseServer(signer, clock=_now)
    req = LicenseIssueRequest(
        customer_id=uuid4(),
        tenant_id=uuid4(),
        mode=LicenseMode.AIRGAP_ENTERPRISE,
        hardware_id="hw-1",
    )
    token = await server.issue_license(req)
    lid = UUID(verify_license(token, keyring.as_mapping()).license_id)
    await server.revoke_license(LicenseRevokeRequest(license_id=lid, reason="fraud"))

    validator = Ed25519LicenseValidator(keyring, clock=_now)
    crl = server.signed_crl()
    validator.load_signed_crl(crl)  # verifies before applying
    assert lid in validator._revoked  # noqa: SLF001

    # A tampered CRL is refused and the revocation set is left untouched.
    forged = server.signed_crl()
    forged.revoked_ids = [str(uuid4())]
    with pytest.raises(InvalidLicenseError):
        validator.load_signed_crl(forged)


# --- validator: heartbeat grace / restricted -----------------------------------
async def test_heartbeat_airgap_always_true(keyring: LicenseKeyring) -> None:
    payload = _payload(mode=LicenseMode.AIRGAP_ENTERPRISE)
    validator = Ed25519LicenseValidator(keyring, clock=_now)
    assert await validator.verify_heartbeat(payload) is True


async def test_heartbeat_success_keeps_active(keyring: LicenseKeyring) -> None:
    async def live(_payload: LicensePayload) -> bool:
        return True

    validator = Ed25519LicenseValidator(keyring, heartbeat_client=live, clock=_now)
    payload = _payload(mode=LicenseMode.ONLINE_SUBSCRIPTION)
    assert await validator.verify_heartbeat(payload) is True
    assert validator.state is LicenseState.ACTIVE


async def test_heartbeat_grace_then_restricted(
    keypair: Ed25519KeyPair, keyring: LicenseKeyring
) -> None:
    now = {"t": _now()}

    async def unreachable(_payload: LicensePayload) -> bool:
        raise ConnectionError("network down")

    validator = Ed25519LicenseValidator(
        keyring, heartbeat_client=unreachable, clock=lambda: now["t"]
    )
    online = _payload(mode=LicenseMode.ONLINE_SUBSCRIPTION)
    token = sign_license(online, keypair.private_seed, keypair.kid)
    payload = validator.validate_token(token)  # arms the grace window at t0

    # Within 72h: still alive, but in GRACE.
    now["t"] = _now() + timedelta(hours=48)
    assert await validator.verify_heartbeat(payload) is True
    assert validator.state is LicenseState.GRACE

    # Past 72h: Restricted Mode.
    now["t"] = _now() + timedelta(hours=73)
    assert await validator.verify_heartbeat(payload) is False
    assert validator.restricted


async def test_heartbeat_server_reports_dead(keyring: LicenseKeyring) -> None:
    async def dead(_payload: LicensePayload) -> bool:
        return False

    validator = Ed25519LicenseValidator(keyring, heartbeat_client=dead, clock=_now)
    payload = _payload(mode=LicenseMode.ONLINE_SUBSCRIPTION)
    assert await validator.verify_heartbeat(payload) is False
    assert validator.restricted


async def test_heartbeat_interval_debounce(keyring: LicenseKeyring) -> None:
    # A live heartbeat within the 24h interval is trusted without re-pinging the master server
    # (DEFAULT_HEARTBEAT_INTERVAL, §3.2); after the interval elapses the ping happens again.
    calls: list[int] = []

    async def live(_payload: LicensePayload) -> bool:
        calls.append(1)
        return True

    now = {"t": _now()}
    validator = Ed25519LicenseValidator(keyring, heartbeat_client=live, clock=lambda: now["t"])
    payload = _payload(mode=LicenseMode.ONLINE_SUBSCRIPTION)

    assert await validator.verify_heartbeat(payload) is True
    assert len(calls) == 1  # the first ping

    now["t"] = _now() + timedelta(hours=1)
    assert await validator.verify_heartbeat(payload) is True
    assert len(calls) == 1  # still within the interval -> skipped

    now["t"] = _now() + timedelta(hours=25)
    assert await validator.verify_heartbeat(payload) is True
    assert len(calls) == 2  # interval elapsed -> pinged again


# --- validator: quotas ---------------------------------------------------------
async def test_enforce_quota_raises(keypair: Ed25519KeyPair, keyring: LicenseKeyring) -> None:
    usage = {"documents": 150, "users": 5}
    validator = Ed25519LicenseValidator(
        keyring, usage_provider=lambda _tid, m: usage[m], clock=_now
    )
    token = sign_license(_payload(), keypair.private_seed, keypair.kid)
    validator.validate_token(token)
    tid = uuid4()
    await validator.enforce_quota(tid, "users")  # 5 <= 10, OK
    with pytest.raises(QuotaExceededError) as exc:
        await validator.enforce_quota(tid, "documents")  # 150 > 100
    assert exc.value.metric == "documents"
    # 'requests' has no license cap.
    await validator.enforce_quota(tid, "requests")


# --- server --------------------------------------------------------------------
@pytest.fixture
def server(keypair: Ed25519KeyPair) -> InMemoryLicenseServer:
    signer = Ed25519TokenSigner(keypair.private_seed, keypair.kid)
    return InMemoryLicenseServer(signer, clock=_now)


def _airgap_req(**overrides: object) -> LicenseIssueRequest:
    base: dict[str, object] = {
        "customer_id": uuid4(),
        "tenant_id": uuid4(),
        "plan": LicensePlan.ENTERPRISE_BOX,
        "mode": LicenseMode.AIRGAP_ENTERPRISE,
        "hardware_id": "hw-1",
    }
    base.update(overrides)
    return LicenseIssueRequest(**base)


def _online_req(**overrides: object) -> LicenseIssueRequest:
    base: dict[str, object] = {
        "customer_id": uuid4(),
        "tenant_id": uuid4(),
        "mode": LicenseMode.ONLINE_SUBSCRIPTION,
        "expires_at": _now() + timedelta(days=365),
    }
    base.update(overrides)
    return LicenseIssueRequest(**base)


async def test_server_issue_is_verifiable(
    server: InMemoryLicenseServer, keyring: LicenseKeyring
) -> None:
    req = _airgap_req(
        max_documents=500,
        features=[LicenseFeature.HYBRID_SEARCH, LicenseFeature.S3_CONNECTOR],
    )
    token = await server.issue_license(req)
    payload = verify_license(token, keyring.as_mapping())
    assert payload.max_documents == 500
    assert payload.client_id == str(req.customer_id)
    assert LicenseFeature.HYBRID_SEARCH in payload.features


async def test_server_issue_profile_validation(server: InMemoryLicenseServer) -> None:
    # Airgap without hardware binding is rejected.
    with pytest.raises(LicenseError):
        await server.issue_license(_airgap_req(hardware_id=None))
    # Online that carries a hardware_id is rejected.
    with pytest.raises(LicenseError):
        await server.issue_license(_online_req(hardware_id="hw-x"))
    # Online without an expiry is rejected (perpetual is airgap-only).
    with pytest.raises(LicenseError):
        await server.issue_license(_online_req(expires_at=None))


async def test_server_revoke_publishes_crl(
    server: InMemoryLicenseServer, keyring: LicenseKeyring
) -> None:
    token = await server.issue_license(_online_req())
    issued_id = UUID(verify_license(token, keyring.as_mapping()).license_id)
    assert server.is_revoked(issued_id) is False
    await server.revoke_license(LicenseRevokeRequest(license_id=issued_id, reason="fraud"))
    assert server.is_revoked(issued_id) is True
    crl = server.crl()
    assert crl[0].license_id == issued_id and crl[0].reason == "fraud"


async def test_server_heartbeat_unknown_rejected(server: InMemoryLicenseServer) -> None:
    # A heartbeat for a never-issued license id is fail-closed.
    with pytest.raises(InvalidLicenseError):
        await server.register_heartbeat("hw-1", uuid4())


async def test_server_heartbeat_revoked_rejected(
    server: InMemoryLicenseServer, keyring: LicenseKeyring
) -> None:
    token = await server.issue_license(_online_req())
    issued_id = UUID(verify_license(token, keyring.as_mapping()).license_id)
    await server.revoke_license(LicenseRevokeRequest(license_id=issued_id, reason="x"))
    with pytest.raises(LicenseRevokedError):
        await server.register_heartbeat("hw-1", issued_id)


async def test_server_heartbeat_hardware_mismatch(
    server: InMemoryLicenseServer, keyring: LicenseKeyring
) -> None:
    token = await server.issue_license(_airgap_req(hardware_id="hw-bound"))
    issued_id = UUID(verify_license(token, keyring.as_mapping()).license_id)
    with pytest.raises(HardwareMismatchError):
        await server.register_heartbeat("hw-different", issued_id)


async def test_server_activation_code_redeem(
    server: InMemoryLicenseServer, keyring: LicenseKeyring
) -> None:
    code = await server.generate_activation_code(uuid4(), LicensePlan.PRO)
    assert code.code.startswith("RAG-ENT-")
    assert len(code.code.split("-")) == 5
    tenant = uuid4()
    token = await server.redeem_activation_code(
        code.code, tenant, expires_at=_now() + timedelta(days=30)
    )
    payload = verify_license(token, keyring.as_mapping())
    assert payload.plan is LicensePlan.PRO
    # The code cannot be redeemed twice.
    with pytest.raises(LicenseError):
        await server.redeem_activation_code(code.code, tenant, expires_at=_now())


async def test_server_reactivation_audited_and_limited(
    server: InMemoryLicenseServer, keyring: LicenseKeyring
) -> None:
    token = await server.issue_license(_airgap_req(hardware_id="hw-old"))
    lid = UUID(verify_license(token, keyring.as_mapping()).license_id)

    new_token = await server.reactivate_license(lid, "hw-new", reason="disk swap")
    rebound = verify_license(new_token, keyring.as_mapping())
    assert rebound.hardware_id == "hw-new"
    records = server.reactivations(lid)
    assert records[0].old_hardware_id == "hw-old" and records[0].new_hardware_id == "hw-new"

    # Exhaust the re-activation limit (default 3 total).
    await server.reactivate_license(lid, "hw-3", reason="x")
    await server.reactivate_license(lid, "hw-4", reason="x")
    with pytest.raises(LicenseError):
        await server.reactivate_license(lid, "hw-5", reason="x")


async def test_server_heartbeat_status_grace(
    keypair: Ed25519KeyPair, keyring: LicenseKeyring
) -> None:
    now = {"t": _now()}
    signer = Ed25519TokenSigner(keypair.private_seed, keypair.kid)
    srv = InMemoryLicenseServer(signer, clock=lambda: now["t"])
    token = await srv.issue_license(_online_req(expires_at=_now() + timedelta(days=365)))
    lid = UUID(verify_license(token, keyring.as_mapping()).license_id)
    hb = HeartbeatPayload(license_id=lid, hardware_id="hw", version="1.0", requests_last_period=42)
    status = await srv.ingest_heartbeat(hb)
    assert status.restricted is False and status.status is HeartbeatState.ACTIVE
    # 73h later without a fresh heartbeat -> restricted.
    now["t"] = _now() + timedelta(hours=73)
    assert srv.heartbeat_status(lid).restricted is True


async def test_heartbeat_payload_is_anonymized() -> None:
    # The heartbeat contract carries only aggregates - no query text / document / PII fields.
    hb = HeartbeatPayload(license_id=uuid4(), hardware_id="hw", version="1.0")
    fields = set(hb.model_dump().keys())
    assert fields == {
        "license_id",
        "hardware_id",
        "version",
        "requests_last_period",
        "uptime_seconds",
        "p95_latency_ms",
        "telemetry_opt_out",
    }
