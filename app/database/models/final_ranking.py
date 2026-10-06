"""FinalRanking / FinalRankingEntry — the post-interview final ranking of one job's
interviewed candidates (CLAUDE.md §§5, 10, 12, 20, 21; Phase 4 Step 10b).

One ``final_rankings`` row = one HR-triggered RUN for one
``(job_id, rubric_version_id)`` partition; its ``final_ranking_entries`` are a
complete census of that partition's interviewed candidates, ranked or not.

NON-DESTRUCTIVE, LIKE STEP 9 (NOT LIKE STEP 5)
----------------------------------------------
Step 5's ``candidate_rankings`` is replaced wholesale (DELETE + INSERT) on every
regeneration. A final ranking is the artefact the hiring decision (Step 11) will
point at, so it must stay readable: a new run marks the previous CURRENT run
``SUPERSEDED`` (``superseded_at``) and inserts a new CURRENT one. Nothing is ever
deleted. Unlike ``post_interview_analyses``, "at most one CURRENT run per
``(job_id, rubric_version_id)``" is ENFORCED BY THE DATABASE with a partial unique
index, as it is on ``interview_transcripts``.

NO FOREIGN KEY INTO ``candidate_rankings``
------------------------------------------
Step 5 deletes and re-inserts its rows with new ids. An inbound RESTRICT FK would
make the next Step 5 regeneration fail, and Step 5 must not change. Screening
provenance is therefore stored as plain values — ``screening_generation_batch_id``,
``screening_generated_at`` and ``screening_rank`` — never as a reference. Nothing
here references ``screening_evaluations`` either. The only FKs are to rows that are
never deleted: jobs, rubric versions, users, applications, this table's own run
row, and ``post_interview_analyses`` (non-destructive since Step 9). All RESTRICT.

WHAT IS NOT STORED
------------------
No names, no notes, no evidence, no transcript text, no file names. ``status_reason``
is a short SYSTEM-WRITTEN sentence, never candidate or interviewer text.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class FinalRankingStatus:
    """Which run counts right now (validated string, not a DB enum)."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"

    ALL: frozenset[str] = frozenset({CURRENT, SUPERSEDED})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class FinalRankingEntryStatus:
    """What became of one candidate in a run (validated string)."""

    RANKED = "RANKED"
    NOT_RANKED_INELIGIBLE = "NOT_RANKED_INELIGIBLE"
    INCOMPLETE_SCREENING = "INCOMPLETE_SCREENING"
    INCOMPLETE_INTERVIEW = "INCOMPLETE_INTERVIEW"

    ALL: frozenset[str] = frozenset(
        {RANKED, NOT_RANKED_INELIGIBLE, INCOMPLETE_SCREENING, INCOMPLETE_INTERVIEW}
    )

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class FinalRanking(Base):
    """One run of the final ranking for one job / rubric-version partition."""

    __tablename__ = "final_rankings"

    __table_args__ = (
        # At most ONE current run per (job, rubric version), enforced by the
        # database. Partial, so any number of SUPERSEDED runs may coexist.
        Index(
            "uq_final_rankings_one_current_per_partition",
            "job_id",
            "rubric_version_id",
            unique=True,
            postgresql_where=text("status = 'CURRENT'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # The weights this run used — stored so an old run stays reproducible even if
    # the constants change, and shown to HR.
    screening_weight: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    interview_weight: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=FinalRankingStatus.CURRENT,
        server_default=text("'CURRENT'"),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"<FinalRanking id={self.id!r} job={self.job_id!r} "
            f"rubric_version={self.rubric_version_id!r} status={self.status!r}>"
        )


class FinalRankingEntry(Base):
    """One candidate's outcome in one final-ranking run."""

    __tablename__ = "final_ranking_entries"

    __table_args__ = (
        UniqueConstraint(
            "final_ranking_id",
            "application_id",
            name="uq_final_ranking_entries_run_application",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    final_ranking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("final_rankings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 0-10, 2 decimals. NULL = unknown, never 0.
    screening_score: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    interview_score: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Competition rank within the run; NULL unless RANKED.
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Step 5's ``eligible`` / ``mandatory_unknown_flag``, read as stored.
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mandatory_unknown: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    entry_status: Mapped[str] = mapped_column(String(30), nullable=False)
    # Plain, system-written. Never candidate or interviewer text.
    status_reason: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )

    # HIGH/MEDIUM/LOW; NULL when no final score exists.
    final_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The screening evaluation's overall confidence at run time (display).
    screening_confidence: Mapped[str | None] = mapped_column(
        String(10), nullable=True
    )

    # Screening provenance as PLAIN VALUES — deliberately not a foreign key
    # (see the module docstring).
    screening_generation_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    screening_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    screening_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The CURRENT, same-rubric-version analysis at run time; NULL when absent or
    # on a different version. Context only — never part of the score.
    post_interview_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("post_interview_analyses.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # Breakdown, so the score is reproducible from the row alone.
    rounds_used: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # {"1": "3.50", ...} — mean rating per round that had ratings.
    round_means: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    feedback_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"<FinalRankingEntry run={self.final_ranking_id!r} "
            f"application={self.application_id!r} rank={self.rank!r} "
            f"status={self.entry_status!r}>"
        )
