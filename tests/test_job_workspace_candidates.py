"""The workspace's candidate entry points (HR UI, Increment 3), through AppTest.

* Interviews stage  -> a read-only overview table; selecting a row opens the page.
* Shortlist stage   -> the existing read-only table, now with row opening.
* Applicants / Final ranking -> their reused sections, plus an "Open candidate page"
  selector at the top.
* Setup             -> unchanged (no selector, no table).

Services and the reused sections are stubbed (no database, no AI). A ``st.dataframe``
row selection cannot be clicked in AppTest, but its state can be set before a run
(the same state a click writes), which is what drives "select a row" here; the real
click is verified in the browser.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.candidate_page as CP
import app.pages.job_workspace as W
import app.pages.jobs as J

_TIMEOUT = 60
_JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"
_A = "00000000-0000-4000-8000-00000000000a"
_B = "00000000-0000-4000-8000-00000000000b"
_C = "00000000-0000-4000-8000-00000000000c"

_W_ATTRS = (
    "session_scope", "get_job_stage_summary", "list_interview_overview",
    "list_job_candidates", "get_shortlisted_candidates_for_job", "list_feedback_views",
    "_render_ranking_section", "_render_applications_tab",
    "_render_final_ranking_section", "_stage_setup",
)
_C_ATTRS = ("session_scope", "get_candidate_header", "list_job_candidates",
            "_tab_overview")
_J_ATTRS = ("_render_create_form", "_render_job_list")
_PRISTINE = {
    W: {n: getattr(W, n) for n in _W_ATTRS},
    CP: {n: getattr(CP, n) for n in _C_ATTRS},
    J: {n: getattr(J, n) for n in _J_ATTRS},
}


def _restore():
    for module, attrs in _PRISTINE.items():
        for name, value in attrs.items():
            setattr(module, name, value)


@pytest.fixture(autouse=True)
def _restore_modules():
    _restore()
    try:
        yield
    finally:
        _restore()


_SCRIPT = '''
import contextlib
import uuid
from types import SimpleNamespace as NS

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import app.pages.candidate_page as CP
import app.pages.job_workspace as W
import app.pages.jobs as J
from app.services.job_workspace_service import InterviewOverviewRow, JobCandidate
from app.utils.authorization import UnauthorizedError
from app.utils.session import SESSION_USER_KEY
from tests.test_candidate_progress import make_header
from tests.test_candidate_page import _A, _B, _C, _JOB
from tests.test_job_workspace_page import _build_summary

st.session_state.setdefault("_ran", [])
RAISE = __RAISE__
EMPTY = __EMPTY__


@contextlib.contextmanager
def _scope():
    yield None


def _maybe_raise():
    if RAISE == "unauthorized":
        raise UnauthorizedError("no")
    if RAISE == "sql":
        raise SQLAlchemyError("boom")


def _u(i):
    return uuid.UUID(i)


def _overview(db, job_id, *, acting_user_id):
    _maybe_raise()
    if EMPTY:
        return []
    return [
        InterviewOverviewRow(_u(__A__), "Ada Lovelace", 2, False, 1, True, "PROCEED"),
        InterviewOverviewRow(_u(__B__), "Bo Builder", 1, True, 0, False, None),
        InterviewOverviewRow(_u(__C__), "Cy Coder", 0, False, 0, False, None),
    ]


def _candidates(db, job_id, *, acting_user_id):
    _maybe_raise()
    if EMPTY:
        return []
    return [JobCandidate(_u(__A__), "Ada Lovelace", 1), JobCandidate(_u(__B__), "Bo Builder", 2),
            JobCandidate(_u(__C__), "Cy Coder", None)]


def _shortlisted(db, *, job_id, acting_user_id):
    _maybe_raise()
    if EMPTY:
        return []
    return [
        NS(application_id=_u(__A__), candidate_name="Ada Lovelace",
           current_rank_available=True, current_rank_position=1,
           rank_position_at_decision=1, guide_exists=True),
        NS(application_id=_u(__B__), candidate_name="Bo Builder",
           current_rank_available=False, current_rank_position=None,
           rank_position_at_decision=2, guide_exists=False),
    ]


def _spy(name):
    def body(*args, **kwargs):
        st.session_state["_ran"].append(name)
        st.write("SECTION " + name)
    return body


def _header(db, job_id, application_id, *, acting_user_id):
    names = {__A__: "Ada Lovelace", __B__: "Bo Builder", __C__: "Cy Coder"}
    key = str(application_id)
    if key not in names:
        return None
    return make_header(application_id=uuid.UUID(key), candidate_name=names[key])


W.session_scope = _scope
W.get_job_stage_summary = lambda db, job_id, *, acting_user_id: _build_summary()
W.list_interview_overview = _overview
W.list_job_candidates = _candidates
W.get_shortlisted_candidates_for_job = _shortlisted
W.list_feedback_views = lambda db, app_id, *, acting_user_id: ["round"] * (2 if str(app_id) == __A__ else 0)
W._render_ranking_section = _spy("ranking")
W._render_applications_tab = _spy("applications")
W._render_final_ranking_section = _spy("final_ranking")
W._stage_setup = _spy("setup")

CP.session_scope = _scope
CP.get_candidate_header = _header
CP.list_job_candidates = _candidates
CP._tab_overview = lambda ctx: st.write("CANDIDATE OVERVIEW " + ctx.header.candidate_name)

J._render_create_form = lambda uid: None
J._render_job_list = lambda uid: None
st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test", "full_name": "P", "role": "HIRING_MANAGER",
}
J.render_jobs_page()
'''


def _app(stage: str, *, raise_: str | None = None, empty: bool = False,
         **extra: str) -> AppTest:
    script = (
        _SCRIPT.replace("__RAISE__", repr(raise_)).replace("__EMPTY__", repr(empty))
        .replace("__A__", repr(_A)).replace("__B__", repr(_B)).replace("__C__", repr(_C))
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT)
    at.query_params["job"] = _JOB
    at.query_params["stage"] = stage
    for key, value in extra.items():
        at.query_params[key] = value
    return at.run()


def _ok(at: AppTest) -> AppTest:
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _qp(at: AppTest, name: str):
    value = at.query_params.get(name)
    return value[0] if isinstance(value, list) else value


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [w.value for w in at.warning] + [e.value for e in at.error]
    parts += [i.value for i in at.info] + [s.value for s in at.subheader]
    return "\n".join(str(p) for p in parts)


def _select_row(at: AppTest, key: str, row: int) -> AppTest:
    at.session_state[key] = {"selection": {"rows": [row], "columns": [], "cells": []}}
    return at.run()


# --- Interviews: the read-only overview table ---------------------------------------------


def test_the_interviews_stage_is_an_overview_table_with_the_specified_columns():
    at = _ok(_app("interviews"))
    df = at.dataframe[0].value
    assert list(df.columns) == [
        "Candidate", "Rounds", "Feedback", "Transcripts", "AI analysis", "Final decision",
    ]
    assert df.to_dict("records") == [
        {"Candidate": "Ada Lovelace", "Rounds": 2, "Feedback": "Rated",
         "Transcripts": 1, "AI analysis": "Generated", "Final decision": "Proceed"},
        {"Candidate": "Bo Builder", "Rounds": 1, "Feedback": "Ratings missing",
         "Transcripts": 0, "AI analysis": "Not generated", "Final decision": "Not decided"},
        {"Candidate": "Cy Coder", "Rounds": 0, "Feedback": "None recorded",
         "Transcripts": 0, "AI analysis": "Not generated", "Final decision": "Not decided"},
    ]


def test_the_interviews_stage_no_longer_renders_the_per_candidate_cards():
    at = _ok(_app("interviews"))
    assert at.session_state["_ran"] == []                 # no reused section ran
    assert "candidate's page" in _text(at)                # and the table points to the page
    assert not [b for b in at.button if b.key.startswith(("guide_", "pia_", "fd_", "fsc_"))]


@pytest.mark.parametrize("stage", ["interviews", "shortlist"])
def test_the_tables_are_single_row_selectable_and_rerun_on_select(stage):
    at = _ok(_app(stage))
    proto = at.dataframe[0].proto
    # SelectionMode.SINGLE_ROW == 0 in streamlit.proto.Dataframe_pb2
    assert list(proto.selection_mode) == [type(proto).SelectionMode.SINGLE_ROW]
    assert proto.form_id == ""                            # not inside a form


def test_an_empty_interview_stage_says_so():
    at = _ok(_app("interviews", empty=True))
    assert not at.dataframe
    assert "No candidates are in the interview stage for this job yet." in _text(at)


@pytest.mark.parametrize("raise_, expected", [
    ("unauthorized", "no longer active"),
    ("sql", "Couldn't load the interviews right now."),
])
def test_interview_stage_load_failures_are_calm(raise_, expected):
    at = _ok(_app("interviews", raise_=raise_))
    assert any(expected in e.value for e in at.error)
    assert not at.dataframe


@pytest.mark.parametrize("row, expected_id, expected_name", [
    (0, _A, "Ada Lovelace"), (1, _B, "Bo Builder"), (2, _C, "Cy Coder"),
])
def test_selecting_an_interviews_row_opens_that_candidates_page(row, expected_id, expected_name):
    at = _ok(_app("interviews"))
    _select_row(at, f"ws_interviews_table_{_JOB}", row)
    _ok(at)
    assert _qp(at, "candidate") == expected_id
    assert _qp(at, "stage") == "interviews" and _qp(at, "job") == _JOB   # stage kept
    assert [t.value for t in at.title] == [expected_name]
    assert f"CANDIDATE OVERVIEW {expected_name}" in _text(at)


def test_opening_from_the_table_does_not_loop():
    at = _ok(_app("interviews"))
    _select_row(at, f"ws_interviews_table_{_JOB}", 1)
    _ok(at)
    again = at.run()
    _ok(again)
    assert _qp(again, "candidate") == _B
    assert [t.value for t in again.title] == ["Bo Builder"]


def test_no_selection_opens_nothing():
    at = _ok(_app("interviews"))
    assert _qp(at, "candidate") is None
    assert at.session_state["_ran"] == []


# --- Shortlist: the same table, now with row opening ---------------------------------------


def test_the_shortlist_table_keeps_its_columns_and_values():
    at = _ok(_app("shortlist"))
    df = at.dataframe[0].value
    assert list(df.columns) == [
        "Candidate", "Screening rank", "Interview guide", "Rounds recorded",
    ]
    assert df.to_dict("records") == [
        {"Candidate": "Ada Lovelace", "Screening rank": "#1",
         "Interview guide": "Generated", "Rounds recorded": 2},
        {"Candidate": "Bo Builder", "Screening rank": "#2 (at shortlisting)",
         "Interview guide": "Not generated", "Rounds recorded": 0},
    ]


def test_selecting_a_shortlist_row_opens_that_candidate():
    at = _ok(_app("shortlist"))
    _select_row(at, f"ws_shortlist_table_{_JOB}", 1)
    _ok(at)
    assert _qp(at, "candidate") == _B and _qp(at, "stage") == "shortlist"
    assert [t.value for t in at.title] == ["Bo Builder"]


def test_an_empty_shortlist_says_so_as_before():
    at = _ok(_app("shortlist", empty=True))
    assert "No candidates are shortlisted for this job yet." in _text(at)
    assert not at.dataframe


# --- Applicants / Final ranking: reused sections + the selector --------------------------------


@pytest.mark.parametrize("stage, key, sections", [
    ("applicants", f"ws_open_applicants_{_JOB}", ["ranking", "applications"]),
    ("final_ranking", f"ws_open_final_ranking_{_JOB}", ["final_ranking"]),
])
def test_applicants_and_final_ranking_keep_their_sections_and_gain_the_selector(
    stage, key, sections
):
    at = _ok(_app(stage))
    assert at.session_state["_ran"] == sections           # the reused sections still run
    selector = at.selectbox(key=key)
    assert selector.label == "Open candidate page"
    assert selector.value is None
    assert list(selector.options) == [
        "Ada Lovelace (screening rank #1)", "Bo Builder (screening rank #2)",
        "Cy Coder (not ranked)",
    ]


@pytest.mark.parametrize("stage, key", [
    ("applicants", f"ws_open_applicants_{_JOB}"),
    ("final_ranking", f"ws_open_final_ranking_{_JOB}"),
])
def test_choosing_a_candidate_in_the_selector_opens_their_page(stage, key):
    at = _ok(_app(stage))
    at.selectbox(key=key).set_value(_B).run()
    _ok(at)
    assert _qp(at, "candidate") == _B and _qp(at, "stage") == stage
    assert [t.value for t in at.title] == ["Bo Builder"]


@pytest.mark.parametrize("stage", ["applicants", "final_ranking"])
def test_the_selector_is_omitted_when_the_job_has_no_candidates(stage):
    at = _ok(_app(stage, empty=True))
    assert not at.selectbox


@pytest.mark.parametrize("stage, raise_, expected", [
    ("applicants", "unauthorized", "no longer active"),
    ("final_ranking", "sql", "Couldn't load the candidate list right now."),
])
def test_selector_load_failures_are_calm_and_the_sections_still_run(stage, raise_, expected):
    at = _ok(_app(stage, raise_=raise_))
    assert any(expected in e.value for e in at.error)
    assert not at.selectbox
    assert at.session_state["_ran"]                       # the reused section is unaffected


def test_setup_has_no_selector_and_no_table():
    at = _ok(_app("setup"))
    assert not at.selectbox and not at.dataframe
    assert at.session_state["_ran"] == ["setup"]
