"""Document parsing (`core/architecture.md` §3, `IMPLEMENTATION_PLAN.md` Stage D).

Docling is the primary parser for rich formats (``.pdf/.docx/.xlsx/.pptx/.html/.epub``). Plain-text
formats (``.txt/.md``) are decoded directly - this keeps the pipeline importable and unit-testable
without the heavy Docling dependency, and avoids spinning up Docling for trivial text files.

``DocumentParser.parse`` dispatches on the file extension and returns a :class:`ParsedDocument`
(the extracted text plus structural metadata). Docling itself is imported lazily, only when a rich
format is encountered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Formats handled natively (decoded as UTF-8 text, no Docling needed).
PLAIN_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown"})

# Rich formats delegated to Docling.
DOCLING_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm", ".epub"})

SUPPORTED_EXTENSIONS = PLAIN_TEXT_EXTENSIONS | DOCLING_EXTENSIONS


@dataclass
class ParsedDocument:
    """The result of parsing a raw file: extracted text plus structural metadata."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _extension(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


class DocumentParser:
    """Parses raw document bytes into text, dispatching by extension.

    Plain-text formats are decoded directly; rich formats go through Docling (imported lazily).
    """

    def __init__(self, docling_converter: object | None = None) -> None:
        self._converter = docling_converter

    @staticmethod
    def supports(filename: str) -> bool:
        return _extension(filename) in SUPPORTED_EXTENSIONS

    def parse(self, filename: str, data: bytes) -> ParsedDocument:
        ext = _extension(filename)
        if ext in PLAIN_TEXT_EXTENSIONS:
            return self._parse_plain_text(filename, data, ext)
        if ext in DOCLING_EXTENSIONS:
            return self._parse_with_docling(filename, data, ext)
        raise ValueError(f"unsupported document type: {filename!r} (extension {ext!r})")

    def _parse_plain_text(self, filename: str, data: bytes, ext: str) -> ParsedDocument:
        text = data.decode("utf-8", errors="replace")
        return ParsedDocument(
            text=text,
            metadata={"filename": filename, "extension": ext, "parser": "plain_text"},
        )

    def _parse_with_docling(self, filename: str, data: bytes, ext: str) -> ParsedDocument:
        converter = self._resolve_converter()
        text = self._convert(converter, filename, data)
        return ParsedDocument(
            text=text,
            metadata={"filename": filename, "extension": ext, "parser": "docling"},
        )

    def _resolve_converter(self) -> object:
        if self._converter is not None:
            return self._converter
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:  # pragma: no cover - exercised only without the heavy dep
            raise RuntimeError(
                "docling is not installed; install it or inject a docling_converter "
                "(pip install docling)"
            ) from exc
        self._converter = DocumentConverter()
        return self._converter

    @staticmethod
    def _convert(converter: object, filename: str, data: bytes) -> str:
        """Writes the bytes to a temp file and runs the Docling converter, returning Markdown."""
        import tempfile
        from pathlib import Path

        suffix = _extension(filename) or ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            result = converter.convert(str(tmp_path))  # type: ignore[attr-defined]
            return str(result.document.export_to_markdown())
        finally:
            tmp_path.unlink(missing_ok=True)
