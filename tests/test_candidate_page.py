"""The candidate page (HR UI, Increment 3), through Streamlit's AppTest.

The header/list services and every tab body are stubbed — no database, no AI. The
tab bodies are SPIES that record when they run, which is what proves the page is
lazy: only the ACTIVE tab's renderer is ever called, and the header helper runs
once per script run.

The page is reached the way users reach it: through the Jobs page, which hands a
``?job=..&candidate=..`` URL to the job workspace, which renders the candidate page.

AppTest scripts rebind real page-module attributes process-wide, so the pristine
values are snapshotted at import time and restored before and after every test (see
tests/test_interviews_page_analysis_section.py).
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.candidate_page as CP
import app.pages.job_workspace as W
import app.pages.jobs as J
from app.utils.workspace_nav import CANDIDATE_TABS

_TIMEOUT = 60
_JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"
_A = "00000000-0000-4000-8000-00000000000a"
_B = "00000000-0000-4000-8000-00000000000b"
_C = "00000000-0000-4000-8000-00000000000c"
_FOREIGN = "00000000-0000-4000-8000-0000000000ff"

_C_ATTRS = (
    "session_scope", "get_candidate_header", "list_job_candidates",
    "_tab_overview", "_tab_screening", "_tab_interview", "_tab_analysis",
    "_tab_scorecard", "_tab_decision",
)
_W_ATTRS = (
    "session_scope", "get_job_stage_summary", "_stage_setup", "_stage_applicants",
    "_stage_shortlist", "_stage_interviews", "_stage_final_ranking",
)
_J_ATTRS = ("_render_create_form", "_render_job_list")
_PRISTINE = {
    CP: {n: getattr(CP, n) for n in _C_ATTRS},
    W: {n: getattr(W, n) for n in _W_ATTRS},
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


# The script every test runs. ``__ROLE__`` and ``__OVERRIDES__`` are substituted.
_SCRIPT = '''
import contextlib
import datetime
import uuid

import streamlit as st

import app.pages.candidate_page as CP
import app.pages.job_workspace as W
import app.pages.jobs as J
from app.services.job_workspace_service import JobCandidate
from app.utils.authorization import UnauthorizedError
from app.utils.session import SESSION_USER_KEY
from sqlalchemy.exc import SQLAlchemyError
from tests.test_candidate_page import _A, _B, _C, _JOB
from tests.test_candidate_progress import make_header
from tests.test_job_workspace_page import _build_summary

st.session_state["_ran"] = []
st.session_state["_header_calls"] = st.session_state.get("_header_calls", 0)
st.session_state["_list_calls"] = st.session_state.get("_list_calls", 0)
st.session_state["_summary_calls"] = st.session_state.get("_summary_calls", 0)

OVERRIDES = __OVERRIDES__          # {app id: {header field: value}}
RAISE = __RAISE__                  # None | "unauthorized" | "sql"
NAMES = {_A: "Ada Lovelace", _B: "Bo Builder", _C: "Cy Coder"}


@contextlib.contextmanager
def _scope():
    yield None


def _get_header(db, job_id, application_id, *, acting_user_id):
    st.session_state["_header_calls"] += 1
    if RAISE == "unauthorized":
        raise UnauthorizedError("no")
    if RAISE == "sql":
        raise SQLAlchemyError("boom")
    key = str(application_id)
    if str(job_id) != _JOB or key not in NAMES:
        return None
    return make_header(
        application_id=uuid.UUID(key), candidate_name=NAMES[key],
        **OVERRIDES.get(key, {}),
    )


def _list(db, job_id, *, acting_user_id):
    st.session_state["_list_calls"] += 1
    return [JobCandidate(uuid.UUID(k), n, i + 1) for i, (k, n) in enumerate(NAMES.items())]


def _summary(db, job_id, *, acting_user_id):
    st.session_state["_summary_calls"] += 1
    return _build_summary() if str(job_id) == _JOB else None


def _spy(name):
    def body(ctx):
        st.session_state["_ran"].append(name)
        st.write("BODY " + name)
    return body


CP.session_scope = _scope
CP.get_candidate_header = _get_header
CP.list_job_candidates = _list
for _tab in ("overview", "screening", "interview", "analysis", "scorecard", "decision"):
    setattr(CP, "_tab_" + _tab, _spy(_tab))

W.session_scope = _scope
W.get_job_stage_summary = _summary
for _stage in ("setup", "applicants", "shortlist", "interviews", "final_ranking"):
    setattr(W, "_stage_" + _stage, lambda job_id, uid, _s=_stage: st.write("STAGE " + _s))

J._render_create_form = lambda uid: None
J._render_job_list = lambda uid: None

st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test", "full_name": "P", "role": "__ROLE__",
}
J.render_jobs_page()
'''


def _app(role: str = "HIRING_MANAGER", overrides: dict | None = None,
         raise_: str | None = None, **params: str) -> AppTest:
    script = (
        _SCRIPT.replace("__ROLE__", role)
        .replace("__OVERRIDES__", repr(overrides or {}))
        .replace("__RAISE__", repr(raise_))
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT)
    for key, value in params.items():
        at.query_params[key] = value
    return at.run()


def _ok(at: AppTest) -> AppTest:
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _qp(at: AppTest, name: str):
    value = at.query_params.get(name)
    return value[0] if isinstance(value, list) else value


def _button(at: AppTest, key: str):
    return next(b for b in at.button if b.key == key)


def _keys(at: AppTest) -> list[str]:
    return [b.key for b in at.button]


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [w.value for w in at.warning] + [e.value for e in at.error]
    parts += [i.value for i in at.info] + [t.value for t in at.title]
    return "\n".join(str(p) for p in parts)


# --- reaching the page ----------------------------------------------------------------


def test_a_candidate_parameter_renders_the_candidate_page_not_the_workspace():
    at = _ok(_app(job=_JOB, candidate=_A))
    assert [t.value for t in at.title] == ["Ada Lovelace"]
    assert at.session_state["_ran"] == ["overview"]
    assert at.session_state["_summary_calls"] == 0            # the workspace never loaded
    assert list(at.segmented_control[0].options) == [
        "Overview", "Screening", "Interview", "AI analysis", "Scorecard", "Decision",
    ]


def test_without_a_candidate_the_workspace_is_shown_as_before():
    at = _ok(_app(job=_JOB, stage="shortlist"))
    assert "STAGE shortlist" in _text(at)
    assert at.session_state["_header_calls"] == 0
    assert at.session_state["_ran"] == []


def test_the_header_shows_name_email_and_job_code():
    at = _ok(_app(job=_JOB, candidate=_A))
    assert [t.value for t in at.title] == ["Ada Lovelace"]
    body = _text(at)
    assert "ada@example.test" in body and "Backend Engineer" in body
    assert "V_001" in body


def test_the_four_figure_strip_uses_plain_text_when_a_number_is_missing():
    at = _ok(_app(job=_JOB, candidate=_A))
    values = {m.label: m.value for m in at.metric}
    assert values == {
        "Screening": "8.00 / 10", "Interview": "Not available",
        "Final score": "Not available", "Rank": "Not ranked yet",
    }


def test_the_strip_shows_the_stored_numbers():
    at = _ok(_app(job=_JOB, candidate=_A, overrides={_A: dict(
        interview_score="9", final_score="8.6", final_rank=1, final_ranked_count=3,
        entry_status="RANKED", has_current_final_ranking=True, rounds_count=2,
    )}))
    values = {m.label: m.value for m in at.metric}
    assert values["Interview"] == "9.00 / 10" and values["Final score"] == "8.60 / 10"
    assert values["Rank"] == "#1 of 3 ranked"


# --- tabs -------------------------------------------------------------------------------


def test_the_default_tab_is_overview():
    at = _ok(_app(job=_JOB, candidate=_A))
    assert at.segmented_control[0].value == "overview"


@pytest.mark.parametrize("bad", ["bogus", "", "OVERVIEW", "scorecard "])
def test_an_unknown_tab_falls_back_to_overview(bad):
    at = _ok(_app(job=_JOB, candidate=_A, tab=bad))
    if bad.strip() == "scorecard":
        assert at.segmented_control[0].value == "scorecard"      # stripped, then valid
    else:
        assert at.segmented_control[0].value == "overview"
        assert at.session_state["_ran"] == ["overview"]
    assert not at.warning                                        # no message for a bad tab


@pytest.mark.parametrize("tab", CANDIDATE_TABS)
def test_only_the_active_tabs_body_runs(tab):
    at = _ok(_app(job=_JOB, candidate=_A, tab=tab))
    assert at.session_state["_ran"] == [tab]
    assert f"BODY {tab}" in _text(at)


def test_header_helpers_run_once_per_script_run():
    at = _ok(_app(job=_JOB, candidate=_A, tab="decision"))
    assert at.session_state["_header_calls"] == 1
    assert at.session_state["_list_calls"] == 1
    at.segmented_control[0].set_value("scorecard").run()
    _ok(at)
    assert at.session_state["_header_calls"] == 2                # one more run, one more call
    assert at.session_state["_list_calls"] == 2


def test_choosing_a_tab_writes_it_to_the_url_and_runs_only_that_body():
    at = _ok(_app(job=_JOB, candidate=_A, tab="overview"))
    at.segmented_control[0].set_value("interview").run()
    _ok(at)
    assert _qp(at, "tab") == "interview"
    assert at.session_state["_ran"] == ["interview"]             # overview did NOT run again
    assert at.segmented_control[0].value == "interview"
    assert _qp(at, "candidate") == _A and _qp(at, "job") == _JOB


def test_an_empty_selection_falls_back_to_the_tab_in_the_url():
    at = _ok(_app(job=_JOB, candidate=_A, tab="screening"))
    at.segmented_control[0].set_value(None).run()
    _ok(at)
    assert at.segmented_control[0].value == "screening"
    assert at.session_state["_ran"] == ["screening"]


# --- progress ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key, tab", [
    ("applied", "overview"), ("screened", "screening"), ("shortlisted", "overview"),
    ("interviewed", "interview"), ("analysis", "analysis"), ("ranked", "scorecard"),
    ("decided", "decision"),
])
def test_progress_buttons_switch_tabs(key, tab):
    at = _ok(_app(job=_JOB, candidate=_A, tab="screening" if tab != "screening" else "decision"))
    _button(at, f"cp_prog_{key}").click().run()
    _ok(at)
    assert _qp(at, "tab") == tab
    assert at.segmented_control[0].value == tab
    assert at.session_state["_ran"] == [tab]


def test_progress_buttons_state_status_in_words():
    at = _ok(_app(job=_JOB, candidate=_A, overrides={_A: dict(
        rounds_count=1, has_unrated_round=True)}))
    labels = {b.key: b.label for b in at.button}
    assert labels["cp_prog_interviewed"] == "Interviewed — ratings missing"
    assert labels["cp_prog_analysis"] == "AI analysis (optional) — not generated"
    assert labels["cp_prog_decided"] == "Decided"


# --- next step ----------------------------------------------------------------------------------


def test_next_step_with_no_ratings_goes_to_the_interview_tab():
    at = _ok(_app(job=_JOB, candidate=_A, tab="overview"))
    assert _button(at, "cp_next_step").label == "Go to Interview"
    _button(at, "cp_next_step").click().run()
    _ok(at)
    assert _qp(at, "tab") == "interview"
    assert at.session_state["_ran"] == ["interview"]


def test_next_step_when_decided_views_the_decision():
    at = _ok(_app(job=_JOB, candidate=_A, overrides={_A: dict(
        rounds_count=1, decision="HOLD", decided_by_name="Mia")}))
    assert _button(at, "cp_next_step").label == "View decision"
    _button(at, "cp_next_step").click().run()
    _ok(at)
    assert _qp(at, "tab") == "decision"


def test_next_step_record_decision_opens_the_dialog_with_the_existing_form():
    at = _ok(_app(job=_JOB, candidate=_A, overrides={_A: dict(rounds_count=1)}))
    assert _button(at, "cp_next_step").label == "Record decision"
    _button(at, "cp_next_step").click().run()
    _ok(at)
    assert [r.label for r in at.radio] == ["Your decision"]
    assert [t.label for t in at.text_area] == ["Rationale (required)"]
    assert any("I have reviewed the scorecard" in c.label for c in at.checkbox)


def test_a_non_decider_gets_no_record_decision_button_on_next_step():
    at = _ok(_app(role="HR", job=_JOB, candidate=_A, overrides={_A: dict(rounds_count=1)}))
    assert "cp_next_step" not in _keys(at)
    assert "A hiring manager or an admin records the final decision." in _text(at)


# --- the decision button and role gating -----------------------------------------------------------


@pytest.mark.parametrize("role", ["HIRING_MANAGER", "ADMIN"])
def test_deciders_see_the_header_button(role):
    at = _ok(_app(role=role, job=_JOB, candidate=_A))
    assert _button(at, "cp_decide_header").label == "Record decision"


@pytest.mark.parametrize("role", ["HR", "SYSTEM", "", "SOMETHING"])
def test_everyone_else_sees_no_decision_button_anywhere(role):
    for tab in ("overview", "decision"):
        at = _ok(_app(role=role, job=_JOB, candidate=_A, tab=tab))
        assert not [k for k in _keys(at) if k.startswith("cp_decide")]


def test_the_button_says_change_when_a_decision_exists():
    at = _ok(_app(job=_JOB, candidate=_A, overrides={_A: dict(
        rounds_count=1, decision="PROCEED", decided_by_name="Mia")}))
    assert _button(at, "cp_decide_header").label == "Change decision"


def test_clicking_the_header_button_opens_the_dialog_on_any_tab():
    at = _ok(_app(job=_JOB, candidate=_A, tab="scorecard"))
    _button(at, "cp_decide_header").click().run()
    _ok(at)
    assert [r.label for r in at.radio] == ["Your decision"]
    assert _qp(at, "tab") == "scorecard"                     # the tab is untouched


def test_the_dialog_form_says_change_for_an_existing_decision():
    at = _ok(_app(job=_JOB, candidate=_A, overrides={_A: dict(
        rounds_count=1, decision="PROCEED", decided_by_name="Mia")}))
    _button(at, "cp_decide_header").click().run()
    _ok(at)
    assert any(b.label == "Change final decision" for b in at.button)


# --- the decision summary in the right panel ---------------------------------------------------------


def test_the_summary_says_when_nothing_is_recorded():
    at = _ok(_app(job=_JOB, candidate=_A))
    assert "Not recorded yet. Recorded decisions do not notify the candidate" in _text(at)


def test_the_summary_shows_badge_text_who_and_when_but_no_rationale():
    from datetime import datetime, timezone

    at = _ok(_app(job=_JOB, candidate=_A, overrides={_A: dict(
        decision="REJECT", decided_by_name="Mia Manager",
        decided_at=datetime(2026, 10, 6, tzinfo=timezone.utc))}))
    body = _text(at)
    assert "Final decision: Reject" in body                  # text, not only colour
    assert "Decided by Mia Manager on 2026-10-06." in body


# --- previous / next candidate ---------------------------------------------------------------------------


def test_the_switcher_shows_the_position_and_disables_the_ends():
    first = _ok(_app(job=_JOB, candidate=_A))
    assert "1 of 3 candidates" in _text(first)
    assert _button(first, "cp_prev").disabled and not _button(first, "cp_next").disabled
    last = _ok(_app(job=_JOB, candidate=_C))
    assert "3 of 3 candidates" in _text(last)
    assert _button(last, "cp_next").disabled and not _button(last, "cp_prev").disabled
    middle = _ok(_app(job=_JOB, candidate=_B))
    assert "2 of 3 candidates" in _text(middle)
    assert not _button(middle, "cp_prev").disabled and not _button(middle, "cp_next").disabled


def test_next_opens_the_next_candidate_and_keeps_the_tab():
    at = _ok(_app(job=_JOB, candidate=_A, tab="scorecard", stage="interviews"))
    _button(at, "cp_next").click().run()
    _ok(at)
    assert _qp(at, "candidate") == _B and _qp(at, "tab") == "scorecard"
    assert _qp(at, "stage") == "interviews"
    assert [t.value for t in at.title] == ["Bo Builder"]
    assert at.session_state["_ran"] == ["scorecard"]


def test_previous_opens_the_previous_candidate():
    at = _ok(_app(job=_JOB, candidate=_C))
    _button(at, "cp_prev").click().run()
    _ok(at)
    assert [t.value for t in at.title] == ["Bo Builder"]
    assert _qp(at, "candidate") == _B


# --- breadcrumb -------------------------------------------------------------------------------------------


def test_the_breadcrumb_job_title_returns_to_the_workspace_keeping_the_stage():
    at = _ok(_app(job=_JOB, candidate=_A, tab="decision", stage="shortlist"))
    _button(at, "cp_crumb_job").click().run()
    _ok(at)
    assert _qp(at, "candidate") is None and _qp(at, "tab") is None
    assert _qp(at, "job") == _JOB and _qp(at, "stage") == "shortlist"
    assert "STAGE shortlist" in _text(at)
    assert at.segmented_control[0].value == "shortlist"


def test_the_breadcrumb_jobs_button_returns_to_the_job_list():
    at = _ok(_app(job=_JOB, candidate=_A))
    _button(at, "cp_crumb_jobs").click().run()
    _ok(at)
    assert len(at.query_params) == 0
    assert not at.segmented_control


def test_the_breadcrumb_names_the_job_and_the_candidate():
    at = _ok(_app(job=_JOB, candidate=_A))
    assert _button(at, "cp_crumb_jobs").label == "Jobs"
    assert _button(at, "cp_crumb_job").label == "Backend Engineer"
    assert "/  Ada Lovelace" in _text(at)


# --- invalid candidates ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["not-a-uuid", "123", "'; DROP TABLE applications;--"])
def test_a_malformed_candidate_gets_a_calm_message_and_a_back_button(bad):
    at = _ok(_app(job=_JOB, candidate=bad, tab="decision"))
    assert any("could not be found" in w.value for w in at.warning)
    assert "cp_back_to_workspace" in _keys(at)
    assert not at.segmented_control
    assert at.session_state["_ran"] == [] and at.session_state["_header_calls"] == 0


@pytest.mark.parametrize("who", [_FOREIGN, "11111111-2222-4333-8444-555555555555"])
def test_a_candidate_of_another_job_or_unknown_gets_the_same_calm_message(who):
    at = _ok(_app(job=_JOB, candidate=who))
    assert any("could not be found for this job" in w.value for w in at.warning)
    assert "cp_back_to_workspace" in _keys(at)
    assert at.session_state["_ran"] == []
    assert not any("Traceback" in str(e.value) for e in at.exception)


def test_a_known_candidate_under_another_job_is_refused():
    at = _ok(_app(job="99999999-9999-4999-8999-999999999999", candidate=_A))
    assert any("could not be found for this job" in w.value for w in at.warning)


def test_the_back_button_returns_to_the_workspace_without_a_loop():
    at = _ok(_app(job=_JOB, candidate=_FOREIGN, stage="applicants"))
    _button(at, "cp_back_to_workspace").click().run()
    _ok(at)
    assert _qp(at, "candidate") is None
    assert _qp(at, "stage") == "applicants"
    assert "STAGE applicants" in _text(at)
    again = at.run()
    _ok(again)
    assert not again.warning                                  # stays on the workspace


def test_an_invalid_session_user_is_told_so():
    script = (
        _SCRIPT.replace("__ROLE__", "HR").replace("__OVERRIDES__", "{}")
        .replace("__RAISE__", "None")
        .replace('"id": "11111111-1111-1111-1111-111111111111"', '"id": "not-a-uuid"')
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT)
    at.query_params["job"] = _JOB
    at.query_params["candidate"] = _A
    at.run()
    _ok(at)
    assert any("session looks invalid" in e.value for e in at.error)
    assert at.session_state["_header_calls"] == 0


def test_an_inactive_account_is_told_so_calmly():
    at = _ok(_app(raise_="unauthorized", job=_JOB, candidate=_A))
    assert any("no longer active" in e.value for e in at.error)
    assert at.session_state["_ran"] == []


def test_a_database_failure_is_a_calm_error_with_a_way_back():
    at = _ok(_app(raise_="sql", job=_JOB, candidate=_A))
    assert any("Couldn't load this candidate" in e.value for e in at.error)
    assert "cp_back_to_workspace" in _keys(at)
    assert at.session_state["_ran"] == []


# --- plain text only --------------------------------------------------------------------------------------------


def test_no_html_is_emitted_by_the_page():
    import inspect

    source = inspect.getsource(CP)
    assert "unsafe_allow_html" not in source
    assert "<style" not in source and "<div" not in source


def test_the_page_makes_no_ai_call_and_no_write():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(CP))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [a.name for a in node.names]
            )
            for name in names:
                low = name.lower()
                assert not low.startswith("app.ai") and "claude" not in low, name
                assert "audit_service" not in low, name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"add", "add_all", "delete", "commit", "flush"}
