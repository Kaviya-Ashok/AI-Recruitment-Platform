"""The candidate page's shortlist context (HR UI, Increment 5): the rubric version a
candidate was shortlisted against and the ranking-drift caution, under the page header.

Uses the stubbed-service harness of tests/test_candidate_page.py (no database); the
header values are the real ``CandidateHeader`` fields the service now fills.
"""

from __future__ import annotations

from tests.test_candidate_page import (  # noqa: F401  (the autouse restore fixture)
    _A, _JOB, _app, _ok, _restore_modules, _text,
)

_V2 = dict(
    shortlist_rubric_version_number=2, shortlist_rubric_version_status="APPROVED",
    rank_position_at_shortlisting=1, current_rank_position=1, current_rank_available=True,
)


def _shown(at):
    return [w.value for w in at.warning], [c.value for c in at.caption]


def test_an_unchanged_rank_is_a_quiet_line_with_the_rubric_version():
    at = _ok(_app(overrides={_A: _V2}, job=_JOB, candidate=_A))
    warnings, captions = _shown(at)
    assert "Shortlisted on Rubric v2 · Currently #1" in captions
    assert not any("Ranking changed" in w for w in warnings)


def test_a_moved_rank_is_a_warning_with_the_exact_words_and_the_version():
    moved = dict(_V2, current_rank_position=3)
    at = _ok(_app(overrides={_A: moved}, job=_JOB, candidate=_A))
    warnings, _ = _shown(at)
    assert "Rubric v2 · Ranking changed — shortlisted at #1, now #3" in warnings


def test_a_ranking_that_was_not_regenerated_is_said_so_without_a_warning():
    stale = dict(_V2, current_rank_position=None, current_rank_available=False)
    at = _ok(_app(overrides={_A: stale}, job=_JOB, candidate=_A))
    warnings, captions = _shown(at)
    assert "Shortlisted on Rubric v2 · Ranking not regenerated since shortlisting" in captions
    assert not any("Rubric v2" in w for w in warnings)


def test_a_candidate_who_is_not_currently_ranked_is_said_so():
    unranked = dict(_V2, current_rank_position=None, current_rank_available=True)
    at = _ok(_app(overrides={_A: unranked}, job=_JOB, candidate=_A))
    assert "Shortlisted on Rubric v2 · Not currently ranked" in _shown(at)[1]


def test_a_version_that_is_no_longer_approved_is_labelled_in_words():
    old = dict(_V2, shortlist_rubric_version_number=1,
               shortlist_rubric_version_status="SUPERSEDED")
    at = _ok(_app(overrides={_A: old}, job=_JOB, candidate=_A))
    assert "Shortlisted on Rubric v1 — Superseded · Currently #1" in _shown(at)[1]


def test_a_missing_version_is_labelled_unknown():
    at = _ok(_app(overrides={_A: dict(_V2, shortlist_rubric_version_number=None,
                                      shortlist_rubric_version_status=None)},
                  job=_JOB, candidate=_A))
    assert "Shortlisted on Rubric (unknown version) · Currently #1" in _shown(at)[1]


def test_a_candidate_who_is_not_shortlisted_shows_no_shortlist_line():
    at = _ok(_app(overrides={_A: dict(is_shortlisted=False)}, job=_JOB, candidate=_A))
    warnings, captions = _shown(at)
    assert not any("Shortlisted on" in c for c in captions)
    assert not any("Ranking" in w for w in warnings)
    assert "Rubric v" not in _text(at)


def test_the_context_is_in_the_header_area_on_every_tab():
    moved = dict(_V2, current_rank_position=3)
    for tab in ("overview", "screening", "interview", "analysis", "scorecard", "decision"):
        at = _ok(_app(overrides={_A: moved}, job=_JOB, candidate=_A, tab=tab))
        assert any("Ranking changed — shortlisted at #1, now #3" in w
                   for w in _shown(at)[0]), tab
