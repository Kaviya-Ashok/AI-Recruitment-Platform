"""The Jobs page as a table (HR UI, Increment 4), through Streamlit's AppTest.

The list services are stubbed (no database). Covered: the table, the text filter, the
status control built from the existing status groups, paging, opening a job from a row
(including that a stale selection can never open the wrong job), and that the
create-job form above the table works exactly as before.

A ``st.dataframe`` row selection cannot be clicked in AppTest, but its state can be
set before a run (the same state a click writes); the real click is verified in the
browser.
"""

from __future__ import annotations

import uuid

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.jobs as J
import app.pages.job_workspace as W
from app.database.models.job import JobStatus
from app.utils.overview_helpers import table_key

_TIMEOUT = 60
_ME = "11111111-1111-1111-1111-111111111111"

_J_ATTRS = ("session_scope", "count_jobs_by_status", "list_jobs_overview", "create_job")
_PRISTINE_J = {n: getattr(J, n) for n in _J_ATTRS}
_PRISTINE_W = {"render_job_workspace": W.render_job_workspace}


@pytest.fixture(autouse=True)
def _restore():
    def put():
        for n, v in _PRISTINE_J.items():
            setattr(J, n, v)
        for n, v in _PRISTINE_W.items():
            setattr(W, n, v)

    put()
    try:
        yield
    finally:
        put()


_SCRIPT = '''
import contextlib
import uuid
from types import SimpleNamespace as NS

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import app.pages.job_workspace as W
import app.pages.jobs as J
from app.database.models.job import JobStatus
from app.services.job_service import JobValidationError
from app.services.overview_service import JobOverviewRow, JobsOverview
from app.utils.authorization import UnauthorizedError
from app.utils.session import SESSION_USER_KEY

st.session_state.setdefault("CALLS", [])
CALLS = st.session_state["CALLS"]
RAISE = __RAISE__
CREATE_RAISE = __CREATE_RAISE__
N = __N__


@contextlib.contextmanager
def _scope():
    yield "DB"


def _maybe_raise():
    if RAISE == "unauthorized":
        raise UnauthorizedError("no")
    if RAISE == "sql":
        raise SQLAlchemyError("boom")


def _job_id(i):
    return uuid.UUID(int=0xA0000 + i)


STATUSES = [JobStatus.OPEN, JobStatus.DRAFT, JobStatus.CLOSED]
ALL = [
    JobOverviewRow(_job_id(i), f"V_{i:03d}", f"Role {i:02d}", STATUSES[i % 3],
                   ["setup", "applicants", "shortlist", "interviews", "final_ranking"][i % 5],
                   i, i // 2, i // 4, None)
    for i in range(N)
]


def fake_counts(db, **kw):
    if RAISE == "sql":                      # the real counts query has no user guard
        raise SQLAlchemyError("boom")
    return {JobStatus.OPEN: 2, JobStatus.DRAFT: 1, JobStatus.JD_ANALYZED: 1,
            JobStatus.RUBRIC_APPROVED: 1, JobStatus.CLOSED: 1, JobStatus.ARCHIVED: 1}


def fake_list(db, *, acting_user_id, statuses=None, search=None, limit=25, offset=0):
    CALLS.append(("list", dict(statuses=statuses, search=search, limit=limit, offset=offset,
                               acting_user_id=acting_user_id)))
    _maybe_raise()
    if RAISE == "list_sql":                 # only the page query fails; counts succeeded
        raise SQLAlchemyError("boom")
    rows = [r for r in ALL if (statuses is None or r.status in statuses)
            and (not search or search.lower() in r.title.lower())]
    return JobsOverview(rows=tuple(rows[offset:offset + limit]), total=len(rows))


def fake_create(db, **kw):
    CALLS.append(("create", kw))
    if CREATE_RAISE == "validation":
        raise JobValidationError("Job title is required.")
    if CREATE_RAISE == "boom":
        raise RuntimeError("secret-internal-detail")
    return NS(title=kw["title"])


J.session_scope = _scope
J.count_jobs_by_status = fake_counts
J.list_jobs_overview = fake_list
J.create_job = fake_create
W.render_job_workspace = lambda uid: st.write("WORKSPACE " + str(uid))

st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111", "email": "p@x.test",
    "full_name": "P", "role": "HR",
}
J.render_jobs_page()
'''


