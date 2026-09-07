"""Tests for app.utils.parsing — real PDF/DOCX extraction, no mocks."""

from __future__ import annotations

import pytest

from app.utils.parsing import (
    DocumentParsingError,
    extract_text_from_docx,
    extract_text_from_pdf,
)


def test_extract_text_from_pdf_returns_text(sample_pdf_bytes):
    text = extract_text_from_pdf(sample_pdf_bytes)
    assert text.strip()
    assert "Senior Data Engineer" in text


def test_extract_text_from_docx_returns_text(sample_docx_bytes):
    text = extract_text_from_docx(sample_docx_bytes)
    assert text.strip()
    assert "PySpark" in text


def test_extract_text_from_pdf_raises_on_garbage(corrupt_document_bytes):
    with pytest.raises(DocumentParsingError):
        extract_text_from_pdf(corrupt_document_bytes)


def test_extract_text_from_docx_raises_on_garbage(corrupt_document_bytes):
    with pytest.raises(DocumentParsingError):
        extract_text_from_docx(corrupt_document_bytes)


def test_extract_text_from_docx_rejects_a_pdf(sample_pdf_bytes):
    # A PDF renamed .docx / passed to the wrong parser must not silently pass.
    with pytest.raises(DocumentParsingError):
        extract_text_from_docx(sample_pdf_bytes)


def test_empty_bytes_raise(_none=None):
    with pytest.raises(DocumentParsingError):
        extract_text_from_pdf(b"")
    with pytest.raises(DocumentParsingError):
        extract_text_from_docx(b"")
