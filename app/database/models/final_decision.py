"""FinalDecision model — the hiring manager's final human decision on one
candidate (CLAUDE.md §§11, 12, 22, 24; Phase 4 Step 11).

This is the ONE place in the system where a REJECT can be recorded, and the only
record of the human deciding. Everything before it — scores, rankings, analyses,
recommendations — is input; this row is the decision.

APPEND-ONLY REVISION, LIKE STEP 9 / 10b
---------------------------------------
Revising a decision never edits or deletes the old one. Each submission INSERTS a
new row; the previous CURRENT row is marked ``SUPERSEDED`` (``superseded_at``) in
the same transaction. "At most one CURRENT decision per application" is enforced by
the database with a PARTIAL UNIQUE INDEX and, first, by the service. Because a
decision may be the thing a later dispute turns on, every earlier decision and its
rationale stay readable.

THE RATIONALE
-------------
Required, stored exactly as written (trimmed; 10-2000 characters, enforced by the
service). It is a person's own words about a real candidate, so it lives on this
row ONLY — never in an audit event, a log line, an exception message or any prompt.
``__repr__`` omits it.

THE SNAPSHOT
------------
Alongside the decision the row copies, as plain values, what the manager could
see at that moment: the final-ranking entry (id, score, rank, status, confidence),
that run's rubric version, the CURRENT post-interview analysis (id and its AI
recommendation) and the ids of the interview-feedback records that existed. All
nullable — a decision may be recorded with no ranking or analysis. It lets the UI
say "based on earlier evidence" when any of those has since changed. The AI
recommendation snapshot is DISPLAY CONTEXT: nothing compares it to ``decision``.

FOREIGN KEYS
------------
``application_id`` and ``decided_by_user_id`` are RESTRICT: a decision must not
vanish as a side effect of deleting a parent, and accounts are disabled, not
deleted (§23). ``final_ranking_entry_id`` and ``post_interview_analysis_id`` are
RESTRICT FKs too, because Step 0 of Step 11 verified that NOTHING deletes those
rows — both tables are non-destructive (CURRENT/SUPERSEDED) and no code path or
cascade removes them. (The lesson of Step 10b: an FK into a table that CAN be
deleted from — ``candidate_rankings`` — must not be added; none is.)

NO decider name is stored (it is joined at read time), and no candidate name,
notes or transcript text is stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class FinalDecisionStatus:
    """Which decision counts right now (validated string, not a DB enum)."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"

    ALL: frozenset[str] = frozenset({CURRENT, SUPERSEDED})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class FinalDecision(Base):
    """One final human decision for one application."""

    __tablename__ = "final_decisions"

    __table_args__ = (
        # At most ONE current decision per application, enforced by the database.
        # Partial, so any number of SUPERSEDED rows may coexist.
        Index(
            "uq_final_decisions_one_current_per_application",
            "application_id",
            unique=True,
            postgresql_where=text("status = 'CURRENT'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # The real hiring manager / admin. Never the SYSTEM actor (service-enforced).
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # PROCEED / HOLD / REJECT, validated against ScreeningRecommendation.ALL in the
    # service. A human MAY choose REJECT (the automated paths may not).
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    # The decider's own words. Never audited, logged or sent to an AI.
    rationale: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=FinalDecisionStatus.CURRENT,
        server_default=text("'CURRENT'"),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- snapshot of what the decider could see (all nullable) --------------
    final_ranking_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("final_ranking_entries.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    post_interview_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("post_interview_analyses.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    final_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entry_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    final_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    rubric_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # The analysis's own AI recommendation at decision time. Context only.
    ai_recommendation_snapshot: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    interview_feedback_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    # Set EXPLICITLY in Python by the service (one clock reading per write, kept
    # strictly after the previous decision's), not by ``now()`` — two writes in one
    # transaction share Postgres' transaction-start time and would tie.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No rationale (the decider's free text about a real person).
        return (
            f"<FinalDecision id={self.id!r} application={self.application_id!r} "
            f"decision={self.decision!r} status={self.status!r}>"
        )
