"""Upload validation and filename sanitisation.

Streamlit's ``st.file_uploader`` restricts extensions client-side only; that is
not a security control. Everything here re-checks server-side before any bytes
are parsed or stored.
"""

from __future__ import annotations

import os

# MVP limit. This is a deliberate guess — JDs are small; 10 MB comfortably
# covers a text-heavy PDF/DOCX while bounding memory and parse time. Revisit if
# real documents hit it.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = frozenset({".pdf", ".docx"})


class FileValidationError(Exception):
    """Raised when an uploaded file fails a validation or sanitisation rule."""


def sanitize_filename(filename: str) -> str:
    """Reduce an uploaded filename to a safe base name.

    Strips any directory components (both ``/`` and ``\\``), rejects null bytes
    and control characters, and collapses surrounding whitespace. Returns the
    bare name (e.g. ``senior-data-engineer.pdf``), never a path.

    Raises
    ------
    FileValidationError
        If the name is empty, contains a null byte / control character, or has
        no usable base component.
    """
    if not filename or not filename.strip():
        raise FileValidationError("Filename is missing.")

    if "\x00" in filename:
        raise FileValidationError("Filename contains a null byte.")
    if any(ord(ch) < 32 for ch in filename):
        raise FileValidationError("Filename contains control characters.")

    # Take the last component regardless of separator style.
    base = filename.replace("\\", "/").split("/")[-1].strip()
    base = base.strip(". ")  # no leading/trailing dots or spaces (".", "..")

    if not base:
        raise FileValidationError("Filename has no usable name component.")
    return base


def validate_uploaded_file(filename: str, file_bytes: bytes) -> None:
    """Validate an upload before it is parsed or stored.

    Enforces: non-empty content, size <= :data:`MAX_UPLOAD_BYTES`, extension in
    :data:`ALLOWED_EXTENSIONS`, and a sanitisable filename.

    Raises
    ------
    FileValidationError
        On any violation. Returns ``None`` on success.
    """
    if not file_bytes:
        raise FileValidationError("The uploaded file is empty.")

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise FileValidationError(
            f"File is too large (limit {limit_mb} MB)."
        )

    safe_name = sanitize_filename(filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise FileValidationError(
            f"Unsupported file type {ext or '(none)'!r}. Allowed: {allowed}."
        )