def _app(n: int = 60, raise_: str | None = None, create_raise: str | None = None,
         **params: str) -> AppTest:
    script = (
        _SCRIPT.replace("__N__", str(n)).replace("__RAISE__", repr(raise_))
        .replace("__CREATE_RAISE__", repr(create_raise))
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT)
    for k, v in params.items():
        at.query_params[k] = v
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _calls(at, kind):
    return [c[1] for c in at.session_state["CALLS"] if c[0] == kind]


def _last_list(at):
    return _calls(at, "list")[-1]


def _text(at) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [s.value for s in at.subheader]
    parts += [s.value for s in at.success] + [t.value for t in at.title]
    return "\n".join(str(p) for p in parts)


def _button(at, key):
    return next(b for b in at.button if b.key == key)


def _qp(at, name):
    v = at.query_params.get(name)
    return v[0] if isinstance(v, list) else v


def _select(at, key, row):
    at.session_state[key] = {"selection": {"rows": [row], "columns": [], "cells": []}}
    return at.run()


def _key(search="", choice="all", page=0):
    return table_key("jobs_table", search, choice, page)


# --- the table ---------------------------------------------------------------------------------


def test_the_table_has_the_specified_columns_and_values():
    at = _app()
    df = at.dataframe[0].value
    assert list(df.columns) == [
        "Job", "Status", "Current stage", "Applicants", "Interviewed", "Decided"]
    first = df.iloc[0].to_dict()
    assert first == {"Job": "V_000 Role 00", "Status": "Open", "Current stage": "Setup",
                     "Applicants": 0, "Interviewed": 0, "Decided": 0}
    second = df.iloc[1].to_dict()
    assert second["Status"] == "Draft" and second["Current stage"] == "Applicants"
    assert len(df) == 25


def test_the_list_is_one_call_as_this_user_with_one_page():
    at = _app()
    (call,) = _calls(at, "list")
    assert call == dict(statuses=None, search="", limit=25, offset=0,
                        acting_user_id=uuid.UUID(_ME))


def test_the_card_list_is_gone():
    at = _app()
    assert not at.expander
    assert not [b for b in at.button if b.key.startswith(("ws_open_", "ws_title_", "jobcard_"))]


def test_the_table_is_single_row_selectable():
    proto = _app().dataframe[0].proto
    assert list(proto.selection_mode) == [type(proto).SelectionMode.SINGLE_ROW]


# --- the status control ----------------------------------------------------------------------------


def test_the_status_control_is_built_from_the_status_groups_with_counts():
    at = _app()
    seg = at.segmented_control(key="jobs_status")
    assert list(seg.options) == ["All (7)", "Open (2)", "Draft (3)", "Closed (2)"]
    assert seg.value == "all"


def test_the_control_keys_are_the_existing_groups():
    from app.pages.jobs import _TABS, _status_choices

    assert list(_status_choices({})) == ["all", *[k for k, _l, _s in _TABS]]


def test_every_status_group_is_reachable_and_the_groups_partition_all_statuses():
    from app.pages.jobs import _TABS, _statuses_for

    seen: set[str] = set()
    for key, _label, _statuses in _TABS:
        group = _statuses_for(key)
        assert group and not (seen & group)
        seen |= group
    assert seen == set(JobStatus.ALL)
    assert _statuses_for("all") is None and _statuses_for("nonsense") is None


@pytest.mark.parametrize("choice, expected", [
    ("open", {JobStatus.OPEN}),
    ("draft", {JobStatus.DRAFT, JobStatus.JD_ANALYZED, JobStatus.RUBRIC_PENDING,
               JobStatus.RUBRIC_APPROVED}),
    ("closed", {JobStatus.CLOSED, JobStatus.ARCHIVED}),
])
def test_choosing_a_group_filters_by_its_statuses(choice, expected):
    at = _app()
    at.segmented_control(key="jobs_status").set_value(choice).run()
    assert not at.exception
    assert set(_last_list(at)["statuses"]) == expected
    assert _last_list(at)["offset"] == 0


def test_choosing_all_again_removes_the_status_filter():
    at = _app()
    at.segmented_control(key="jobs_status").set_value("open").run()
    at.segmented_control(key="jobs_status").set_value("all").run()
    assert _last_list(at)["statuses"] is None


def test_an_empty_status_selection_falls_back_to_all():
    at = _app()
    at.segmented_control(key="jobs_status").set_value(None).run()
    assert not at.exception and _last_list(at)["statuses"] is None


# --- the text filter ---------------------------------------------------------------------------------


