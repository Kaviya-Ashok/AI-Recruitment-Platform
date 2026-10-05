"""Interview transcript model — an uploaded transcript file attached to one
round of human interview feedback (CLAUDE.md §§7, 17, 24; Increment B).

WHAT THIS TABLE IS
------------------
A *pointer* to a PDF/DOCX that lives in Google Drive, plus metadata. The
transcript TEXT is deliberately never stored in Postgres: Drive is the document
store (CLAUDE.md §17) and a transcript is a candidate-sensitive document
(§24). Nothing here is read by any AI yet.

WHY A SEPARATE TABLE (NOT ``documents``)
----------------------------------------
``documents`` has no type column, and more than ten call sites treat "any
document on an application" as the candidate's résumé. A transcript stored there
would be silently parsed, prequalified and screened as if it were a résumé.

WHY KEYED TO THE FEEDBACK ROW
-----------------------------
``interview_feedback`` rows are immutable (no update path), so the transcript
hangs off the feedback row and can be attached or replaced AFTER the feedback
exists. No ``application_id`` and no candidate-name column are stored: both are
resolved through the feedback row, so there is exactly one place they live.

WHY CURRENT / SUPERSEDED, AND NEVER DELETE
------------------------------------------
Same non-destructive convention as ``post_interview_analyses``: a replacement
inserts a new CURRENT row and marks the old one SUPERSEDED (with
``superseded_at``). No row and no Drive file is ever deleted. Unlike that table,
"at most one CURRENT row" IS enforced by the database, through a PARTIAL UNIQUE
INDEX on ``interview_feedback_id`` WHERE ``status = 'CURRENT'``.

Design decisions
----------------
ID type — UUID v4, Python-side default (project precedent).

FK delete behaviour — both FKs are ``ON DELETE RESTRICT``: an independent
business/audit record that must not vanish as a side effect of deleting a
parent (same convention as ``interview_feedback``).

``status`` — a validated String, not a native Postgres enum, like every other
status vocabulary in this codebase.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
``file_name`` embeds the candidate's name (``Ananya_Rao_Transcript_R1.pdf``),
so it is personal data: it lives on this row ONLY — never in an audit event,
never in a log line, never in an exception message. ``__repr__`` omits it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class InterviewTranscriptStatus:
    """Which transcript counts for a round right now (validated string, not a
    DB enum — the same growing-vocabulary convention as every other status)."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"

    ALL: frozenset[str] = frozenset({CURRENT, SUPERSEDED})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class InterviewTranscript(Base):
    """One transcript file attached to one interview-feedback round."""

    __tablename__ = "interview_transcripts"

    __table_args__ = (
        # At most ONE current transcript per round, enforced by the database.
        # Partial, so any number of SUPERSEDED rows may coexist.
        Index(
            "uq_interview_transcripts_one_current_per_feedback",
            "interview_feedback_id",
            unique=True,
            postgresql_where=text("status = 'CURRENT'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    interview_feedback_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_feedback.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # The real internal user who attached it. The service rejects the SYSTEM
    # actor: an automated pipeline cannot hold an interview transcript.
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    drive_file_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    drive_folder_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # The standardized Drive name — contains the candidate's name (personal
    # data). Never enters audit metadata, logs or exception messages.
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)

    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # False when the file has no (or trivially little) extractable text — most
    # likely a scan. Stored so the UI can warn and a later AI step can skip it.
    text_extractable: Mapped[bool] = mapped_column(Boolean, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=InterviewTranscriptStatus.CURRENT,
        server_default=text("'CURRENT'"),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No file_name (embeds the candidate's name).
        return (
            f"<InterviewTranscript id={self.id!r} "
            f"feedback={self.interview_feedback_id!r} status={self.status!r}>"
        )
