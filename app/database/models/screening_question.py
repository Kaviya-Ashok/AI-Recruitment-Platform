"""ScreeningQuestion model — one AI-generated screening question
(CLAUDE.md §§3, 6, 12, 19, 20, 23; Phase 4 Step 3).

One row = one question the screening AI generated for one round of one
:class:`~app.database.models.screening_session.ScreeningSession`. Questions are
**batch-generated per round** (4–8 for round 1, 0–3 follow-ups for round 2), not
produced turn-by-turn — this MVP has no live conversational loop (CLAUDE.md §3
"structured but conversational", not a chat agent).

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``screening_session_id`` — FK to ``screening_sessions.id``, **``ON DELETE
RESTRICT``**, indexed. Same non-cascading convention as every other AI-output
table (``resume_extractions`` / ``prequalification_results`` / ``screening_
sessions`` itself): a generated question is a business/audit record and must not
vanish as a side effect of deleting its parent.

``round`` — plain ``SmallInteger``, **1 or 2 only**, validated in the service
(:class:`ScreeningQuestionRound`). NOT a growing vocabulary — the two-round
design is fixed by this step's scope — so a bare int column with a code-level
check is the right weight, not a validated-string vocabulary and not a PG enum.

``sequence_index`` — 0-based order of this question **within its round**.
Rendering and the round-1-Q&A block sent into the round-2 prompt both use it.

``category`` — ``String(50)``, validated against
:class:`ScreeningQuestionCategory` (``JD`` / ``CV`` / ``BEHAVIORAL`` / ``GAP``),
same validated-string pattern as ``ApplicationStatus``. Mirrors CLAUDE.md §3's
question sources (JD / CV / Behavioral / UNKNOWN-GAPS).

``rubric_criterion_id`` — FK to ``rubric_criteria.id`` (the APPROVED rubric's
criteria — verified in Step 0; **not** ``job_requirements``), ``ON DELETE
RESTRICT``, indexed, **NULLABLE**. A ``BEHAVIORAL`` or broad ``GAP`` question
may not map to a single criterion. When present, the service has already
verified it belongs to the approved rubric version for this application's job —
a hallucinated id is rejected before persist (the "AI reasons, Python enforces
the boundary" rule from prequalification).

``question_text`` — ``Text``. The question shown to the candidate. Candidate
never sees ``generated_reason``, ``category``, or ``rubric_criterion_id``.

``generated_reason`` — ``Text``, NOT NULL. Why the AI generated this question
(which UNKNOWN/FAIL it probes, which claim it validates). Mirrors CLAUDE.md §6's
"why the question was generated" requirement for the human interview guide;
consumed by HR review and the later evaluation step. NEVER shown to the
candidate and NEVER put in audit metadata (it can quote resume/prequal text).

``ai_model`` — resolved model id that produced this question (§23 traceability).

No ``updated_at``: a question row is **write-once**. Re-generation is refused by
the service (``force=False`` convention); there is no in-place edit path.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ScreeningQuestionRound:
    """The two screening rounds. Fixed by design — not a growing vocabulary."""

    ROUND_1 = 1
    ROUND_2 = 2

    ALL: frozenset[int] = frozenset({ROUND_1, ROUND_2})

    @classmethod
    def is_valid(cls, value: int) -> bool:
        return value in cls.ALL


class ScreeningQuestionCategory:
    """What a screening question is probing (validated string column).

    Mirrors CLAUDE.md §3's four question sources:

    * ``JD``         — validates a job-requirement / rubric criterion directly.
    * ``CV``         — validates a candidate claim, project, or technology.
    * ``BEHAVIORAL`` — assesses an approved behavioural competency.
    * ``GAP``        — clarifies a missing or ambiguous piece of evidence
                       (an UNKNOWN prequalification result, typically).
    """

    JD = "JD"
    CV = "CV"
    BEHAVIORAL = "BEHAVIORAL"
    GAP = "GAP"

    ALL: frozenset[str] = frozenset({JD, CV, BEHAVIORAL, GAP})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class ScreeningQuestion(Base):
    """One AI-generated screening question for one round of one session."""

    __tablename__ = "screening_questions"

    __table_args__ = (
        # Stable ordering key: no two questions share a slot in a round.
        UniqueConstraint(
            "screening_session_id",
            "round",
            "sequence_index",
            name="uq_screening_questions_session_round_seq",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    screening_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("screening_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    round: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Validated against ScreeningQuestionCategory at the service layer.
    category: Mapped[str] = mapped_column(String(50), nullable=False)

    # NULL for a behavioural / broad question that maps to no single criterion.
    rubric_criterion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_criteria.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Why the AI asked this. HR/audit only — NEVER shown to the candidate,
    # NEVER in audit metadata (may quote resume / prequalification text).
    generated_reason: Mapped[str] = mapped_column(Text, nullable=False)

    ai_model: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No question_text / generated_reason (free text, may echo content).
        return (
            f"<ScreeningQuestion id={self.id!r} "
            f"session={self.screening_session_id!r} round={self.round} "
            f"seq={self.sequence_index} category={self.category!r}>"
        )
