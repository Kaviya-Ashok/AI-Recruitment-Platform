"""CandidateShortlistEntry model — the HR "selected to proceed toward human
interview" marker for one candidate on one job (CLAUDE.md §§5, 6, 11, 12, 24;
Phase 4 Step 6).

Shortlisting is a **reversible HR judgment**, NOT a status transition and NOT the
final hiring decision (CLAUDE.md §11 — that stays a separate human decision).
It never touches ``Application.status``.

WHY UPDATE-IN-PLACE, NOT APPEND-ONLY
-----------------------------------
Exactly one row per ``(job_id, application_id)`` (``UNIQUE``). Each
shortlist / unshortlist action **updates that one row in place** — flipping
``is_shortlisted`` and rewriting ``decided_by_user_id`` / ``decided_at``. The
full history of who shortlisted whom and when lives in the **audit log**
(``CANDIDATE_SHORTLISTED`` / ``CANDIDATE_UNSHORTLISTED``, one event per real
transition), not in accumulated rows here. This mirrors ``candidate_rankings``'
"the table holds current state, the audit log holds history" philosophy (though
this table is keyed per application, not per ranking batch).

WHY ``rubric_version_id`` IS CAPTURED AT DECISION TIME
----------------------------------------------------
The decision is made against a specific ranking partition — the candidates
evaluated against one approved rubric version. The job's approved rubric can be
superseded afterward, so re-deriving "which rubric was this decided against?"
later would be wrong. It is stored on the row at the moment of the decision and
never rewritten on unshortlist (it stays informative: "this was the evaluation
basis of the most recent decision, whichever direction").

WHY ``reason`` NEVER REACHES AUDIT METADATA
-----------------------------------------
``reason`` is optional free text. Per the established safe-metadata convention
(CLAUDE.md §§12, 24; the same rule enforced for ``SCORE_GENERATED`` and
``RANKING_GENERATED``), audit-event ``action`` / ``previous_state`` /
``new_state`` / ``event_metadata`` carry only structured ids / enums / counts.
The reason lives **only** on this row; the audit event records that a transition
happened and its structured context, never the prose.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

FKs — ``job_id`` / ``application_id`` / ``rubric_version_id`` /
``decided_by_user_id`` all **``ON DELETE RESTRICT``**, the same "independent
business/audit record, never a silent cascade" convention as ``applications`` /
``candidate_rankings``. ``decided_by_user_id`` is RESTRICT + NOT NULL: every
shortlist decision has a real HR actor (``require_internal_user`` guarantees the
id resolves), and CLAUDE.md §23 keeps accounts disabled, not deleted.

``rank_position_at_decision`` — the candidate's ``candidate_rankings.rank_position``
for this partition at the moment of the decision. NULL when the candidate had no
ranking row, or an ineligible row (``rank_position`` itself NULL). Captured
because rank shifts on re-ranking and HR wants to know where the candidate stood
when the call was made.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class CandidateShortlistEntry(Base):
    """Current shortlist state for one candidate on one job. Updated in place."""

    __tablename__ = "candidate_shortlist_entries"

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "application_id",
            name="uq_candidate_shortlist_entries_job_application",
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

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Which ranking partition this decision was made against — captured at
    # decision time, never re-derived, never cleared on unshortlist.
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    is_shortlisted: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Optional HR note. NEVER copied into audit metadata.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # candidate_rankings.rank_position for this partition at decision time.
    rank_position_at_decision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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
            f"<CandidateShortlistEntry job={self.job_id!r} "
            f"application={self.application_id!r} "
            f"is_shortlisted={self.is_shortlisted!r}>"
        )
