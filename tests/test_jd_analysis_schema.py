"""Tests for app.ai.schemas.jd_analysis and its drift guard."""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from app.ai.schemas.jd_analysis import (
    ExtractedRequirement,
    JdAnalysisResult,
    RequirementTypeLiteral,
)
from app.database.models.job_requirement import RequirementType


def test_literal_matches_db_requirement_type_constants():
    """The duplicated Literal must not drift from the DB-side constants class."""
    literal_values = set(typing.get_args(RequirementTypeLiteral))
    assert literal_values == set(RequirementType.ALL)


def test_valid_result_parses(ai_response_dict):
    result = JdAnalysisResult.model_validate(ai_response_dict("jd_analysis_valid.json"))
    assert len(result.requirements) == 6
    assert result.requirements[0].requirement_type == "MANDATORY"


def test_empty_requirements_rejected(ai_response_dict):
    with pytest.raises(ValidationError):
        JdAnalysisResult.model_validate(ai_response_dict("jd_analysis_empty.json"))


def test_invalid_schema_fixture_rejected(ai_response_dict):
    with pytest.raises(ValidationError):
        JdAnalysisResult.model_validate(
            ai_response_dict("jd_analysis_invalid_schema.json")
        )


def test_blank_requirement_text_rejected():
    with pytest.raises(ValidationError):
        ExtractedRequirement(requirement_type="MANDATORY", requirement_text="   ")


def test_bad_requirement_type_rejected():
    with pytest.raises(ValidationError):
        ExtractedRequirement(requirement_type="NICE_TO_HAVE", requirement_text="x")


def test_category_blank_becomes_none():
    req = ExtractedRequirement(
        requirement_type="OTHER", requirement_text="x", category="   "
    )
    assert req.category is None
