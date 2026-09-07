"""Exhaustive tests for the deterministic confidence function.

This function is Python-only (CLAUDE.md §§4, 20, 21, B): no AI call is involved
anywhere in this file. Every branch of the documented rule set is exercised
directly.
"""

from __future__ import annotations

import pytest

from app.ai.schemas.prequalification import CriterionAssessment
from app.services.prequalification_confidence import (
    MIN_EVIDENCE_CHARS,
    MIN_REASONING_CHARS,
    STRONG_EVIDENCE_CHARS,
    compute_confidence,
    compute_overall_confidence,
    confidence_for_assessment,
)

_LONG_EVID = "x" * (STRONG_EVIDENCE_CHARS + 5)
_MID_EVID = "x" * (MIN_EVIDENCE_CHARS + 5)
_SHORT_EVID = "x" * (MIN_EVIDENCE_CHARS - 1)
_LONG_REASON = "y" * (MIN_REASONING_CHARS + 5)
_SHORT_REASON = "y" * (MIN_REASONING_CHARS - 1)


# --- Rule 1: UNKNOWN is always LOW --------------------------------


@pytest.mark.parametrize("evidence", ["", _SHORT_EVID, _MID_EVID, _LONG_EVID])
@pytest.mark.parametrize("reasoning", ["", _SHORT_REASON, _LONG_REASON])
def test_unknown_is_always_low(evidence, reasoning):
    assert compute_confidence(
        result="UNKNOWN", evidence_summary=evidence, reasoning=reasoning
    ) == "LOW"


# --- Rule 2: PASS/FAIL with thin evidence is LOW ------------------


@pytest.mark.parametrize("result", ["PASS", "FAIL"])
def test_pass_fail_with_short_evidence_is_low(result):
    assert compute_confidence(
        result=result, evidence_summary=_SHORT_EVID, reasoning=_LONG_REASON
    ) == "LOW"


@pytest.mark.parametrize("result", ["PASS", "FAIL"])
def test_pass_fail_with_empty_evidence_is_low(result):
    assert compute_confidence(
        result=result, evidence_summary="", reasoning=_LONG_REASON
    ) == "LOW"


def test_evidence_exactly_at_min_is_not_low():
    # boundary: exactly MIN_EVIDENCE_CHARS passes rule 2
    assert compute_confidence(
        result="PASS",
        evidence_summary="x" * MIN_EVIDENCE_CHARS,
        reasoning=_SHORT_REASON,
    ) == "MEDIUM"


# --- Rule 3: HIGH needs strong evidence AND strong reasoning ------


@pytest.mark.parametrize("result", ["PASS", "FAIL"])
def test_strong_evidence_and_reasoning_is_high(result):
    assert compute_confidence(
        result=result, evidence_summary=_LONG_EVID, reasoning=_LONG_REASON
    ) == "HIGH"


@pytest.mark.parametrize("result", ["PASS", "FAIL"])
def test_strong_evidence_but_thin_reasoning_is_medium(result):
    assert compute_confidence(
        result=result, evidence_summary=_LONG_EVID, reasoning=_SHORT_REASON
    ) == "MEDIUM"


def test_high_boundary_exact_thresholds():
    assert compute_confidence(
        result="FAIL",
        evidence_summary="x" * STRONG_EVIDENCE_CHARS,
        reasoning="y" * MIN_REASONING_CHARS,
    ) == "HIGH"


# --- Rule 4: everything else is MEDIUM ---------------------------


@pytest.mark.parametrize("result", ["PASS", "FAIL"])
def test_mid_evidence_is_medium(result):
    assert compute_confidence(
        result=result, evidence_summary=_MID_EVID, reasoning=_LONG_REASON
    ) == "MEDIUM"


# --- wrapper ----------------------------------------------------


def test_confidence_for_assessment_wrapper():
    a = CriterionAssessment(
        criterion_index=1, result="PASS",
        evidence_summary=_LONG_EVID, reasoning=_LONG_REASON,
    )
    assert confidence_for_assessment(a) == "HIGH"

    b = CriterionAssessment(
        criterion_index=2, result="UNKNOWN",
        evidence_summary="nothing in the resume speaks to this either way",
        reasoning="no mention, so this stays open for screening follow-up",
    )
    assert confidence_for_assessment(b) == "LOW"


def test_return_value_is_always_a_known_level():
    for result in ("PASS", "FAIL", "UNKNOWN"):
        for ev in ("", "short", _MID_EVID, _LONG_EVID):
            for rs in ("", "short", _LONG_REASON):
                assert compute_confidence(
                    result=result, evidence_summary=ev, reasoning=rs
                ) in {"HIGH", "MEDIUM", "LOW"}


