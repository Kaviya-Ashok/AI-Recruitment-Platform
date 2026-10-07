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
* Shortlist      -> a READ-ONLY table (name, screening rank, guide, rounds)
* Interviews     -> a READ-ONLY overview table (rounds, feedback, transcripts,
                    AI analysis, decision) — the per-candidate work moved to the
                    candidate page
* Final ranking  -> ``interviews._render_final_ranking_section``

so every existing behaviour, audit event, confirmation and permission check inside
them is unchanged. (Those renderers already took ``job_id``; the old Candidates
and Interviews pages still call them with the job picker's value.)

CANDIDATE PAGE (Increment 3)
----------------------------
With ``&candidate=<application id>`` in the URL this module renders the candidate
page (``app/pages/candidate_page.py``) instead of the workspace. The Shortlist and
Interviews tables open it by row selection (``st.dataframe`` single-row
``on_select``); Applicants and Final ranking carry a small "Open candidate page"
selector at the top. ``stage`` stays in the URL, so closing the candidate returns to
the stage it was opened from.

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
from app.pages.candidate_page import render_candidate_page
from app.pages.candidates import _render_applications_tab, _render_ranking_section
from app.pages.interviews import _render_final_ranking_section
from app.pages.jobs import _job_view, _render_job_body
from app.services.job_service import get_job
from app.services.job_workspace_service import (
    STAGE_KEYS,
    STAGE_NAMES,
    JobStageSummary,
    default_stage,
    get_job_stage_summary,
    list_interview_overview,
    list_job_candidates,
    list_shortlist_overview,
)
from app.utils.authorization import UnauthorizedError
from app.utils.ranking_drift import (
    drift_note,
    ranking_drift,
    rubric_label,
    version_note,
)
from app.utils.ui import label_for, stage_label, status_kind
from app.utils.ui_widgets import load_error, page_header
from app.utils.workspace_nav import (
    close_workspace,
    open_candidate,
    requested_candidate,
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


def selected_application_id(selected_rows, application_ids) -> str | None:
    """The application id of the row a ``st.dataframe`` single-row selection
    points at, or ``None`` (nothing selected, or an index outside the table).
    Pure."""
    rows = list(selected_rows or [])
    if not rows:
        return None
    index = rows[0]
    if not isinstance(index, int) or not 0 <= index < len(application_ids):
        return None
    return str(application_ids[index])


def feedback_status_text(rounds: int, has_unrated_round: bool) -> str:
    """The Interviews table's Feedback cell, in words. Pure."""
    if rounds == 0:
        return "None recorded"
    if has_unrated_round:
        return "Ratings missing"
    return "Rated"


def _candidate_option_label(candidate) -> str:
    rank = (
        f"screening rank #{candidate.screening_rank}"
        if candidate.screening_rank is not None else "not ranked"
    )
    return f"{candidate.candidate_name} ({rank})"


# --- opening a candidate -----------------------------------------------------------


def _open_selected(widget_key: str) -> None:
    """``on_change`` for the "Open candidate page" selector: point the URL at the
    chosen application (callbacks run before the next run — no ``st.rerun()``)."""
    chosen = st.session_state.get(widget_key)
    if chosen:
        open_candidate(chosen)


def _candidate_selector(job_id: str, acting_user_id, *, scope: str) -> None:
    """A small "Open candidate page" selectbox for the stages that keep their
    reused sections (Applicants, Final ranking)."""
    try:
        with session_scope() as db:
            candidates = list_job_candidates(
                db, job_id, acting_user_id=acting_user_id
            )
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load the candidate list right now.")
        return
    if not candidates:
        return
    labels = {str(c.application_id): _candidate_option_label(c) for c in candidates}
    widget_key = f"ws_open_{scope}_{job_id}"
    st.selectbox(
        "Open candidate page",
        list(labels),
        index=None,
        placeholder="Choose a candidate",
        format_func=labels.__getitem__,
        key=widget_key,
        on_change=_open_selected,
        args=(widget_key,),
    )


def _open_from_table(event, application_ids) -> None:
    """If a table row is selected, open that candidate and rerun so the URL change
    is picked up."""
    target = selected_application_id(event.selection.rows, application_ids)
    if target is not None:
        open_candidate(target)
        st.rerun()


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
    _candidate_selector(job_id, acting_user_id, scope="applicants")
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
            rows = list_shortlist_overview(
                db, job_id, acting_user_id=acting_user_id
            )
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

    note = version_note(r.rubric_version_number for r in rows)
    if note:
        st.caption(note)

    event = st.dataframe(
        [
            {
                "Candidate": r.candidate_name,
                "Screening rank": _shortlist_rank_text(r),
                "Ranking note": drift_note(
                    ranking_drift(
                        r.current_rank_available,
                        r.current_rank_position,
                        r.rank_position_at_decision,
                    )
                ),
                "Rubric": rubric_label(
                    r.rubric_version_number, r.rubric_version_status
                ),
                "Interview guide": "Generated" if r.guide_exists else "Not generated",
                "Rounds recorded": r.rounds_count,
            }
            for r in rows
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"ws_shortlist_table_{job_id}",
    )
    st.caption(
        "Select a row to open that candidate's page. Adding or removing "
        "candidates is done in Applicants (Ranking & Shortlist)."
    )
    _open_from_table(event, [r.application_id for r in rows])


def _stage_interviews(job_id: str, acting_user_id) -> None:
    st.subheader("Interviews")
    try:
        with session_scope() as db:
            rows = list_interview_overview(
                db, job_id, acting_user_id=acting_user_id
            )
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load the interviews right now.")
        return

    if not rows:
        st.caption(
            "No candidates are in the interview stage for this job yet. "
            "Shortlisting is done in Applicants."
        )
        return

    st.info(
        "The interview guide, feedback, transcripts, AI analysis and the decision "
        "for each person are on that candidate's page. This table is the overview."
    )
    event = st.dataframe(
        [
            {
                "Candidate": r.candidate_name,
                "Rounds": r.rounds_count,
                "Feedback": feedback_status_text(r.rounds_count, r.has_unrated_round),
                "Transcripts": r.transcripts_count,
                "AI analysis": "Generated" if r.has_analysis else "Not generated",
                "Final decision": (
                    label_for(r.decision) if r.decision else "Not decided"
                ),
            }
            for r in rows
        ],
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=f"ws_interviews_table_{job_id}",
    )
    st.caption("Select a row to open that candidate's page.")
    _open_from_table(event, [r.application_id for r in rows])


def _stage_final_ranking(job_id: str, acting_user_id) -> None:
    _candidate_selector(job_id, acting_user_id, scope="final_ranking")
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

    # ``&candidate=`` turns the workspace into that application's page.
    if requested_candidate() is not None:
        render_candidate_page(acting_user_id)
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
