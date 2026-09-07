"""JobRequirement model — one extracted evaluation criterion for a job.

This table is created **empty** in Phase 1.1. Rows are inserted by the JD
analysis flow in Phase 1.2 (Claude extracts requirements from
``jobs.jd_source_text``). No service or UI in this step writes to it.

Design decisions
----------------
ID type — **UUID v4**, Python-side default (Phase 0 precedent).

``requirement_type`` — **validated String, NOT a DB enum**. CLAUDE.md §1's
    categories are ``mandatory / preferred / experience / behavioral / other`` —
    the explicit ``other`` bucket signals this is open-ended. Validated at the
    service layer against :class:`RequirementType`.

``source_version`` / ``is_current`` — columns only, no versioning *logic* in
    this step. They exist so Phase 1.2 re-analysis can supersede a prior set of
    requirements without a schema change.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.database.database import Base


class RequirementType:
    """Canonical requirement categories (CLAUDE.md §1) — source of truth for
    the ``job_requirements.requirement_type`` **string** column.
    """

    MANDATORY = "MANDATORY"
    PREFERRED = "PREFERRED"
    EXPERIENCE = "EXPERIENCE"
    BEHAVIORAL = "BEHAVIORAL"
    OTHER = "OTHER"

    ALL: frozenset[str] = frozenset(
        {MANDATORY, PREFERRED, EXPERIENCE, BEHAVIORAL, OTHER}
    )

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class JobRequirement(Base):
    """One evaluation criterion extracted from a job's JD (populated Phase 1.2)."""

    __tablename__ = "job_requirements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Validated against RequirementType at the service layer.
    requirement_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Free-text sub-label ("Technical Skill", "Certification", ...); Phase 1.2.
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    requirement_text: Mapped[str] = mapped_column(Text, nullable=False)

    source_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=expression.true(),
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
        return (
            f"<JobRequirement id={self.id!r} job_id={self.job_id!r} "
            f"type={self.requirement_type!r}>"
        )
