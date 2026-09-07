"""Structural guarantees for the Candidates page (Phase C-2, audit H13).

These are the few page-level behaviours that are worth pinning because getting
them wrong is silent and expensive:

* a COLLAPSED application card must not run its per-application queries — the
  whole point of the lazy split. A refactor that moved those reads back into an
  eager loop would look fine on screen and quietly restore the old cost;
* "Show more" must page in tens and stop at the end;
* ranking and applications must live in two tabs, so the same candidate is
  never rendered twice in one scroll;
* the shared job picker must survive navigating to another page and back —
  Streamlit does NOT do this for a bare widget key (verified: the widget is
  re-created at its default), which is exactly why ``job_picker`` mirrors the
  choice into a separate non-widget key.

They use Streamlit's own ``AppTest`` harness with the service layer stubbed, so
no database and no Streamlit server is involved.
"""

from __future__ import annotations

import pytest

from streamlit.testing.v1 import AppTest

from app.utils.ui_widgets import HR_JOB_PICKER_KEY

_TIMEOUT = 60

_PICKER_WIDGET = f"{HR_JOB_PICKER_KEY}__widget"


def _candidates_script(n_applications: int) -> str:
    """A page script with the two loaders stubbed, recording detail loads."""
    return f'''
import streamlit as st
import app.pages.candidates as C
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
C.load_job_options = lambda: [
    {{"id": "job-1", "title": "Backend Engineer", "status": "OPEN"}}
]
C._render_ranking_section = lambda *a, **k: st.write("RANKING-TAB-BODY")

st.session_state[SESSION_USER_KEY] = {{
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test",
    "full_name": "P",
    "role": "HR",
}}
C.render_candidates_page()
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


# --- shared job picker persistence (H19) -----------------------------

_PICKER_SCRIPT = '''
import streamlit as st
import app.pages.candidates as C
import app.pages.interviews as I
from app.utils.session import SESSION_USER_KEY

JOBS = [
    {"id": f"job-{i}", "title": f"Role {i}", "status": "OPEN"} for i in range(3)
]
C.load_job_options = lambda: JOBS
I.load_job_options = lambda: JOBS
C._render_ranking_section = lambda *a, **k: None
C._render_applications_tab = lambda job_id, *a, **k: st.write(f"job={job_id}")
I._render_shortlisted_section = lambda job_id, *a, **k: st.write(f"job={job_id}")

st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test",
    "full_name": "P",
    "role": "HR",
}
if st.session_state.get("page", "candidates") == "candidates":
    C.render_candidates_page()
else:
    I.render_interviews_page()
'''


def _selected(at: AppTest) -> str | None:
    for m in at.markdown:
        if m.value.startswith("job="):
            return m.value.removeprefix("job=")
    return None


def test_job_picker_selection_survives_navigating_away_and_back():
    at = AppTest.from_string(_PICKER_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert _selected(at) == "job-0"

    at.selectbox(key=_PICKER_WIDGET).set_value("job-2").run()
    assert _selected(at) == "job-2"

    # ...to the Interviews page: it must land on the same job...
    at.session_state["page"] = "interviews"
    at.run()
    assert _selected(at) == "job-2"

    # ...and back again, still job-2. A bare widget key resets to job-0 here.
    at.session_state["page"] = "candidates"
    at.run()
    assert _selected(at) == "job-2"


def test_both_pages_use_the_same_picker_key():
    """Cross-page persistence only works while they agree on the key."""
    import app.pages.candidates as candidates_page
    import app.pages.interviews as interviews_page

    assert candidates_page.HR_JOB_PICKER_KEY == HR_JOB_PICKER_KEY
    assert interviews_page.HR_JOB_PICKER_KEY == HR_JOB_PICKER_KEY
