"""Composite hardware fingerprint (`core/license_server.md` §5, `deepseek-criticism.md` §11-13).

A naive ``hardware_id = sha256(cpu_id + mac)`` is UNRELIABLE inside Docker / K8s / VMs: a container
restart or a rescheduled pod changes the MAC, which would wrongly invalidate an air-gapped license.
Stage K therefore uses a *composite* fingerprint built from several independent signals and matches
it with a *threshold* (a fraction of signals must agree) instead of demanding a byte-exact match.

The canonical ``hardware_id`` embedded into a token (`core/licensing_and_distribution.md` §3.1) is
the deterministic SHA-256 over the sorted signal set, and it is what the validator compares
byte-for-byte to bind an air-gapped license. The threshold helpers :func:`fingerprint_similarity` /
:func:`hardware_id_matches` instead compare *two raw signal dumps* (e.g. the issuance-time vs
re-activation-time fingerprints) so the vendor backoffice can distinguish a minor drift (a changed
container MAC) from a different machine when approving a re-activation (`/licenses/reactivate`,
`§5`).

No third-party dependency is required - only the standard library.
"""

from __future__ import annotations

import hashlib
import os
import platform
import socket
import uuid
from dataclasses import dataclass, field

# A signal that agrees on at least this fraction of the fingerprint is considered the same host.
DEFAULT_MATCH_THRESHOLD = 0.6


def collect_signals() -> dict[str, str]:
    """Collects the raw composite fingerprint signals of the current host.

    Each signal is independent, so losing one (e.g. a changed MAC in a container) does not flip the
    whole fingerprint. Missing signals are simply omitted rather than faked.
    """
    signals: dict[str, str] = {}

    # The MAC address (least stable in Docker/VM - just ONE of several signals here).
    mac = uuid.getnode()
    # getnode() sets the multicast bit when it had to invent a random address; treat as unknown.
    if not (mac >> 40) & 0x1:
        signals["mac"] = f"{mac:012x}"

    signals["machine"] = platform.machine()  # cpu arch (x86_64 / arm64)
    signals["system"] = platform.system()  # OS family
    signals["processor"] = platform.processor() or platform.machine()
    signals["hostname"] = socket.gethostname()
    signals["cpu_count"] = str(os.cpu_count() or 0)

    # A stable per-OS machine id when available (systemd / dbus). Highly stable inside a VM image.
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, encoding="ascii") as fh:
                value = fh.read().strip()
            if value:
                signals["machine_id"] = value
                break
        except OSError:
            continue

    return {k: v for k, v in signals.items() if v}


def _hash_signals(signals: dict[str, str]) -> str:
    """Deterministic SHA-256 over the sorted ``key=value`` signal pairs."""
    canonical = "\n".join(f"{k}={signals[k]}" for k in sorted(signals))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HardwareFingerprint:
    """A captured composite fingerprint: the raw signals plus their canonical hash."""

    signals: dict[str, str] = field(default_factory=dict)

    @property
    def hardware_id(self) -> str:
        """The canonical ``hardware_id`` (SHA-256 over the sorted signal set)."""
        return _hash_signals(self.signals)

    def matches(
        self, other: HardwareFingerprint, threshold: float = DEFAULT_MATCH_THRESHOLD
    ) -> bool:
        """True iff enough individual signals agree (composite, threshold-based)."""
        return fingerprint_similarity(self.signals, other.signals) >= threshold


def current_fingerprint() -> HardwareFingerprint:
    """Captures the current host's composite fingerprint."""
    return HardwareFingerprint(signals=collect_signals())


def compute_hardware_id(signals: dict[str, str] | None = None) -> str:
    """The canonical ``hardware_id`` for a signal set (defaults to the current host)."""
    return _hash_signals(signals if signals is not None else collect_signals())


def fingerprint_similarity(a: dict[str, str], b: dict[str, str]) -> float:
    """Fraction of shared signal keys whose values agree (0.0 - 1.0).

    The denominator is the set of keys present in BOTH fingerprints, so adding a brand-new signal
    type later does not retroactively lower historical matches.
    """
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    agree = sum(1 for key in shared if a[key] == b[key])
    return agree / len(shared)


def hardware_id_matches(
    expected: dict[str, str], actual: dict[str, str], threshold: float = DEFAULT_MATCH_THRESHOLD
) -> bool:
    """Composite, threshold-based match of two raw signal dumps (re-activation review, `§5`)."""
    return fingerprint_similarity(expected, actual) >= threshold
