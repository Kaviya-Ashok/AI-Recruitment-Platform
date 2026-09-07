"""Schema tests for app.ai.schemas.prequalification.

The schema must:
* accept a well-formed list of per-criterion assessments,
* constrain ``result`` to exactly {PASS, FAIL, UNKNOWN} at the schema level,
* reject empty ``assessments``, blank evidence/reasoning, non-positive index,
* have NO field for an AI-provided overall recommendation / score / confidence
  (that boundary is the whole point of this step).
"""

from __future__ import annotations

import typing

import pytest
from pydantic import BaseModel, ValidationError

from app.ai.schemas.prequalification import (
    RESULT_VALUES,
    CriterionAssessment,
    PrequalificationAssessment,
    ResultLiteral,
)

_FORBIDDEN_SUBSTRINGS = (
    "overall",
    "aggregate",
    "recommend",
    "score",
    "confidence",
    "eligib",
    "verdict",
    "decision",
    "weight",
    "rank",
)


def test_result_literal_is_exactly_the_three_values():
    assert set(typing.get_args(ResultLiteral)) == {"PASS", "FAIL", "UNKNOWN"}
    assert set(RESULT_VALUES) == {"PASS", "FAIL", "UNKNOWN"}


def _field_names(model: type[BaseModel]) -> set[str]:
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        ann = field.annotation
        for arg in getattr(ann, "__args__", ()):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                names |= _field_names(arg)
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            names |= _field_names(ann)
    return names


def test_no_aggregate_or_confidence_field_in_schema():
    names = _field_names(PrequalificationAssessment)
    assert {"assessments", "criterion_index", "result", "evidence_summary"} <= names
    for field_name in names:
        low = field_name.lower()
        for bad in _FORBIDDEN_SUBSTRINGS:
            assert bad not in low, (
                f"field {field_name!r} crosses the AI/Python boundary — "
                "confidence and any aggregate outcome are Python-side only"
            )


def test_valid_result_parses(ai_response_dict):
    result = PrequalificationAssessment.model_validate(
        ai_response_dict("prequalification_valid.json")
    )
    assert len(result.assessments) == 5
    assert result.assessments[0].result == "PASS"
    assert result.assessments[1].result == "UNKNOWN"


def test_bad_result_value_rejected(ai_response_dict):
    with pytest.raises(ValidationError):
        PrequalificationAssessment.model_validate(
            ai_response_dict("prequalification_bad_result_value.json")
        )


def test_empty_assessments_rejected():
    with pytest.raises(ValidationError):
        PrequalificationAssessment.model_validate({"assessments": []})


def test_blank_evidence_rejected():
    with pytest.raises(ValidationError):
        CriterionAssessment(
            criterion_index=1, result="PASS",
            evidence_summary="   ", reasoning="ok",
        )


def test_blank_reasoning_rejected():
    with pytest.raises(ValidationError):
        CriterionAssessment(
            criterion_index=1, result="UNKNOWN",
            evidence_summary="none found", reasoning="",
        )


def test_non_positive_index_rejected():
    with pytest.raises(ValidationError):
        CriterionAssessment(
            criterion_index=0, result="PASS",
            evidence_summary="x" * 50, reasoning="y" * 50,
        )


def test_text_is_stripped():
    a = CriterionAssessment(
        criterion_index=2, result="FAIL",
        evidence_summary="  contradicted by remote-only preference  ",
        reasoning="  clear contradiction  ",
    )
    assert a.evidence_summary == "contradicted by remote-only preference"
    assert a.reasoning == "clear contradiction"
