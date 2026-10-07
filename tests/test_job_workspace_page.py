"""The job workspace page (HR UI, Increment 2), through Streamlit's AppTest.

The service layer and every stage body are stubbed — no database, no AI. The stage
bodies are SPIES that record when they run, which is what proves the workspace is
lazy: only the active stage's renderer is ever called, and the summary service is
the only thing that runs for the chrome.

Streamlit's AppTest scripts rebind real page-module attributes process-wide, so the
pristine values are snapshotted at import time and restored before and after every
test (see tests/test_interviews_page_analysis_section.py).
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.job_workspace as W
import app.pages.jobs as J
from app.services.job_workspace_service import STAGE_KEYS

_TIMEOUT = 60
_JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"

_W_ATTRS = (
    "session_scope", "get_job_stage_summary", "_stage_setup", "_stage_applicants",
    "_stage_shortlist", "_stage_interviews", "_stage_final_ranking",
)
_J_ATTRS = ("_render_create_form", "_render_job_list")
_PRISTINE_W = {n: getattr(W, n) for n in _W_ATTRS}
_PRISTINE_J = {n: getattr(J, n) for n in _J_ATTRS}


def _restore():
    for n, v in _PRISTINE_W.items():
        setattr(W, n, v)
    for n, v in _PRISTINE_J.items():
        setattr(J, n, v)


@pytest.fixture(autouse=True)
def _restore_modules():
    _restore()
    try:
        yield
    finally:
        _restore()


# The script every test runs: stubs the summary and the five stage bodies, then
# renders the Jobs page (which dispatches to the workspace when ?job= is set).
_SCRIPT = '''
import contextlib
import streamlit as st

import app.pages.job_workspace as W
import app.pages.jobs as J
from app.services.job_workspace_service import StageSummary
from app.utils.session import SESSION_USER_KEY
from tests.test_job_workspace_helpers import _summary
from tests.test_job_workspace_page import _JOB, _build_summary

st.session_state["_ran"] = []
st.session_state["_summary_calls"] = st.session_state.get("_summary_calls", 0)


@contextlib.contextmanager
def _scope():
    yield None


def _get_summary(db, job_id, *, acting_user_id):
    st.session_state["_summary_calls"] += 1
    if str(job_id) != _JOB:
        return None
    return _build_summary()


def _spy(name):
    def body(job_id, acting_user_id):
        st.session_state["_ran"].append(name)
        st.write("BODY " + name)
    return body


W.session_scope = _scope
W.get_job_stage_summary = _get_summary
W._stage_setup = _spy("setup")
W._stage_applicants = _spy("applicants")
W._stage_shortlist = _spy("shortlist")
W._stage_interviews = _spy("interviews")
W._stage_final_ranking = _spy("final_ranking")

J._render_create_form = lambda uid: None


def _stand_in_job_list(uid):
    """A minimal job list for the workspace tests. The real list is a table whose row
    selection opens the workspace (tests/test_jobs_table_page.py); the old card list
    these buttons came from was removed in Increment 5. What these tests need is only
    a way to open the workspace and to recognise "the list" again afterwards."""
    from app.utils.workspace_nav import open_workspace

    st.button("Open workspace", key=f"ws_open_{_JOB}", on_click=open_workspace,
              args=(_JOB,))


J._render_job_list = _stand_in_job_list

st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test", "full_name": "P", "role": "HR",
}
J.render_jobs_page()
'''


def _build_summary():
    from app.services.job_workspace_service import (
        JobHeaderFacts, JobStageSummary, StageSummary,
    )

    return JobStageSummary(
        header=JobHeaderFacts(uuid.UUID(_JOB), "V_001", "Backend Engineer", "OPEN", 1),
        setup=StageSummary(1, True, 0),
        applicants=StageSummary(4, True, 2),
        shortlist=StageSummary(2, True, 0),
        interviews=StageSummary(0, False, 0),
        final_ranking=StageSummary(0, False, 0),
        interviewed_count=0,
    )


def _app(**params: str) -> AppTest:
    at = AppTest.from_string(_SCRIPT, default_timeout=_TIMEOUT)
    for key, value in params.items():
        at.query_params[key] = value
    return at.run()


def _ok(at: AppTest) -> AppTest:
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [w.value for w in at.warning] + [e.value for e in at.error]
    parts += [t.value for t in at.title]
    return "\n".join(str(p) for p in parts)


def _button(at: AppTest, key: str):
    return next(b for b in at.button if b.key == key)


def _keys(at: AppTest) -> list[str]:
    return [b.key for b in at.button]


# --- list -> workspace -------------------------------------------------------------


def test_the_list_alone_shows_no_workspace_and_runs_no_stage_body():
    at = _ok(_app())
    assert f"ws_open_{_JOB}" in _keys(at)
    assert not at.segmented_control                      # still the list, no workspace
    assert at.session_state["_ran"] == []                # and no stage body ran


def test_opening_a_job_puts_it_in_the_url_and_shows_its_workspace():
    at = _ok(_app())
    _button(at, f"ws_open_{_JOB}").click().run()
    _ok(at)
    assert at.query_params["job"] == [_JOB] or at.query_params["job"] == _JOB
    assert len(at.segmented_control) == 1
    assert "Backend Engineer" in _text(at)


def test_opening_lands_on_the_first_incomplete_stage():
    at = _ok(_app())
    _button(at, f"ws_open_{_JOB}").click().run()
    _ok(at)
    # setup, applicants, shortlist are complete in the stub -> interviews
    assert at.segmented_control[0].value == "interviews"
    assert at.session_state["_ran"] == ["interviews"]


# --- deep links ------------------------------------------------------------------------


@pytest.mark.parametrize("stage", STAGE_KEYS)
def test_a_deep_link_shows_that_stage_and_runs_only_its_body(stage):
    at = _ok(_app(job=_JOB, stage=stage))
    assert at.segmented_control[0].value == stage
    assert at.session_state["_ran"] == [stage]
    assert f"BODY {stage}" in _text(at)


def test_a_deep_link_without_a_stage_uses_the_default():
    at = _ok(_app(job=_JOB))
    assert at.session_state["_ran"] == ["interviews"]


# --- invalid ids --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [str(uuid.uuid4()), "not-a-uuid", "123"])
def test_an_unknown_or_malformed_job_gets_a_calm_message_and_a_back_button(bad):
    at = _ok(_app(job=bad, stage="setup"))
    assert any("could not be found" in w.value for w in at.warning)
    assert "ws_back_to_jobs" in _keys(at)
    assert not at.segmented_control
    assert at.session_state["_ran"] == []


def test_an_unknown_stage_gets_a_calm_message_and_a_back_button():
    at = _ok(_app(job=_JOB, stage="payroll"))
    assert any("stage doesn't exist" in w.value for w in at.warning)
    assert "ws_back_to_jobs" in _keys(at)
    assert not at.segmented_control
    assert at.session_state["_ran"] == []


def test_the_back_button_returns_to_the_list():
    at = _ok(_app(job="not-a-uuid"))
    _button(at, "ws_back_to_jobs").click().run()
    _ok(at)
    assert len(at.query_params) == 0
    assert f"ws_open_{_JOB}" in _keys(at)


def test_an_invalid_session_user_is_told_so():
    script = _SCRIPT.replace('"id": "11111111-1111-1111-1111-111111111111"',
                             '"id": "not-a-uuid"')
    at = AppTest.from_string(script, default_timeout=_TIMEOUT)
    at.query_params["job"] = _JOB
    at.run()
    _ok(at)
    assert any("session looks invalid" in e.value for e in at.error)
    assert at.session_state["_ran"] == []


# --- stage switching ------------------------------------------------------------------------


def test_choosing_a_stage_writes_it_to_the_url_and_runs_only_that_body():
    at = _ok(_app(job=_JOB, stage="setup"))
    assert at.session_state["_ran"] == ["setup"]
    at.segmented_control[0].set_value("shortlist").run()
    _ok(at)
    assert at.query_params["stage"] in ("shortlist", ["shortlist"])
    assert at.session_state["_ran"] == ["shortlist"]         # setup did NOT run again
    assert at.segmented_control[0].value == "shortlist"


def test_the_control_labels_follow_the_spec():
    at = _ok(_app(job=_JOB, stage="setup"))
    assert list(at.segmented_control[0].options) == [
        "✓ 1. Setup · v1",
        "✓ 2. Applicants · 4 · 2 to review",
        "✓ 3. Shortlist · 2",
        "4. Interviews · 0",
        "5. Final ranking",
    ]


def test_an_empty_selection_falls_back_to_the_stage_in_the_url():
    at = _ok(_app(job=_JOB, stage="shortlist"))
    at.segmented_control[0].set_value(None).run()
    _ok(at)
    assert at.query_params["stage"] in ("shortlist", ["shortlist"])
    assert at.segmented_control[0].value == "shortlist"
    assert at.session_state["_ran"] == ["shortlist"]


# --- previous / next --------------------------------------------------------------------------


def test_next_moves_forward_one_stage():
    at = _ok(_app(job=_JOB, stage="setup"))
    _button(at, f"ws_next_{_JOB}").click().run()
    _ok(at)
    assert at.session_state["_ran"] == ["applicants"]
    assert at.segmented_control[0].value == "applicants"


def test_previous_moves_back_one_stage():
    at = _ok(_app(job=_JOB, stage="interviews"))
    _button(at, f"ws_prev_{_JOB}").click().run()
    _ok(at)
    assert at.session_state["_ran"] == ["shortlist"]


def test_previous_is_disabled_on_the_first_stage_and_next_on_the_last():
    first = _ok(_app(job=_JOB, stage="setup"))
    assert _button(first, f"ws_prev_{_JOB}").disabled
    assert not _button(first, f"ws_next_{_JOB}").disabled
    last = _ok(_app(job=_JOB, stage="final_ranking"))
    assert _button(last, f"ws_next_{_JOB}").disabled
    assert not _button(last, f"ws_prev_{_JOB}").disabled


def test_walking_next_through_every_stage_runs_each_body_once():
    at = _ok(_app(job=_JOB, stage="setup"))
    seen = list(at.session_state["_ran"])
    for _ in range(len(STAGE_KEYS) - 1):
        _button(at, f"ws_next_{_JOB}").click().run()
        _ok(at)
        seen += at.session_state["_ran"]
    assert seen == list(STAGE_KEYS)


# --- breadcrumb / chrome ----------------------------------------------------------------------------


def test_the_breadcrumb_returns_to_the_job_list():
    at = _ok(_app(job=_JOB, stage="setup"))
    _button(at, "ws_crumb_jobs").click().run()
    _ok(at)
    assert not at.segmented_control
    assert f"ws_open_{_JOB}" in _keys(at)


def test_the_header_shows_the_title_code_and_status():
    at = _ok(_app(job=_JOB, stage="setup"))
    body = _text(at)
    assert "Backend Engineer" in body and "V_001" in body
    assert "Open" in body                                 # status label


# --- laziness ----------------------------------------------------------------------------------------


def test_the_summary_service_is_the_only_thing_run_for_the_chrome():
    at = _ok(_app(job=_JOB, stage="final_ranking"))
    assert at.session_state["_summary_calls"] == 1
    assert at.session_state["_ran"] == ["final_ranking"]


def test_inactive_stages_never_call_their_services():
    """The REAL stage renderers are replaced by the spies' bodies; here the real
    ``_render_stage`` dispatcher is checked directly: it calls exactly one body."""
    calls: list[str] = []
    for name in ("setup", "applicants", "shortlist", "interviews", "final_ranking"):
        setattr(W, f"_stage_{name}", lambda j, u, _n=name: calls.append(_n))
    for stage in STAGE_KEYS:
        calls.clear()
        W._render_stage(stage, _JOB, None)
        assert calls == [stage]
    calls.clear()
    W._render_stage("bogus", _JOB, None)
    assert calls == []


def test_rendering_a_stage_does_not_use_st_tabs_for_the_stage_nav():
    """A tab bar would run every body on every rerun. The stage nav is the
    segmented control; the only ``st.tabs`` is inside Applicants' own content."""
    import inspect

    chrome = inspect.getsource(W.render_job_workspace)
    assert "st.tabs" not in chrome and "st.segmented_control" in chrome
    assert inspect.getsource(W._stage_applicants).count("st.tabs") == 1
    for other in (W._stage_setup, W._stage_shortlist, W._stage_interviews,
                  W._stage_final_ranking):
        assert "st.tabs" not in inspect.getsource(other)


# --- no writes from the workspace chrome -------------------------------------------------------------------


def test_the_workspace_module_itself_never_writes():
    """Structural: the new page's own code (the chrome and the Shortlist table) holds
    no write, audit or AI call. Writes happen only inside the reused section
    renderers, exactly as before."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(W))
    forbidden = {"add", "add_all", "delete", "commit", "flush", "merge", "execute",
                 "generate_screening_ranking", "set_shortlist", "record_audit_event"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden, node.func.attr
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
            for m in mods:
                assert "audit_service" not in m and "claude" not in m.lower(), m
                assert not m.startswith("app.ai"), m
