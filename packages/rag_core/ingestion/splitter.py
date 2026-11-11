"""Token-aware Parent-Child splitter (`core/architecture.md` §3, `IMPLEMENTATION_PLAN.md` Stage D).

The splitter implements the Small-to-Big strategy:

- **Parent** chunks (800-1200 tokens) are the logical sections used for LLM context hydration;
- **Child** chunks (150-250 tokens, 20-40 token overlap) are the granular units that get embedded
  and searched.

Two invariants hold simultaneously:

1. **A word is never cut.** The atomic unit is a *word* (a run of non-whitespace), so boundaries
   always fall between words - a subword/token is never split across chunks.
2. **Bounds are measured in real tokens.** Chunk sizes are decided by an injectable
   ``count_tokens`` (words -> token count), so the 800-1200 / 150-250 bounds are honored in the
   *model* tokenizer (BGE-M3 SentencePiece / the LLM tokenizer), not in whitespace "words". The
   default counter is 1-token-per-word (whitespace); :func:`build_hf_token_counter` wraps a real
   HuggingFace tokenizer so words keep intact while their subword length drives the bounds.
   Supplying a real counter is what makes the splitter genuinely "token-aware" rather than
   whitespace-aware.

The minimum bounds are enforced, not merely validated: the trailing parent/child is rebalanced
(pulling words from the neighbour, or extending the child window backwards into the overlap) so a
tail chunk is not left below ``*_min_tokens`` - unless the whole document is smaller than the
minimum (then a single under-min chunk is unavoidable) or a single word already exceeds the maximum
(then "never cut a word" wins over the upper bound).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag_core.contracts.settings import Settings

_DEFAULT_TOKEN_RE = re.compile(r"\S+")

# A word tokenizer splits raw text into atomic words (never cut). A token counter maps one word to
# its number of model tokens (subwords). Both are injectable so a real tokenizer can drive the
# bounds without changing the splitting algorithm.
WordTokenizer = Callable[[str], list[str]]
TokenCounter = Callable[[str], int]


def _whitespace_tokenizer(text: str) -> list[str]:
    return _DEFAULT_TOKEN_RE.findall(text)


def _one_token_per_word(_word: str) -> int:
    return 1


def build_hf_token_counter(tokenizer: object) -> TokenCounter:
    """Wraps a HuggingFace tokenizer as a per-word :data:`TokenCounter` (subword length).

    ``tokenizer`` must expose ``encode(text, add_special_tokens=False) -> list[int]`` (the
    transformers / tokenizers API). This is the seam that makes the 150-250 / 800-1200 bounds count
    real BGE-M3 subwords instead of whitespace words.
    """

    def _count(word: str) -> int:
        ids = tokenizer.encode(word, add_special_tokens=False)  # type: ignore[attr-defined]
        return max(1, len(ids))

    return _count


@dataclass(frozen=True)
class SplitterConfig:
    """Chunk-size bounds in tokens (canonical values from `core/architecture.md` §3).

    ``child_overlap_tokens`` is the number of trailing tokens of a child that are repeated at the
    start of the next child (the Small-to-Big sliding window). It must be strictly smaller than
    ``child_max_tokens`` so the window always advances.
    """

    parent_min_tokens: int = 800
    parent_max_tokens: int = 1200
    child_min_tokens: int = 150
    child_max_tokens: int = 250
    child_overlap_tokens: int = 30

    def __post_init__(self) -> None:
        if not 0 < self.child_min_tokens <= self.child_max_tokens:
            raise ValueError("child_min_tokens must be in (0, child_max_tokens]")
        if not self.parent_min_tokens <= self.parent_max_tokens:
            raise ValueError("parent_min_tokens must be <= parent_max_tokens")
        if not 0 <= self.child_overlap_tokens < self.child_max_tokens:
            raise ValueError("child_overlap_tokens must be in [0, child_max_tokens)")
        if self.child_max_tokens > self.parent_max_tokens:
            raise ValueError("child_max_tokens must be <= parent_max_tokens")


@dataclass
class ChildDraft:
    """A child chunk before it is associated with DB identifiers."""

    content: str
    token_count: int
    chunk_index: int


@dataclass
class ParentDraft:
    """A parent chunk with its ordered child chunks (before DB identifiers are assigned)."""

    content: str
    token_count: int
    chunk_index: int
    children: list[ChildDraft] = field(default_factory=list)


class ParentChildSplitter:
    """Splits raw text into Parent chunks and, inside each parent, overlapping Child chunks."""

    def __init__(
        self,
        config: SplitterConfig | None = None,
        token_pattern: WordTokenizer = _whitespace_tokenizer,
        count_tokens: TokenCounter = _one_token_per_word,
    ) -> None:
        self._config = config or SplitterConfig()
        self._tokenizer = token_pattern
        self._count_tokens = count_tokens

    @property
    def config(self) -> SplitterConfig:
        return self._config

    def tokenize(self, text: str) -> list[str]:
        """Returns the ordered list of words (never splits inside a word)."""
        return self._tokenizer(text)

    def split(self, text: str) -> list[ParentDraft]:
        """Splits ``text`` into parents (and their children). Empty/blank text yields ``[]``."""
        words = self.tokenize(text)
        if not words:
            return []
        word_tokens = [self._count_tokens(w) for w in words]

        cfg = self._config
        parent_windows = self._partition(word_tokens, cfg.parent_max_tokens, cfg.parent_min_tokens)
        parents: list[ParentDraft] = []
        for p_index, (p_start, p_end) in enumerate(parent_windows):
            p_words = words[p_start:p_end]
            p_tokens = word_tokens[p_start:p_end]
            parents.append(
                ParentDraft(
                    content=" ".join(p_words),
                    token_count=sum(p_tokens),
                    chunk_index=p_index,
                    children=self._split_children(p_words, p_tokens),
                )
            )
        return parents

    @staticmethod
    def _window_sum(tokens: list[int], start: int, end: int) -> int:
        return sum(tokens[start:end])

    def _partition(
        self, word_tokens: list[int], max_tokens: int, min_tokens: int
    ) -> list[tuple[int, int]]:
        """Partitions words into contiguous, non-overlapping windows <= ``max_tokens``.

        The trailing window is rebalanced (boundary moved left) so it reaches ``min_tokens`` when
        possible without pushing it over ``max_tokens`` or dropping the previous window below
        ``min_tokens``. A single word larger than ``max_tokens`` becomes its own (oversized) window
        - never cutting a word takes priority over the upper bound.
        """
        windows: list[tuple[int, int]] = []
        n = len(word_tokens)
        start = 0
        while start < n:
            total = 0
            end = start
            while end < n:
                t = word_tokens[end]
                if end > start and total + t > max_tokens:
                    break
                total += t
                end += 1
            windows.append((start, end))
            start = end

        self._rebalance_tail(windows, word_tokens, max_tokens, min_tokens)
        return windows

    def _rebalance_tail(
        self,
        windows: list[tuple[int, int]],
        word_tokens: list[int],
        max_tokens: int,
        min_tokens: int,
    ) -> None:
        if len(windows) < 2:
            return
        prev_start, prev_end = windows[-2]
        last_start, last_end = windows[-1]
        while self._window_sum(word_tokens, last_start, last_end) < min_tokens:
            if last_start <= prev_start:
                break
            candidate = last_start - 1
            if self._window_sum(word_tokens, candidate, last_end) > max_tokens:
                break
            if self._window_sum(word_tokens, prev_start, candidate) < min_tokens:
                break
            last_start = candidate
        windows[-2] = (prev_start, last_start)
        windows[-1] = (last_start, last_end)

    def _split_children(
        self, parent_words: list[str], parent_tokens: list[int]
    ) -> list[ChildDraft]:
        """Builds overlapping child windows over a parent's words (token-aware, no word cuts)."""
        cfg = self._config
        n = len(parent_words)
        if n == 0:
            return []
        step_tokens = cfg.child_max_tokens - cfg.child_overlap_tokens  # > 0 (validated)
        windows: list[tuple[int, int]] = []
        start = 0
        while start < n:
            total = 0
            end = start
            while end < n:
                t = parent_tokens[end]
                if end > start and total + t > cfg.child_max_tokens:
                    break
                total += t
                end += 1
            windows.append((start, end))
            if end >= n:
                break
            # Advance by ~step_tokens worth of words (and always at least one word).
            acc = 0
            s = start
            while s < end and acc < step_tokens:
                acc += parent_tokens[s]
                s += 1
            start = max(s, start + 1)

        self._extend_last_child(windows, parent_tokens, cfg.child_max_tokens, cfg.child_min_tokens)

        return [
            ChildDraft(
                content=" ".join(parent_words[c_start:c_end]),
                token_count=self._window_sum(parent_tokens, c_start, c_end),
                chunk_index=c_index,
            )
            for c_index, (c_start, c_end) in enumerate(windows)
        ]

    def _extend_last_child(
        self,
        windows: list[tuple[int, int]],
        parent_tokens: list[int],
        max_tokens: int,
        min_tokens: int,
    ) -> None:
        """Extends the last child window backwards (more overlap) to reach ``min_tokens``."""
        if len(windows) < 2:
            return
        last_start, last_end = windows[-1]
        while self._window_sum(parent_tokens, last_start, last_end) < min_tokens:
            if last_start <= 0:
                break
            if self._window_sum(parent_tokens, last_start - 1, last_end) > max_tokens:
                break
            last_start -= 1
        windows[-1] = (last_start, last_end)


def build_splitter(
    settings: Settings,
    *,
    count_tokens: TokenCounter = _one_token_per_word,
    token_pattern: WordTokenizer = _whitespace_tokenizer,
) -> ParentChildSplitter:
    """Builds a :class:`ParentChildSplitter` from ``Settings`` (single source of the bounds).

    The chunk bounds come from ``Settings`` (``CHUNK_*``), so the canonical 800-1200 / 150-250
    values are not hardcoded in the splitter. Pass a real ``count_tokens`` (e.g.
    :func:`build_hf_token_counter`) to make the bounds count model subwords.
    """
    config = SplitterConfig(
        parent_min_tokens=settings.chunk_parent_min_tokens,
        parent_max_tokens=settings.chunk_parent_max_tokens,
        child_min_tokens=settings.chunk_child_min_tokens,
        child_max_tokens=settings.chunk_child_max_tokens,
        child_overlap_tokens=settings.chunk_child_overlap_tokens,
    )
    return ParentChildSplitter(config, token_pattern=token_pattern, count_tokens=count_tokens)
