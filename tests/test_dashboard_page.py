"""The Dashboard page and the Quick find widget (HR UI, Increment 4), through
Streamlit's AppTest.

Services are stubbed (no database). ``st.switch_page`` is replaced by a recorder so
every navigation is observable: what is asserted is WHERE each button goes, in the
existing deep-link scheme (job, stage, candidate, tab).
"""

from __future__ import annotations

import uuid

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import app.pages.dashboard as D
import app.pages.quick_find as Q

_TIMEOUT = 60
_ME = "11111111-1111-1111-1111-111111111111"
_JOB_A = "aaaaaaaa-0000-4000-8000-00000000000a"
_JOB_B = "bbbbbbbb-0000-4000-8000-00000000000b"
_APP = "cccccccc-0000-4000-8000-00000000000c"
_APP2 = "dddddddd-0000-4000-8000-00000000000d"

_ST_SWITCH, _ST_PAGE_LINK = st.switch_page, st.page_link
_PRISTINE = {
    D: {n: getattr(D, n) for n in ("session_scope", "get_dashboard_summary", "render_quick_find")},
    Q: {n: getattr(Q, n) for n in ("session_scope", "quick_find")},
}


@pytest.fixture(autouse=True)
def _restore():
    def put():
        # the AppTest scripts below overwrite these on the real streamlit module
        st.switch_page, st.page_link = _ST_SWITCH, _ST_PAGE_LINK
        for module, attrs in _PRISTINE.items():
            for name, value in attrs.items():
                setattr(module, name, value)

    put()
    try:
        yield
    finally:
        put()


_PRELUDE = '''
import contextlib
import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import app.pages.dashboard as D
import app.pages.quick_find as Q
from app.services.overview_service import (
    AttentionItem, DashboardSummary, FindMatch, JobPipeline,
)
from app.utils.authorization import UnauthorizedError
from app.utils.session import SESSION_USER_KEY

st.session_state.setdefault("CALLS", [])
CALLS = st.session_state["CALLS"]
RAISE = __RAISE__
JOB_A, JOB_B = uuid.UUID("__JOB_A__"), uuid.UUID("__JOB_B__")
APP, APP2 = uuid.UUID("__APP__"), uuid.UUID("__APP2__")


@contextlib.contextmanager
def _scope():
    yield "DB"


def _maybe_raise():
    if RAISE == "unauthorized":
        raise UnauthorizedError("no")
    if RAISE == "sql":
        raise SQLAlchemyError("boom")


def _switch(page, query_params=None):
    CALLS.append(("switch", page, dict(query_params or {})))


st.switch_page = _switch
st.page_link = lambda page, **kw: CALLS.append(("page_link", page, kw.get("label")))
PAGES = {"jobs": "JOBS_PAGE", "candidates": "CANDIDATES_PAGE"}

D.session_scope = _scope
Q.session_scope = _scope

st.session_state[SESSION_USER_KEY] = {
    "id": "__SESSION_ID__", "email": "p@x.test", "full_name": "P", "role": "HR",
}

__BODY__
'''


def _run(body: str, *, raise_: str | None = None, session_id: str = _ME,
         params: dict | None = None) -> AppTest:
    script = (
        _PRELUDE.replace("__BODY__", body).replace("__RAISE__", repr(raise_))
        .replace("__JOB_A__", _JOB_A).replace("__JOB_B__", _JOB_B)
        .replace("__APP2__", _APP2).replace("__APP__", _APP)
        .replace("__SESSION_ID__", session_id)
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT)
    for k, v in (params or {}).items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _calls(at, kind=None):
    calls = at.session_state["CALLS"]
    return [c for c in calls if kind is None or c[0] == kind]


def _text(at) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [i.value for i in at.info]
    parts += [t.value for t in at.text] + [s.value for s in at.subheader]
    parts += [t.value for t in at.title]
    return "\n".join(str(p) for p in parts)


def _button(at, key):
    return next(b for b in at.button if b.key == key)


# --- the dashboard ---------------------------------------------------------------------

