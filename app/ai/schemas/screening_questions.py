"""Pydantic schema for the screening-question generation task
(CLAUDE.md §§3, 6, 19, 20; Phase 4 Step 3).

BOUNDARY THIS SCHEMA MUST HOLD
-----------------------------
The AI's job here is **question generation only**: given the approved rubric,
the candidate's resume evidence, and Claude's own prior per-criterion
prequalification output, propose a small batch of screening questions — each
with the category it probes, the criterion it maps to (or ``None``), the
question text, and a short reason it was generated.

The AI does **not** decide, and this schema deliberately has **no field for**:
* any score, rating, confidence, or pass/fail on a question or the candidate,
* an "overall assessment" of the candidate,
* the round number or sequence index (Python assigns those — the AI is not
  trusted to order or count),
* which round-1 answers were "good" (evaluation is a separate future step).

Same discipline as :mod:`app.ai.schemas.prequalification`: the AI supplies only
what only the AI can determine; Python enforces every boundary (round length
bounds, criterion-id resolves to a real rubric criterion, category vocabulary).

``CategoryLiteral`` is a closed 4-value set fixed by CLAUDE.md §3's question
sources — a constrained ``Literal`` is the right tool (as with
prequalification's ``ResultLiteral``). It is kept in lock-step with
:class:`app.database.models.screening_question.ScreeningQuestionCategory` by
``tests/test_screening_questions_schema.py``.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, field_validator

#: The four question categories, from CLAUDE.md §3 (JD / CV / Behavioral / GAP).
CategoryLiteral = Literal["JD", "CV", "BEHAVIORAL", "GAP"]

#: Convenience tuple for Python-side code.
CATEGORY_VALUES: tuple[str, ...] = get_args(CategoryLiteral)

#: Post-generation length bounds enforced by the service (NOT the model —
#: an LLM cannot be trusted to count). Round 1: a real batch. Round 2: 0-3
#: follow-ups, and zero is a legitimate, meaningful outcome (nothing worth
#: following up on).
ROUND_1_MIN_QUESTIONS = 4
ROUND_1_MAX_QUESTIONS = 8
ROUND_2_MIN_QUESTIONS = 0
ROUND_2_MAX_QUESTIONS = 3


class ScreeningQuestion(BaseModel):
    """One AI-proposed screening question."""

    category: CategoryLiteral

    # UUID string of a rubric criterion this question maps to, or None for a
    # behavioural / broad question. The service REJECTS a value that does not
    # resolve to a criterion of this application's approved rubric — a
    # hallucinated id is a validation failure, not silently dropped.
    criterion_id: str | None = None

    # The question shown to the candidate. Non-empty.
    question_text: str

    # Brief: why this question was generated (which UNKNOWN/FAIL it probes,
    # which claim it validates). Non-empty. HR/audit only — never shown to the
    # candidate, never in audit metadata.
    generated_reason: str

    @field_validator("question_text", "generated_reason")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned

    @field_validator("criterion_id")
    @classmethod
    def _blank_criterion_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ScreeningQuestionSet(BaseModel):
    """The full batch the AI returned for one round.

    Length bounds are enforced by the **service** per round (round 1: 4-8;
    round 2: 0-3), not here — the round number is not part of the AI's output.
    An empty list is valid at the schema level (it is the round-2 "no
    follow-ups" outcome); the service rejects an empty round-1 result.
    """

    questions: list["ScreeningQuestion"]
