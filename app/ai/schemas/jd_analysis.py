"""Pydantic schema for the JD-analysis AI task (Phase 1.2).

Schema validation only. Business rules (e.g. "at least one MANDATORY
requirement") live in ``job_service``.

Drift note
----------
``RequirementTypeLiteral`` below duplicates the values of
``app.database.models.job_requirement.RequirementType``. It is NOT imported from
there on purpose: that module imports ``app.database.database.Base``, which
builds the SQLAlchemy engine and needs a live ``DATABASE_URL`` — too heavy a
dependency for a plain schema, and it would couple ``app/ai`` to ``app/database``.
The duplication risk is covered by ``tests/test_jd_analysis_schema.py``, which
asserts the two sets stay identical.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

RequirementTypeLiteral = Literal[
    "MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"
]


class ExtractedRequirement(BaseModel):
    """One requirement Claude extracted from the JD."""

    requirement_type: RequirementTypeLiteral
    category: str | None = None
    requirement_text: str

    @field_validator("requirement_text")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("requirement_text must be non-empty")
        return cleaned

    @field_validator("category")
    @classmethod
    def _clean_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class JdAnalysisResult(BaseModel):
    """The full structured result of analysing one JD."""

    requirements: list[ExtractedRequirement]

    @field_validator("requirements")
    @classmethod
    def _requirements_not_empty(
        cls, value: list[ExtractedRequirement]
    ) -> list[ExtractedRequirement]:
        # Zero requirements is an unusable result, not a valid "UNKNOWN" —
        # surface it as an error so we never persist a job with no rubric input.
        if not value:
            raise ValueError(
                "requirements must contain at least one entry; an empty list "
                "is treated as a failed analysis"
            )
        return value
