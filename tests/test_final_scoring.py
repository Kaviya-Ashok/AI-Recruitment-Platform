"""Pure tests for app.services.final_scoring (Step 10b) — no DB, no AI.

The formula under test: final = round_half_up(0.40 x screening + 0.60 x interview,
2), where interview = 2 x mean(mean rating per round), every round equal, and
both inputs are rounded half-up to 2 decimals FIRST.
"""

from __future__ import annotations

import inspect
import itertools
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import final_scoring as fs
from app.services.final_scoring import (
    INTERVIEW_WEIGHT,
    SCREENING_WEIGHT,
    RankInput,
    compute_final_confidence,
    compute_final_score,
    compute_interview_score_all_rounds,
    interview_round_means,
    rank_candidates,
    round_half_up,
    round_screening_score,
)

D = Decimal
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --- constants -----------------------------------------------------------


def test_weights_are_named_decimals_that_sum_to_exactly_one():
    assert SCREENING_WEIGHT == D("0.40")
    assert INTERVIEW_WEIGHT == D("0.60")
    assert isinstance(SCREENING_WEIGHT, Decimal) and isinstance(INTERVIEW_WEIGHT, Decimal)
    assert SCREENING_WEIGHT + INTERVIEW_WEIGHT == D(1)


def test_rating_scale_agrees_with_the_persisted_model():
    from app.database.models.interview_feedback import RATING_MAX

    assert fs.RATING_MAX == RATING_MAX


# --- rounding ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("0.125", "0.13"),       # banker's rounding would give 0.12
        ("0.135", "0.14"),
        ("2.675", "2.68"),       # the classic float trap
        ("7.994", "7.99"),
        ("7.995", "8.00"),
        ("0.004", "0.00"),
        ("0.005", "0.01"),
        ("10", "10.00"),
    ],
)
def test_round_half_up(value, expected):
    assert round_half_up(D(value), 2) == D(expected)


def test_screening_float_is_rounded_through_its_decimal_text():
    # 2.675 is 2.67499999... as a binary float; the stored text is what counts.
    assert round_screening_score(2.675) == D("2.68")
    assert round_screening_score(7.25) == D("7.25")
    assert round_screening_score(10) == D("10.00")
    assert round_screening_score(None) is None


# --- round means ----------------------------------------------------------------


def test_round_mean_is_per_round_and_rounds_without_ratings_are_excluded():
    means = interview_round_means({1: [4, 3], 2: [], 3: [5]})
    assert means == {1: D("3.5"), 3: D("5")}
    assert 2 not in means                       # excluded, never present as 0


def test_non_integer_and_boolean_ratings_are_ignored():
    assert interview_round_means({1: [None, "x", True, 4]}) == {1: D(4)}
    assert interview_round_means({1: [None, "x", True]}) == {}


def test_rounds_come_back_in_ascending_order():
    assert list(interview_round_means({3: [1], 1: [2], 2: [3]})) == [1, 2, 3]


# --- the interview score over ALL rounds --------------------------------------------


def test_rounds_with_different_rating_counts_count_equally():
    """Round 1: eight 5s. Round 2: two 1s. Pooled mean would be 4.2 (8.40); the
    mean of round means is (5 + 1) / 2 = 3 -> 6.00."""
    ratings = {1: [5] * 8, 2: [1, 1]}
    assert compute_interview_score_all_rounds(ratings) == D("6.00")
    pooled = sum(ratings[1] + ratings[2]) / 10
    assert D(str(pooled)) * 2 != D("6.00")           # the two methods really differ


@pytest.mark.parametrize(
    "ratings, expected",
    [
        ({1: [5]}, "10.00"),
        ({1: [1]}, "2.00"),                    # lowest AVAILABLE rating, not zero
        ({1: [3]}, "6.00"),
        ({1: [4, 3]}, "7.00"),
        ({1: [2, 5, 3, 1]}, "5.50"),           # mean 2.75
        ({1: [4], 2: [3]}, "7.00"),
        ({1: [1], 2: [1], 3: [2]}, "2.67"),    # 8/3 -> 2.666... -> 2.67
    ],
)
def test_interview_score_values(ratings, expected):
    assert compute_interview_score_all_rounds(ratings) == D(expected)


def test_interview_score_rounding_boundary_is_half_up():
    # one round, 16 ratings summing to 49 -> mean 3.0625 -> x2 = 6.125 -> 6.13
    ratings = {1: [3] * 15 + [4] + []}
    assert sum(ratings[1]) == 49 and len(ratings[1]) == 16
    assert compute_interview_score_all_rounds(ratings) == D("6.13")
    # ...whereas Python's round() (banker's) would have produced 6.12
    assert round(6.125, 2) == 6.12


def test_no_ratings_means_no_interview_score_not_zero():
    assert compute_interview_score_all_rounds({}) is None
    assert compute_interview_score_all_rounds({1: [], 2: []}) is None
    assert compute_interview_score_all_rounds({1: [None, True]}) is None


