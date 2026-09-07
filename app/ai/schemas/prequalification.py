"""Pydantic schema for the prequalification AI task (CLAUDE.md §§4, 20, B).

BOUNDARY THIS SCHEMA MUST HOLD
-----------------------------
The AI's job here is **per-criterion evidence matching only**: look at one
approved rubric criterion next to the extracted resume evidence and produce a
reasoned ``PASS`` / ``FAIL`` / ``UNKNOWN`` with supporting text. That is
genuine reasoning ("3 years Python + Kafka + Postgres" vs "PySpark required" is
not a keyword search).

The AI does **not** decide, and this schema deliberately has **no field for**:
* an overall / aggregate prequalification outcome or recommendation,
* an overall score,
* confidence (HIGH/MEDIUM/LOW) — that is computed in Python from these outputs
  by ``app.services.prequalification_confidence`` per a documented rule set,
* mandatory-vs-preferred weighting or any "compensation" between criteria.

If a future edit is tempted to add such a field here, that is the boundary this
step exists to protect — don't.

``ResultLiteral`` is a closed 3-value set fixed by CLAUDE.md itself (not a
growing business vocabulary), so a constrained ``Literal`` is the right tool —
this is the first AI schema where that applies. ``tests/test_prequalification_
schema.py`` pins the exact value set.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, field_validator

#: The only three per-criterion results, defined by CLAUDE.md §4/§B.
ResultLiteral = Literal["PASS", "FAIL", "UNKNOWN"]

#: Convenience tuple for Python-side code (confidence logic, service validation).
RESULT_VALUES: tuple[str, ...] = get_args(ResultLiteral)


class CriterionAssessment(BaseModel):
    """The AI's judgment for ONE rubric criterion against the resume evidence."""

    # 1-based index into the numbered criteria list AS SENT in the prompt, in
    # the exact order sent. NOT a UUID (LLMs echo small ints far more reliably —
    # same rationale as rubric_generation's source_requirement_index). The
    # service resolves this back to RubricCriterion.id.
    criterion_index: int

    result: ResultLiteral

    # What evidence was found, or why it is insufficient. Must reference actual
    # extracted evidence — not a bare restatement of the verdict. Non-empty.
    evidence_summary: str

    # Brief: WHY this result follows from that evidence (not just what). Non-empty.
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


class PrequalificationAssessment(BaseModel):
    """The full set of per-criterion assessments the AI returned for one resume.

    One entry per rubric criterion. NO aggregate/overall fields — see the module
    docstring. The service additionally enforces exactly-one-per-criterion
    coverage before persisting.
    """

    assessments: list[CriterionAssessment]

    @field_validator("assessments")
    @classmethod
    def _not_empty(cls, value: list[CriterionAssessment]) -> list[CriterionAssessment]:
        if not value:
            raise ValueError(
                "assessments must contain at least one entry; an empty result "
                "is treated as a failed prequalification"
            )
        return value