_SUMMARY = '''
def item(kind, code, name, app=APP, job=JOB_A):
    return AttentionItem(kind, job, code, "Backend Engineer", app, name)


ITEMS = __ITEMS__
SUMMARY = DashboardSummary(
    jobs=2, open_jobs=1, applications=12, interviewed=7, decided=3,
    attention=tuple(ITEMS), attention_total=__TOTAL__,
    pipelines=(JobPipeline(JOB_A, "V_001", "Backend Engineer", 10, 8, 5, 4, 2),),
)


def fake_summary(db, *, acting_user_id):
    CALLS.append(("summary", acting_user_id))
    _maybe_raise()
    return SUMMARY


D.get_dashboard_summary = fake_summary
D.render_dashboard_page(PAGES)
'''

_THREE = ('[item("RATINGS_MISSING", "V_001", "Ben Notes", APP), '
          'item("RESUME_MISSING", "V_001", "Cat Cole", APP2), '
          'item("DECISION_PENDING", "V_001", "Dan Dee", APP)]')


def _dash(items=_THREE, total=None, **kw):
    n = items.count("item(") if total is None else total
    return _run(_SUMMARY.replace("__ITEMS__", items).replace("__TOTAL__", str(n)), **kw)


def test_the_four_figures_are_native_metrics():
    at = _dash()
    assert {m.label: m.value for m in at.metric} == {
        "Open jobs": "1", "Applications": "12", "Interviewed": "7", "Final decisions": "3",
    }
    assert [t.value for t in at.title] == ["Dashboard"]


def test_the_summary_is_read_once_per_run_as_this_user():
    at = _dash()
    assert _calls(at, "summary") == [("summary", uuid.UUID(_ME))]
    at.run()
    assert len(_calls(at, "summary")) == 2                  # one per run, never more


def test_each_attention_row_shows_plain_text_type_job_code_and_candidate():
    at = _dash()
    texts = [t.value for t in at.text]
    for expected in ("Ratings missing", "Résumé missing", "Decision pending",
                     "V_001", "Ben Notes", "Cat Cole", "Dan Dee"):
        assert expected in texts, expected


def test_the_attention_count_is_stated():
    assert "3 items" in _text(_dash())


def test_a_capped_list_states_the_total():
    at = _dash(total=61)
    assert "Showing 3 of 61 items" in _text(at)


def test_an_empty_attention_list_says_so():
    at = _dash("[]")
    assert "Nothing needs attention right now." in _text(at)
    assert "0 items" in _text(at)


@pytest.mark.parametrize("key, label, expected", [
    ("dash_attention_0", "Add ratings", {"job": _JOB_A, "candidate": _APP, "tab": "interview"}),
    ("dash_attention_1", "Review", {"job": _JOB_A, "stage": "applicants"}),
    ("dash_attention_2", "Open decision", {"job": _JOB_A, "candidate": _APP, "tab": "decision"}),
])
def test_each_action_deep_links_to_its_destination(key, label, expected):
    at = _dash()
    assert _button(at, key).label == label
    _button(at, key).click().run()
    assert not at.exception
    assert _calls(at, "switch") == [("switch", "JOBS_PAGE", expected)]


def test_the_action_buttons_are_distinct_per_row():
    at = _dash()
    keys = [b.key for b in at.button if b.key.startswith("dash_attention_")]
    assert keys == ["dash_attention_0", "dash_attention_1", "dash_attention_2"]


def test_a_pipeline_block_shows_five_stages_with_counts_for_each_open_job():
    at = _dash()
    bars = at.get("progress")
    assert len(bars) == 5
    assert [b.proto.text for b in bars] == [
        "Applied · 10", "Screened · 8", "Shortlisted · 5", "Interviewed · 4", "Decided · 2",
    ]
    assert [b.proto.value for b in bars] == [100, 80, 50, 40, 20]
    assert "V_001" in _text(at) and "Backend Engineer" in _text(at)


def test_the_open_job_button_opens_the_workspace():
    at = _dash()
    _button(at, f"dash_open_{_JOB_A}").click().run()
    assert _calls(at, "switch") == [("switch", "JOBS_PAGE", {"job": _JOB_A})]


