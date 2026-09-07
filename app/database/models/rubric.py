"""Rubric models — the versioned, governable evaluation rubric for a job.

Phase 1.3 scope: generation (from ``job_requirements``), HR edit while DRAFT,
and explicit approval. Once a :class:`RubricVersion` is ``APPROVED`` it is
**immutable** — no criterion of an approved version is ever UPDATEd or DELETEd.
A change after approval means a new DRAFT version (via re-generation), which HR
must explicitly approve to supersede the prior approved one (CLAUDE.md §5/§F).

Design decisions
----------------
ID type — **UUID v4**, Python-side default (project precedent).

``RubricVersion.status`` — **validated String, not a DB enum**. Per the
    growing-vocabulary rule a future ``REJECTED`` state is plausible; validated
    at the service layer against :class:`RubricVersionStatus`.

``RubricCriterion.requirement_type`` — reuses the same vocabulary as
    ``job_requirements`` (:class:`RequirementType`), validated in the service.

No ``weight`` / scoring column — deferred to the scoring-engine phase
(CLAUDE.md §§20, 21). Adding it now, with no consumer, would be premature.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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

from app.database.database import Base


class RubricVersionStatus:
    """Lifecycle states for a rubric version (validated string column).

    ``DRAFT``      — generated / being edited; at most one per job at a time.
    ``APPROVED``   — locked, immutable, the effective evaluation rubric; at most
                     one per job at a time.
    ``SUPERSEDED`` — replaced by a later DRAFT (on re-generation) or a later
                     APPROVED version. Kept for history, never deleted.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"

    ALL: frozenset[str] = frozenset({DRAFT, APPROVED, SUPERSEDED})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class RubricVersion(Base):
    """One version of a job's evaluation rubric."""

    __tablename__ = "rubric_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 1, 2, 3... per job. Independent of job_requirements.source_version.
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Validated against RubricVersionStatus at the service layer.
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default=text(f"'{RubricVersionStatus.DRAFT}'"),
    )

    # Traceability: the job_requirements.source_version this was generated from.
    # Not an FK — job_requirements versions are not uniquely keyed.
    generated_from_requirements_version: Mapped[int] = mapped_column(
        Integer, nullable=False
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"<RubricVersion id={self.id!r} job_id={self.job_id!r} "
            f"v{self.version_number} status={self.status!r}>"
        )


class RubricCriterion(Base):
    """One evaluable criterion within a rubric version."""

    __tablename__ = "rubric_criteria"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Validated against RequirementType at the service layer.
    requirement_type: Mapped[str] = mapped_column(String(50), nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    criterion_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Stable HR-facing ordering within a version (generation order for now).
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)

    # Best-effort trace back to the originating extracted requirement; NULL when
    # HR adds a criterion manually or the AI's mapping could not be resolved.
    source_job_requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_requirements.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"<RubricCriterion id={self.id!r} "
            f"version={self.rubric_version_id!r} type={self.requirement_type!r}>"
        )
