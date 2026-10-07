"""Structural guarantees for the Applicants stage's two tabs (Phase C-2, audit H13;
re-pointed in HR UI Increment 5).

The old Candidates page is gone. These tests used to run it; they now run the job
workspace's Applicants stage (``job_workspace._stage_applicants``), which renders the
very same two tabs through the same renderers (``candidates._render_ranking_section``
and ``candidates._render_applications_tab``), so the guarantees below are unchanged.

These are the few page-level behaviours that are worth pinning because getting
them wrong is silent and expensive:

* a COLLAPSED application card must not run its per-application queries — the
  whole point of the lazy split. A refactor that moved those reads back into an
  eager loop would look fine on screen and quietly restore the old cost;
* "Show more" must page in tens and stop at the end;
* ranking and applications must live in two tabs, so the same candidate is
  never rendered twice in one scroll;

(The "job picker survives navigating to the Interviews page and back" tests were
deleted in Increment 5: both pages and their shared picker are gone.)

They use Streamlit's own ``AppTest`` harness with the service layer stubbed, so
no database and no Streamlit server is involved.
"""

from __future__ import annotations

import pytest

from streamlit.testing.v1 import AppTest

import app.pages.candidates as C
import app.pages.job_workspace as W

_TIMEOUT = 60

# THE STUB LEAK, FIXED. ``AppTest.from_string`` runs its script in THIS process, so the
# ``C.x = stub`` / ``W.x = stub`` lines in the script mutate the real page modules. They
# used to be left in place, which leaked a do-nothing ``_render_shortlisted_section``
# into later test files (tests/test_interviews_page_analysis_section.py documented it).
# Every attribute the script assigns is listed here, snapshotted at import time (before
# any test has run) and put back after EVERY test. There is deliberately no restore
# BEFORE a test: the last test of this file asserts nothing leaked, which only means
# something if the fixture does not quietly mask it.
_PATCHED = {
    C: ("_load_application_summaries", "_load_application_detail",
        "_render_ranking_section", "_render_applications_tab"),
    W: ("_candidate_selector", "_render_ranking_section", "_render_applications_tab"),
}
_PRISTINE = {
    module: {name: getattr(module, name) for name in names}
    for module, names in _PATCHED.items()
}


def _restore() -> None:
    for module, attrs in _PRISTINE.items():
        for name, value in attrs.items():
            setattr(module, name, value)


@pytest.fixture(autouse=True)
def _restore_page_modules():
    try:
        yield
    finally:
        _restore()


def _candidates_script(n_applications: int) -> str:
    """A page script with the two loaders stubbed, recording detail loads."""
    return f'''
import streamlit as st
import app.pages.candidates as C
import app.pages.job_workspace as W
from app.utils.session import SESSION_USER_KEY

N = {n_applications}
DETAIL_CALLS = []


def fake_summaries(job_id, acting_user_id, *, limit):
    rows = [
        {{
            "application_id": f"app-{{i:02d}}",
            "status": "SCREENING_EVALUATED",
            "candidate_name": f"Cand {{i:02d}}",
            "candidate_email": f"c{{i:02d}}@x.test",
            "screening_stall_kind": None,
        }}
        for i in range(N)
    ]
    return rows[:limit], N


def fake_detail(application_id, acting_user_id):
    DETAIL_CALLS.append(application_id)
    return {{
        "screening_session_id": None,
        "screening_status": "SCREENING_COMPLETE",
        "document_id": None,
        "document_name": None,
        "extracted_data": None,
        "extraction_created_at": None,
        "prequal_results": None,
        "prequal_created_at": None,
        "evaluation": None,
    }}


C._load_application_summaries = fake_summaries
C._load_application_detail = fake_detail
# the Applicants stage's own prelude (the candidate selector) is not under test here
W._candidate_selector = lambda *a, **k: None
W._render_ranking_section = lambda *a, **k: st.write("RANKING-TAB-BODY")

st.session_state[SESSION_USER_KEY] = {{
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test",
    "full_name": "P",
    "role": "HR",
}}
W._stage_applicants("job-1", "11111111-1111-1111-1111-111111111111")
st.session_state["_detail_calls"] = list(DETAIL_CALLS)
'''


def _run(n_applications: int) -> AppTest:
    at = AppTest.from_string(
        _candidates_script(n_applications), default_timeout=_TIMEOUT
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


# --- the two tabs (H13) ----------------------------------------------


def test_page_renders_ranking_and_applications_as_separate_tabs():
    at = _run(3)
    assert [t.label for t in at.tabs] == ["Ranking & Shortlist", "Applications"]


# --- lazy cards: the core H13 guarantee ------------------------------


def test_collapsed_cards_do_not_run_their_per_application_queries():
    """23 applications -> all collapsed -> zero detail loads.

    If this fails, the expander is decorative and every application is paying
    its five reads on every rerun again.
    """
    at = _run(23)
    assert at.session_state["_detail_calls"] == []


def test_a_short_list_expands_and_does_load_its_details():
    """<= 5 applications open by default, so their bodies must load."""
    at = _run(3)
    assert at.session_state["_detail_calls"] == ["app-00", "app-01", "app-02"]


@pytest.mark.parametrize(
    "count,expect_loads", [(5, True), (6, False)]
)
def test_expand_threshold_is_five(count, expect_loads):
    at = _run(count)
    loaded = bool(at.session_state["_detail_calls"])
    assert loaded is expect_loads


# --- "Show more" paging ----------------------------------------------


def _show_more(at: AppTest):
    return [b for b in at.button if b.key == "apps_more"]


def test_show_more_pages_in_tens_and_stops_at_the_end():
    at = _run(23)
    assert at.session_state["candidates_apps_shown"] == 10
    assert _show_more(at), "a 23-application job needs a Show more control"

    at.button(key="apps_more").click().run()
    assert at.session_state["candidates_apps_shown"] == 20
    assert _show_more(at)

    at.button(key="apps_more").click().run()
    assert at.session_state["candidates_apps_shown"] == 30
    assert not _show_more(at), "no Show more once everything is loaded"


def test_no_show_more_when_everything_already_fits():
    at = _run(4)
    assert not _show_more(at)


def test_paging_more_does_not_wake_collapsed_cards():
    """Loading more rows must not start executing their bodies."""
    at = _run(23)
    at.button(key="apps_more").click().run()
    assert at.session_state["_detail_calls"] == []


# --- the stub leak is gone --------------------------------------------------------


def test_running_the_scripts_patches_the_modules_and_the_fixture_puts_them_back():
    """The first half proves the scripts really do overwrite page attributes (so the
    fixture is needed); the next test then proves they were restored."""
    _run(3)
    assert C._load_application_summaries is not _PRISTINE[C]["_load_application_summaries"]
    assert W._render_ranking_section is not _PRISTINE[W]["_render_ranking_section"]


def test_nothing_the_scripts_patched_is_left_behind_by_the_previous_test():
    """Runs right after the test above, with no restore of its own in between: if the
    fixture stopped restoring, this fails. (The old file left these stubs behind.)"""
    for module, attrs in _PRISTINE.items():
        for name, value in attrs.items():
            assert getattr(module, name) is value, (module.__name__, name)