def test_pipeline_rows_are_pure_and_guard_against_zero_applicants():
    rows = D.pipeline_rows(D.JobPipeline(uuid.uuid4(), "V_1", "T", 0, 0, 0, 0, 0))
    assert [r[1:] for r in rows] == [(0, 0.0)] * 5
    rows = D.pipeline_rows(D.JobPipeline(uuid.uuid4(), "V_1", "T", 4, 4, 2, 1, 0))
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("Applied", 4, 1.0), ("Screened", 4, 1.0), ("Shortlisted", 2, 0.5),
        ("Interviewed", 1, 0.25), ("Decided", 0, 0.0),
    ]


def test_the_stage_words_are_the_candidate_pages():
    from app.utils.candidate_progress import STAGE_LABELS

    assert [r[0] for r in D.pipeline_rows(D.JobPipeline(uuid.uuid4(), "V", "T", 1, 1, 1, 1, 1))] == [
        STAGE_LABELS[k] for k in ("applied", "screened", "shortlisted", "interviewed", "decided")]


def test_no_jobs_shows_the_first_job_prompt():
    body = _SUMMARY.replace("__ITEMS__", "[]").replace("__TOTAL__", "0").replace(
        "jobs=2, open_jobs=1, applications=12, interviewed=7, decided=3",
        "jobs=0, open_jobs=0, applications=0, interviewed=0, decided=0").replace(
        "pipelines=(JobPipeline(JOB_A, \"V_001\", \"Backend Engineer\", 10, 8, 5, 4, 2),)",
        "pipelines=()")
    at = _run(body)
    assert "No jobs yet." in _text(at)
    assert ("page_link", "JOBS_PAGE", "Go to Jobs") in _calls(at)
    assert "No open jobs." in _text(at)


def test_open_jobs_do_not_show_the_no_open_jobs_note():
    at = _dash()
    assert "No open jobs." not in _text(at)


def test_an_inactive_account_gets_a_calm_message():
    at = _dash(raise_="unauthorized")
    assert any("no longer active" in e.value for e in at.error)
    assert not at.metric


def test_a_database_failure_is_a_calm_error():
    at = _dash(raise_="sql")
    assert any("Couldn't load the dashboard" in e.value for e in at.error)
    assert not at.metric


def test_an_invalid_session_user_is_told_so_and_nothing_is_read():
    at = _dash(session_id="not-a-uuid")
    assert any("session looks invalid" in e.value for e in at.error)
    assert _calls(at, "summary") == []


# --- the sign-in redirect still hands a workspace link on ----------------------------------------


def test_a_workspace_link_is_handed_to_the_jobs_page_before_anything_else():
    at = _dash(params={"job": _JOB_A, "stage": "interviews",
                       "candidate": _APP, "tab": "decision"})
    switches = _calls(at, "switch")
    assert switches[0] == ("switch", "JOBS_PAGE", {
        "job": _JOB_A, "stage": "interviews", "candidate": _APP, "tab": "decision"})


def test_without_a_link_the_dashboard_does_not_redirect():
    assert _calls(_dash(), "switch") == []


def test_a_malformed_job_parameter_is_ignored():
    at = _dash(params={"job": "not-a-uuid"})
    assert _calls(at, "switch") == []
    assert [m.label for m in at.metric]


# --- Quick find on the dashboard ----------------------------------------------------------------------


def test_the_dashboard_has_a_quick_find_box_and_does_not_search_on_load():
    at = _dash()
    assert [t.label for t in at.text_input] == ["Quick find"]
    assert any(b.label == "Find" for b in at.button)
    assert "Quick find" in _text(at)


# --- structure ------------------------------------------------------------------------------------------


def test_the_dashboard_module_emits_no_html_and_imports_no_ai():
    import ast
    from pathlib import Path

    for module in (D, Q):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "unsafe_allow_html" not in source and "<div" not in source
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] + [a.name for a in node.names])
                for n in names:
                    assert not n.startswith("app.ai") and "anthropic" not in n.lower(), n


# =========================================================== Quick find widget ================

_FIND = '''
RESULT = __RESULT__


def fake_find(db, query, *, acting_user_id, limit=8):
    CALLS.append(("find", query, acting_user_id))
    _maybe_raise()
    return list(RESULT)


Q.quick_find = fake_find
Q.render_quick_find(PAGES, uuid.UUID("__SESSION_ID__"), scope="t")
'''

