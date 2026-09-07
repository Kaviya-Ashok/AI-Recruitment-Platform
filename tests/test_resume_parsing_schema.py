"""Schema tests for app.ai.schemas.resume_parsing.

The evidence-inventory schema must:
* accept a fully-populated inventory,
* accept an (almost) empty one — every list may be zero-length,
* normalise whitespace / drop blanks / de-dupe string lists,
* contain NO field into which an assessment (score, rating, pass/fail,
  recommendation, confidence, verdict) could be written — that is the
  structural half of prompt-injection resistance for this task.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from app.ai.schemas.resume_parsing import (
    CertificationEntry,
    EducationEntry,
    ExperienceEntry,
    ProjectEntry,
    ResumeExtractionResult,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ai_responses"

_FORBIDDEN_SUBSTRINGS = (
    "score",
    "rating",
    "rank",
    "recommend",
    "verdict",
    "pass",
    "fail",
    "confidence",
    "assessment",
    "decision",
    "eligib",
    "qualif_result",
)


def _all_field_names(model: type[BaseModel]) -> set[str]:
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        ann = field.annotation
        # unwrap list[...] / Optional[...]
        for arg in getattr(ann, "__args__", ()):  # noqa: SLF001 - typing introspection
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names |= _all_field_names(arg)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            names |= _all_field_names(ann)
    return names


def test_no_assessment_field_anywhere_in_schema():
    names = _all_field_names(ResumeExtractionResult)
    # sanity: we actually walked the nested models
    assert {"role", "organization", "qualification", "technologies"} <= names
    for field_name in names:
        lowered = field_name.lower()
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad not in lowered, (
                f"field {field_name!r} looks like an assessment field; the "
                "resume-parsing schema must hold evidence only"
            )


def test_empty_inventory_is_valid():
    result = ResumeExtractionResult.model_validate({})
    assert result.skills == []
    assert result.experience == []
    assert result.certifications == []
    assert result.other_relevant_claims == []


def test_zero_length_sublists_accepted_explicitly():
    result = ResumeExtractionResult.model_validate(
        {
            "skills": [],
            "technologies": [],
            "experience": [],
            "projects": [],
            "certifications": [],
            "education": [],
            "other_relevant_claims": [],
        }
    )
    assert result.model_dump() == {
        "skills": [],
        "technologies": [],
        "experience": [],
        "projects": [],
        "certifications": [],
        "education": [],
        "other_relevant_claims": [],
    }


def test_string_lists_are_stripped_deduped_and_blanks_dropped():
    result = ResumeExtractionResult.model_validate(
        {"skills": ["  Python ", "python", "", "   ", "SQL", "Python"]}
    )
    assert result.skills == ["Python", "SQL"]


def test_optional_subfields_collapse_blank_to_none():
    entry = ExperienceEntry.model_validate(
        {"role": "  Engineer ", "organization": "   ", "dates": "", "description": None}
    )
    assert entry.role == "Engineer"
    assert entry.organization is None
    assert entry.dates is None
    assert entry.description is None


def test_project_technologies_normalised():
    entry = ProjectEntry.model_validate(
        {"name": "X", "technologies": [" Spark ", "spark", "Kafka"]}
    )
    assert entry.technologies == ["Spark", "Kafka"]


def test_valid_fixture_round_trips():
    payload = json.loads((_FIXTURES / "resume_parsing_valid.json").read_text())
    result = ResumeExtractionResult.model_validate(payload)
    assert len(result.experience) == 2
    assert result.experience[0].dates == "Jan 2021 - Present"  # verbatim, not normalised
    assert "Apache Spark" in result.technologies
    assert len(result.certifications) == 1
    assert isinstance(result.certifications[0], CertificationEntry)


def test_sparse_fixture_round_trips():
    payload = json.loads((_FIXTURES / "resume_parsing_sparse.json").read_text())
    result = ResumeExtractionResult.model_validate(payload)
    assert result.technologies == []
    assert result.certifications == []
    assert result.education == []
    assert len(result.experience) == 1
    assert isinstance(result.education, list)
    assert isinstance(result.experience[0], ExperienceEntry)
    _ = EducationEntry  # imported for the introspection helper above
