"""The cross-job Candidates page (HR UI, Increment 4), through Streamlit's AppTest.

Services are stubbed (no database); ``st.switch_page`` is a recorder. Covered: the
table, the three filters, paging with an "N-M of T" count, opening a candidate from a
row through the existing deep link, and — the property that matters most — that a
stale selection (a row index from before a filter or page change) can never open the
wrong person. A real row click is verified in the browser; here the selection state is
set before a run, which is exactly what a click writes.
"""

from __future__ import annotations

import uuid

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import app.pages.candidates_list as CL
import app.pages.quick_find as Q
from app.utils.overview_helpers import table_key

_TIMEOUT = 60
_ME = "11111111-1111-1111-1111-111111111111"
_JOB_1 = "10000000-0000-4000-8000-000000000001"
_JOB_2 = "20000000-0000-4000-8000-000000000002"

_ST_SWITCH, _ST_PAGE_LINK = st.switch_page, st.page_link
_PRISTINE = {
    CL: {n: getattr(CL, n) for n in ("session_scope", "list_job_options", "list_candidates_overview")},
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


_SCRIPT = '''
import contextlib
import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import app.pages.candidates_list as CL
import app.pages.quick_find as Q
from app.services.overview_service import CandidateOverviewRow, CandidatesOverview, JobOption
from app.utils.authorization import UnauthorizedError
from app.utils.session import SESSION_USER_KEY

st.session_state.setdefault("CALLS", [])
CALLS = st.session_state["CALLS"]
RAISE = __RAISE__
N = __N__
SHRINK = __SHRINK__
JOB_1, JOB_2 = uuid.UUID("__JOB_1__"), uuid.UUID("__JOB_2__")
STAGES = ["Applied", "Screened", "Shortlisted", "Interviewed", "Ranked", "Decided"]


@contextlib.contextmanager
def _scope():
    yield "DB"


def _maybe_raise():
    if RAISE == "unauthorized":
        raise UnauthorizedError("no")
    if RAISE == "sql":
        raise SQLAlchemyError("boom")


def app_id(i):
    return uuid.UUID(int=0xB0000 + i)


ALL = [
    CandidateOverviewRow(
        app_id(i), JOB_1 if i % 2 == 0 else JOB_2, f"Person {i:02d}", f"p{i:02d}@x.test",
        "V_001" if i % 2 == 0 else "V_002",
        "Backend Engineer" if i % 2 == 0 else "Data Analyst",
        STAGES[i % 6], "PROCEED" if i % 6 == 5 else None)
    for i in range(N)
]


def fake_options(db, *, acting_user_id):
    _maybe_raise()
    return [JobOption(JOB_1, "V_001", "Backend Engineer"), JobOption(JOB_2, "V_002", "Data Analyst")]


def fake_list(db, *, acting_user_id, search=None, job_id=None, stage=None, limit=25, offset=0):
    CALLS.append(("list", dict(search=search, job_id=job_id, stage=stage, limit=limit,
                               offset=offset, acting_user_id=acting_user_id)))
    _maybe_raise()
    rows = [r for r in ALL
            if (not search or search.lower() in (r.candidate_name + r.candidate_email).lower())
            and (job_id is None or str(r.job_id) == str(job_id))
            and (stage is None or r.stage == stage)]
    if SHRINK:                               # only 10 rows exist now, whatever page is asked
        rows = rows[:10]
    total = len(rows)
    return CandidatesOverview(rows=tuple(rows[offset:offset + limit]), total=total)


def _switch(page, query_params=None):
    CALLS.append(("switch", page, dict(query_params or {})))


st.switch_page = _switch
PAGES = {"jobs": "JOBS_PAGE"}
CL.session_scope = _scope
Q.session_scope = _scope
CL.list_job_options = fake_options
CL.list_candidates_overview = fake_list
Q.quick_find = lambda db, q, *, acting_user_id, limit=8: []

st.session_state[SESSION_USER_KEY] = {
    "id": "__SESSION_ID__", "email": "p@x.test", "full_name": "P", "role": "HR",
}
CL.render_candidates_list_page(PAGES)
'''


def _app(n: int = 60, raise_: str | None = None, shrink: bool = False,
         session_id: str = _ME) -> AppTest:
    script = (
        _SCRIPT.replace("__N__", str(n)).replace("__RAISE__", repr(raise_))
        .replace("__SHRINK__", repr(shrink)).replace("__JOB_1__", _JOB_1)
        .replace("__JOB_2__", _JOB_2).replace("__SESSION_ID__", session_id)
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _calls(at, kind):
    return [c[1:] if kind == "switch" else c[1] for c in at.session_state["CALLS"] if c[0] == kind]


def _last(at):
    return _calls(at, "list")[-1]


def _text(at) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [t.value for t in at.title]
    return "\n".join(str(p) for p in parts)


def _button(at, key):
    return next(b for b in at.button if b.key == key)


def _key(search="", job=None, stage=None, page=0):
    return table_key("cl_table", search, job, stage, page)


def _select(at, key, row):
    at.session_state[key] = {"selection": {"rows": [row], "columns": [], "cells": []}}
    return at.run()


# --- the table ---------------------------------------------------------------------------------------


def test_the_table_has_candidate_job_stage_and_decision_columns():
    at = _app()
    df = at.dataframe[0].value
    assert list(df.columns) == ["Candidate", "Job", "Stage", "Final decision"]
    assert df.iloc[0].to_dict() == {
        "Candidate": "Person 00 (p00@x.test)", "Job": "V_001 Backend Engineer",
        "Stage": "Applied", "Final decision": "Not decided"}
    assert df.iloc[5].to_dict()["Final decision"] == "Proceed"
    assert len(df) == 25


def test_the_first_load_asks_for_one_page_with_no_filters_as_this_user():
    at = _app()
    (call,) = _calls(at, "list")
    assert call == dict(search="", job_id=None, stage=None, limit=25, offset=0,
                        acting_user_id=uuid.UUID(_ME))


def test_the_table_is_single_row_selectable():
    proto = _app().dataframe[0].proto
    assert list(proto.selection_mode) == [type(proto).SelectionMode.SINGLE_ROW]


def test_the_page_names_itself_and_has_quick_find():
    at = _app()
    assert [t.value for t in at.title] == ["Candidates"]
    assert "Quick find" in [t.label for t in at.text_input]


# --- the toolbar ----------------------------------------------------------------------------------------


def test_the_toolbar_offers_a_text_filter_a_job_select_and_a_stage_select():
    at = _app()
    assert at.text_input(key="cl_search").placeholder == "Filter by name or email"
    job = at.selectbox(key="cl_job")
    assert list(job.options) == ["All jobs", "V_001 · Backend Engineer", "V_002 · Data Analyst"]
    assert at.selectbox(key="cl_stage").options == [
        "All stages", "Applied", "Screened", "Shortlisted", "Interviewed", "Ranked", "Decided"]


def test_the_text_filter_is_passed_normalised_and_narrows_the_table():
    at = _app()
    at.text_input(key="cl_search").set_value("  person   07 ").run()
    assert _last(at)["search"] == "person 07"
    assert list(at.dataframe[0].value["Candidate"]) == ["Person 07 (p07@x.test)"]


def test_the_job_filter_passes_the_job_id():
    at = _app()
    at.selectbox(key="cl_job").set_value(_JOB_2).run()
    assert _last(at)["job_id"] == _JOB_2
    assert set(at.dataframe[0].value["Job"]) == {"V_002 Data Analyst"}


def test_choosing_all_jobs_again_clears_the_job_filter():
    at = _app()
    at.selectbox(key="cl_job").set_value(_JOB_1).run()
    at.selectbox(key="cl_job").set_value("All jobs").run()
    assert _last(at)["job_id"] is None


def test_the_stage_filter_passes_the_stage_word():
    at = _app()
    at.selectbox(key="cl_stage").set_value("Decided").run()
    assert _last(at)["stage"] == "Decided"
    assert set(at.dataframe[0].value["Stage"]) == {"Decided"}
    at.selectbox(key="cl_stage").set_value("All stages").run()
    assert _last(at)["stage"] is None


def test_filters_combine():
    at = _app()
    at.selectbox(key="cl_job").set_value(_JOB_1).run()
    at.selectbox(key="cl_stage").set_value("Shortlisted").run()
    at.text_input(key="cl_search").set_value("person").run()
    call = _last(at)
    assert (call["job_id"], call["stage"], call["search"]) == (_JOB_1, "Shortlisted", "person")


def test_no_match_says_so_and_no_data_at_all_says_that():
    at = _app()
    at.text_input(key="cl_search").set_value("zzzz").run()
    assert "No candidates match these filters." in _text(at) and not at.dataframe
    assert "No candidates yet." in _text(_app(n=0))


# --- paging ------------------------------------------------------------------------------------------------


def test_paging_shows_the_range_and_walks_every_page():
    at = _app()
    assert "1-25 of 60" in _text(at)
    assert _button(at, "cl_prev").disabled and not _button(at, "cl_next").disabled
    _button(at, "cl_next").click().run()
    assert "26-50 of 60" in _text(at) and _last(at)["offset"] == 25
    assert list(at.dataframe[0].value["Candidate"])[0] == "Person 25 (p25@x.test)"
    _button(at, "cl_next").click().run()
    assert "51-60 of 60" in _text(at) and _last(at)["offset"] == 50
    assert _button(at, "cl_next").disabled and not _button(at, "cl_prev").disabled
    assert len(at.dataframe[0].value) == 10
    _button(at, "cl_prev").click().run()
    assert "26-50 of 60" in _text(at)


def test_each_page_is_one_list_call_of_25():
    at = _app()
    _button(at, "cl_next").click().run()
    assert {c["limit"] for c in _calls(at, "list")} == {25}


@pytest.mark.parametrize("change", ["search", "job", "stage"])
def test_changing_any_filter_returns_to_the_first_page(change):
    at = _app()
    _button(at, "cl_next").click().run()
    assert _last(at)["offset"] == 25
    if change == "search":
        at.text_input(key="cl_search").set_value("person").run()
    elif change == "job":
        at.selectbox(key="cl_job").set_value(_JOB_1).run()
    else:
        at.selectbox(key="cl_stage").set_value("Applied").run()
    assert _last(at)["offset"] == 0


def test_a_page_that_no_longer_exists_falls_back_to_the_last_one():
    """Rows can disappear between page loads: asking for page 3 of what is now 10 rows
    must show what exists, not an empty table."""
    at = _app(shrink=True)
    at.session_state["cl_page"] = 2                    # a page index from before the shrink
    at.run()
    assert not at.exception
    assert "1-10 of 10" in _text(at) and len(at.dataframe[0].value) == 10
    assert [c["offset"] for c in _calls(at, "list")][-2:] == [50, 0]   # asked, then fell back


def test_a_short_list_has_no_paging():
    at = _app(n=7)
    assert _button(at, "cl_prev").disabled and _button(at, "cl_next").disabled
    assert "1-7 of 7" in _text(at)


# --- opening a candidate ------------------------------------------------------------------------------------


@pytest.mark.parametrize("row", [0, 1, 24])
def test_selecting_a_row_opens_that_candidates_page_by_deep_link(row):
    at = _app()
    _select(at, _key(), row)
    assert not at.exception
    job = _JOB_1 if row % 2 == 0 else _JOB_2
    assert _calls(at, "switch") == [
        ("JOBS_PAGE", {"job": job, "candidate": str(uuid.UUID(int=0xB0000 + row))})]


def test_no_selection_navigates_nowhere():
    assert _calls(_app(), "switch") == []


def test_a_row_on_a_later_page_opens_the_right_person():
    at = _app()
    _button(at, "cl_next").click().run()
    _select(at, _key(page=1), 3)
    assert _calls(at, "switch")[0][1]["candidate"] == str(uuid.UUID(int=0xB0000 + 28))


def test_a_selection_under_a_filter_opens_the_filtered_row_not_the_unfiltered_one():
    at = _app()
    at.selectbox(key="cl_job").set_value(_JOB_2).run()
    _select(at, _key(job=_JOB_2), 0)                    # first row of the FILTERED table
    assert _calls(at, "switch") == [
        ("JOBS_PAGE", {"job": _JOB_2, "candidate": str(uuid.UUID(int=0xB0001))})]


def _change(at, change):
    if change == "search":
        at.text_input(key="cl_search").set_value("person").run()
    elif change == "job":
        at.selectbox(key="cl_job").set_value(_JOB_1).run()
    elif change == "stage":
        at.selectbox(key="cl_stage").set_value("Applied").run()
    else:
        _button(at, "cl_next").click().run()


@pytest.mark.parametrize("change", ["search", "job", "stage", "page"])
def test_the_tables_widget_key_changes_with_every_filter_and_page(change):
    at = _app()
    before = at.dataframe[0].key
    _change(at, change)
    assert at.dataframe[0].key != before


@pytest.mark.parametrize("change", ["search", "job", "stage", "page"])
def test_a_stale_selection_never_opens_anyone_after_a_filter_or_page_change(change):
    """A row index selected under the old filter/page must not carry over. The
    selection is injected into the key the table ACTUALLY has, so if that key ever
    stopped tracking the filters the old index would survive and open someone."""
    at = _app()
    at.session_state[at.dataframe[0].key] = {
        "selection": {"rows": [4], "columns": [], "cells": []}}
    _change(at, change)
    assert not at.exception
    assert _calls(at, "switch") == []


def test_the_real_key_does_carry_a_selection_when_nothing_changed():
    """Control for the test above: the same injection DOES open a candidate when the
    filters are unchanged, so the stale tests are not passing vacuously."""
    at = _app()
    _select(at, at.dataframe[0].key, 4)
    assert len(_calls(at, "switch")) == 1


def test_returning_to_the_original_filter_does_not_resurrect_the_old_selection():
    at = _app()
    at.session_state[at.dataframe[0].key] = {
        "selection": {"rows": [2], "columns": [], "cells": []}}
    at.text_input(key="cl_search").set_value("x").run()
    at.text_input(key="cl_search").set_value("").run()
    assert _calls(at, "switch") == []


def test_a_selection_outside_the_table_opens_nothing():
    at = _app(n=3)
    _select(at, _key(), 9)
    assert not at.exception and _calls(at, "switch") == []


# --- errors -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raise_, expected", [
    ("unauthorized", "no longer active"), ("sql", "Couldn't load the candidates right now."),
])
def test_load_failures_are_calm(raise_, expected):
    at = _app(raise_=raise_)
    assert any(expected in e.value for e in at.error)
    assert not at.dataframe


def test_an_invalid_session_user_is_told_so_and_nothing_is_read():
    at = _app(session_id="not-a-uuid")
    assert any("session looks invalid" in e.value for e in at.error)
    assert _calls(at, "list") == []


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_stage_words_come_from_the_candidate_pages_helpers():
    from app.utils.candidate_progress import STAGE_LABELS

    assert at_options() == ["All stages", *STAGE_LABELS.values()]


def at_options():
    from app.utils.overview_helpers import stage_filter_options

    return stage_filter_options()


def test_the_decision_cell_is_in_words():
    assert CL.decision_text(None) == "Not decided"
    assert CL.decision_text("PROCEED") == "Proceed"
    assert CL.decision_text("HOLD") == "Hold" and CL.decision_text("REJECT") == "Reject"


def test_the_module_emits_no_html_and_imports_no_ai():
    import ast
    from pathlib import Path

    source = Path(CL.__file__).read_text(encoding="utf-8")
    assert "unsafe_allow_html" not in source and "<div" not in source
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] + [a.name for a in node.names])
            for n in names:
                assert not n.startswith("app.ai") and "anthropic" not in n.lower(), n
