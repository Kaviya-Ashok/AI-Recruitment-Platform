"""Candidates — everyone across all jobs (HR UI redesign, Increment 4).

Replaces the old per-job Candidates sidebar entry (its renderers live on in the job
workspace's Applicants stage). A table of candidate, job, stage and final decision
with a toolbar — a text filter (name or e-mail), a job select and a stage select —
25 rows to a page with Previous / Next and an "N-M of T" count. Selecting a row
opens that candidate's page through the existing deep link. Quick find sits above.

THREE QUERIES, WHATEVER THE SIZE
--------------------------------
One page statement plus one count (``overview_service.list_candidates_overview``),
plus the job filter's options (``list_job_options``) — never a query per row. The
STAGE is the one the candidate page's pill shows (``candidate_progress.stage_name``
written once as SQL; a test compares the two).

A STALE SELECTION CAN NEVER OPEN THE WRONG PERSON
-------------------------------------------------
A dataframe selection is a row INDEX held in the widget's state. The table's widget
key is derived from the filters and the page (``overview_helpers.table_key``), so a
new filter or another page starts a fresh, unselected table; the page number itself
returns to the first page whenever a filter changes.
"""

from __future__ import annotations

import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.pages.quick_find import render_quick_find
from app.services.overview_service import (
    CandidatesOverview,
    list_candidates_overview,
    list_job_options,
)
from app.utils.authorization import UnauthorizedError
from app.utils.overview_helpers import (
    ALL_JOBS,
    DEFAULT_PAGE_SIZE,
    build_link,
    clamp_page,
    has_next,
    has_previous,
    normalise_filter_text,
    page_offset,
    page_range_text,
    selected_row_id,
    stage_filter_options,
    stage_filter_value,
    table_key,
)
from app.utils.session import get_current_user
from app.utils.ui import label_for
from app.utils.ui_widgets import go_to_jobs, load_error, page_header

_PAGE_KEY = "cl_page"
_SIGNATURE_KEY = "cl_filters"

_SESSION_INVALID = "Your session looks invalid — please sign out and back in."
_INACTIVE = "Your account is no longer active — please contact an admin."


def decision_text(decision: str | None) -> str:
    """The Final decision cell, in words. Pure."""
    return label_for(decision) if decision else "Not decided"


def _set_page(page: int) -> None:
    st.session_state[_PAGE_KEY] = page


def _current_page(signature: tuple) -> int:
    """The page index, returned to 0 whenever the filters changed since last run."""
    if st.session_state.get(_SIGNATURE_KEY) != signature:
        st.session_state[_SIGNATURE_KEY] = signature
        st.session_state[_PAGE_KEY] = 0
    page = st.session_state.get(_PAGE_KEY, 0)
    return page if isinstance(page, int) else 0


def _load(acting_user_id, *, search, job_id, stage, page) -> tuple[CandidatesOverview, int]:
    """The page's rows, with the page clamped to what exists (a page that vanished
    because rows were removed falls back to the last one)."""
    with session_scope() as db:
        data = list_candidates_overview(
            db, acting_user_id=acting_user_id, search=search, job_id=job_id,
            stage=stage, limit=DEFAULT_PAGE_SIZE, offset=page_offset(page),
        )
        clamped = clamp_page(page, data.total)
        if clamped != page:
            page = clamped
            data = list_candidates_overview(
                db, acting_user_id=acting_user_id, search=search, job_id=job_id,
                stage=stage, limit=DEFAULT_PAGE_SIZE, offset=page_offset(page),
            )
    return data, page


def render_candidates_list_page(pages) -> None:
    current = get_current_user(st.session_state)
    if current is None:  # defensive: the gate is in main.py
        st.error("Please sign in.")
        return
    try:
        acting_user_id = uuid.UUID(current["id"])
    except (ValueError, KeyError, TypeError):
        st.error(_SESSION_INVALID)
        return

    page_header("Candidates", "Everyone across all jobs.")
    render_quick_find(pages, acting_user_id, scope="candidates")

    # --- toolbar (filters are read BEFORE the data, so the query matches the UI) ---
    bar = st.columns([3, 3, 2], vertical_alignment="bottom")
    search = normalise_filter_text(
        bar[0].text_input(
            "Filter", key="cl_search", placeholder="Filter by name or email",
            label_visibility="collapsed",
        )
    )
    try:
        with session_scope() as db:
            options = list_job_options(db, acting_user_id=acting_user_id)
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load the candidates right now.")
        return
    job_labels = {str(o.job_id): f"{o.job_code} · {o.title}" for o in options}
    job_choice = bar[1].selectbox(
        "Job", [ALL_JOBS, *job_labels], key="cl_job",
        format_func=lambda v: v if v == ALL_JOBS else job_labels.get(v, v),
        label_visibility="collapsed",
    )
    stage_choice = bar[2].selectbox(
        "Stage", stage_filter_options(), key="cl_stage", label_visibility="collapsed",
    )
    job_id = None if job_choice == ALL_JOBS else job_choice
    stage = stage_filter_value(stage_choice)

    page = _current_page((search, job_id, stage))
    try:
        data, page = _load(
            acting_user_id, search=search, job_id=job_id, stage=stage, page=page,
        )
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load the candidates right now.")
        return
    st.session_state[_PAGE_KEY] = page

    if data.total == 0:
        st.caption(
            "No candidates match these filters."
            if (search or job_id or stage) else "No candidates yet."
        )
        return

    rows = list(data.rows)
    event = st.dataframe(
        [
            {
                "Candidate": f"{r.candidate_name} ({r.candidate_email})",
                "Job": f"{r.job_code} {r.job_title}",
                "Stage": r.stage,
                "Final decision": decision_text(r.decision),
            }
            for r in rows
        ],
        hide_index=True,
        width="stretch",
        height="content",
        on_select="rerun",
        selection_mode="single-row",
        key=table_key("cl_table", search, job_id, stage, page),
    )

    pager = st.columns([1, 1, 6], vertical_alignment="center")
    pager[0].button(
        "Previous", key="cl_prev", disabled=not has_previous(page),
        on_click=_set_page, args=(page - 1,),
    )
    pager[1].button(
        "Next", key="cl_next", disabled=not has_next(data.total, page),
        on_click=_set_page, args=(page + 1,),
    )
    pager[2].caption(page_range_text(data.total, page))
    st.caption("Select a row to open that candidate's page.")

    chosen = selected_row_id(event.selection.rows, [r.application_id for r in rows])
    if chosen is not None:
        row = next(r for r in rows if str(r.application_id) == chosen)
        go_to_jobs(pages, build_link(row.job_id, candidate=row.application_id))
