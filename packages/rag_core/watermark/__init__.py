"""Hidden leak-attribution marks (`core/licensing_and_distribution.md` §4a).

Hidden marks do **not** prevent copying - they provide **attribution** and legal proof after the
fact (who leaked a pirated Box copy). The package lives only in the obfuscated backend core; the
frontend never touches the marks (§4.5).

Stage M ships the **per-build stamp** layer (:mod:`build_stamp`): a signed ``client_id`` /
``build_id`` stamp baked into the ``.so`` / Docker artifact, deterministically derived from the
license fields and a *separate* vendor secret (``vendor_key`` != ``LICENSE_SIGNING_KEY``). The
remaining layers (zero-width / linguistic watermarks, canary records, heartbeat fingerprint) are
tracked by the same contract and can be added here without changing the derivation rule.
"""

from __future__ import annotations

from rag_core.watermark.build_stamp import (
    FINGERPRINT_SEPARATOR,
    STAMP_ENV_VAR,
    STAMP_FILENAME,
    BuildStamp,
    attribute_leak,
    compute_fingerprint,
    current_stamp,
    load_stamp,
    write_stamp,
)

__all__ = [
    "BuildStamp",
    "FINGERPRINT_SEPARATOR",
    "STAMP_ENV_VAR",
    "STAMP_FILENAME",
    "attribute_leak",
    "compute_fingerprint",
    "current_stamp",
    "load_stamp",
    "write_stamp",
]
