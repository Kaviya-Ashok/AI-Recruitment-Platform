"""Human interview feedback models — what the *person* who ran the interview
recorded (CLAUDE.md §§7, 12, 22, 24; Phase 4 Step 8).

This is the first table in the system whose entire contents are **human
authorship**. Nothing here is AI-generated, AI-summarised, or AI-scored, and no
column exists for an AI opinion. CLAUDE.md §7: *"The original human feedback
must be preserved. Do not rewrite human feedback as if it were AI-generated
evidence."* There is deliberately no ``ai_model`` column (every AI-output table
in this codebase has one) — its absence is the schema-level statement that this
row was written by a person.

WHY APPEND-ONLY, NOT REPLACE-IN-PLACE
-------------------------------------
``screening_evaluations`` / ``interview_guides`` / ``candidate_rankings`` are
all *replace-in-place*: one current row, history in the audit log. This table is
the opposite — **one row per interview round, accumulated**, keyed by
``UNIQUE(application_id, interview_round)``.

The reason is the §7 preservation rule. Those other tables hold a regenerable
*derivation*: re-running the AI over the same inputs reproduces an equivalent
row, so the old one is disposable. A human's interview notes are irreplaceable
testimony — a second interviewer's round-2 write-up does not supersede round
1's, it *accompanies* it. Round 2 must never overwrite round 1, so there is no
"current" row to replace and consequently:

* no ``is_current`` column — "latest" is derived at read time from
  ``created_at DESC, id DESC``, never stored and never mutated;
* no ``updated_at`` — a submitted row is immutable once written.

WHY ``interview_round`` IS SUBMITTED, NOT DERIVED
-------------------------------------------------
``interview_round`` is supplied by the interviewer and stored verbatim. It is
**never** inferred from ``created_at`` or from insertion order, because those
diverge in practice: an interviewer can write up round 2 on the day and only
back-fill round 1's notes a week later. The service offers
``get_suggested_next_interview_round`` as an *advisory* default for the form —
it never overrides what was submitted.

WHY ``recommendation`` REUSES ``ScreeningRecommendation``
---------------------------------------------------------
CLAUDE.md §7's human vocabulary (PROCEED / HOLD / REJECT) is textually identical
to §4's AI one, and
:class:`~app.database.models.screening_evaluation.ScreeningRecommendation`
already carries exactly those three values with an ``is_valid`` that accepts all
three. Its ``AUTOMATED`` subset ({PROCEED, HOLD}) constrains only the automated
evaluation path (``screening_scoring.compute_recommendation``); it does not
apply here. A human **may** recommend REJECT — that is the whole point of §7 —
so this column validates against ``ScreeningRecommendation.ALL``. Inventing a
second, byte-identical vocabulary would create two places to keep in sync.

Note what this is NOT: a human REJECT recorded here is a *recommendation*, not
the final hiring decision (CLAUDE.md §11). The final decision is a later step
with its own record.

WHY ``competency_label`` IS FREE TEXT WITH NO TAXONOMY
------------------------------------------------------
CLAUDE.md §7 says "competency ratings" and stops there. This codebase has **no**
competency entity: ``RubricCriterion.category`` is uncontrolled free text and
``requirement_type`` ({MANDATORY, PREFERRED, EXPERIENCE, BEHAVIORAL, OTHER}) is
a requirement axis, not a competency taxonomy. Rather than invent one — or force
a mapping to ``rubric_criteria`` that would silently make an interviewer's
judgment look like a rubric verdict — the label is the interviewer's own words.
There is deliberately **no** ``rubric_criterion_id`` column here.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

FK delete behaviour — ``application_id`` / ``interview_guide_id`` /
``submitted_by_user_id`` are all **``ON DELETE RESTRICT``**: this is an
independent business/audit record that must not vanish as a side effect of
deleting a parent (the same convention as ``candidate_shortlist_entries`` and
``interview_guides``). ``interview_feedback_ratings.interview_feedback_id`` is
the one **``CASCADE``**, because a rating is a child *of* its feedback row and
has no meaning without it (the same child-of-aggregate rule as
``rubric_criteria`` -> ``rubric_versions``).

CHECK constraints — ``ck_interview_feedback_round_positive``,
``ck_interview_feedback_ratings_rating_range`` and
``ck_interview_feedback_ratings_label_not_blank`` are the **first DB-level CHECK
constraints in this codebase**; every prior bound (screening round 1|2, score
ranges) is validated in Python only. They are added here as a backstop, not a
replacement: the service still validates all three in Python and raises a clear
business error, so a caller never sees a raw ``IntegrityError``. The DB
constraint exists so a future code path that bypasses the service cannot store a
0-star rating or a blank competency.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
``notes`` and ``comment`` are an interviewer's free text about a real person.
They live ONLY on these rows: never in an audit event's ``action`` /
``previous_state`` / ``new_state`` / ``event_metadata``, never in a log line,
never in an exception message. ``__repr__`` deliberately omits them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base

#: Inclusive bounds of the competency rating scale. The single source of truth —
#: the service validates against these and the migration's CHECK repeats them.
RATING_MIN: int = 1
RATING_MAX: int = 5

#: Lowest valid interview round. Rounds are 1-based and unbounded above (an MVP
#: does not get to decide how many conversations a hiring loop needs).
MIN_INTERVIEW_ROUND: int = 1


class InterviewFeedback(Base):
    """One human interviewer's write-up of one interview round."""

    __tablename__ = "interview_feedback"

    __table_args__ = (
        # Round numbers are unique per application: round 2 accompanies round 1,
        # it never replaces it, and the same round is never recorded twice.
        UniqueConstraint(
            "application_id",
            "interview_round",
            name="uq_interview_feedback_application_round",
        ),
        CheckConstraint(
            "interview_round >= 1",
            name="ck_interview_feedback_round_positive",
        ),
        # Serves the "history for this application, newest first" read, which is
        # how both accessors and the UI order this table.
        Index(
            "ix_interview_feedback_application_created",
            "application_id",
            text("created_at DESC"),
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

    # The guide the interviewer worked from. Required: feedback is always
    # feedback *against* a generated guide. The service additionally enforces
    # that the guide belongs to this application.
    interview_guide_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_guides.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # The real internal user who recorded this. NOT NULL and RESTRICT — every
    # piece of human feedback has a human author, and accounts are disabled
    # rather than deleted (CLAUDE.md §23). The SYSTEM actor is rejected by the
    # service: an automated pipeline cannot have sat in an interview.
    submitted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Submitted by the interviewer; never derived from created_at or row order.
    interview_round: Mapped[int] = mapped_column(Integer, nullable=False)

    # The interviewer's own words. Never enters audit metadata or logs.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Validated against ScreeningRecommendation.ALL at the service layer —
    # PROCEED / HOLD / REJECT. A human may recommend REJECT (CLAUDE.md §7);
    # it is a recommendation, not the final decision (§11).
    recommendation: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No notes (interviewer free text about a real person).
        return (
            f"<InterviewFeedback id={self.id!r} "
            f"application={self.application_id!r} "
            f"round={self.interview_round} rec={self.recommendation!r}>"
        )


class InterviewFeedbackRating(Base):
    """One competency rating within one interview-feedback write-up."""

    __tablename__ = "interview_feedback_ratings"

    __table_args__ = (
        CheckConstraint(
            f"rating >= {RATING_MIN} AND rating <= {RATING_MAX}",
            name="ck_interview_feedback_ratings_rating_range",
        ),
        CheckConstraint(
            "length(btrim(competency_label)) > 0",
            name="ck_interview_feedback_ratings_label_not_blank",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # CASCADE: a rating is a child of its feedback row and has no independent
    # meaning (unlike every RESTRICT FK above, which points at an independent
    # business record).
    interview_feedback_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interview_feedback.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The interviewer's own wording. No controlled taxonomy, and deliberately
    # NOT linked to rubric_criteria — see the module docstring.
    competency_label: Mapped[str] = mapped_column(String(255), nullable=False)

    # 1-5 inclusive, validated in the service and by the CHECK above.
    rating: Mapped[int] = mapped_column(Integer, nullable=False)

    # Optional free text. Never enters audit metadata or logs.
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No competency_label / comment (interviewer free text).
        return (
            f"<InterviewFeedbackRating id={self.id!r} "
            f"feedback={self.interview_feedback_id!r} rating={self.rating}>"
        )
