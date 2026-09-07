"""Tests for app.ai.schemas.rubric_generation and its drift guard."""

from __future__ import annotations

import typing

import pytest
from pydantic import ValidationError

from app.ai.schemas.rubric_generation import (
    ProposedCriterion,
    RequirementTypeLiteral,
    RubricGenerationResult,
)
from app.database.models.job_requirement import RequirementType


def test_literal_matches_db_requirement_type_constants():
    literal_values = set(typing.get_args(RequirementTypeLiteral))
    assert literal_values == set(RequirementType.ALL)


def test_valid_result_parses(ai_response_dict):
    result = RubricGenerationResult.model_validate(
        ai_response_dict("rubric_generation_valid.json")
    )
    assert len(result.criteria) == 6
    assert result.criteria[0].requirement_type == "MANDATORY"
    assert result.criteria[-1].source_requirement_index is None


def test_empty_criteria_rejected(ai_response_dict):
    with pytest.raises(ValidationError):
        RubricGenerationResult.model_validate(
            ai_response_dict("rubric_generation_empty.json")
        )


def test_no_mandatory_rejected(ai_response_dict):
    with pytest.raises(ValidationError):
        RubricGenerationResult.model_validate(
            ai_response_dict("rubric_generation_no_mandatory.json")
        )


def test_bad_index_fixture_still_parses_at_schema_level(ai_response_dict):
    # Out-of-range source_requirement_index is tolerated by the schema; the
    # service resolves it to None. Only structural validity matters here.
    result = RubricGenerationResult.model_validate(
        ai_response_dict("rubric_generation_bad_index.json")
    )
    assert result.criteria[1].source_requirement_index == 99


def test_blank_criterion_text_rejected():
    with pytest.raises(ValidationError):
        ProposedCriterion(requirement_type="MANDATORY", criterion_text="   ")


def test_bad_requirement_type_rejected():
    with pytest.raises(ValidationError):
        ProposedCriterion(requirement_type="MUST_HAVE", criterion_text="x")


def test_category_blank_becomes_none():
    c = ProposedCriterion(
        requirement_type="MANDATORY", criterion_text="x", category="  "
    )
    assert c.category is None
