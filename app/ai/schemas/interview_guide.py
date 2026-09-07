"""Pydantic schema for the interview-guide generation task
(CLAUDE.md §§3, 6, 19, 20; Phase 4 Step 7).

BOUNDARY THIS SCHEMA MUST HOLD
-----------------------------
The AI's job here is **question generation only**: given the candidate's full
evaluation record (rubric criteria, résumé evidence, prequalification, screening
transcript, screening evaluation), propose a set of human-interview questions —
each with the category it probes, the criterion it maps to (or ``None``), the
question text, what it evaluates, and why it was generated.

The AI does **not** decide, and this schema deliberately has **no field for**:
* any score, rating, confidence, or pass/fail — on a question or the candidate,
* an "overall assessment" or recommendation,
* the sequence index (Python assigns that — the AI is not trusted to order or
  count),
* anything about the eventual human interview outcome (a separate future step).

Same discipline as :mod:`app.ai.schemas.screening_questions` and
:mod:`app.ai.schemas.screening_evaluation`: the AI supplies only what only the
AI can determine; Python enforces every boundary (count bounds, category
vocabulary, criterion-id resolves to a criterion of the GUIDE's captured rubric
version).

``CategoryLiteral`` is a closed 5-value set, kept in lock-step with
:class:`app.database.models.interview_guide.InterviewQuestionCategory` by
``tests/test_interview_guide_schema.py``. There is deliberately no "TECHNICAL"
value — this codebase has no reliable technical / non-technical axis.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, field_validator

#: The five interview-question categories. No "TECHNICAL".
CategoryLiteral = Literal[
    "REQUIREMENTS", "EXPERIENCE", "BEHAVIORAL", "RESUME_VALIDATION", "PROBING"
]

#: Convenience tuple for Python-side code.
CATEGORY_VALUES: tuple[str, ...] = get_args(CategoryLiteral)

#: Post-generation total-count bounds enforced by the SERVICE (not the model —
#: an LLM cannot be trusted to count). Five categories vs. screening's 4-8 +
#: 0-3 per round: a usable human-interview guide needs at least one question in
#: each active area with room to probe, but must stay interview-length.
MIN_QUESTIONS = 6
MAX_QUESTIONS = 14


class InterviewQuestionDraft(BaseModel):
    """One AI-proposed interview question."""

    category: CategoryLiteral

    # UUID string of a rubric criterion this question maps to, or None for a
    # BEHAVIORAL / PROBING question with no single-criterion mapping. The service
    # REJECTS a value that does not resolve to a criterion of the guide's
    # CAPTURED rubric version — a hallucinated id is a validation failure, not
    # silently dropped.
    rubric_criterion_id: str | None = None

    # The question the interviewer will ask. Non-empty.
    question_text: str

    # What the interviewer should be checking with this question. Non-empty.
    evaluates: str

    # Brief: why this question was generated (which gap/unknown it probes, which
    # claim it validates, which strength it confirms). Non-empty. HR/interviewer
    # only — never in audit metadata.
    generated_reason: str

    @field_validator("question_text", "evaluates", "generated_reason")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned

    @field_validator("rubric_criterion_id")
    @classmethod
    def _blank_criterion_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class InterviewGuideAssessment(BaseModel):
    """The full set of interview questions the AI returned for one guide.

    Total-count bounds are enforced by the **service** (6-14), not here. An
    empty list is rejected at the schema level (an interview guide with no
    questions is a failed generation).
    """

    questions: list["InterviewQuestionDraft"]

    @field_validator("questions")
    @classmethod
    def _not_empty(
        cls, value: list["InterviewQuestionDraft"]
    ) -> list["InterviewQuestionDraft"]:
        if not value:
            raise ValueError(
                "questions must contain at least one entry; an empty guide is "
                "treated as a failed generation"
            )
        return value