def test_notes_only_round_is_excluded_not_zero():
    with_notes_only = compute_interview_score_all_rounds({1: [4, 4], 2: []})
    without_it = compute_interview_score_all_rounds({1: [4, 4]})
    assert with_notes_only == without_it == D("8.00")


def test_interview_score_is_always_within_range():
    for combo in itertools.product(range(1, 6), repeat=3):
        score = compute_interview_score_all_rounds({1: [combo[0]], 2: list(combo[1:])})
        assert D(0) <= score <= D(10)


def test_interview_score_takes_nothing_but_the_ratings():
    assert list(inspect.signature(compute_interview_score_all_rounds).parameters) == [
        "ratings_by_round"
    ]


# --- the final score ---------------------------------------------------------------------


def test_formula_worked_example():
    # 0.40 x 7.25 = 2.900 ; 0.60 x 8.33 = 4.998 ; sum 7.898 -> 7.90
    assert compute_final_score(screening_score=7.25, interview_score=D("8.33")) == D("7.90")
    # 0.40 x 7.25 + 0.60 x 8.32 = 2.9 + 4.992 = 7.892 -> 7.89
    assert compute_final_score(screening_score=7.25, interview_score=D("8.32")) == D("7.89")


@pytest.mark.parametrize(
    "screening, interview, expected",
    [
        (10, 10, "10.00"),
        (0, 0, "0.00"),                     # a REAL zero is a score, not unknown
        (10, 0, "4.00"),
        (0, 10, "6.00"),
        (5, 5, "5.00"),
        (8, 6, "6.80"),
    ],
)
def test_final_score_values(screening, interview, expected):
    assert compute_final_score(screening_score=screening, interview_score=interview) == D(expected)


def test_inputs_are_rounded_to_two_decimals_before_weighting():
    """HR must be able to reproduce the final score from the DISPLAYED numbers.

    Screening 0.985 displays as 0.99, so the final is 0.4 x 0.99 = 0.396 -> 0.40.
    Weighting the unrounded 0.985 would give 0.394 -> 0.39, which no reader of the
    screen could reproduce."""
    assert compute_final_score(screening_score=0.985, interview_score=0) == D("0.40")
    assert round_screening_score(0.985) == D("0.99")
    # and the interview side: 0.995 displays as 1.00 -> 0.6 x 1.00 = 0.60
    assert compute_final_score(screening_score=0, interview_score=D("0.995")) == D("0.60")


def test_missing_screening_means_no_final_score_and_no_redistribution():
    assert compute_final_score(screening_score=None, interview_score=8) is None
    # not "8.00" (weights moved onto the part that exists) and not 4.80 (zero)
    assert compute_final_score(screening_score=None, interview_score=8) != D("8.00")


def test_missing_interview_means_no_final_score_and_no_redistribution():
    assert compute_final_score(screening_score=8, interview_score=None) is None
    assert compute_final_score(screening_score=None, interview_score=None) is None


def test_final_score_has_no_recommendation_confidence_or_transcript_parameter():
    params = set(inspect.signature(compute_final_score).parameters)
    assert params == {"screening_score", "interview_score"}
    for name in params:
        for forbidden in ("recommend", "confidence", "transcript", "analysis", "disagree"):
            assert forbidden not in name


def test_module_has_no_recommendation_parameter_anywhere():
    for name, fn in inspect.getmembers(fs, inspect.isfunction):
        for param in inspect.signature(fn).parameters:
            assert "recommend" not in param.lower(), (name, param)


def test_module_is_pure_no_db_no_streamlit_no_ai():
    src = Path(fs.__file__).read_text(encoding="utf-8")
    for needle in (
        "import streamlit", "from streamlit", "sqlalchemy", "app.ai", "anthropic",
        "session_scope", "get_structured_response", "import requests",
    ):
        assert needle not in src, needle


# --- ranking --------------------------------------------------------------------------------


def _ri(score, *, eligible=True, screening=None, minutes=0):
    return RankInput(
        application_id=uuid.uuid4(),
        final_score=None if score is None else D(str(score)),
        screening_score=None if screening is None else D(str(screening)),
        created_at=_T0 + timedelta(minutes=minutes),
        eligible=eligible,
    )


def _ranks(entries):
    by_id = {r.application_id: r for r in rank_candidates(entries)}
    return [by_id[e.application_id].rank for e in entries]


def test_ranking_orders_by_final_score_descending():
    a, b, c = _ri("7.00"), _ri("9.00"), _ri("8.00")
    out = rank_candidates([a, b, c])
    assert [r.application_id for r in out] == [b.application_id, c.application_id, a.application_id]
    assert [r.rank for r in out] == [1, 2, 3]


def test_ties_share_a_rank_and_the_next_rank_skips():
    entries = [_ri("9.00"), _ri("8.00"), _ri("8.00"), _ri("7.00")]
    assert _ranks(entries) == [1, 2, 2, 4]


