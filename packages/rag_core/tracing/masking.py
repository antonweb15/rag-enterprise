"""PII Masking and Trace Sanitization (`core/eval_and_ops.md` §2.3, Stage H).

Enforces:
1. Masking of sensitive credentials, API keys, and passwords matching configured fields;
2. Pattern-based masking of emails, phone numbers, and bearer/API tokens in strings;
3. Elimination of raw document/chunk text from trace spans and metadata (only chunk IDs and scores).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_MASK_FIELDS: set[str] = {
    "password",
    "api_key",
    "apikey",
    "secret",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "jwt",
    "authorization",
    "auth",
    "private_key",
    "license_key",
}

# Regex patterns for inline PII detection in free text
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_PATTERN = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{2,4}[-.\s]?\d{2,4}[-.\s]?\d{2,4}(?:[-.\s]?\d{2,4})?\b"
)
BEARER_TOKEN_PATTERN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.]{16,}\b")
API_KEY_PREFIX_PATTERN = re.compile(r"\b(?:sk|gsk|pk|lf_pk|lf_sk)_[A-Za-z0-9_\-]{16,}\b")
SECRET_INLINE_PATTERN = re.compile(
    r"(?i)\b(password|secret|api_key|token|private_key)\s*(?:is\s*|[:=]\s*)(\S+)"
)


# Keys associated with raw chunk / document text that must not be sent to traces
RAW_TEXT_KEYS: set[str] = {
    "chunk_content",
    "raw_text",
    "raw_chunk_text",
    "document_content",
    "page_content",
    "parent_content",
    "child_content",
}


def mask_pii_string(value: str) -> str:
    """Replaces inline PII patterns (emails, phones, tokens, inline secrets) with masks."""
    if not value or not isinstance(value, str):
        return value

    res = EMAIL_PATTERN.sub("[EMAIL_MASKED]", value)
    res = PHONE_PATTERN.sub("[PHONE_MASKED]", res)
    res = BEARER_TOKEN_PATTERN.sub(r"\1[MASKED]", res)
    res = API_KEY_PREFIX_PATTERN.sub("[KEY_MASKED]", res)
    res = SECRET_INLINE_PATTERN.sub(r"\1 is [MASKED]", res)
    return res


def mask_pii(
    data: Any,
    mask_fields: Sequence[str] | set[str] | None = None,
    *,
    strip_raw_chunk_text: bool = True,
    _seen: set[int] | None = None,
) -> Any:
    """Recursively masks sensitive dictionary keys, string PII patterns, and raw chunk texts."""
    if _seen is None:
        _seen = set()

    data_id = id(data)
    if data_id in _seen:
        return "[CIRCULAR_REFERENCE]"

    effective_mask_fields: set[str] = (
        {f.lower() for f in mask_fields} | DEFAULT_MASK_FIELDS
        if mask_fields is not None
        else DEFAULT_MASK_FIELDS
    )

    if isinstance(data, str):
        return mask_pii_string(data)

    if isinstance(data, Mapping):
        _seen.add(data_id)
        sanitized_dict: dict[str, Any] = {}
        for key, val in data.items():
            str_key = str(key)
            lower_key = str_key.lower()

            # Check if key is a raw chunk/document text key that must not be logged
            if strip_raw_chunk_text and (
                lower_key in RAW_TEXT_KEYS or lower_key.endswith("_raw_text")
            ):
                sanitized_dict[str_key] = "[RAW_TEXT_OMITTED]"
                continue

            # Check if key is sensitive / credentials
            if (
                any(sensitive in lower_key for sensitive in effective_mask_fields)
                or lower_key in effective_mask_fields
            ):
                sanitized_dict[str_key] = "[MASKED]"
                continue

            sanitized_dict[str_key] = mask_pii(
                val,
                effective_mask_fields,
                strip_raw_chunk_text=strip_raw_chunk_text,
                _seen=_seen,
            )
        _seen.remove(data_id)
        return sanitized_dict

    if isinstance(data, Sequence) and not isinstance(data, (bytes, bytearray)):
        _seen.add(data_id)
        sanitized_list = [
            mask_pii(
                item,
                effective_mask_fields,
                strip_raw_chunk_text=strip_raw_chunk_text,
                _seen=_seen,
            )
            for item in data
        ]
        _seen.remove(data_id)
        return sanitized_list

    return data
