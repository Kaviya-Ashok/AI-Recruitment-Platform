"""Tests for app.utils.validation."""

from __future__ import annotations

import pytest

from app.utils.validation import (
    MAX_UPLOAD_BYTES,
    FileValidationError,
    sanitize_filename,
    validate_uploaded_file,
)


def test_accepts_valid_pdf(sample_pdf_bytes):
    validate_uploaded_file("job-description.pdf", sample_pdf_bytes)  # no raise


def test_accepts_valid_docx(sample_docx_bytes):
    validate_uploaded_file("JD Final.docx", sample_docx_bytes)  # no raise


def test_rejects_disallowed_extension(sample_pdf_bytes):
    with pytest.raises(FileValidationError):
        validate_uploaded_file("resume.txt", sample_pdf_bytes)
    with pytest.raises(FileValidationError):
        validate_uploaded_file("archive.pdf.exe", sample_pdf_bytes)


def test_rejects_oversized_file():
    big = b"x" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(FileValidationError):
        validate_uploaded_file("huge.pdf", big)


def test_rejects_empty_file():
    with pytest.raises(FileValidationError):
        validate_uploaded_file("empty.pdf", b"")


def test_rejects_control_char_and_null_byte_filenames(sample_pdf_bytes):
    with pytest.raises(FileValidationError):
        validate_uploaded_file("bad\x00name.pdf", sample_pdf_bytes)
    with pytest.raises(FileValidationError):
        validate_uploaded_file("bad\nname.pdf", sample_pdf_bytes)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        (r"C:\Users\evil\jd.docx", "jd.docx"),
        ("/tmp/jd.pdf", "jd.pdf"),
        ("  spaced.pdf  ", "spaced.pdf"),
    ],
)
def test_sanitize_filename_strips_paths(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_rejects_empty():
    with pytest.raises(FileValidationError):
        sanitize_filename("   ")
    with pytest.raises(FileValidationError):
        sanitize_filename("...")


def test_validate_rejects_path_traversal_name_without_valid_ext(sample_pdf_bytes):
    # "../../etc/passwd" sanitises to "passwd" -> no allowed extension -> reject.
    with pytest.raises(FileValidationError):
        validate_uploaded_file("../../etc/passwd", sample_pdf_bytes)
