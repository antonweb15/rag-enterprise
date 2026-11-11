"""File Watcher (`core/architecture.md` §3-4, `IMPLEMENTATION_PLAN.md` Stage D).

The watcher scans a per-tenant local folder and emits one ingest job per new/modified, *stable*
file. It guards against two classic failure modes:

- **Partial writes** - a file still being copied is skipped until its ``st_size`` has been stable
  for at least ``debounce_seconds`` of wall-clock time (``core/architecture.md`` §4: "Debounce
  2.5s, checking ``st_size`` stability before reading"). Both conditions are enforced: the size
  must be unchanged **and** the debounce window must have elapsed since the size last changed.
- **Re-ingesting unchanged files** - the SHA-256 content hash is compared against the set of
  already-indexed hashes (the ``documents`` table), so a file is enqueued only once per content.

The watcher is transport-agnostic: it returns :class:`WatchedFile` descriptors; the caller enqueues
``process_document_task`` for each. The clock and the file reader are injectable for testing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from rag_core.ingestion.dedup import compute_content_hash


@dataclass(frozen=True)
class WatchedFile:
    """A stable, not-yet-indexed file ready to be enqueued for ingestion."""

    path: Path
    key: str  # the storage key relative to the tenant root
    content_hash: str
    size: int


class FileWatcher:
    """Scans a tenant folder for new/modified, stable, not-yet-indexed files.

    ``root`` is the watch root; per-tenant files live under ``{root}/{tenant_id}/``. A file is
    emitted only once its ``st_size`` has stayed constant for at least ``debounce_seconds`` (as
    measured by the injectable ``clock``), so a file still being written is never read mid-copy.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        debounce_seconds: float = 2.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = Path(root)
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        # path -> (size, timestamp when this size was first observed).
        self._observed: dict[Path, tuple[int, float]] = {}

    @property
    def debounce_seconds(self) -> float:
        return self._debounce_seconds

    def _tenant_dir(self, tenant_id: UUID) -> Path:
        return self._root / str(tenant_id)

    def scan(
        self,
        tenant_id: UUID,
        known_hashes: set[str] | None = None,
        *,
        reader: Callable[[Path], bytes] = lambda p: p.read_bytes(),
    ) -> list[WatchedFile]:
        """Returns stable, not-yet-indexed files for the tenant.

        A file is returned only when its current ``st_size`` has been unchanged for at least
        ``debounce_seconds`` AND its content hash is not in ``known_hashes``. A file whose size
        just changed (or that is seen for the first time) resets its debounce timer.
        """
        known = known_hashes or set()
        tenant_dir = self._tenant_dir(tenant_id)
        now = self._clock()
        result: list[WatchedFile] = []
        current: dict[Path, tuple[int, float]] = {}

        if not tenant_dir.is_dir():
            self._observed = current
            return result

        for path in sorted(tenant_dir.rglob("*")):
            if not path.is_file():
                continue
            size = path.stat().st_size
            previous = self._observed.get(path)
            # Keep the original first-seen timestamp while the size is unchanged; otherwise reset.
            first_seen = previous[1] if previous is not None and previous[0] == size else now
            current[path] = (size, first_seen)
            if now - first_seen < self._debounce_seconds:
                continue  # not stable long enough => defer (partial-write guard)
            content_hash = compute_content_hash(reader(path))
            if content_hash in known:
                continue
            result.append(
                WatchedFile(
                    path=path,
                    key=path.relative_to(tenant_dir).as_posix(),
                    content_hash=content_hash,
                    size=size,
                )
            )

        self._observed = current
        return result
