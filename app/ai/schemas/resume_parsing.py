"""Pydantic schema for the resume-parsing AI task (CLAUDE.md §3).

This schema captures an **evidence inventory** — a structured record of what the
resume *states*. It is deliberately NOT an assessment:

* No PASS / FAIL / UNKNOWN fields. Comparing evidence against the approved
  rubric is prequalification's job (the next step), not this one.
* No scores, ratings, confidence, or recommendations.
* No "gaps" / "missing" fields — absence of evidence is represented simply by a
  shorter (or empty) list, never by a judgement.

Every list may be empty. A resume with zero certifications, zero projects, or
even almost nothing extractable is a valid input, not a failed extraction — so
(unlike ``rubric_generation``) there is **no** "must contain at least one X"
rule anywhere in this schema. The service layer likewise adds no business-rule
validation on top; see ``resume_parsing_service`` for the reasoning.

Field validators here only normalise (strip whitespace, drop empty strings,
collapse "" -> None). They never reject content for being sparse.

No ``Literal`` / enum is used, so there is no DB-constants drift-guard test to
keep (contrast ``jd_analysis`` / ``rubric_generation``).
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


def _clean_optional(value: str | None) -> str | None:
    """"" / whitespace-only / None -> None; otherwise the stripped string."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    cleaned = value.strip()
    return cleaned or None


def _clean_str_list(value) -> list[str]:
    """Strip each entry, drop blanks, de-duplicate case-insensitively (order kept)."""
    if not value:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


class ExperienceEntry(BaseModel):
    """One employment / work-experience item, exactly as the resume states it."""

    role: str | None = None
    organization: str | None = None
    # Dates/duration reproduced verbatim from the resume ("Jan 2020 - Present",
    # "2018-2021", "3 years"). NOT normalised, NOT computed into a number.
    dates: str | None = None
    description: str | None = None

    @field_validator("role", "organization", "dates", "description")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_optional(value)


class ProjectEntry(BaseModel):
    """One project the resume describes."""

    name: str | None = None
    description: str | None = None
    technologies: list[str] = []

    @field_validator("name", "description")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_optional(value)

    @field_validator("technologies", mode="before")
    @classmethod
    def _clean_techs(cls, value) -> list[str]:
        return _clean_str_list(value)


class EducationEntry(BaseModel):
    """One education item, as stated (no ranking of institutions/qualifications)."""

    qualification: str | None = None
    institution: str | None = None
    dates: str | None = None

    @field_validator("qualification", "institution", "dates")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_optional(value)


class CertificationEntry(BaseModel):
    """One certification / licence / credential the resume lists."""

    name: str | None = None
    issuer: str | None = None
    date: str | None = None

    @field_validator("name", "issuer", "date")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_optional(value)


class ResumeExtractionResult(BaseModel):
    """The full evidence inventory extracted from one resume document.

    All fields are lists; all may be empty. Nothing here encodes a judgement.
    """

    skills: list[str] = []
    technologies: list[str] = []
    experience: list[ExperienceEntry] = []
    projects: list[ProjectEntry] = []
    certifications: list[CertificationEntry] = []
    education: list[EducationEntry] = []
    # Catch-all for job-relevant factual claims that do not fit the buckets above
    # (publications, patents, awards, open-source maintainership, security
    # clearances explicitly stated, etc.). Still evidence, never assessment.
    other_relevant_claims: list[str] = []

    @field_validator(
        "skills", "technologies", "other_relevant_claims", mode="before"
    )
    @classmethod
    def _clean_string_lists(cls, value) -> list[str]:
        return _clean_str_list(value)

    @field_validator(
        "experience", "projects", "certifications", "education", mode="before"
    )
    @classmethod
    def _coerce_none_list(cls, value):
        return value or []
