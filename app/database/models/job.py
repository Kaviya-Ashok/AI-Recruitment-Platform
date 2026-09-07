"""Job model — a role/requisition and its stored Job Description (JD).

Phase 1.1 scope: a job can be *created* with a JD (pasted text or an uploaded
PDF/DOCX whose text is extracted and stored). No AI analysis happens here —
``job_requirements`` rows are populated by Claude in Phase 1.2.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (Phase 0 precedent).

``status`` — **validated String, NOT a DB enum**:
    The job lifecycle vocabulary grows every phase (DRAFT -> JD_ANALYZED ->
    RUBRIC_PENDING -> RUBRIC_APPROVED -> OPEN -> CLOSED -> ARCHIVED, and more).
    A native Postgres ENUM would need an ``ALTER TYPE ... ADD VALUE`` migration
    each time. Same reasoning as ``AuditEvent.event_type``: a ``String`` column
    validated at the service layer against :class:`JobStatus`. Only
    ``DRAFT`` is reachable in this step.

``jd_input_method`` — **native Postgres ENUM**:
    A genuinely closed, permanent 2-value set (paste vs upload). Follows the
    ``User.role`` precedent: native enum with an explicit create/drop lifecycle
    in the migration.

``jd_source_text`` is always populated (the extracted or pasted text), so the
rest of the system never needs to know which input method was used.
``jd_original_filename`` is a sanitised base name (never a path) and is only set
for ``FILE_UPLOAD``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class JobStatus:
    """Canonical job lifecycle states — the application-layer source of truth
    for the ``jobs.status`` **string** column (not a DB enum; see module
    docstring).

    Later phases add members here with no schema migration. Only ``DRAFT`` is
    produced in Phase 1.1.
    """

    DRAFT = "DRAFT"
    JD_ANALYZED = "JD_ANALYZED"
    RUBRIC_PENDING = "RUBRIC_PENDING"
    RUBRIC_APPROVED = "RUBRIC_APPROVED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"

    ALL: frozenset[str] = frozenset(
        {DRAFT, JD_ANALYZED, RUBRIC_PENDING, RUBRIC_APPROVED, OPEN, CLOSED, ARCHIVED}
    )

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class JdInputMethod(str, enum.Enum):
    """How the JD was supplied. Closed, permanent 2-value set -> native DB enum."""

    TEXT_PASTE = "TEXT_PASTE"
    FILE_UPLOAD = "FILE_UPLOAD"


class Job(Base):
    """A job/requisition with its stored Job Description."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Validated against JobStatus at the service layer.
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default=text(f"'{JobStatus.DRAFT}'"),
    )

    jd_source_text: Mapped[str] = mapped_column(Text, nullable=False)

    jd_input_method: Mapped[JdInputMethod] = mapped_column(
        Enum(
            JdInputMethod,
            name="jd_input_method",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # Sanitised base filename, only for FILE_UPLOAD.
    jd_original_filename: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return f"<Job id={self.id!r} title={self.title!r} status={self.status!r}>"
