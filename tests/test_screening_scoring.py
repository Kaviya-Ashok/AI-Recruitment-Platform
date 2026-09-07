"""Tests for app.services.screening_scoring — deterministic, Python-only
(CLAUDE.md §§4, 20, B; Phase 4 Step 4). No AI, no DB.

Every number here is hand-computed in the test against the formula documented
in the module docstring.
"""

from __future__ import annotations

import itertools

import pytest

from app.database.models.screening_evaluation import ScreeningRecommendation
from app.services.screening_scoring import (
    assemble_strengths_gaps_unknowns,
    bucket_of,
    compute_bucket_scores,
    compute_recommendation,
    weight_of,
)


def _c(rt, result, *, text="crit", evidence="ev"):
    return {
        "requirement_type": rt,
        "result": result,
        "criterion_text": text,
        "evidence_summary": evidence,
    }


# --- weights + buckets ------------------------------------------


def test_weights_exactly_as_specified():
    assert weight_of("MANDATORY") == 2
    assert weight_of("PREFERRED") == 1
    assert weight_of("EXPERIENCE") == 1
    assert weight_of("BEHAVIORAL") == 1
    assert weight_of("OTHER") == 1


def test_bucket_assignment_by_requirement_type_only():
    assert bucket_of("MANDATORY") == "Requirements"
    assert bucket_of("PREFERRED") == "Requirements"
    assert bucket_of("OTHER") == "Requirements"
    assert bucket_of("EXPERIENCE") == "Experience"
    assert bucket_of("BEHAVIORAL") == "Behavioral"


# --- score + coverage: hand-computed ---------------------------


def test_all_pass_bucket_is_ten_full_coverage():
    rows = [_c("MANDATORY", "PASS"), _c("PREFERRED", "PASS"), _c("OTHER", "PASS")]
    b = compute_bucket_scores(rows)
    # weighted points = 2*1 + 1*1 + 1*1 = 4; weight = 4; 10*4/4 = 10
    assert b["requirements_score"] == 10
    assert b["requirements_coverage"] == 1.0
    assert b["experience_score"] is None and b["experience_coverage"] is None
    assert b["behavioral_score"] is None and b["behavioral_coverage"] is None


def test_all_fail_bucket_is_zero_full_coverage():
    rows = [_c("EXPERIENCE", "FAIL"), _c("EXPERIENCE", "FAIL")]
    b = compute_bucket_scores(rows)
    assert b["experience_score"] == 0
    assert b["experience_coverage"] == 1.0


def test_unknown_is_excluded_from_score_sum_not_counted_as_zero():
    # 1 PASS (MANDATORY w=2) + 1 UNKNOWN (MANDATORY w=2).
    # If UNKNOWN counted as 0: 10 * (2*1) / (2+2) = 5.
    # Correct (UNKNOWN excluded): 10 * (2*1) / 2 = 10.
    rows = [_c("MANDATORY", "PASS"), _c("MANDATORY", "UNKNOWN")]
    b = compute_bucket_scores(rows)
    assert b["requirements_score"] == 10
    # coverage = scored weight (2) / total weight incl. UNKNOWN (4) = 0.5
    assert b["requirements_coverage"] == 0.5


def test_empty_bucket_is_null_score_and_null_coverage():
    b = compute_bucket_scores([_c("MANDATORY", "PASS")])  # only Requirements
    assert b["experience_score"] is None
    assert b["experience_coverage"] is None
    assert b["behavioral_score"] is None
    assert b["behavioral_coverage"] is None


def test_all_unknown_bucket_is_null_score_and_null_coverage():
    rows = [_c("BEHAVIORAL", "UNKNOWN"), _c("BEHAVIORAL", "UNKNOWN")]
    b = compute_bucket_scores(rows)
    assert b["behavioral_score"] is None
    assert b["behavioral_coverage"] is None


def test_mandatory_weight_2_actually_changes_the_result():
    # Requirements bucket: MANDATORY FAIL, PREFERRED PASS.
    # weight 2 for MANDATORY: 10 * (2*0 + 1*1) / (2+1) = 10/3 = 3.33 -> round 3
    weighted = compute_bucket_scores(
        [_c("MANDATORY", "FAIL"), _c("PREFERRED", "PASS")]
    )["requirements_score"]
    assert weighted == 3
    # If both weighted 1 (hypothetical): 10 * (0 + 1) / 2 = 5. Prove the
    # weighting matters by scoring the same results as two PREFERRED:
    equal = compute_bucket_scores(
        [_c("PREFERRED", "FAIL"), _c("PREFERRED", "PASS")]
    )["requirements_score"]
    assert equal == 5
    assert weighted != equal