_RESULTS = ('[FindMatch("job", JOB_A, None, "Backend Engineer", "V_001"), '
            'FindMatch("candidate", JOB_A, APP, "Ada Lovelace", "ada@x.test · V_001")]')


def _find(result=_RESULTS, **kw):
    return _run(_FIND.replace("__RESULT__", result), **kw)


def _submit(at, text):
    next(t for t in at.text_input if t.label == "Quick find").set_value(text)
    next(b for b in at.button if b.label == "Find").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_nothing_is_queried_before_submitting():
    at = _find()
    assert _calls(at, "find") == []
    at.run()
    assert _calls(at, "find") == []


def test_typing_alone_never_queries_so_there_is_no_typeahead():
    at = _find()
    next(t for t in at.text_input if t.label == "Quick find").set_value("ada").run()
    assert _calls(at, "find") == []


@pytest.mark.parametrize("short", ["a", " a ", "  "])
def test_a_query_under_two_characters_is_not_searched(short):
    at = _submit(_find(), short)
    assert _calls(at, "find") == []
    if short.strip():
        assert "Type at least 2 characters to search." in _text(at)
        assert not [b for b in at.button if b.key.startswith("quick_find_t_")]


def test_a_submitted_query_searches_once_and_lists_the_matches_as_buttons():
    at = _submit(_find(), "  ada  ")
    assert _calls(at, "find") == [("find", "ada", uuid.UUID(_ME))]
    labels = [b.label for b in at.button if b.key.startswith("quick_find_t_")
              and b.key != "quick_find_clear_t"]
    assert labels == ["Backend Engineer — V_001", "Ada Lovelace — ada@x.test · V_001"]


def test_clicking_a_candidate_opens_their_page():
    at = _submit(_find(), "ada")
    _button(at, "quick_find_t_1").click().run()
    assert not at.exception
    assert _calls(at, "switch") == [("switch", "JOBS_PAGE", {"job": _JOB_A, "candidate": _APP})]


def test_clicking_a_job_opens_its_workspace():
    at = _submit(_find(), "back")
    _button(at, "quick_find_t_0").click().run()
    assert _calls(at, "switch") == [("switch", "JOBS_PAGE", {"job": _JOB_A})]


def test_the_results_survive_the_rerun_a_click_causes():
    """The query is kept in session state, so the buttons are still drawn when the
    rerun that handles a click on one of them runs."""
    at = _submit(_find(), "ada")
    _button(at, "quick_find_t_0").click().run()
    assert any(b.key == "quick_find_t_1" for b in at.button)


def test_no_matches_says_so():
    at = _submit(_find("[]"), "zzz")
    assert "No matches." in _text(at)


def test_at_most_the_services_matches_are_shown():
    many = "[" + ", ".join(
        f'FindMatch("candidate", JOB_A, APP, "Person {i}", "p{i}@x.test · V_001")' for i in range(8)
    ) + "]"
    at = _submit(_find(many), "per")
    assert len([b for b in at.button if b.key.startswith("quick_find_t_")
                and b.key != "quick_find_clear_t"]) == 8


def test_clear_removes_the_results():
    at = _submit(_find(), "ada")
    _button(at, "quick_find_clear_t").click().run()
    assert not [b for b in at.button if b.key.startswith("quick_find_t_")]
    assert "quick_find_t_query" not in at.session_state


def test_the_query_is_passed_as_plain_text():
    hostile = "50%_'; DROP TABLE jobs;--"
    at = _submit(_find("[]"), hostile)
    assert _calls(at, "find")[0][1] == hostile              # untouched: binding is the service's job
    assert not at.exception


def test_whitespace_in_the_query_is_collapsed():
    at = _submit(_find(), "ada    lovelace")
    assert _calls(at, "find")[0][1] == "ada lovelace"


@pytest.mark.parametrize("raise_, expected", [
    ("unauthorized", "no longer active"), ("sql", "Couldn't search right now."),
])
def test_search_failures_are_calm(raise_, expected):
    at = _submit(_find(raise_=raise_), "ada")
    assert any(expected in e.value for e in at.error)
    assert not at.exception
