"""Job workspace — one job, five stages, one place (HR UI redesign, Increment 2).

Shown by the Jobs page when the URL carries ``?job=<id>`` (see
``app/utils/workspace_nav.py``). Stages, in order: Setup, Applicants, Shortlist,
Interviews, Final ranking.

NOTHING IS REWRITTEN HERE
-------------------------
Every stage body is an EXISTING renderer, reused as-is and called with this job's
id:

* Setup          -> ``jobs._render_job_body`` (extracted from the old job card:
                    details, JD analysis, requirements, rubric, application link)
* Applicants     -> ``candidates._render_ranking_section`` and
                    ``candidates._render_applications_tab``
* Shortlist      -> a new READ-ONLY table (name, screening rank, guide, rounds)
* Interviews     -> ``interviews._render_shortlisted_section``
* Final ranking  -> ``interviews._render_final_ranking_section``

so every existing behaviour, audit event, confirmation and permission check inside
them is unchanged. (Those renderers already took ``job_id``; the old Candidates
and Interviews pages still call them with the job picker's value.)

LAZY BY CONSTRUCTION
--------------------
The stage control is ``st.segmented_control``, NOT ``st.tabs``. ``st.tabs`` runs
every tab body on every rerun; here only the ACTIVE stage's renderer is called, so
an inactive stage never executes a query. (Inside Applicants the two sections
still sit in the same two ``st.tabs`` the old Candidates page used — that is one
stage's own content.)

STATE LIVES IN THE URL
----------------------
``job`` and ``stage`` query parameters are the source of truth, so a refresh,
back/forward and a bookmark all work. The stage control's widget value is only a
mirror: it is overwritten from the URL at the start of every run, and its
``on_change`` callback writes a user's choice back to the URL (callbacks run
before the next script run, so no ``st.rerun()`` and no rerun loop). The control is
``required=True``; if it ever returns ``None`` anyway the URL's stage is used.

Navigating to another sidebar page clears the query string (Streamlit's own
behaviour), which is exactly right: leaving the Jobs page leaves the workspace.

A bad ``job`` or ``stage`` shows a calm message and a button back to the list —
never a traceback.
"""

from __future__ import annotations

import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.pages.candidates import _render_applications_tab, _render_ranking_section
from app.pages.interviews import (
    _render_final_ranking_section,
    _render_shortlisted_section,
)
from app.pages.jobs import _job_view, _render_job_body
from app.services.interview_feedback_service import list_feedback_views
from app.services.interview_guide_service import get_shortlisted_candidates_for_job
from app.services.job_service import get_job
from app.services.job_workspace_service import (
    STAGE_KEYS,
    STAGE_NAMES,
    JobStageSummary,
    default_stage,
    get_job_stage_summary,
)
from app.utils.authorization import UnauthorizedError
from app.utils.ui import label_for, stage_label, status_kind
from app.utils.ui_widgets import load_error, page_header
from app.utils.workspace_nav import (
    close_workspace,
    requested_job,
    requested_stage,
    set_stage,
)

_NOT_FOUND = (
    "That job could not be found. It may have been removed, or the link is wrong."
)
_BAD_STAGE = (
    "That stage doesn't exist for a job workspace. Go back to the job list and "
    "open the job again."
)
_SESSION_INVALID = "Your session looks invalid — please sign out and back in."
_INACTIVE = "Your account is no longer active — please contact an admin."


# --- pure helpers ------------------------------------------------------------


def stage_count_text(key: str, summary: JobStageSummary) -> str | None:
    """What a stage's label shows after its name. Pure.

    Setup: ``v2`` (the approved rubric's version) or nothing. Applicants,
    Shortlist, Interviews: the count. Final ranking: ``N of M decided`` (M =
    interviewed applications) or nothing when nobody has been interviewed."""
    stage = summary.stage(key)
    if key == "setup":
        return f"v{stage.count}" if stage.count else None
    if key == "final_ranking":
        if summary.interviewed_count == 0:
            return None
        return f"{stage.count} of {summary.interviewed_count} decided"
    return str(stage.count)


def stage_labels(summary: JobStageSummary) -> dict[str, str]:
    """Stage key -> the plain-text label shown on the stage control. Pure."""
    return {
        key: stage_label(
            number,
            STAGE_NAMES[key],
            count_text=stage_count_text(key, summary),
            complete=summary.stage(key).complete,
            attention=summary.stage(key).attention,
        )
        for number, key in enumerate(STAGE_KEYS, start=1)
    }


def neighbours(stage: str) -> tuple[str | None, str | None]:
    """``(previous, next)`` stage keys, ``None`` at either end. Pure."""
    index = STAGE_KEYS.index(stage)
    previous = STAGE_KEYS[index - 1] if index > 0 else None
    following = STAGE_KEYS[index + 1] if index < len(STAGE_KEYS) - 1 else None
    return previous, following


# --- stage bodies -------------------------------------------------------------


def _stage_setup(job_id: str, acting_user_id) -> None:
    try:
        with session_scope() as db:
            job = get_job(db, job_id)
            job_view = _job_view(job) if job is not None else None
    except SQLAlchemyError:
        load_error("Couldn't load this job right now.")
        return
    if job_view is None:
        load_error("Couldn't find this job.")
        return
    _render_job_body(job_view, acting_user_id)


def _stage_applicants(job_id: str, acting_user_id) -> None:
    ranking_tab, applications_tab = st.tabs(["Ranking & Shortlist", "Applications"])
    with ranking_tab:
        _render_ranking_section(job_id, acting_user_id)
    with applications_tab:
        _render_applications_tab(job_id, acting_user_id)


