"""License-token codec - a thin wrapper over the CANONICAL codec in `admin.license_crypto`.

Stage K originally shipped a *second*, incompatible token codec (compact JWS over PEM keys using
the ``cryptography`` backend). That duplicated the already-wired Stage I codec in
``rag_core.admin.license_crypto`` and could not round-trip with the admin panel
(`deepseek-criticism.md` Step K §🔴1-🔴2). This module now has NO independent crypto: it delegates
to the single source of truth - the pure-Python RFC 8032 Ed25519 implementation that produces the
canonical ``PASETO v4.public`` token (and the compatible ``JWT (EdDSA)`` option) described in
``core/licensing_and_distribution.md`` §3.

Keys are therefore raw Ed25519 bytes (``private_seed`` 32 bytes, ``public_key`` 32 bytes), exactly
as ``admin.license_crypto`` and ``Settings.license_public_key`` use them. The private seed NEVER
lives in the box artifact: only public keys are embedded for offline verification (`§4.3`). Signing
happens behind the :class:`TokenSigner` port (the vendor signing service / HSM / KMS), while the
client holds a read-only :class:`LicenseKeyring` of public keys keyed by ``kid`` for rotation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from rag_core.contracts.models import LicensePayload
from rag_core.licensing.errors import InvalidLicenseError

# NOTE: ``rag_core.admin.license_crypto`` is imported LAZILY inside the functions below. That module
# imports ``rag_core.licensing.errors`` (to unify the error family), which would otherwise create a
# circular import through this package's ``__init__``; deferring the import breaks the cycle.


@dataclass(frozen=True)
class Ed25519KeyPair:
    """A generated signing key pair bound to a key id (``kid``) for rotation.

    ``private_seed`` (32 raw bytes) belongs ONLY in the vendor signing service; ``public_key``
    (32 raw bytes) is the part embedded into the client core for offline verification.
    """

    kid: str
    private_seed: bytes
    public_key: bytes


def generate_keypair(kid: str) -> Ed25519KeyPair:
    """Generates a fresh Ed25519 key pair bound to a key id (``kid``)."""
    from rag_core.admin.license_crypto import ed25519_generate_keypair

    private_seed, public_key = ed25519_generate_keypair()
    return Ed25519KeyPair(kid=kid, private_seed=private_seed, public_key=public_key)


def sign_license(
    payload: LicensePayload, private_seed: bytes, kid: str, format_type: str = "paseto"
) -> str:
    """Signs a :class:`LicensePayload` into the canonical ``PASETO v4.public`` token (default).

    ``kid`` is accepted for API symmetry with the keyring/rotation model; the canonical PASETO/JWT
    wire format carries no key id, so the keyring resolves the right public key by trying each
    registered key on verification.
    """
    from rag_core.admin.license_crypto import create_signed_license_token

    return create_signed_license_token(payload, private_seed, format_type=format_type)


def verify_license(token: str, public_keys: Mapping[str, bytes]) -> LicensePayload:
    """Verifies the Ed25519 signature OFFLINE and returns the decoded :class:`LicensePayload`.

    * ``public_keys`` maps ``kid -> raw public key bytes`` (supporting rotation / several active
      keys): each key is tried until one verifies.
    * This is a pure codec - only the signature and payload structure are checked here. Expiry, CRL
      and hardware binding are enforced by :class:`Ed25519LicenseValidator`, so ``allow_expired`` is
      set and no ``expected_hardware_id`` is passed.

    Raises :class:`InvalidLicenseError` when no registered key verifies the token.
    """
    from rag_core.admin.license_crypto import verify_license_token

    if not public_keys:
        raise InvalidLicenseError("No public keys registered for verification")
    last_error: Exception | None = None
    for public_key in public_keys.values():
        try:
            return verify_license_token(
                token=token,
                public_key_bytes=public_key,
                expected_hardware_id=None,
                allow_expired=True,
            )
        except InvalidLicenseError as exc:  # try the next key (rotation)
            last_error = exc
            continue
    raise InvalidLicenseError(f"Token did not verify against any registered key: {last_error}")


@runtime_checkable
class TokenSigner(Protocol):
    """The signing port (the vendor signing service / HSM / KMS).

    The license server depends on this abstraction and NEVER touches a raw private key, honouring
    ``core/license_server.md`` §2 p.1 (the key lives in a separate signing process).
    """

    @property
    def kid(self) -> str:
        """The active key id (used by the keyring for rotation bookkeeping)."""

    def sign(self, payload: LicensePayload) -> str:
        """Returns a canonical signed license token for the payload."""

    def sign_bytes(self, data: bytes) -> bytes:
        """Signs arbitrary bytes with the same offline key (used for the signed CRL, `§6`)."""


class Ed25519TokenSigner:
    """A local reference :class:`TokenSigner` holding the Ed25519 private seed in-process.

    It models the signing service / HSM: production deployments replace it with a remote client to
    ``license_signing_service_url``. The private seed is accepted raw and never leaves here.
    """

    def __init__(self, private_seed: bytes, kid: str, format_type: str = "paseto") -> None:
        self._private_seed = private_seed
        self._kid = kid
        self._format_type = format_type

    @property
    def kid(self) -> str:
        return self._kid

    def sign(self, payload: LicensePayload) -> str:
        return sign_license(payload, self._private_seed, self._kid, self._format_type)

    def sign_bytes(self, data: bytes) -> bytes:
        from rag_core.admin.license_crypto import ed25519_sign

        return ed25519_sign(data, self._private_seed)


class LicenseKeyring:
    """A read-only registry of public keys (``kid -> raw bytes``) for offline verification.

    Supports seamless rotation: adding a new ``kid`` does not revoke tokens signed under the old
    one (`core/license_server.md` §2 p.6). The client core embeds one of these.
    """

    def __init__(self, keys: Mapping[str, bytes] | None = None) -> None:
        self._keys: dict[str, bytes] = dict(keys or {})

    def add(self, kid: str, public_key: bytes) -> None:
        """Registers (or replaces) a public key under a key id."""
        self._keys[kid] = public_key

    def as_mapping(self) -> Mapping[str, bytes]:
        """The ``kid -> raw bytes`` mapping passed to :func:`verify_license`."""
        return dict(self._keys)

    def verify(self, token: str) -> LicensePayload:
        """Convenience wrapper over :func:`verify_license` with the keyring's keys."""
        return verify_license(token, self._keys)