# ===================================================================
# compute_overall_confidence (Phase 4 Step 4) — additive; the tests above
# are unchanged and still exercise compute_confidence exactly as before.
# ===================================================================


def _crit(rt, result, conf):
    return {"requirement_type": rt, "result": result, "confidence": conf}


def test_overall_empty_list_is_low():
    assert compute_overall_confidence([]) == "LOW"


def test_overall_low_when_a_mandatory_is_unknown():
    rows = [
        _crit("MANDATORY", "PASS", "HIGH"),
        _crit("MANDATORY", "UNKNOWN", "LOW"),
        _crit("PREFERRED", "PASS", "HIGH"),
    ]
    assert compute_overall_confidence(rows) == "LOW"


def test_overall_low_when_over_30pct_unknown():
    # 2 of 5 = 40% UNKNOWN, none mandatory-unknown, confidences fine
    rows = [
        _crit("PREFERRED", "PASS", "HIGH"),
        _crit("PREFERRED", "PASS", "HIGH"),
        _crit("PREFERRED", "PASS", "HIGH"),
        _crit("BEHAVIORAL", "UNKNOWN", "MEDIUM"),
        _crit("BEHAVIORAL", "UNKNOWN", "MEDIUM"),
    ]
    assert compute_overall_confidence(rows) == "LOW"


def test_overall_exactly_30pct_unknown_is_not_low_by_that_rule():
    # 3 of 10 = exactly 30% -> NOT "> 30%", so this rule doesn't fire.
    rows = [_crit("PREFERRED", "PASS", "HIGH") for _ in range(7)] + [
        _crit("PREFERRED", "UNKNOWN", "MEDIUM") for _ in range(3)
    ]
    # No mandatory-unknown, no >half-LOW -> MEDIUM (more than one UNKNOWN blocks HIGH).
    assert compute_overall_confidence(rows) == "MEDIUM"


def test_overall_low_when_more_than_half_confidences_are_low():
    rows = [
        _crit("PREFERRED", "PASS", "LOW"),
        _crit("PREFERRED", "FAIL", "LOW"),
        _crit("PREFERRED", "PASS", "HIGH"),
    ]  # 2 of 3 LOW -> "> half"
    assert compute_overall_confidence(rows) == "LOW"


def test_overall_exactly_half_low_is_not_low_by_that_rule():
    rows = [
        _crit("PREFERRED", "PASS", "LOW"),
        _crit("PREFERRED", "PASS", "HIGH"),
    ]  # 1 of 2 LOW -> exactly half, not "> half"
    assert compute_overall_confidence(rows) == "MEDIUM"


def test_overall_high_all_mandatory_pass_at_most_one_unknown_no_low():
    rows = [
        _crit("MANDATORY", "PASS", "HIGH"),
        _crit("MANDATORY", "PASS", "MEDIUM"),
        _crit("PREFERRED", "PASS", "HIGH"),
        _crit("BEHAVIORAL", "UNKNOWN", "MEDIUM"),  # exactly one UNKNOWN
        _crit("EXPERIENCE", "PASS", "HIGH"),
    ]
    assert compute_overall_confidence(rows) == "HIGH"


def test_overall_not_high_when_two_unknowns():
    rows = [
        _crit("MANDATORY", "PASS", "HIGH"),
        _crit("PREFERRED", "UNKNOWN", "MEDIUM"),
        _crit("BEHAVIORAL", "UNKNOWN", "MEDIUM"),
    ]  # 2 UNKNOWN of 3 = 66% -> LOW by the >30% rule actually
    assert compute_overall_confidence(rows) == "LOW"


def test_overall_not_high_when_a_mandatory_fails():
    rows = [
        _crit("MANDATORY", "FAIL", "HIGH"),
        _crit("PREFERRED", "PASS", "HIGH"),
        _crit("BEHAVIORAL", "PASS", "HIGH"),
    ]  # no unknowns, no low conf, but mandatory FAIL blocks HIGH -> MEDIUM
    assert compute_overall_confidence(rows) == "MEDIUM"


def test_overall_medium_is_the_fallback():
    rows = [
        _crit("MANDATORY", "PASS", "HIGH"),
        _crit("PREFERRED", "PASS", "LOW"),   # one LOW (not > half of 3)
        _crit("BEHAVIORAL", "PASS", "HIGH"),
    ]  # not LOW, but a LOW conf present blocks HIGH -> MEDIUM
    assert compute_overall_confidence(rows) == "MEDIUM"


def test_overall_return_value_always_known_level():
    import itertools

    for combo in itertools.product(
        ["MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"],
        ["PASS", "FAIL", "UNKNOWN"],
        ["HIGH", "MEDIUM", "LOW"],
        repeat=1,
    ):
        rows = [_crit(*combo)]
        assert compute_overall_confidence(rows) in {"HIGH", "MEDIUM", "LOW"}
