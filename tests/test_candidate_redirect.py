"""The Dashboard -> Jobs redirect carries a candidate deep link (HR UI, Increment 3).

``/?job=..&stage=..&candidate=..&tab=..`` (what a logged-out visit to a candidate
link becomes) is handed on to the Jobs page after sign-in, which shows the candidate
page. Same harness as tests/test_job_workspace_redirect.py: the Jobs page is replaced
by a recorder and ``app.main`` is re-imported inside the AppTest script.
"""

from __future__ import annotations

import sys

import pytest

import app.pages.jobs as J
from tests.test_job_workspace_redirect import _JOB, _PRISTINE_RENDER, _run

_CAND = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _restore():
    J.render_jobs_page = _PRISTINE_RENDER
    sys.modules.pop("app.main", None)
    try:
        yield
    finally:
        J.render_jobs_page = _PRISTINE_RENDER
        sys.modules.pop("app.main", None)


def _params(at) -> dict:
    return at.session_state["_jobs_params"]


def test_a_candidate_link_is_handed_to_the_jobs_page_with_every_parameter():
    at = _run({"job": _JOB, "stage": "interviews", "candidate": _CAND, "tab": "decision"})
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["_jobs_page_ran"] == 1
    assert _params(at) == {"job": _JOB, "stage": "interviews", "candidate": _CAND,
                           "tab": "decision"}


def test_a_candidate_link_without_a_tab_or_stage():
    at = _run({"job": _JOB, "candidate": _CAND})
    assert _params(at) == {"job": _JOB, "candidate": _CAND}


def test_an_unknown_tab_is_dropped_so_the_page_shows_overview():
    at = _run({"job": _JOB, "candidate": _CAND, "tab": "payroll"})
    assert _params(at) == {"job": _JOB, "candidate": _CAND}


@pytest.mark.parametrize("bad", ["not-a-uuid", "123", "'; DROP TABLE applications;--", "   "])
def test_a_malformed_candidate_is_dropped_but_the_workspace_link_still_works(bad):
    at = _run({"job": _JOB, "stage": "shortlist", "candidate": bad, "tab": "decision"})
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _params(at) == {"job": _JOB, "stage": "shortlist"}


def test_a_candidate_without_a_valid_job_never_redirects():
    at = _run({"candidate": _CAND, "tab": "decision"})
    assert "_jobs_page_ran" not in at.session_state
    assert [t.value for t in at.title] == ["Dashboard"]
    at = _run({"job": "nope", "candidate": _CAND})
    assert "_jobs_page_ran" not in at.session_state


def test_the_redirect_runs_the_jobs_page_exactly_once_so_there_is_no_loop():
    at = _run({"job": _JOB, "candidate": _CAND, "tab": "scorecard"})
    assert at.session_state["_jobs_page_ran"] == 1
    again = at.run()
    assert not again.exception
    assert again.session_state["_jobs_page_ran"] == 2        # one run per rerun, never more