def _shortlist_rank_text(row) -> str:
    if row.current_rank_available and row.current_rank_position is not None:
        return f"#{row.current_rank_position}"
    if row.rank_position_at_decision is not None:
        return f"#{row.rank_position_at_decision} (at shortlisting)"
    return "—"


def _stage_shortlist(job_id: str, acting_user_id) -> None:
    st.subheader("Shortlist")
    try:
        with session_scope() as db:
            rows = get_shortlisted_candidates_for_job(
                db, job_id=job_id, acting_user_id=acting_user_id
            )
            rounds = {
                r.application_id: len(
                    list_feedback_views(
                        db, r.application_id, acting_user_id=acting_user_id
                    )
                )
                for r in rows
            }
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load the shortlist right now.")
        return

    if not rows:
        st.caption(
            "No candidates are shortlisted for this job yet. Shortlisting is "
            "done in Applicants."
        )
        return

    st.table(
        [
            {
                "Candidate": r.candidate_name,
                "Screening rank": _shortlist_rank_text(r),
                "Interview guide": "Generated" if r.guide_exists else "Not generated",
                "Rounds recorded": rounds.get(r.application_id, 0),
            }
            for r in rows
        ]
    )
    st.caption(
        "Read-only. Adding or removing candidates is done in Applicants "
        "(Ranking & Shortlist)."
    )


def _stage_interviews(job_id: str, acting_user_id) -> None:
    _render_shortlisted_section(job_id, acting_user_id)


def _stage_final_ranking(job_id: str, acting_user_id) -> None:
    _render_final_ranking_section(job_id, acting_user_id)


def _render_stage(stage: str, job_id: str, acting_user_id) -> None:
    """Run ONLY the named stage's renderer (looked up when called, so each name
    can be replaced in tests and nothing else is ever executed)."""
    if stage == "setup":
        _stage_setup(job_id, acting_user_id)
    elif stage == "applicants":
        _stage_applicants(job_id, acting_user_id)
    elif stage == "shortlist":
        _stage_shortlist(job_id, acting_user_id)
    elif stage == "interviews":
        _stage_interviews(job_id, acting_user_id)
    elif stage == "final_ranking":
        _stage_final_ranking(job_id, acting_user_id)


# --- page chrome ------------------------------------------------------------------


def _problem(message: str) -> None:
    """A calm "can't show this" with the way back. Never a traceback."""
    st.warning(message)
    st.button(
        "Back to jobs", key="ws_back_to_jobs", type="primary", on_click=close_workspace
    )


def _on_stage_change(widget_key: str) -> None:
    """The control's callback: write the user's choice to the URL. A ``None``
    (should not happen with ``required=True``) is ignored, which keeps the
    previous stage."""
    chosen = st.session_state.get(widget_key)
    if chosen in STAGE_KEYS:
        set_stage(chosen)


def render_job_workspace(acting_user_id: uuid.UUID | None) -> None:
    """Render the workspace for the ``?job=`` in the URL."""
    if acting_user_id is None:
        st.error(_SESSION_INVALID)
        return

    job_param = requested_job()
    stage_param = requested_stage()

    try:
        with session_scope() as db:
            summary = get_job_stage_summary(
                db, job_param, acting_user_id=acting_user_id
            )
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load this job right now.")
        st.button("Back to jobs", key="ws_back_to_jobs", on_click=close_workspace)
        return

    if summary is None:
        _problem(_NOT_FOUND)
        return
    if stage_param is not None and stage_param not in STAGE_KEYS:
        _problem(_BAD_STAGE)
        return

    header = summary.header
    job_id = str(header.job_id)
    stage = stage_param or default_stage(summary)

    # breadcrumb: Jobs / <this job>
    crumbs = st.columns([1, 8], vertical_alignment="center")
    crumbs[0].button(
        "Jobs", key="ws_crumb_jobs", type="tertiary", on_click=close_workspace
    )
    crumbs[1].caption(f"/  {header.title}")

    page_header(
        header.title,
        code=header.code,
        status_kind=status_kind(header.status),
        status_text=label_for(header.status),
    )

    # The URL is the truth: overwrite the widget's mirrored value from it BEFORE
    # the widget is created, every run.
    widget_key = f"ws_stage_{job_id}"
    st.session_state[widget_key] = stage
    labels = stage_labels(summary)
    selected = st.segmented_control(
        "Stage",
        list(STAGE_KEYS),
        format_func=labels.__getitem__,
        selection_mode="single",
        required=True,
        key=widget_key,
        on_change=_on_stage_change,
        args=(widget_key,),
        label_visibility="collapsed",
    )
    # Never empty: fall back to the stage the URL named.
    active = selected if selected in STAGE_KEYS else stage

    _render_stage(active, job_id, acting_user_id)

    st.divider()
    previous, following = neighbours(active)
    nav = st.columns([1, 1, 6])
    nav[0].button(
        "Previous",
        key=f"ws_prev_{job_id}",
        disabled=previous is None,
        on_click=set_stage,
        args=(previous or active,),
        help=f"Go to {STAGE_NAMES[previous]}" if previous else None,
    )
    nav[1].button(
        "Next",
        key=f"ws_next_{job_id}",
        type="primary",
        disabled=following is None,
        on_click=set_stage,
        args=(following or active,),
        help=f"Go to {STAGE_NAMES[following]}" if following else None,
    )
