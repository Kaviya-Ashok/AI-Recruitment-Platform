"""Pydantic schema for the rubric-generation AI task (Phase 1.3).

Schema validation only. Business rules that must also be enforced *later*
(e.g. re-checking "at least one MANDATORY criterion" at approval time, because
HR can delete criteria after generation) live in ``rubric_service``.

Why "at least one MANDATORY" IS enforced here (unlike ``jd_analysis``)
--------------------------------------------------------------------
A rubric with zero mandatory criteria cannot support CLAUDE.md §B's
"mandatory must not be diluted by preferred" principle — there would be nothing
mandatory to protect. So an AI rubric proposal with no MANDATORY criterion is
an unusable output that should trigger a retry / error, not be silently
accepted. (For ``jd_analysis`` the equivalent rule was deliberately omitted
because a sparse JD can legitimately yield only preferred/experience items.)

Drift note
----------
``RequirementTypeLiteral`` duplicates
``app.database.models.job_requirement.RequirementType`` for the same
import-layering reason documented in ``jd_analysis.py``. Kept honest by
``tests/test_rubric_generation_schema.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

RequirementTypeLiteral = Literal[
    "MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"
]


class ProposedCriterion(BaseModel):
    """One rubric criterion proposed by Claude from the job requirements."""

    requirement_type: RequirementTypeLiteral
    category: str | None = None
    criterion_text: str
    # 1-based index into the job_requirements list AS SENT to the prompt, in the
    # exact order sent. NOT a UUID (LLMs echo small ints far more reliably).
    # None if the criterion doesn't map cleanly to a single source requirement
    # (consolidated / AI-synthesised). Out-of-range values are tolerated and
    # resolved to None by the service — see rubric_service.
    source_requirement_index: int | None = None

    @field_validator("criterion_text")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("criterion_text must be non-empty")
        return cleaned

    @field_validator("category")
    @classmethod
    def _clean_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class RubricGenerationResult(BaseModel):
    """The full structured result of proposing a rubric for one job."""

    criteria: list[ProposedCriterion]

    @field_validator("criteria")
    @classmethod
    def _criteria_not_empty(
        cls, value: list[ProposedCriterion]
    ) -> list[ProposedCriterion]:
        if not value:
            raise ValueError(
                "criteria must contain at least one entry; an empty rubric is "
                "treated as a failed generation"
            )
        return value

    @model_validator(mode="after")
    def _has_mandatory(self) -> "RubricGenerationResult":
        if not any(c.requirement_type == "MANDATORY" for c in self.criteria):
            raise ValueError(
                "a proposed rubric must contain at least one MANDATORY criterion"
            )
        return self