def test_the_text_filter_is_passed_normalised_and_narrows_the_table():
    at = _app()
    at.text_input(key="jobs_filter").set_value("  role   07 ").run()
    assert _last_list(at)["search"] == "role 07"
    assert list(at.dataframe[0].value["Job"]) == ["V_007 Role 07"]


def test_the_status_counts_are_not_narrowed_by_the_filter():
    at = _app()
    at.text_input(key="jobs_filter").set_value("zzz").run()
    assert list(at.segmented_control(key="jobs_status").options)[0] == "All (7)"


def test_no_match_says_so():
    at = _app()
    at.text_input(key="jobs_filter").set_value("zzzz").run()
    assert "No jobs match these filters." in _text(at)
    assert not at.dataframe


def test_no_jobs_at_all_points_at_the_create_form():
    at = _app(n=0)
    assert "No jobs yet — create one above." in _text(at)


# --- paging ----------------------------------------------------------------------------------------------


def test_paging_shows_the_range_and_walks_through_every_page():
    at = _app()
    assert "1-25 of 60" in _text(at)
    assert _button(at, "jobs_prev").disabled and not _button(at, "jobs_next").disabled
    _button(at, "jobs_next").click().run()
    assert "26-50 of 60" in _text(at) and _last_list(at)["offset"] == 25
    assert list(at.dataframe[0].value["Job"])[0] == "V_025 Role 25"
    _button(at, "jobs_next").click().run()
    assert "51-60 of 60" in _text(at) and _last_list(at)["offset"] == 50
    assert _button(at, "jobs_next").disabled and not _button(at, "jobs_prev").disabled
    _button(at, "jobs_prev").click().run()
    assert "26-50 of 60" in _text(at)


def test_a_filter_change_returns_to_the_first_page():
    at = _app()
    _button(at, "jobs_next").click().run()
    assert _last_list(at)["offset"] == 25
    at.segmented_control(key="jobs_status").set_value("open").run()
    assert _last_list(at)["offset"] == 0 and "1-" in _text(at)
    _button(at, "jobs_next").click().run()
    at.text_input(key="jobs_filter").set_value("role").run()
    assert _last_list(at)["offset"] == 0


def test_a_short_list_has_no_next_page():
    at = _app(n=10)
    assert _button(at, "jobs_prev").disabled and _button(at, "jobs_next").disabled
    assert "1-10 of 10" in _text(at)


# --- opening a job from a row ------------------------------------------------------------------------------


@pytest.mark.parametrize("row", [0, 3, 24])
def test_selecting_a_row_opens_that_jobs_workspace(row):
    at = _app()
    _select(at, _key(), row)
    assert not at.exception
    job_id = str(uuid.UUID(int=0xA0000 + row))
    assert _qp(at, "job") == job_id
    assert f"WORKSPACE {_ME}" in _text(at)             # the workspace renders instead of the list
    assert not at.dataframe


def test_opening_does_not_loop():
    at = _app()
    _select(at, _key(), 1)
    again = at.run()
    assert not again.exception and _qp(again, "job") == str(uuid.UUID(int=0xA0001))


def test_a_row_on_a_later_page_opens_the_right_job():
    at = _app()
    _button(at, "jobs_next").click().run()
    _select(at, _key(page=1), 2)
    assert _qp(at, "job") == str(uuid.UUID(int=0xA0000 + 27))


def _inject_real(at, row):
    at.session_state[at.dataframe[0].key] = {
        "selection": {"rows": [row], "columns": [], "cells": []}}


@pytest.mark.parametrize("change", ["filter", "status", "page"])
def test_the_tables_widget_key_changes_with_every_filter_and_page(change):
    at = _app()
    before = at.dataframe[0].key
    if change == "filter":
        at.text_input(key="jobs_filter").set_value("role").run()
    elif change == "status":
        at.segmented_control(key="jobs_status").set_value("open").run()
    else:
        _button(at, "jobs_next").click().run()
    assert at.dataframe[0].key != before


@pytest.mark.parametrize("change", ["filter", "status", "page"])
def test_a_stale_selection_is_forgotten_when_the_filters_or_page_change(change):
    """The selection is injected into the key the table ACTUALLY has; a changed
    filter or page must give the table a new key so the old row index is gone."""
    at = _app()
    _inject_real(at, 2)
    if change == "filter":
        at.text_input(key="jobs_filter").set_value("role 1").run()
    elif change == "status":
        at.segmented_control(key="jobs_status").set_value("closed").run()
    else:
        _button(at, "jobs_next").click().run()
    assert not at.exception and _qp(at, "job") is None
    assert at.dataframe                                  # still the list, unselected


