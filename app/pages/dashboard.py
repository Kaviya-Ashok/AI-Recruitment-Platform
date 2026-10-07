"""Dashboard — the whole pipeline at a glance (HR UI redesign, Increment 4).

Four figures (open jobs, applications, interviewed, final decisions recorded), a
"Needs attention" table, a pipeline block for each open job, and Quick find.

THREE QUERIES, WHATEVER THE SIZE
--------------------------------
Everything comes from ``overview_service.get_dashboard_summary`` — the guard, one
per-job counts statement and one Needs-attention statement — not from walking jobs
and applications. The summary is read once per run.

NEEDS ATTENTION has exactly three item types — ratings missing, résumé missing,
decision pending — in that order, then job code, then candidate name; at most 25 are
shown and the total is always stated. Each row's button opens the existing deep link
(job, stage, candidate, tab): résumé missing -> the workspace's Applicants stage;
ratings missing -> the candidate page's Interview tab; decision pending -> its
Decision tab.

A workspace link (``/?job=..&stage=..``) lands here after sign-in or a refresh,
because Streamlit's logged-out redirect drops the Jobs page's path; it is handed on
to the Jobs page FIRST, before anything else runs. ``switch_page`` replaces the query
string, so the parameters are consumed and cannot loop. (Back/Forward limitation: see
app/utils/workspace_nav.py.)
"""

from __future__ import annotations

import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.pages.quick_find import render_quick_find
from app.services.overview_service import (
    AttentionItem,
    DashboardSummary,
    JobPipeline,
    get_dashboard_summary,
)
from app.utils.authorization import UnauthorizedError
from app.utils.candidate_progress import STAGE_LABELS
from app.utils.overview_helpers import (
    ATTENTION_ACTIONS,
    ATTENTION_LABELS,
    attention_count_text,
    attention_link,
    build_link,
)
from app.utils.session import get_current_user
from app.utils.ui_widgets import go_to_jobs, load_error, page_header
from app.utils.workspace_nav import deep_link_target

_SESSION_INVALID = "Your session looks invalid — please sign out and back in."
_INACTIVE = "Your account is no longer active — please contact an admin."


def pipeline_rows(pipeline: JobPipeline) -> list[tuple[str, int, float]]:
    """``(label, count, fraction of applicants)`` for each stage of one open job, in
    order — the stage words are the candidate page's. Pure."""
    counts = [
        ("applied", pipeline.applicants),
        ("screened", pipeline.screened),
        ("shortlisted", pipeline.shortlisted),
        ("interviewed", pipeline.interviewed),
        ("decided", pipeline.decided),
    ]
    total = pipeline.applicants
    return [
        (STAGE_LABELS[key], count, min(count / total, 1.0) if total > 0 else 0.0)
        for key, count in counts
    ]


def _render_metrics(summary: DashboardSummary) -> None:
    cols = st.columns(4)
    cols[0].metric("Open jobs", summary.open_jobs, border=True)
    cols[1].metric("Applications", summary.applications, border=True)
    cols[2].metric("Interviewed", summary.interviewed, border=True)
    cols[3].metric(
        "Final decisions", summary.decided, border=True,
        help="Candidates with a recorded final decision.",
    )


def _render_attention_row(pages, index: int, item: AttentionItem) -> None:
    cols = st.columns([2, 1, 3, 2], vertical_alignment="center")
    cols[0].text(ATTENTION_LABELS[item.kind])
    cols[1].text(item.job_code)
    cols[2].text(item.candidate_name)
    if cols[3].button(
        ATTENTION_ACTIONS[item.kind], key=f"dash_attention_{index}", width="stretch"
    ):
        go_to_jobs(pages, attention_link(item.kind, item.job_id, item.application_id))


def _render_attention(pages, summary: DashboardSummary) -> None:
    with st.container(border=True):
        head = st.columns([3, 1], vertical_alignment="center")
        head[0].subheader("Needs attention")
        head[1].caption(
            attention_count_text(len(summary.attention), summary.attention_total)
        )
        if not summary.attention:
            st.caption("Nothing needs attention right now.")
            return
        labels = st.columns([2, 1, 3, 2])
        for col, text in zip(labels, ("Item", "Job", "Candidate", "")):
            col.caption(text)
        for index, item in enumerate(summary.attention):
            _render_attention_row(pages, index, item)


def _render_pipelines(pages, summary: DashboardSummary) -> None:
    st.subheader("Pipeline by stage")
    if not summary.pipelines:
        st.caption("No open jobs.")
        return
    for pipeline in summary.pipelines:
        with st.container(border=True):
            head = st.columns([4, 1], vertical_alignment="center")
            head[0].markdown(f"**{pipeline.job_code}** {pipeline.job_title}")
            if head[1].button(
                "Open job", key=f"dash_open_{pipeline.job_id}", width="stretch"
            ):
                go_to_jobs(pages, build_link(pipeline.job_id))
            for label, count, fraction in pipeline_rows(pipeline):
                st.progress(fraction, text=f"{label} · {count}")


def render_dashboard_page(pages) -> None:
    target = deep_link_target()
    if target is not None:
        st.switch_page(pages["jobs"], query_params=target)

    page_header("Dashboard", "Overview across all jobs.")

    current = get_current_user(st.session_state)
    try:
        acting_user_id = uuid.UUID(current["id"])
    except (KeyError, TypeError, ValueError):
        st.error(_SESSION_INVALID)
        return

    render_quick_find(pages, acting_user_id, scope="dashboard")

    try:
        with session_scope() as db:
            summary = get_dashboard_summary(db, acting_user_id=acting_user_id)
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except SQLAlchemyError:
        load_error("Couldn't load the dashboard right now. Please try again.")
        return

    if summary.jobs == 0:
        st.info("No jobs yet. Create your first job on the **Jobs** page.")
        st.page_link(pages["jobs"], label="Go to Jobs", icon=":material/work:")

    _render_metrics(summary)
    _render_attention(pages, summary)
    _render_pipelines(pages, summary)
