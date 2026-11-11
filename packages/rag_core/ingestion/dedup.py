"""Content hashing and near-duplicate suppression (`core/architecture.md` §3).

The File Watcher computes a SHA-256 checksum of every candidate file and skips files already
indexed (``documents`` table). Inside a single document the splitter can emit near-identical child
chunks (repeated headers/boilerplate); ``suppress_near_duplicates`` collapses them so the vector
store is not polluted with redundant near-duplicates.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import TypeVar

_WHITESPACE_RE = re.compile(r"\s+")
# Punctuation/symbol runs are folded to a single space so boilerplate differing only in
# punctuation (e.g. "Header - 2024" vs "Header 2024") collapses to the same near-duplicate key.
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def compute_content_hash(data: bytes | str) -> str:
    """Returns the hex SHA-256 digest used for ingest idempotency (1 file = 1 hash)."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def near_duplicate_key(text: str) -> str:
    """A normalized key for near-duplicate detection.

    Normalization (documented scope): Unicode NFKC fold, case fold, punctuation/symbol runs folded
    to a space, and all whitespace runs collapsed. So chunks that differ only in spacing, casing or
    punctuation map to the same key (the SHA-256 of that normalized form is the key). It is a
    *near*-duplicate heuristic, not a semantic one: different wording still yields different keys.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    depunct = _PUNCT_RE.sub(" ", folded)
    normalized = _WHITESPACE_RE.sub(" ", depunct).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_T = TypeVar("_T")


def suppress_near_duplicates(
    items: Iterable[_T],
    *,
    key: Callable[[_T], str] | None = None,
) -> list[_T]:
    """Returns items with near-duplicates removed, preserving first-seen order.

    ``key`` is a callable mapping an item to its text; when ``None`` the item itself is treated as
    the text (``str``). Deduplication is by :func:`near_duplicate_key`.
    """
    seen: set[str] = set()
    result: list[_T] = []
    for item in items:
        text = str(item) if key is None else key(item)
        dup_key = near_duplicate_key(text)
        if dup_key in seen:
            continue
        seen.add(dup_key)
        result.append(item)
    return result
