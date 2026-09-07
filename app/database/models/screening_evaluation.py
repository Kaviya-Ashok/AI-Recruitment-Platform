"""ScreeningEvaluation model — the per-candidate INITIAL SCORECARD
(CLAUDE.md §§4, 20, 21, B; Phase 4 Step 4).

One row = one run of the ``screening_evaluation`` AI task for one screening
session. It reconciles the resume-only prequalification verdicts against the
candidate's screening answers (per criterion), then Python computes the bucket
scores, coverage, aggregate confidence, and the PROCEED/HOLD recommendation.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``screening_session_id`` — FK to ``screening_sessions.id``, **``ON DELETE
RESTRICT``** and **UNIQUE**. One evaluation per session. Write-once /
replace-on-force, the same posture as ``prequalification_results`` (the service
returns the existing row rather than regenerating when ``force`` is not set).

``rubric_version_id`` — FK to ``rubric_versions.id``, **``ON DELETE RESTRICT``**,
indexed. Same traceability rule as ``prequalification_results``: an old
evaluation must stay interpretable against the exact criteria it ran on, even
if the job's rubric is later superseded (§23).

``results`` — **JSONB**, a list of per-criterion objects, **the same shape as
``PrequalificationResult.results``**: ``criterion_id`` / ``criterion_index`` /
``requirement_type`` / ``category`` / ``criterion_text`` / ``result`` /
``evidence_summary`` / ``reasoning`` / ``confidence``. The difference from
prequalification is only the content: for a criterion that a screening question
(with a submitted answer) targeted, ``result`` is the AI's *reconciled* verdict
over resume evidence + that answer; for every other criterion the
prequalification row is copied through **verbatim** (enforced in Python, not
just prompted — a criterion with no answered screening question can never be
promoted or demoted here). ``confidence`` is re-derived by
``prequalification_confidence.compute_confidence`` on the final reconciled
strings. There is no AI-provided score / recommendation anywhere in this list.

WHY "Requirements", NOT "Technical"
----------------------------------
CLAUDE.md §§4/10 historically listed a "Technical" scorecard bucket. This
codebase has no reliable technical/non-technical axis: ``requirement_type`` is
the only controlled dimension ({MANDATORY, PREFERRED, EXPERIENCE, BEHAVIORAL,
OTHER} — no TECHNICAL), and ``category`` is uncontrolled free text with
real-world near-synonyms already present ("Technical Skill" alongside
"Communication Skill", "Technologies", etc.). So the three score buckets are
built **only** from ``requirement_type``:

* ``Requirements`` = MANDATORY + PREFERRED + OTHER
* ``Experience``   = EXPERIENCE
* ``Behavioral``   = BEHAVIORAL

The scoring formula is documented verbatim in
``app/services/screening_scoring.py`` (CLAUDE.md §20 "document scoring formulas
clearly" — see that module).

SCORE vs COVERAGE, and why UNKNOWN is excluded
--------------------------------------------
Each ``*_score`` is 0–10, computed only over the PASS/FAIL criteria in the
bucket (PASS = 1.0, FAIL = 0.0). **UNKNOWN criteria are excluded from the score
sum entirely** — never counted as 0, never as 0.5 (CLAUDE.md §B: unknown stays
unknown; an insufficient-evidence criterion must not drag a score down as if it
were a failure). Instead, ``*_coverage`` (0.0–1.0) is the weight-fraction of the
bucket that was actually PASS/FAIL-scoreable, so HR can tell "8/10, fully
covered" apart from "8/10, but half the bucket was UNKNOWN". A bucket with zero
PASS/FAIL criteria (empty, or all UNKNOWN) has ``score = NULL`` **and**
``coverage = NULL`` — rendered as "Not assessed for this role", never 0.

``ai_recommendation`` — ``String(20)``, validated against
:class:`ScreeningRecommendation` ({PROCEED, HOLD, REJECT}) for schema
compatibility with CLAUDE.md §4. **This step's logic (see
``screening_scoring.compute_recommendation``) can only ever write PROCEED or
HOLD.** REJECT remains a value the column *accepts* but that no automated path
in this MVP produces — rejection is a human decision (§11).

``overall_confidence`` — ``String(10)``, HIGH/MEDIUM/LOW, from
``prequalification_confidence.compute_overall_confidence`` (documented there).

``strengths`` / ``gaps`` / ``unknowns`` — **JSONB ``list[str]``, Python-
assembled** from the final reconciled per-criterion results
(``f"{criterion_text}: {evidence_summary}"`` for each PASS / FAIL / UNKNOWN
respectively). The AI schema deliberately has no free-text field for these —
they must stay a faithful projection of the per-criterion list, not an
independent narrative.

``ai_model`` — resolved model id (§23 traceability).

No ``updated_at``: a row is write-once (replace = delete + insert under
``force=True``, matching ``prequalification_results``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ScreeningRecommendation:
    """The three recommendation values from CLAUDE.md §4 (validated string).

    The column accepts all three so the schema matches the spec and a future
    step could record a human REJECT here. **The automated evaluation logic in
    this MVP only ever writes PROCEED or HOLD** — see
    ``app/services/screening_scoring.py`` and its REJECT-unreachability test.
    """

    PROCEED = "PROCEED"
    HOLD = "HOLD"
    REJECT = "REJECT"

    ALL: frozenset[str] = frozenset({PROCEED, HOLD, REJECT})

    #: The subset the automated path is allowed to produce.
    AUTOMATED: frozenset[str] = frozenset({PROCEED, HOLD})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class ScreeningEvaluationBucket:
    """The three score buckets — built from ``requirement_type`` only."""

    REQUIREMENTS = "Requirements"   # MANDATORY + PREFERRED + OTHER
    EXPERIENCE = "Experience"       # EXPERIENCE
    BEHAVIORAL = "Behavioral"       # BEHAVIORAL

    ALL: frozenset[str] = frozenset({REQUIREMENTS, EXPERIENCE, BEHAVIORAL})


class ScreeningEvaluation(Base):
    """One per-candidate screening evaluation / initial scorecard."""

    __tablename__ = "screening_evaluations"

    __table_args__ = (
        UniqueConstraint(
            "screening_session_id", name="uq_screening_evaluations_session"
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

    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubric_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Per-criterion reconciled results — same shape as
    # PrequalificationResult.results. Validated before write.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    # Bucket scores 0-10 / coverage 0.0-1.0. NULL together when the bucket has
    # no PASS/FAIL criterion (empty or all-UNKNOWN).
    requirements_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requirements_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    experience_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    experience_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    behavioral_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    behavioral_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Python-assembled projections of the per-criterion list — never AI free text.
    strengths: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    gaps: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    unknowns: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    overall_confidence: Mapped[str] = mapped_column(String(10), nullable=False)

    # Validated against ScreeningRecommendation. Automated path: PROCEED/HOLD only.
    ai_recommendation: Mapped[str] = mapped_column(String(20), nullable=False)

    ai_model: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # No criterion / evidence text (may echo candidate content).
        return (
            f"<ScreeningEvaluation id={self.id!r} "
            f"session={self.screening_session_id!r} "
            f"rec={self.ai_recommendation!r} conf={self.overall_confidence!r}>"
        )
