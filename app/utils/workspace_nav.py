"""Query-parameter navigation for the job workspace (presentation only).

The workspace's state lives in the URL — ``?job=<job id>&stage=<stage key>`` — so
a refresh, the browser's back/forward buttons and a bookmark all land on the same
job and stage. A candidate page (HR UI redesign, Increment 3) adds two more
parameters to the same URL: ``&candidate=<application id>&tab=<tab key>``. The
``stage`` is KEPT while a candidate is open, so closing the candidate returns to the
stage it was opened from. ``st.session_state`` only MIRRORS it (the stage control's widget
value); it is never the source of truth.

These helpers are written to be used as ``on_click`` / ``on_change`` callbacks:
Streamlit runs a callback BEFORE the next script run, so the run that follows
already sees the new URL and no explicit ``st.rerun()`` (and so no rerun loop) is
needed. This is a leaf module (it imports only Streamlit) so both the Jobs page
and the workspace can use it without importing each other.

Nothing here touches the database, and nothing user-supplied is ever written
anywhere but the query string.

KNOWN LIMITATION — BACK / FORWARD
---------------------------------
Every stage change — and, on the candidate page, every candidate or tab change —
pushes a browser history entry, so the URL is always correct and shareable. But
Streamlit does NOT rerun a page when only the query string changes through the
browser's Back/Forward buttons: the URL reverts while the page keeps showing the
previous stage (or candidate, or tab) until the user's next click, which re-reads
the URL. Fixing that needs client-side JavaScript, which this app does not use. Nothing
here is wrong or lost in the meantime — the URL is still the truth.
"""

from __future__ import annotations

import uuid

import streamlit as st

from app.utils.candidate_progress import CANDIDATE_TABS, DEFAULT_TAB

#: Query-string parameter names.
QP_JOB = "job"
QP_STAGE = "stage"
QP_CANDIDATE = "candidate"
QP_TAB = "tab"

# ``CANDIDATE_TABS`` / ``DEFAULT_TAB`` live in the pure ``candidate_progress`` module
# (they are re-exported here): the tab keys are the ``tab`` parameter's values, and
# an unknown value falls back to Overview.


def open_workspace(job_id: str, stage: str | None = None) -> None:
    """Point the URL at one job's workspace (``stage`` optional: the page then
    picks the first incomplete stage). Usable as an ``on_click`` callback."""
    st.query_params.clear()
    st.query_params[QP_JOB] = str(job_id)
    if stage:
        st.query_params[QP_STAGE] = stage


def set_stage(stage: str) -> None:
    """Change only the stage, keeping the job. Usable as a callback."""
    st.query_params[QP_STAGE] = stage


def close_workspace() -> None:
    """Back to the job list: drop both parameters. Usable as a callback."""
    st.query_params.clear()


def requested_job() -> str | None:
    """The ``job`` parameter, or ``None`` when absent/blank."""
    value = st.query_params.get(QP_JOB)
    return value.strip() or None if isinstance(value, str) else None


def requested_stage() -> str | None:
    """The ``stage`` parameter, or ``None`` when absent/blank."""
    value = st.query_params.get(QP_STAGE)
    return value.strip() or None if isinstance(value, str) else None


def open_candidate(application_id: str, tab: str | None = None) -> None:
    """Open one candidate page: set ``candidate`` (and ``tab`` when given), keeping
    the job, the stage and — when ``tab`` is omitted — the tab already in the URL
    (so the Prev/Next switcher stays on the tab the user is reading). Usable as an
    ``on_click`` / ``on_change`` callback."""
    st.query_params[QP_CANDIDATE] = str(application_id)
    if tab:
        st.query_params[QP_TAB] = tab


def set_tab(tab: str) -> None:
    """Change only the candidate tab. Usable as a callback."""
    st.query_params[QP_TAB] = tab


def close_candidate() -> None:
    """Back to the job workspace: drop ``candidate`` and ``tab``, keep ``job`` and
    ``stage``. Usable as a callback."""
    for name in (QP_CANDIDATE, QP_TAB):
        if name in st.query_params:
            del st.query_params[name]


def requested_candidate() -> str | None:
    """The ``candidate`` parameter, or ``None`` when absent/blank. Not validated
    here — the candidate page checks it parses AND belongs to the job."""
    value = st.query_params.get(QP_CANDIDATE)
    return value.strip() or None if isinstance(value, str) else None


def requested_tab() -> str | None:
    """The ``tab`` parameter, or ``None`` when absent/blank."""
    value = st.query_params.get(QP_TAB)
    return value.strip() or None if isinstance(value, str) else None


def resolve_tab(value: str | None) -> str:
    """A known tab key, else :data:`DEFAULT_TAB`. Pure."""
    return value if value in CANDIDATE_TABS else DEFAULT_TAB


def deep_link_target() -> dict[str, str] | None:
    """The query parameters to hand to the Jobs page when the Dashboard was
    reached with a workspace link, or ``None`` to stay on the Dashboard.

    A logged-out visit to ``/jobs?job=..&stage=..`` is redirected by Streamlit to
    ``/?job=..&stage=..`` (the login page is the only registered page), so after
    sign-in the Dashboard is what loads. Only a ``job`` that parses as a UUID
    qualifies; anything else is ignored — no error, no redirect. The ``stage`` is
    passed through unvalidated: the workspace itself shows a calm message for an
    unknown one. A ``candidate`` is carried only when it parses as a UUID (whether
    it belongs to the job is the candidate page's business, one read there), and a
    ``tab`` only alongside a candidate and only when it is a known tab — an unknown
    tab is dropped, which the page reads as Overview. Pure apart from reading the
    query string; nothing is logged."""
    job = requested_job()
    if job is None:
        return None
    try:
        uuid.UUID(job)
    except ValueError:
        return None
    target = {QP_JOB: job}
    stage = requested_stage()
    if stage is not None:
        target[QP_STAGE] = stage
    candidate = requested_candidate()
    if candidate is not None:
        try:
            uuid.UUID(candidate)
        except ValueError:
            return target
        target[QP_CANDIDATE] = candidate
        tab = requested_tab()
        if tab in CANDIDATE_TABS:
            target[QP_TAB] = tab
    return target
