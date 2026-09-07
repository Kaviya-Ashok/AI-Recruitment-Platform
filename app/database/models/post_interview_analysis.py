"""PostInterviewAnalysis model — the AI's consolidated read of a candidate
*after* the human interview (CLAUDE.md §9; Phase 4 Step 9).

One row = one run of the ``post_interview_analysis`` AI task for one
application, against ONE interview-feedback record.

WHY REGENERATION IS NON-DESTRUCTIVE — A DELIBERATE DIVERGENCE
-------------------------------------------------------------
Every other regenerable AI artefact in this codebase is **replace-in-place**:
``prequalification_results``, ``resume_extractions``, ``screening_evaluations``,
``interview_guides`` and ``candidate_rankings`` all delete the old row (or
overwrite it) under ``force=True``, on the reasoning that a regenerable
derivation is disposable and the audit log is the history.

**This table breaks that pattern on purpose.** A post-interview analysis is the
last AI artefact a human reads before making a hiring decision (§11), and it is
generated *from* irreplaceable human testimony (§7). If HR regenerates it —
after a second interview round, or simply to re-run it — the analysis a decision
may already have been discussed against must remain readable, not vanish. So a
regeneration INSERTS a new row and marks the previous one ``SUPERSEDED`` with a
``superseded_at`` stamp. Nothing is ever deleted.

That is also why ``status`` is an explicit column rather than something inferred
from ``created_at`` ordering: "which analysis is current" is a fact the system
records, not a guess it re-derives. Ordering answers "which is newest"; only
``status`` answers "which one counts".

Exactly one row per application carries ``status = 'CURRENT'`` at any time. That
invariant is enforced in ``post_interview_service`` inside the same transaction
that inserts the new row — deliberately NOT as a partial unique index, which
would add migration and concurrency complexity this MVP does not need.

WHY ``rubric_version_id`` IS SOURCED FROM THE GUIDE
--------------------------------------------------
``interview_feedback`` carries no ``rubric_version_id`` (Step 8 did not require
one, and this step must not modify that table). The version is therefore
resolved along ``interview_feedback.interview_guide_id ->
interview_guides.rubric_version_id`` and **frozen here at creation**, exactly as
every other downstream artefact freezes it. It is never re-derived from the
job's currently-approved rubric, so an old analysis stays interpretable against
the criteria it was actually produced under.

WHAT THIS TABLE DELIBERATELY DOES NOT HOLD
------------------------------------------
* **No disagreement field of any kind** — no flag, no severity, no normalized
  comparison. §8's AI-vs-human comparison is a separate concern and
  ``AI_HUMAN_DISAGREEMENT_DETECTED`` stays unemitted.
  ``human_recommendation_snapshot`` is a plain copy of what the interviewer
  recorded, carried for display context only; nothing in this step compares it
  to ``ai_recommendation``.
* **No interview transcript** — out of scope entirely. The only transcript this
  step reads is the *screening* transcript, which is an input to the prompt and
  is not stored here.

``ai_recommendation`` — PROCEED / HOLD only, validated against
``ScreeningRecommendation.AUTOMATED``. The AI never returns a recommendation at
all (its schema has no such field); Python computes this one. REJECT stays a
human decision (§11) and is unreachable on this path by construction.

``analyzed_only_latest_feedback`` — records that this analysis read exactly one
feedback record, the latest. It is a stored fact rather than an assumption a
reader has to make: the HR page renders a caption *from this field* ("Based on
the most recent interview-feedback record only..."), so the scope of the
analysis is disclosed on screen. The AI is deliberately NOT asked to state it in
its prose — nothing in the prompt instructs that — because a disclosure this
important must not depend on model behaviour.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
``summary`` / ``strengths`` / ``gaps`` / ``unknowns`` /
``evidence_consistency_notes`` synthesise résumé content, candidate screening
answers and an interviewer's own words. They live ONLY on this row: never in an
audit event, never in a log line, never in an exception message. ``__repr__``
omits all of them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class PostInterviewAnalysisStatus:
    """Which analysis counts right now (validated string, not a DB enum).

    Same growing-vocabulary convention as every other status in this codebase
    (``ApplicationStatus``, ``ScreeningSessionStatus``, ...): a ``String``
    column validated at the service layer, so a later value needs no migration.
    """

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"

    ALL: frozenset[str] = frozenset({CURRENT, SUPERSEDED})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class PostInterviewAnalysis(Base):
    """One AI post-interview analysis for one application."""

    __tablename__ = "post_interview_analyses"

    __table_args__ = (
        # The lookup this table exists to serve: "the CURRENT analysis for this
        # application", plus the history scan for the same application.
        Index(
            "ix_post_interview_analyses_application_status",
            "application_id",
            "status",
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

    # The ONE feedback record this analysis read (the latest at generation time).
    interview_feedback_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_feedback.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # The guide that feedback was recorded against — and the path by which the
    # rubric version below was resolved.
    interview_guide_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_guides.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # FROZEN at creation from the guide. Never re-derived. See module docstring.
    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # The real HR user who asked for this. Stored on the row itself, not only on
    # the audit event — the same convention as ``interview_feedback``.
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # --- AI prose (never audited, never logged) -----------------------
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    strengths: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    gaps: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    unknowns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Singular prose, so Text — unlike strengths/gaps/unknowns, which are JSONB
    # because they are list-shaped (matching ``screening_evaluations``).
    evidence_consistency_notes: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Python-computed, never AI-supplied ---------------------------
    confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    # PROCEED / HOLD only. Validated against ScreeningRecommendation.AUTOMATED.
    ai_recommendation: Mapped[str] = mapped_column(String(20), nullable=False)

    # A plain copy of the interviewer's own recommendation, for display context.
    # NOT compared with ai_recommendation anywhere in this step.
    human_recommendation_snapshot: Mapped[str] = mapped_column(
        String(20), nullable=False
    )

    analyzed_only_latest_feedback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    ai_model: Mapped[str] = mapped_column(String(100), nullable=False)

    # Validated against PostInterviewAnalysisStatus at the service layer.
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text(f"'{PostInterviewAnalysisStatus.CURRENT}'"),
    )
    # Set only when this row stops being current. NULL on a CURRENT row.
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No summary / strengths / gaps / unknowns / notes (synthesised prose
        # about a real person).
        return (
            f"<PostInterviewAnalysis id={self.id!r} "
            f"application={self.application_id!r} status={self.status!r} "
            f"rec={self.ai_recommendation!r} conf={self.confidence!r}>"
        )
