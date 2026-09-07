"""CandidateRanking model — the cross-candidate ranking of one job's
already-evaluated candidates (CLAUDE.md §§5, 12, 20, 34; Phase 4 Step 5).

One row = one candidate's place in one *generation run* of the ranking for one
``(job_id, rubric_version_id)`` **partition**.

WHY ``rubric_version_id`` PARTITIONS THIS TABLE
----------------------------------------------
A job's approved rubric can be superseded over time (``rubric_service`` supports
generating + approving a new version while candidates already exist). Candidates
evaluated against rubric v1 and candidates evaluated against rubric v2 were
scored against **different criteria**, so their scores are not comparable and
must never share a ranked list. Every ranking operation in this MVP therefore
works on exactly one ``(job_id, rubric_version_id)`` pair. The
``screening_evaluations`` row each candidate carries records which version they
were scored against; ranking simply groups by it and never merges across it.

WHY THIS IS A FULL REPLACE, NOT AN APPEND-ONLY LOG
-------------------------------------------------
Unlike every prior AI-task table (``prequalification_results`` /
``screening_evaluations`` — write-once, check-first, one row per subject),
ranking is an **explicit, repeatable HR action**: "rank the candidates I have
right now". Re-running it after more candidates finish screening is normal and
expected. So ``ranking_service.generate_ranking`` DELETEs every existing row for
the ``(job_id, rubric_version_id)`` partition and inserts a fresh set, all in
one transaction. The audit trail (one ``RANKING_GENERATED`` event per run,
correlated by ``generation_batch_id``) is where the history lives — not in
accumulated table rows. The ``UNIQUE (job_id, rubric_version_id, application_id)``
constraint enforces "one row per candidate per partition" *after* the replace.

HOW INELIGIBLE / UNSCORABLE CANDIDATES ARE REPRESENTED
-----------------------------------------------------
The table is a **complete census** of the partition — one row for every
evaluated candidate, ``eligible`` splits them:

* **Ineligible** (a reconciled MANDATORY criterion is FAIL): ``eligible=False``,
  ``rank_position=NULL``. ``overall_score`` is still computed and stored (HR
  context), ``mandatory_unknown_flag`` is still computed. The candidate is never
  assigned a numbered rank and the UI shows them in a separate "ineligible"
  section.
* **Eligible but fully unassessed** (every bucket score on the
  ``screening_evaluations`` row is NULL): ``eligible=True``,
  ``overall_score=NULL``, and ``rank_position`` *is* assigned — at the bottom of
  the eligible order (a NULL score sorts below any real score). This is a
  deliberate edge case: the candidate finished screening but nothing was
  PASS/FAIL-scoreable, so they rank last rather than being hidden.

``mandatory_unknown_flag`` (a reconciled MANDATORY criterion is UNKNOWN) never
affects eligibility or rank — UNKNOWN is not FAIL (CLAUDE.md §B). It is surfaced
prominently in the UI because it materially changes how much weight HR should
give that candidate's position.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

FKs — all three **``ON DELETE RESTRICT``**, same "independent business record,
never a silent cascade" convention as ``applications`` /
``prequalification_results`` / ``screening_evaluations``.

No validated-string vocabulary here — the only enumerable field is the two
booleans; ``rank_position`` is a plain 1-based integer.

No ``updated_at`` — a row is write-once within its generation run (replaced =
delete + insert, never updated in place).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class CandidateRanking(Base):
    """One candidate's place in one generation run of one job/rubric-version
    ranking partition."""

    __tablename__ = "candidate_rankings"

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "rubric_version_id",
            "application_id",
            name="uq_candidate_rankings_partition_application",
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

    # Identifies which partition this ranking run covers — candidates scored
    # against different rubric versions are never ranked together.
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # 1-based within the (job_id, rubric_version_id) partition, assigned in
    # sorted order to ELIGIBLE candidates only. NULL for ineligible candidates
    # (mandatory FAIL) — they are excluded from the numbered ranking.
    rank_position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Weighted average of the non-NULL screening-evaluation bucket scores
    # (Requirements weight 2, Experience 1, Behavioral 1). NULL when every
    # bucket score was NULL for this candidate (fully unassessed).
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)

    mandatory_unknown_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Groups every row from one generate_ranking call together — used for the
    # atomic full-replace and to correlate rows with their RANKING_GENERATED
    # audit event.
    generation_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"<CandidateRanking job={self.job_id!r} "
            f"rubric_version={self.rubric_version_id!r} "
            f"application={self.application_id!r} "
            f"rank={self.rank_position!r} eligible={self.eligible!r}>"
        )
