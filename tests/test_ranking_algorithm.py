"""Tests for the pure ranking algorithm — ``ranking_service.compute_ranking``
(Phase 4 Step 5). No database, no AI: a pure function over
``EvaluatedApplicationRow`` values.

Covers the CLAUDE.md §5 requirements: mandatory-FAIL exclusion, mandatory-UNKNOWN
flag without exclusion, UNKNOWN never treated as FAIL, the NULL-bucket-excluding
weighted average, the full documented tie-break chain, and the guarantee that no
candidate identity attribute is read in the sort.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.ranking_service import (
    EvaluatedApplicationRow,
    RankingError,
    compute_ranking,
)

_RVID = uuid.uuid4()
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(
    *,
    rvid=_RVID,
    created=_T0,
    req=None,
    exp=None,
    beh=None,
    req_cov=None,
    exp_cov=None,
    beh_cov=None,
    conf="MEDIUM",
    mandatory=None,
    app_id=None,
) -> EvaluatedApplicationRow:
    """``mandatory`` is the list of result strings for this row's MANDATORY
    criteria; non-mandatory criteria are irrelevant to the algorithm and omitted.
    """
    results = [
        {"requirement_type": "MANDATORY", "result": r} for r in (mandatory or [])
    ]
    return EvaluatedApplicationRow(
        application_id=app_id or uuid.uuid4(),
        rubric_version_id=rvid,
        created_at=created,
        requirements_score=req,
        experience_score=exp,
        behavioral_score=beh,
        requirements_coverage=req_cov,
        experience_coverage=exp_cov,
        behavioral_coverage=beh_cov,
        overall_confidence=conf,
        results=results,
    )


# --- basics ----------------------------------------------------------


def test_empty_input_returns_empty():
    assert compute_ranking([]) == []


def test_single_eligible_candidate_gets_rank_1():
    [rc] = compute_ranking([_row(req=8, exp=7, beh=6, mandatory=["PASS"])])
    assert rc.rank_position == 1
    assert rc.eligible is True


# --- eligibility / UNKNOWN (CLAUDE.md §B) ---------------------------


def test_mandatory_fail_makes_ineligible_and_unranked():
    ranked = compute_ranking(
        [
            _row(req=9, exp=9, beh=9, mandatory=["PASS", "FAIL"]),
            _row(req=3, exp=3, beh=3, mandatory=["PASS"]),
        ]
    )
    by_elig = {rc.eligible: rc for rc in ranked}
    assert by_elig[False].rank_position is None
    assert by_elig[False].overall_score == 9.0  # still computed for HR context
    assert by_elig[True].rank_position == 1


def test_mandatory_unknown_flags_but_still_ranked():
    [rc] = compute_ranking([_row(req=8, exp=8, beh=8, mandatory=["PASS", "UNKNOWN"])])
    assert rc.eligible is True
    assert rc.mandatory_unknown_flag is True
    assert rc.rank_position == 1


def test_unknown_mandatory_is_never_treated_as_fail():
    # A row whose ONLY mandatory verdict is UNKNOWN must stay eligible.
    [rc] = compute_ranking([_row(req=5, exp=5, beh=5, mandatory=["UNKNOWN"])])
    assert rc.eligible is True
    assert rc.rank_position == 1


def test_no_mandatory_criteria_is_eligible_without_flag():
    [rc] = compute_ranking([_row(req=5, mandatory=[])])
    assert rc.eligible is True
    assert rc.mandatory_unknown_flag is False


# --- weighted average, NULL buckets excluded ----------------------


def test_weighted_average_excludes_null_bucket_not_treated_as_zero():
    # req=8 (w2), exp=6 (w1), beh=NULL -> (2*8 + 1*6) / (2+1) = 22/3
    [rc] = compute_ranking([_row(req=8, exp=6, beh=None, mandatory=["PASS"])])
    assert rc.overall_score == pytest.approx(22 / 3)
    # NOT the "treat missing as 0" value:
    assert rc.overall_score != pytest.approx((2 * 8 + 6 + 0) / 4)


def test_weighted_average_all_three_buckets():
    # (2*9 + 6 + 3) / 4 = 27/4 = 6.75
    [rc] = compute_ranking([_row(req=9, exp=6, beh=3, mandatory=["PASS"])])
    assert rc.overall_score == pytest.approx(6.75)


def test_only_requirements_present():
    [rc] = compute_ranking([_row(req=7, exp=None, beh=None, mandatory=["PASS"])])
    assert rc.overall_score == pytest.approx(7.0)


def test_all_null_buckets_gives_null_score_but_still_ranked():
    [rc] = compute_ranking(
        [_row(req=None, exp=None, beh=None, mandatory=["PASS"])]
    )
    assert rc.overall_score is None
    assert rc.eligible is True
    assert rc.rank_position == 1  # ranked, not hidden


def test_null_score_candidate_ranks_below_any_scored_candidate():
    scored = _row(req=1, exp=1, beh=1, mandatory=["PASS"], app_id=uuid.uuid4())
    unscored = _row(req=None, exp=None, beh=None, mandatory=["PASS"],
                    app_id=uuid.uuid4())
    ranked = compute_ranking([unscored, scored])
    positions = {rc.application_id: rc.rank_position for rc in ranked}
    assert positions[scored.application_id] == 1
    assert positions[unscored.application_id] == 2


# --- carried-through fields ---------------------------------------


def test_confidence_and_coverage_are_carried_through_unchanged():
    row = _row(
        req=8, exp=6, beh=4, req_cov=0.5, exp_cov=1.0, beh_cov=0.25,
        conf="LOW", mandatory=["PASS"],
    )
    [rc] = compute_ranking([row])
    assert rc.overall_confidence == "LOW"
    assert rc.requirements_coverage == 0.5
    assert rc.experience_coverage == 1.0
    assert rc.behavioral_coverage == 0.25
    # coverage / confidence did not leak into the score
    assert rc.overall_score == pytest.approx((2 * 8 + 6 + 4) / 4)


# --- tie-break chain --------------------------------------------


def test_full_tiebreak_chain_overall_then_req_then_exp_then_beh_then_created():
    # All four have overall_score == 8.0 (each sums to 32/4).
    x = _row(req=8, exp=8, beh=8, created=_T0, mandatory=["PASS"])
    w = _row(req=8, exp=8, beh=8, created=_T0 + timedelta(hours=1), mandatory=["PASS"])
    z = _row(req=8, exp=6, beh=10, created=_T0, mandatory=["PASS"])
    y = _row(req=6, exp=10, beh=10, created=_T0, mandatory=["PASS"])

    ranked = compute_ranking([y, z, w, x])  # deliberately shuffled
    order = [rc.application_id for rc in ranked]

    # overall tie -> req (x,w,z beat y) -> exp (x,w beat z) -> beh tie
    # -> created_at ASC (x before w)
    assert order == [x.application_id, w.application_id,
                     z.application_id, y.application_id]
    assert [rc.overall_score for rc in ranked] == [8.0, 8.0, 8.0, 8.0]
    assert [rc.rank_position for rc in ranked] == [1, 2, 3, 4]


def test_created_at_is_the_final_decider_when_all_scores_equal():
    early = _row(req=5, exp=5, beh=5, created=_T0, mandatory=["PASS"])
    late = _row(req=5, exp=5, beh=5, created=_T0 + timedelta(days=3),
                mandatory=["PASS"])
    ranked = compute_ranking([late, early])
    assert ranked[0].application_id == early.application_id
    assert ranked[1].application_id == late.application_id


# --- no identity attribute anywhere in the sort ------------------


def test_evaluated_row_carries_no_candidate_identity_field():
    """Structural guarantee: the ranking input type has no name/email/phone, so
    the sort key literally cannot read one."""
    forbidden = {"name", "candidate_name", "full_name", "email",
                 "candidate_email", "phone", "gender", "age", "location"}
    assert forbidden.isdisjoint(EvaluatedApplicationRow.__dataclass_fields__)


def test_ranking_is_stable_and_identity_independent():
    """Two rows identical on every ranking field (incl. created_at) but with
    very different application_ids must not have their order decided by the id
    — the algorithm preserves input order (stable sort), it does not sort on id.
    """
    a_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    b_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
    a = _row(req=5, exp=5, beh=5, created=_T0, mandatory=["PASS"], app_id=a_id)
    b = _row(req=5, exp=5, beh=5, created=_T0, mandatory=["PASS"], app_id=b_id)

    # input order [a, b] -> a first; input order [b, a] -> b first.
    assert [rc.application_id for rc in compute_ranking([a, b])] == [a_id, b_id]
    assert [rc.application_id for rc in compute_ranking([b, a])] == [b_id, a_id]


# --- defensive: one partition per call --------------------------


def test_multiple_rubric_versions_in_one_call_is_rejected():
    with pytest.raises(RankingError):
        compute_ranking(
            [
                _row(req=8, mandatory=["PASS"], rvid=uuid.uuid4()),
                _row(req=8, mandatory=["PASS"], rvid=uuid.uuid4()),
            ]
        )


def test_ineligible_rows_come_after_ranked_rows_with_null_position():
    ranked = compute_ranking(
        [
            _row(req=9, mandatory=["PASS"]),
            _row(req=2, mandatory=["FAIL"]),
            _row(req=7, mandatory=["PASS"]),
            _row(req=1, mandatory=["PASS", "FAIL"]),
        ]
    )
    positions = [rc.rank_position for rc in ranked]
    assert positions == [1, 2, None, None]
    assert all(rc.eligible for rc in ranked[:2])
    assert not any(rc.eligible for rc in ranked[2:])
