"""CI-blocking enforcement of the English-only language policy (see CONTRIBUTING.md).

Scans all git-tracked files and fails if any Cyrillic character is found. Tracked-only
scanning means intentionally git-ignored artifacts (e.g. internal review drafts) are excluded.
The test is skipped gracefully if git is unavailable (it is always present in CI).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")

# Explicitly whitelisted Russian-language documents (kept in Russian by owner decision,
# see CONTRIBUTING.md "Language policy"). These are planning/explanatory notes that are
# intentionally exempt from the English-only rule.
_EXEMPT: frozenset[str] = frozenset(
    {
        "IMPLEMENTATION_PLAN.md",
        "core/EXPLANATORY_NOTE.md",
    }
)


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git unavailable - cannot enumerate tracked files")
    return [line for line in out.stdout.splitlines() if line]


def test_no_cyrillic_in_tracked_files() -> None:
    """No tracked file may contain Cyrillic characters (English-only policy)."""
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel in _EXEMPT:
            continue  # whitelisted Russian-language document (see CONTRIBUTING.md)
        path = PROJECT_ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable file - not subject to the text policy
        if _CYRILLIC.search(text):
            offenders.append(rel)
    assert not offenders, "Cyrillic text found in tracked files (English-only policy): " + (
        ", ".join(sorted(offenders))
    )
