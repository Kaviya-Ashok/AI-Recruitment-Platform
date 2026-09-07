"""Document model — a persisted reference to one stored candidate file.

Phase 2 scope: resume storage. The bytes live in Google Drive (behind
``app/services/storage_service.py``); this table is Postgres's record of *what*
was stored, *where*, and *for whom* — per CLAUDE.md §17 (Postgres stores the
provider file id + metadata + the candidate relationship; the provider stays
swappable).

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``application_id`` — **required FK, ``ON DELETE RESTRICT``**. A document must not
silently vanish because its application row was deleted — same non-cascading
convention just established for ``applications``' own FKs (migration
``4f6f59712443``). Removing a candidate's data (CLAUDE.md §24) will be an
explicit, audited orchestration that deletes documents first.

``drive_file_id`` — **unique**. One DB row per stored file; a duplicate id would
mean two rows point at the same bytes.

``drive_folder_id`` — the per-job Drive subfolder this file lives in (named by
``job_id``). Stored for traceability / cleanup; Postgres, not Drive's folder
tree, is the lookup mechanism.

``original_filename`` — the **sanitised** name (``sanitize_filename``), never the
raw untrusted upload name. It is fine to store here (it is not audit metadata),
but it may contain PII (e.g. ``Jane_Smith_CV.pdf``) so it must NOT appear in
audit-event metadata.

``mime_type`` — a plain validated ``String`` (not a business-status field, so no
constants class). The service layer validates it against an explicit allowlist
before any upload; the column just records the accepted value.

No ``updated_at``: a document row is write-once (the file is immutable once
stored; a re-upload is a new row).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class Document(Base):
    """Postgres record of one file stored in the document provider (Drive)."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Provider file id (Drive today). UNIQUE: one DB row per stored file.
    drive_file_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )

    # The per-job provider subfolder this file lives in (named by job_id).
    drive_folder_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Sanitised filename — never the raw upload name. May contain PII: keep it
    # out of audit metadata.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)

    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # Deliberately NOT including original_filename (possible PII).
        return (
            f"<Document id={self.id!r} application_id={self.application_id!r} "
            f"mime_type={self.mime_type!r} size={self.file_size_bytes}>"
        )
