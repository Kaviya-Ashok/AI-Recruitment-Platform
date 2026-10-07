"""Candidate page — one application of one job (HR UI redesign, Increment 3).

Shown by the job workspace when the URL carries ``&candidate=<application id>``
(see ``app/utils/workspace_nav.py``); the Jobs page still dispatches to the
workspace, so the post-login redirect keeps working. Header, four-figure summary
strip, six tabs, and a right-hand panel with Progress, Next step, the Final
decision summary and the Prev/Next candidate switcher.

NOTHING IS REWRITTEN HERE
-------------------------
Every tab body is an EXISTING renderer, reused as-is for this one application:

* Overview   -> header facts + ``list_candidate_activity`` (a new read)
* Screening  -> ``candidates._render_application_body`` (screening status, résumé,
                prequalification, initial scorecard, recovery controls) and
                ``interviews._render_screening_transcript``
* Interview  -> ``interviews._render_guide_controls`` / ``_render_guide``,
                ``_render_feedback_section`` (history, add-feedback form,
                transcripts: upload, download, replace)
* AI analysis-> ``interviews._render_analysis_section``
* Scorecard  -> ``interviews._render_final_scorecard_section``
* Decision   -> ``interviews._render_final_decision_record`` + the SAME form
                (``interviews._render_decision_form``) inside a native dialog

so every behaviour, audit event, confirmation and role check inside them is
unchanged.

LAZY BY CONSTRUCTION
--------------------
The tab control is ``st.segmented_control`` (not ``st.tabs``): only the ACTIVE
tab's renderer runs, so an inactive tab never executes a query. The header facts
come from ONE ``get_candidate_header`` call per run (a handful of point queries —
the full scorecard assembly would be dozens), and the Prev/Next order from one
``list_job_candidates`` call.

STATE LIVES IN THE URL
----------------------
``job``, ``candidate`` and ``tab`` are the source of truth. The tab control's widget
value only MIRRORS the URL: it is overwritten from it at the start of every run and
its ``on_change`` callback writes a user's choice back (callbacks run before the
next script run, so no ``st.rerun()`` and no loop). The control is ``required=True``;
if it ever returns ``None`` the URL's tab is used, and an unknown tab is Overview.
The ``stage`` parameter is kept, so closing the candidate returns to the stage it
was opened from.

THE DECISION DIALOG
-------------------
The Record / Change decision form moved into ``st.dialog``, opened from a primary
button in the header (any tab), from Next step and from the Decision tab. Closing
after a successful save is the form's own ``st.rerun()``; a validation message
leaves the dialog open. Anyone whose role may not decide sees no button, only the
existing read-only note.

A bad ``candidate`` (malformed, unknown, or another job's) shows a calm message and
a button back to the workspace — never a traceback.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.pages import candidates as _candidates
from app.pages import interviews as _interviews
from app.utils.ranking_drift import ranking_drift, rubric_label
from app.services.final_decision_service import role_may_decide
from app.services.interview_guide_service import get_interview_guide_for_application
from app.services.job_workspace_service import (
    CandidateHeader,
    JobCandidate,
    get_candidate_header,
    list_candidate_activity,
    list_job_candidates,
)
from app.services.screening_question_service import get_screening_transcript
from app.utils.authorization import UnauthorizedError
from app.utils.candidate_progress import (
    ACTION_RECORD_DECISION,
    activity_label,
    next_step,
    progress_button_label,
    progress_items,
    rank_text,
    score_text,
    stage_name,
    tab_label,
)
from app.utils.session import get_current_user
from app.utils.ui import badge, label_for, recommendation_kind
from app.utils.ui_widgets import detail_lines, load_error, page_header
from app.utils.workspace_nav import (
    CANDIDATE_TABS,
    close_candidate,
    close_workspace,
    open_candidate,
    requested_candidate,
    requested_job,
    requested_tab,
    resolve_tab,
    set_tab,
)

_BAD_CANDIDATE = (
    "That candidate could not be found for this job. The link may be wrong, or "
    "the application belongs to a different job."
)
_SESSION_INVALID = "Your session looks invalid — please sign out and back in."
_INACTIVE = "Your account is no longer active — please contact an admin."
_NOT_SHORTLISTED = (
    "Not currently shortlisted — interview-guide generation is blocked for this "
    "candidate. Interview feedback can still be recorded: an interview that "
    "happened stays recordable."
)
_NO_TRANSCRIPT = "No screening transcript is available for this candidate yet."

_PROGRESS_ICON = {
    "done": ":material/check_circle:",
    "warn": ":material/error:",
    "todo": ":material/radio_button_unchecked:",
}


@dataclass(frozen=True)
class _Ctx:
    """What every tab body needs."""

    header: CandidateHeader
    acting_user_id: uuid.UUID
    can_decide: bool


# --- small helpers -----------------------------------------------------------------


def _can_decide() -> bool:
    """The role gate for recording a decision (hiring manager or admin)."""
    return role_may_decide((get_current_user(st.session_state) or {}).get("role"))


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def neighbours(
    order: list[JobCandidate], application_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None, int | None]:
    """``(previous id, next id, 1-based position)`` of ``application_id`` in the
    switcher order; ``None`` at either end, and ``(None, None, None)`` when the
    application is not in the list. Pure."""
    ids = [c.application_id for c in order]
    if application_id not in ids:
        return None, None, None
    index = ids.index(application_id)
    previous = ids[index - 1] if index > 0 else None
    following = ids[index + 1] if index < len(ids) - 1 else None
    return previous, following, index + 1


def _problem(message: str) -> None:
    """A calm "can't show this" with the way back. Never a traceback."""
    st.warning(message)
    st.button(
        "Back to the job workspace",
        key="cp_back_to_workspace",
        type="primary",
        on_click=close_candidate,
    )


# --- the decision dialog -------------------------------------------------------------


def _decision_dialog_body(header: CandidateHeader, acting_user_id) -> None:
    """The dialog's content: who it is about, then the existing form. A plain
    function (no ``st.dialog`` here) so it can be exercised directly."""
    st.caption(f"{header.candidate_name} · {header.job_code} {header.job_title}")
    _interviews._render_decision_form(
        header.application_id, header.decision, acting_user_id
    )


def _open_decision_dialog(header: CandidateHeader, acting_user_id) -> None:
    title = (
        f"Change final decision — {header.candidate_name}"
        if header.decision
        else f"Record final decision — {header.candidate_name}"
    )

    @st.dialog(title)
    def _dialog() -> None:
        _decision_dialog_body(header, acting_user_id)

    _dialog()


def _decision_button_label(header: CandidateHeader) -> str:
    return "Change decision" if header.decision else "Record decision"


# --- tab bodies -----------------------------------------------------------------------


def _render_shortlist_context(h: CandidateHeader) -> None:
    """Under the page header, for a shortlisted candidate: the rubric version they
    were shortlisted against and what has happened to their rank since. A change is a
    warning (its words say what changed — colour is never the only signal); anything
    else is a quiet line. Nothing is shown for a candidate who is not shortlisted."""
    if not h.is_shortlisted:
        return
    drift = ranking_drift(
        h.current_rank_available,
        h.current_rank_position,
        h.rank_position_at_shortlisting,
    )
    version = rubric_label(
        h.shortlist_rubric_version_number, h.shortlist_rubric_version_status
    )
    if drift.caution:
        st.warning(f"{version} · {drift.text}", icon=":material/swap_vert:")
    else:
        st.caption(f"Shortlisted on {version} · {drift.text}")


def _tab_overview(ctx: _Ctx) -> None:
    h = ctx.header
    if h.entry_status == "NOT_RANKED_INELIGIBLE":
        st.error(h.entry_reason or "This candidate is not ranked.")
    elif h.entry_status in ("INCOMPLETE_SCREENING", "INCOMPLETE_INTERVIEW"):
        st.warning(h.entry_reason or "This candidate's record is incomplete.")
    elif h.application_status == "SCREENING_INCOMPLETE":
        st.warning(
            "Screening was not completed. This is not a rejection — the "
            "candidate can still resume from their saved link."
        )

    with st.container(border=True):
        st.markdown("**Summary**")
        detail_lines([
            ("Application status", label_for(h.application_status)),
            (
                "Screening",
                score_text(h.screening_score)
                + (f" · screening rank #{h.screening_rank}"
                   if h.screening_rank is not None else ""),
            ),
            (
                "Interview",
                score_text(h.interview_score)
                + (f" · {h.rounds_count} round" + ("" if h.rounds_count == 1 else "s")
                   if h.rounds_count else " · no rounds recorded"),
            ),
            (
                "Final",
                score_text(h.final_score)
                + " · "
                + rank_text(h.final_rank, h.final_ranked_count, h.entry_status),
            ),
            (
                "Shortlisted",
                "Yes — " + rubric_label(
                    h.shortlist_rubric_version_number,
                    h.shortlist_rubric_version_status,
                )
                if h.is_shortlisted else "No",
            ),
            ("Interview transcripts", str(h.transcripts_count)),
            ("AI analysis", "Generated" if h.has_analysis else "Not generated"),
        ])

    with st.container(border=True):
        st.markdown("**Activity**")
        try:
            with session_scope() as db:
                items = list_candidate_activity(
                    db, h.application_id, acting_user_id=ctx.acting_user_id
                )
        except UnauthorizedError:
            st.error(_INACTIVE)
            return
        except SQLAlchemyError:
            load_error("Couldn't load this candidate's activity right now.")
            return
        if not items:
            st.caption("No activity recorded yet.")
            return
        st.table([
            {
                "Event": activity_label(i.event_type),
                "When": f"{i.timestamp:%Y-%m-%d %H:%M UTC}",
                "By": i.actor_name or "—",
            }
            for i in items
        ])


def _tab_screening(ctx: _Ctx) -> None:
    app_id = str(ctx.header.application_id)
    uid = ctx.acting_user_id
    try:
        summary = _candidates._load_application_summary(app_id, uid)
        detail = _candidates._load_application_detail(app_id, uid)
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load this application right now.")
        return
    if summary is None:
        load_error("Couldn't find this application.")
        return
    _candidates._render_application_body({**summary, **detail}, uid)

    session_id = detail.get("screening_session_id")
    if session_id is None:
        st.caption(_NO_TRANSCRIPT)
        return
    try:
        with session_scope() as db:
            entries = get_screening_transcript(
                db, screening_session_id=session_id, acting_user_id=uid
            )
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load the screening transcript right now.")
        return
    if entries:
        _interviews._render_screening_transcript(entries, len(entries))
    else:
        st.caption(_NO_TRANSCRIPT)


def _tab_interview(ctx: _Ctx) -> None:
    h = ctx.header
    uid = ctx.acting_user_id
    try:
        with session_scope() as db:
            guide = get_interview_guide_for_application(
                db, h.application_id, acting_user_id=uid
            )
            feedback = {
                h.application_id: _interviews._load_feedback_state(
                    db, h.application_id, uid
                )
            }
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load this candidate's interview details right now.")
        return

    if h.is_shortlisted:
        _interviews._render_guide_controls(
            h.application_id, guide, guide is not None, uid
        )
    else:
        # The "retained guide" rule (kept from the old Interviews page): a guide made
        # while shortlisted stays readable, none can be generated now.
        st.info(_NOT_SHORTLISTED)
        if guide is not None:
            with st.expander("Interview guide", expanded=False):
                _interviews._render_guide(guide)
    _interviews._render_feedback_section(h.application_id, feedback, uid)


def _tab_analysis(ctx: _Ctx) -> None:
    h = ctx.header
    uid = ctx.acting_user_id
    try:
        with session_scope() as db:
            feedback = {
                h.application_id: _interviews._load_feedback_state(
                    db, h.application_id, uid
                )
            }
            analysis = {
                h.application_id: _interviews._load_analysis_state(
                    db, h.application_id, uid
                )
            }
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load this candidate's analysis right now.")
        return
    _interviews._render_analysis_section(h.application_id, feedback, analysis, uid)


def _tab_scorecard(ctx: _Ctx) -> None:
    _interviews._render_final_scorecard_section(
        ctx.header.application_id, ctx.acting_user_id
    )


def _tab_decision(ctx: _Ctx) -> None:
    h = ctx.header
    uid = ctx.acting_user_id
    try:
        with session_scope() as db:
            state = {
                h.application_id: _interviews._load_decision_state(
                    db, h.application_id, uid
                )
            }
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load this candidate's decision right now.")
        return

    available, _current = _interviews._render_final_decision_record(
        h.application_id, state
    )
    if not available:
        return
    if not ctx.can_decide:
        st.info(_interviews._DECISION_READ_ONLY_NOTE)
        return
    if st.button(
        _decision_button_label(h),
        key="cp_decide_tab",
        type="primary",
        icon=":material/gavel:",
    ):
        _open_decision_dialog(h, uid)


def _render_tab(tab: str, ctx: _Ctx) -> None:
    """Run ONLY the named tab's renderer (looked up when called, so each name can
    be replaced in tests and nothing else is ever executed)."""
    if tab == "overview":
        _tab_overview(ctx)
    elif tab == "screening":
        _tab_screening(ctx)
    elif tab == "interview":
        _tab_interview(ctx)
    elif tab == "analysis":
        _tab_analysis(ctx)
    elif tab == "scorecard":
        _tab_scorecard(ctx)
    elif tab == "decision":
        _tab_decision(ctx)


# --- the right-hand panel --------------------------------------------------------------


def _render_progress(ctx: _Ctx) -> None:
    with st.container(border=True):
        st.markdown("**Progress**")
        for item in progress_items(ctx.header):
            icon = _PROGRESS_ICON[
                "done" if item.done else ("warn" if item.warn else "todo")
            ]
            st.button(
                progress_button_label(item),
                key=f"cp_prog_{item.key}",
                type="tertiary",
                icon=icon,
                on_click=set_tab,
                args=(item.tab,),
                help=f"Open the {tab_label(item.tab)} tab",
            )


def _render_next_step(ctx: _Ctx) -> None:
    message, button_label, action = next_step(ctx.header)
    with st.container(border=True):
        st.markdown("**Next step**")
        if action == ACTION_RECORD_DECISION:
            if not ctx.can_decide:
                st.caption(
                    "Review the scorecard. A hiring manager or an admin records "
                    "the final decision."
                )
                return
            st.caption(message)
            if st.button(
                button_label, key="cp_next_step", type="primary",
                icon=":material/gavel:",
            ):
                _open_decision_dialog(ctx.header, ctx.acting_user_id)
            return
        st.caption(message)
        st.button(
            button_label, key="cp_next_step", type="primary",
            on_click=set_tab, args=(action,),
        )


def _render_decision_summary(ctx: _Ctx) -> None:
    h = ctx.header
    with st.container(border=True):
        st.markdown("**Final decision**")
        if h.decision is None:
            st.caption(
                "Not recorded yet. Recorded decisions do not notify the "
                "candidate or change any status."
            )
            return
        st.markdown(
            badge(recommendation_kind(h.decision), f"Final decision: {label_for(h.decision)}")
        )
        when = f" on {h.decided_at:%Y-%m-%d}" if h.decided_at else ""
        st.caption(f"Decided by {h.decided_by_name}{when}.")


def _render_switcher(order: list[JobCandidate], application_id: uuid.UUID) -> None:
    previous, following, position = neighbours(order, application_id)
    cols = st.columns(2)
    cols[0].button(
        "Previous", key="cp_prev", icon=":material/chevron_left:", width="stretch",
        disabled=previous is None,
        on_click=open_candidate, args=(str(previous or application_id),),
    )
    cols[1].button(
        "Next", key="cp_next", icon=":material/chevron_right:", width="stretch",
        disabled=following is None,
        on_click=open_candidate, args=(str(following or application_id),),
    )
    if position is not None:
        st.caption(f"{position} of {len(order)} candidates")


# --- page ------------------------------------------------------------------------------


def _on_tab_change(widget_key: str) -> None:
    """The control's callback: write the user's choice to the URL. A ``None``
    (should not happen with ``required=True``) is ignored, which keeps the
    previous tab."""
    chosen = st.session_state.get(widget_key)
    if chosen in CANDIDATE_TABS:
        set_tab(chosen)


def render_candidate_page(acting_user_id: uuid.UUID | None) -> None:
    """Render the candidate page for the ``?candidate=`` in the URL."""
    if acting_user_id is None:
        st.error(_SESSION_INVALID)
        return

    candidate_id = _as_uuid(requested_candidate())
    if candidate_id is None:
        _problem(_BAD_CANDIDATE)
        return

    try:
        with session_scope() as db:
            header = get_candidate_header(
                db, requested_job(), candidate_id, acting_user_id=acting_user_id
            )
            order = (
                list_job_candidates(db, header.job_id, acting_user_id=acting_user_id)
                if header is not None else []
            )
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load this candidate right now.")
        st.button("Back to the job workspace", key="cp_back_to_workspace",
                  on_click=close_candidate)
        return

    if header is None:
        _problem(_BAD_CANDIDATE)
        return

    ctx = _Ctx(header, acting_user_id, _can_decide())
    tab = resolve_tab(requested_tab())

    # breadcrumb: Jobs / <job> / <candidate>
    with st.container(horizontal=True, gap="xsmall", vertical_alignment="center"):
        st.button(
            "Jobs", key="cp_crumb_jobs", type="tertiary", on_click=close_workspace
        )
        st.caption("/", width="content")
        st.button(
            header.job_title, key="cp_crumb_job", type="tertiary",
            on_click=close_candidate,
        )
        st.caption(f"/  {header.candidate_name}", width="content")

    page_header(
        header.candidate_name,
        subtitle=f"{header.candidate_email} · {header.job_title}",
        code=header.job_code,
        # The furthest stage reached, in one short word (the application status
        # itself is on the Overview tab).
        status_kind=(
            recommendation_kind(header.decision) if header.decision else "info"
        ),
        status_text=stage_name(header),
    )

    _render_shortlist_context(header)

    # The primary decision button is available on every tab (role gated).
    if ctx.can_decide:
        _spacer, action = st.columns([4, 1])
        with action:
            if st.button(
                _decision_button_label(header), key="cp_decide_header",
                type="primary", icon=":material/gavel:", width="stretch",
            ):
                _open_decision_dialog(header, acting_user_id)

    metrics = st.columns(4)
    metrics[0].metric("Screening", score_text(header.screening_score), border=True)
    metrics[1].metric("Interview", score_text(header.interview_score), border=True)
    metrics[2].metric("Final score", score_text(header.final_score), border=True)
    metrics[3].metric(
        "Rank",
        rank_text(header.final_rank, header.final_ranked_count, header.entry_status),
        border=True,
    )

    main, side = st.columns([3, 1])
    with main:
        # The URL is the truth: overwrite the widget's mirrored value from it
        # BEFORE the widget is created, every run.
        widget_key = f"cp_tab_{header.application_id}"
        st.session_state[widget_key] = tab
        selected = st.segmented_control(
            "Section",
            list(CANDIDATE_TABS),
            format_func=tab_label,
            selection_mode="single",
            required=True,
            key=widget_key,
            on_change=_on_tab_change,
            args=(widget_key,),
            label_visibility="collapsed",
        )
        # Never empty: fall back to the tab the URL named.
        active = selected if selected in CANDIDATE_TABS else tab
        _render_tab(active, ctx)
    with side:
        _render_progress(ctx)
        _render_next_step(ctx)
        _render_decision_summary(ctx)
        _render_switcher(order, header.application_id)
