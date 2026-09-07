"""Pydantic schema for the screening-evaluation AI task
(CLAUDE.md §§4, 20, B; Phase 4 Step 4).

BOUNDARY THIS SCHEMA MUST HOLD
-----------------------------
The AI's job here is **per-criterion reconciliation only**: for each approved
rubric criterion, look at (a) the resume-only prequalification verdict and
(b) any screening question that targeted this criterion plus the candidate's
answer, and return one reasoned ``PASS`` / ``FAIL`` / ``UNKNOWN`` with
supporting text that accounts for both sources.

The AI does **not** decide, and this schema deliberately has **no field for**:
* a per-criterion or overall score,
* confidence (HIGH/MEDIUM/LOW) — Python re-derives it from these outputs via
  ``prequalification_confidence.compute_confidence``,
* an overall recommendation (PROCEED/HOLD/REJECT) — Python computes it
  deterministically (``screening_scoring.compute_recommendation``),
* strengths / gaps / unknowns free text — Python assembles those as a faithful
  projection of the per-criterion list.

This mirrors ``PrequalificationAssessment`` exactly — same fields, same
"exactly one assessment per criterion, no silent back-fill, duplicate index ->
error" enforcement in the service (``screening_evaluation_service``). The
per-criterion shape is intentionally identical so a reviewer sees the two
stages produce the same kind of artefact.

``ResultLiteral`` is the closed 3-value set from CLAUDE.md §4/§B, imported from
the prequalification schema so the two never drift.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from app.ai.schemas.prequalification import ResultLiteral


class CriterionEvaluation(BaseModel):
    """The AI's reconciled judgment for ONE rubric criterion."""

    # 1-based index into the numbered criteria list AS SENT in the prompt, in
    # the exact order sent (same rationale as CriterionAssessment — LLMs echo
    # small ints reliably). The service resolves it back to RubricCriterion.id.
    criterion_index: int

    result: ResultLiteral

    # What evidence (resume + screening answer) supports this verdict, or why it
    # is still insufficient. Must reference actual evidence, not restate the
    # verdict. Non-empty.
    evidence_summary: str

    # Brief: WHY this result follows, and — when a screening answer was
    # available — how it changed (or did not change) the prequalification
    # verdict. Non-empty.
    reasoning: str

    @field_validator("criterion_index")
    @classmethod
    def _index_is_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("criterion_index must be a 1-based positive integer")
        return value

    @field_validator("evidence_summary", "reasoning")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned


class ScreeningEvaluationAssessment(BaseModel):
    """The full set of per-criterion reconciled evaluations the AI returned.

    One entry per rubric criterion. NO aggregate / score / confidence /
    recommendation fields — see the module docstring. The service enforces
    exactly-one-per-criterion coverage before persisting.
    """

    assessments: list["CriterionEvaluation"]

    @field_validator("assessments")
    @classmethod
    def _not_empty(cls, value: list["CriterionEvaluation"]) -> list["CriterionEvaluation"]:
        if not value:
            raise ValueError(
                "assessments must contain at least one entry; an empty result "
                "is treated as a failed evaluation"
            )
        return value
