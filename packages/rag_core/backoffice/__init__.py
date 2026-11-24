"""Vendor Backoffice (super-admin) contour (`core/vendor_backoffice.md`, Stage L).

A SEPARATE vendor contour (`/api/v1/backoffice/*`) managing customers, licenses, activation
codes, the box registry and cross-tenant break-glass support. It is NOT the tenant admin panel
and is NEVER part of the ``on_premise_box`` artifact.
"""

from __future__ import annotations

from rag_core.backoffice.auth import (
    BackofficeCapability,
    check_backoffice_capability,
    create_backoffice_session_jwt,
    enforce_backoffice_capability,
    verify_backoffice_session_jwt,
)
from rag_core.backoffice.service import BackofficeService

__all__ = [
    "BackofficeCapability",
    "BackofficeService",
    "check_backoffice_capability",
    "create_backoffice_session_jwt",
    "enforce_backoffice_capability",
    "verify_backoffice_session_jwt",
]