def test_three_way_tie_and_a_tie_at_the_top():
    assert _ranks([_ri("9.00"), _ri("9.00"), _ri("9.00"), _ri("5.00")]) == [1, 1, 1, 4]
    assert _ranks([_ri("9.00"), _ri("9.00"), _ri("8.00")]) == [1, 1, 3]
    same = _ri("6.50")
    others = [_ri("6.50"), _ri("6.50"), _ri("1.00")]
    assert _ranks([same, *others]) == [1, 1, 1, 4]


def test_tied_flag_is_true_only_for_shared_scores():
    a, b, c = _ri("8.00"), _ri("8.00"), _ri("7.00")
    by_id = {r.application_id: r for r in rank_candidates([a, b, c])}
    assert by_id[a.application_id].tied and by_id[b.application_id].tied
    assert not by_id[c.application_id].tied


def test_ties_are_decided_on_the_stored_two_decimal_value():
    """8.00 and 8.004 are different Python numbers but the STORED value is 8.00 for
    both — callers pass the already-rounded value, so equal means equal."""
    a = _ri(compute_final_score(screening_score=8, interview_score=8))
    b = _ri(compute_final_score(screening_score=D("8.004"), interview_score=D("8.004")))
    assert a.final_score == b.final_score == D("8.00")
    assert _ranks([a, b]) == [1, 1]


def test_order_inside_a_tie_is_stable_screening_desc_then_created_at():
    low = _ri("8.00", screening="6.00", minutes=1)
    high = _ri("8.00", screening="9.00", minutes=5)
    early = _ri("8.00", screening="9.00", minutes=2)
    out = rank_candidates([low, high, early])
    assert [r.application_id for r in out] == [
        early.application_id, high.application_id, low.application_id,
    ]
    assert {r.rank for r in out} == {1}


def test_ranking_is_deterministic_regardless_of_input_order():
    entries = [_ri("8.00", screening="7.00", minutes=i) for i in range(5)]
    forward = [r.application_id for r in rank_candidates(entries)]
    backward = [r.application_id for r in rank_candidates(list(reversed(entries)))]
    assert forward == backward


def test_ineligible_candidates_are_never_ranked_even_with_the_top_score():
    top = _ri("10.00", eligible=False)
    ok = _ri("5.00")
    out = rank_candidates([top, ok])
    by_id = {r.application_id: r for r in out}
    assert by_id[top.application_id].rank is None
    assert by_id[ok.application_id].rank == 1
    # ...and listed AFTER every ranked candidate, never above one
    assert out[0].application_id == ok.application_id


def test_candidates_without_a_final_score_are_not_ranked_and_come_last():
    none_score = _ri(None)
    ranked = _ri("1.00")
    out = rank_candidates([none_score, ranked])
    assert out[0].application_id == ranked.application_id
    assert out[1].rank is None and not out[1].tied


def test_an_ineligible_candidate_does_not_affect_other_candidates_ranks():
    entries = [_ri("9.00"), _ri("10.00", eligible=False), _ri("8.00")]
    assert _ranks(entries) == [1, None, 2]


def test_empty_input_ranks_to_empty_output():
    assert rank_candidates([]) == []


def test_ranking_input_carries_no_identity_attribute():
    names = {f for f in RankInput.__dataclass_fields__}
    assert names == {"application_id", "final_score", "screening_score", "created_at", "eligible"}


# --- confidence ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "screening, analysis, expected",
    [
        ("HIGH", "HIGH", "HIGH"),
        ("HIGH", "MEDIUM", "MEDIUM"),
        ("MEDIUM", "HIGH", "MEDIUM"),
        ("HIGH", "LOW", "LOW"),
        ("LOW", "HIGH", "LOW"),
        ("MEDIUM", "MEDIUM", "MEDIUM"),
        ("LOW", "LOW", "LOW"),
    ],
)
def test_confidence_is_the_lowest_of_the_two(screening, analysis, expected):
    assert compute_final_confidence(
        screening_confidence=screening, analysis_confidence=analysis
    ) == (expected, False)


@pytest.mark.parametrize(
    "screening, expected",
    [("HIGH", "MEDIUM"), ("MEDIUM", "MEDIUM"), ("LOW", "LOW")],
)
def test_no_analysis_caps_confidence_at_medium(screening, expected):
    assert compute_final_confidence(
        screening_confidence=screening, analysis_confidence=None
    ) == (expected, True)


def test_unknown_screening_confidence_is_treated_conservatively():
    assert compute_final_confidence(
        screening_confidence=None, analysis_confidence="HIGH"
    ) == ("LOW", False)
    assert compute_final_confidence(
        screening_confidence="BOGUS", analysis_confidence=None
    ) == ("LOW", True)


def test_confidence_function_takes_no_score_and_scores_take_no_confidence():
    assert set(inspect.signature(compute_final_confidence).parameters) == {
        "screening_confidence", "analysis_confidence",
    }
    assert "confidence" not in " ".join(inspect.signature(compute_final_score).parameters)
