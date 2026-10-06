"""Query-parameter navigation for the job workspace (presentation only).

The workspace's state lives in the URL — ``?job=<job id>&stage=<stage key>`` — so
a refresh, the browser's back/forward buttons and a bookmark all land on the same
job and stage. ``st.session_state`` only MIRRORS it (the stage control's widget
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
Every stage change pushes a browser history entry, so the URL is always correct
and shareable. But Streamlit does NOT rerun a page when only the query string
changes through the browser's Back/Forward buttons: the URL reverts while the page
keeps showing the previous stage until the user's next click, which re-reads the
URL. Fixing that needs client-side JavaScript, which this app does not use. Nothing
here is wrong or lost in the meantime — the URL is still the truth.
"""

from __future__ import annotations

import uuid

import streamlit as st

#: Query-string parameter names.
QP_JOB = "job"
QP_STAGE = "stage"


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


def deep_link_target() -> dict[str, str] | None:
    """The query parameters to hand to the Jobs page when the Dashboard was
    reached with a workspace link, or ``None`` to stay on the Dashboard.

    A logged-out visit to ``/jobs?job=..&stage=..`` is redirected by Streamlit to
    ``/?job=..&stage=..`` (the login page is the only registered page), so after
    sign-in the Dashboard is what loads. Only a ``job`` that parses as a UUID
    qualifies; anything else is ignored — no error, no redirect. The ``stage`` is
    passed through unvalidated: the workspace itself shows a calm message for an
    unknown one. Pure apart from reading the query string; nothing is logged."""
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
    return target