def test_the_real_key_does_carry_a_selection_when_nothing_changed():
    """Control for the test above: the same injection DOES open a job when nothing
    changed, so the stale tests are not passing vacuously."""
    at = _app()
    _select(at, at.dataframe[0].key, 2)
    assert _qp(at, "job") == str(uuid.UUID(int=0xA0002))


def test_a_selection_outside_the_table_opens_nothing():
    at = _app(n=3)
    _select(at, _key(), 9)
    assert not at.exception and _qp(at, "job") is None


def test_with_a_job_in_the_url_the_list_is_not_queried_at_all():
    at = _app(job=str(uuid.UUID(int=0xA0001)))
    assert _calls(at, "list") == []
    assert f"WORKSPACE {_ME}" in _text(at)


# --- errors ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raise_, expected", [
    ("unauthorized", "no longer active"),                  # refused by the list reader
    ("sql", "Couldn't load the job list right now."),      # failing at the counts query
])
def test_load_failures_are_calm(raise_, expected):
    at = _app(raise_=raise_)
    assert any(expected in e.value for e in at.error)
    assert not at.dataframe


def test_a_failure_in_the_page_query_itself_is_calm_and_keeps_the_create_form():
    at = _app(raise_="list_sql")
    assert any("Couldn't load the job list right now." in e.value for e in at.error)
    assert not at.dataframe
    assert any(b.label == "Create job" for b in at.button)  # the form above is unaffected


# --- the create-job flow is exactly as before ---------------------------------------------------------------------


def test_the_create_form_is_above_the_table_and_unchanged():
    at = _app()
    assert [s.value for s in at.subheader][:2] == ["Create a job", "Jobs"]
    assert [r.label for r in at.radio] == ["Job Description input"]
    assert list(at.radio[0].options) == ["Paste text", "Upload file"]
    assert [t.label for t in at.text_input if t.key != "jobs_filter"] == [
        "Title", "Department (optional)"]
    assert [t.label for t in at.text_area] == ["Paste the Job Description"]
    assert any(b.label == "Create job" for b in at.button)


def test_creating_a_job_calls_the_service_exactly_as_before():
    at = _app()
    at.text_input(key="job_title").set_value("Platform Engineer")
    at.text_input(key="job_department").set_value("Eng")
    at.text_area(key="jd_text").set_value("We need a platform engineer.")
    next(b for b in at.button if b.label == "Create job").click().run()
    assert not at.exception
    (kw,) = _calls(at, "create")
    from app.database.models.job import JdInputMethod

    assert kw == {
        "title": "Platform Engineer", "department": "Eng",
        "jd_input_method": JdInputMethod.TEXT_PASTE,
        "jd_source_text": "We need a platform engineer.",
        "uploaded_file_bytes": None, "uploaded_filename": None,
        "created_by_user_id": uuid.UUID(_ME),
    }
    assert 'Created job "Platform Engineer".' in [s.value for s in at.success]


def test_a_validation_message_is_shown_as_before():
    at = _app(create_raise="validation")
    next(b for b in at.button if b.label == "Create job").click().run()
    assert any(e.value == "Job title is required." for e in at.error)


def test_an_unexpected_error_never_shows_a_traceback_or_its_detail():
    at = _app(create_raise="boom")
    next(b for b in at.button if b.label == "Create job").click().run()
    assert any("Something went wrong creating the job." in e.value for e in at.error)
    assert "secret-internal-detail" not in _text(at)
    assert not at.exception


def test_switching_to_upload_shows_the_file_uploader_as_before():
    at = _app()
    at.radio(key="jd_method").set_value("Upload file").run()
    assert not at.exception and not at.text_area
    assert at.get("file_uploader")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_page_emits_no_html_and_the_old_card_list_is_gone():
    import ast
    import inspect

    source = inspect.getsource(J)
    assert "unsafe_allow_html" not in source and "<div" not in source
    names = {n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)}
    assert {"_render_create_form", "_handle_submit", "_render_job_body", "_job_view"} <= names
    assert not {"_render_job_card", "_card_label"} & names           # removed in Increment 5
    assert not {"_render_tab", "_shown_count"} & names              # the lazy-card list is gone
