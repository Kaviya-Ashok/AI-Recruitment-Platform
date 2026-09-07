"""Document text extraction.

Generic, job-agnostic helpers used now for Job Descriptions and later for
resume parsing (Phase 2). Callers receive :class:`DocumentParsingError` on any
failure — raw PyMuPDF / python-docx exceptions never propagate.

Uploaded document bytes are untrusted input: they are only parsed for text,
never executed, and never passed to a shell.
"""

from __future__ import annotations

import io

import pymupdf  # PyMuPDF
from docx import Document as _DocxDocument


class DocumentParsingError(Exception):
    """Raised when a PDF or DOCX cannot be read as text."""


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Return the concatenated text of every page of a PDF.

    Raises
    ------
    DocumentParsingError
        If the bytes are not a readable PDF.
    """
    if not file_bytes:
        raise DocumentParsingError("PDF file is empty.")
    try:
        with pymupdf.open(stream=file_bytes, filetype="pdf") as doc:
            pages = [page.get_text() for page in doc]
    except Exception as exc:  # noqa: BLE001 - normalise to our domain error
        raise DocumentParsingError(
            f"Could not read the PDF: {type(exc).__name__}."
        ) from exc
    return "\n".join(pages).strip()


def extract_text_from_docx(file_bytes: bytes) -> str:
    """Return the text of a DOCX: paragraphs plus table cell text, in order.

    Raises
    ------
    DocumentParsingError
        If the bytes are not a readable DOCX.
    """
    if not file_bytes:
        raise DocumentParsingError("DOCX file is empty.")
    try:
        document = _DocxDocument(io.BytesIO(file_bytes))
        parts: list[str] = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    parts.append(cell.text)
    except Exception as exc:  # noqa: BLE001 - normalise to our domain error
        raise DocumentParsingError(
            f"Could not read the DOCX: {type(exc).__name__}."
        ) from exc
    return "\n".join(part for part in parts if part is not None).strip()
