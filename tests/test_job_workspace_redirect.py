"""The Dashboard -> Jobs redirect for workspace deep links (HR UI, Increment 2).

``/jobs?job=..&stage=..`` opened while logged out is redirected by Streamlit to
``/?job=..&stage=..`` and, after sign-in, the Dashboard loads. ``app/main.py``
hands a valid workspace link on to the Jobs page; anything else is ignored.

``app.main`` runs ``main()`` on import, so each run drops it from ``sys.modules``
first and re-imports it inside the AppTest script, with the Jobs page replaced by a
recorder (no database for the redirect case). The real page object is restored
before and after every test (see tests/test_interviews_page_analysis_section.py for
why the pristine snapshot is taken at import time).
"""

from __future__ import annotations

import sys
import uuid

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.jobs as J

_TIMEOUT = 60
_PRISTINE_RENDER = J.render_jobs_page
_JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"


@pytest.fixture(autouse=True)
def _restore():
    J.render_jobs_page = _PRISTINE_RENDER
    sys.modules.pop("app.main", None)
    try:
        yield
    finally:
        J.render_jobs_page = _PRISTINE_RENDER
        sys.modules.pop("app.main", None)


_SCRIPT = '''
import sys
import streamlit as st
import app.pages.jobs as J
from app.utils.session import SESSION_USER_KEY

def recorder():
    st.session_state["_jobs_page_ran"] = st.session_state.get("_jobs_page_ran", 0) + 1
    st.session_state["_jobs_params"] = dict(st.query_params)
    st.write("JOBS PAGE")

J.render_jobs_page = recorder
sys.modules.pop("app.main", None)
st.session_state[SESSION_USER_KEY] = {
    "id": "%s", "email": "p@x.test", "full_name": "P", "role": "HR",
}
import app.main  # noqa: F401  (runs main())
''' % uuid.uuid4()


def _run(params: dict[str, str]) -> AppTest:
    at = AppTest.from_string(_SCRIPT, default_timeout=_TIMEOUT)
    for key, value in params.items():
        at.query_params[key] = value
    at.run()
    return at


def test_valid_job_is_handed_to_the_jobs_page_with_its_stage():
    at = _run({"job": _JOB, "stage": "shortlist"})
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state["_jobs_page_ran"] == 1
    assert at.session_state["_jobs_params"] == {"job": _JOB, "stage": "shortlist"}


def test_valid_job_without_a_stage_passes_only_the_job():
    at = _run({"job": _JOB})
    assert at.session_state["_jobs_params"] == {"job": _JOB}


def test_the_jobs_page_ran_exactly_once_so_there_is_no_loop():
    at = _run({"job": _JOB, "stage": "interviews"})
    assert at.session_state["_jobs_page_ran"] == 1
    again = at.run()
    assert not again.exception
    assert again.session_state["_jobs_page_ran"] == 2   # one run per rerun, never more


@pytest.mark.parametrize("bad", ["not-a-uuid", "123", "'; DROP TABLE jobs;--", "   "])
def test_a_malformed_job_is_ignored_and_the_dashboard_stays(bad):
    at = _run({"job": bad, "stage": "setup"})
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "_jobs_page_ran" not in at.session_state
    assert [t.value for t in at.title] == ["Dashboard"]


def test_no_job_parameter_stays_on_the_dashboard():
    at = _run({})
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "_jobs_page_ran" not in at.session_state
    assert [t.value for t in at.title] == ["Dashboard"]


def test_a_stage_alone_never_redirects():
    at = _run({"stage": "shortlist"})
    assert "_jobs_page_ran" not in at.session_state


def test_the_jobs_page_itself_does_not_redirect():
    """Only the Dashboard function calls the redirect helper."""
    import inspect

    import app.pages.job_workspace as W

    for module in (J, W):
        assert "deep_link_target" not in inspect.getsource(module)
        assert "switch_page" not in inspect.getsource(module)