def test_coverage_distinguishes_thin_from_full_evidence():
    thin = compute_bucket_scores(
        [_c("PREFERRED", "PASS")] + [_c("PREFERRED", "UNKNOWN")] * 3
    )
    assert thin["requirements_score"] == 10          # the one PASS
    assert thin["requirements_coverage"] == pytest.approx(0.25)  # 1 of 4 weight


def test_score_is_clamped_to_0_10():
    # Can't naturally exceed, but the clamp is documented — round of 10.0 stays 10.
    b = compute_bucket_scores([_c("MANDATORY", "PASS")])
    assert 0 <= b["requirements_score"] <= 10


# --- recommendation: all four branches ------------------------


def test_recommendation_rule_1_mandatory_fail_is_hold():
    rows = [_c("MANDATORY", "FAIL"), _c("MANDATORY", "PASS")]
    assert compute_recommendation(rows, "HIGH") == "HOLD"


def test_recommendation_rule_2_mandatory_unknown_is_hold():
    rows = [_c("MANDATORY", "UNKNOWN"), _c("PREFERRED", "PASS")]
    assert compute_recommendation(rows, "HIGH") == "HOLD"


def test_recommendation_rule_3_low_overall_confidence_is_hold():
    rows = [_c("MANDATORY", "PASS"), _c("PREFERRED", "PASS")]
    assert compute_recommendation(rows, "LOW") == "HOLD"


def test_recommendation_rule_4_all_clear_is_proceed():
    rows = [_c("MANDATORY", "PASS"), _c("PREFERRED", "PASS"),
            _c("BEHAVIORAL", "PASS")]
    assert compute_recommendation(rows, "HIGH") == "PROCEED"
    assert compute_recommendation(rows, "MEDIUM") == "PROCEED"


def test_recommendation_no_mandatory_criteria_and_high_conf_is_proceed():
    rows = [_c("PREFERRED", "PASS"), _c("BEHAVIORAL", "FAIL")]
    assert compute_recommendation(rows, "HIGH") == "PROCEED"


# --- REJECT is unreachable -----------------------------------


def test_reject_is_unreachable_exhaustive():
    """Fuzz across a representative combination of criterion sets + confidence
    levels; the recommendation is always PROCEED or HOLD, never REJECT."""
    types = ["MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"]
    results = ["PASS", "FAIL", "UNKNOWN"]
    confs = ["HIGH", "MEDIUM", "LOW"]

    # every 1- and 2-criterion combination
    singles = [[_c(t, r)] for t in types for r in results]
    pairs = [
        [_c(t1, r1), _c(t2, r2)]
        for t1, r1 in itertools.product(types, results)
        for t2, r2 in itertools.product(types, results)
    ]
    # a few larger hand-picked sets
    larger = [
        [_c("MANDATORY", "PASS"), _c("MANDATORY", "FAIL"),
         _c("PREFERRED", "UNKNOWN"), _c("BEHAVIORAL", "PASS")],
        [_c("MANDATORY", "UNKNOWN")] * 3,
        [_c("OTHER", "FAIL")] * 5,
        [],
    ]

    for rows in singles + pairs + larger:
        for conf in confs:
            rec = compute_recommendation(rows, conf)
            assert rec in ScreeningRecommendation.AUTOMATED
            assert rec != "REJECT"


def test_empty_criteria_all_pass_case_never_rejects():
    assert compute_recommendation([], "HIGH") == "PROCEED"
    assert compute_recommendation(
        [_c("MANDATORY", "PASS")] * 4, "HIGH"
    ) == "PROCEED"


# --- strengths / gaps / unknowns -----------------------------


def test_sgu_projection_format_and_partition():
    rows = [
        _c("MANDATORY", "PASS", text="Python", evidence="6 yrs"),
        _c("PREFERRED", "FAIL", text="Kafka", evidence="not used"),
        _c("BEHAVIORAL", "UNKNOWN", text="Mentoring", evidence="not mentioned"),
        _c("EXPERIENCE", "PASS", text="Fintech", evidence="3 yrs at a bank"),
    ]
    strengths, gaps, unknowns = assemble_strengths_gaps_unknowns(rows)
    assert strengths == ["Python: 6 yrs", "Fintech: 3 yrs at a bank"]
    assert gaps == ["Kafka: not used"]
    assert unknowns == ["Mentoring: not mentioned"]


def test_sgu_empty_when_no_rows():
    assert assemble_strengths_gaps_unknowns([]) == ([], [], [])
