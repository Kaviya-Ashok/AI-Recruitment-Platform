"""Streamlit entrypoint — HR-facing, login-gated.

Navigation & the ``app/pages/`` question
----------------------------------------
Streamlit's *automatic* multipage feature would expose every ``app/pages/*.py``
as a sidebar entry that runs **without** the login check below. That is
unacceptable for an auth-gated app. The fix used here: call ``st.navigation``
on *every* run — which disables the automatic ``pages/`` scan entirely — and
only include the real pages once the user is authenticated. Unauthenticated
runs get a single hidden-nav login page. This is the pattern from Streamlit's
own authentication guidance and it lets page modules still live in
``app/pages/`` per CLAUDE.md §14.

DB sessions: Streamlit reruns the whole script per interaction, so we never
hold a session across reruns — each DB call opens a short-lived
``session_scope()`` inside its handler.

Session state holds only a small primitive dict for the logged-in user
(see ``app/utils/session.py``), never a live ORM object.
"""

from __future__ import annotations

# ``streamlit run app/main.py`` puts ``app/`` on sys.path, not the repo root,
# so ``import app.*`` would fail. Put the repo root first before importing.
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.database.models.application import ApplicationStatus
from app.database.models.job import JobStatus
from app.pages.candidates import render_candidates_page
from app.pages.interviews import render_interviews_page
from app.pages.jobs import render_jobs_page
from app.services.application_service import list_applications_for_job
from app.services.auth_service import authenticate_user
from app.services.job_service import list_jobs
from app.services.shortlist_service import get_shortlist_status_for_job
from app.utils.authorization import UnauthorizedError
from app.utils.session import (
    clear_current_user,
    get_current_user,
    is_authenticated,
    set_current_user,
)
from app.utils.ui import label_for

st.set_page_config(
    page_title="Recruitment Intelligence Platform",
    page_icon="🧭",
    # HR app only. The Jobs/Candidates pages are dense, multi-column, tabular
    # views that were cramped in the default ~730px "centered" column (audit
    # H14). The public candidate app (app/public_main.py) deliberately stays
    # "centered" — it is a short mobile-first form.
    layout="wide",
)

_GENERIC_LOGIN_ERROR = "Invalid email or password."
_DB_ERROR = "We couldn't reach the system right now. Please try again in a moment."
_UNEXPECTED_ERROR = "Something went wrong while signing in. Please try again."


def _login_page() -> None:
    st.title("Recruitment Intelligence Platform")
    st.caption("Sign in to continue.")

    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")

    if not submitted:
        return

    if not email or not password:
        st.error("Enter your email and password.")
        return

    try:
        with st.spinner("Signing in…"):
            with session_scope() as db:
                user = authenticate_user(db, email, password)
    except SQLAlchemyError:
        st.error(_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback to the UI
        st.error(_UNEXPECTED_ERROR)
        return

    if user is None:
        st.error(_GENERIC_LOGIN_ERROR)
        return

    # Store only the minimal identifying dict; the plaintext password is a local
    # that disappears when this rerun ends and is never written to session state.
    set_current_user(st.session_state, user)
    st.rerun()


def _acting_user_id() -> uuid.UUID | None:
    current = get_current_user(st.session_state)
    try:
        return uuid.UUID(current["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _gather_landing_data(acting_user_id: uuid.UUID) -> dict:
    """Aggregate the dashboard counts by walking existing per-job accessors.

    No new service code: ``list_jobs`` + ``list_applications_for_job`` +
    ``get_shortlist_status_for_job`` across every job. This is read-only and
    already behind the HR login.

    "Candidates awaiting review" = applications at
    ``SCREENING_EVALUATED`` (their initial scorecard exists) that are **not
    currently shortlisted** — i.e. HR has not yet acted on them. An explicitly
    unshortlisted candidate counts as already reviewed.
    """
    open_jobs = 0
    awaiting_review = 0
    shortlisted = 0
    attention: list[dict] = []

    with session_scope() as db:
        jobs = list_jobs(db)
        for job in jobs:
            if job.status == JobStatus.OPEN:
                open_jobs += 1

            shortlist = get_shortlist_status_for_job(
                db, job_id=job.id, acting_user_id=acting_user_id
            )
            job_awaiting = 0
            for application in list_applications_for_job(db, job.id):
                entry = shortlist.get(application.id)
                is_shortlisted = bool(entry and entry.is_shortlisted)
                if is_shortlisted:
                    shortlisted += 1
                if (
                    application.status == ApplicationStatus.SCREENING_EVALUATED
                    and not is_shortlisted
                ):
                    job_awaiting += 1

            awaiting_review += job_awaiting
            if job_awaiting:
                attention.append(
                    {
                        "title": job.title,
                        "page": "candidates",
                        "note": (
                            f"{job_awaiting} candidate"
                            f"{'s' if job_awaiting != 1 else ''} awaiting review"
                        ),
                    }
                )
            elif job.status in (JobStatus.JD_ANALYZED, JobStatus.RUBRIC_PENDING):
                attention.append(
                    {
                        "title": job.title,
                        "page": "jobs",
                        "note": "rubric awaiting approval",
                    }
                )

    return {
        "has_jobs": bool(jobs),
        "open_jobs": open_jobs,
        "awaiting_review": awaiting_review,
        "shortlisted": shortlisted,
        "attention": attention,
    }


def _dashboard_page() -> None:
    current = get_current_user(st.session_state)
    st.title("Dashboard")
    st.caption(
        f"Signed in as {current['full_name']} · {label_for(current['role'])}"
    )

    acting_user_id = _acting_user_id()
    if acting_user_id is None:
        st.error("Your session looks invalid — please sign out and back in.")
        return

    try:
        data = _gather_landing_data(acting_user_id)
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except SQLAlchemyError:
        st.error("Couldn't load the dashboard right now. Please try again.")
        return

    if not data["has_jobs"]:
        st.info("No jobs yet. Create your first job on the **Jobs** page.")
        st.page_link(_PAGES["jobs"], label="Go to Jobs", icon="📋")
        return

    cols = st.columns(3)
    cols[0].metric("Open jobs", data["open_jobs"])
    cols[1].metric("Candidates awaiting review", data["awaiting_review"])
    cols[2].metric("Shortlisted", data["shortlisted"])

    st.divider()
    st.subheader("Needs your attention")
    if not data["attention"]:
        st.caption("Nothing waiting right now.")
    else:
        for item in data["attention"]:
            row = st.columns([4, 2])
            row[0].markdown(f"**{item['title']}** — {item['note']}")
            row[1].page_link(
                _PAGES[item["page"]],
                label=f"Open {_PAGES[item['page']].title}",
            )


def _render_account_sidebar() -> None:
    current = get_current_user(st.session_state)
    with st.sidebar:
        st.markdown(f"**{current['full_name']}**")
        st.caption(f"Role: {label_for(current['role'])}")
        if st.button("Log out"):
            clear_current_user(st.session_state)
            st.rerun()
        st.divider()


_PAGES: dict[str, st.Page] = {
    "dashboard": st.Page(
        _dashboard_page, title="Dashboard", icon="🏠", default=True
    ),
    "jobs": st.Page(render_jobs_page, title="Jobs", icon="📋", url_path="jobs"),
    "candidates": st.Page(
        render_candidates_page, title="Candidates", icon="👤", url_path="candidates"
    ),
    "interviews": st.Page(
        render_interviews_page, title="Interviews", icon="📝", url_path="interviews"
    ),
}


def main() -> None:
    if not is_authenticated(st.session_state):
        st.navigation([st.Page(_login_page, title="Sign in")], position="hidden").run()
        return

    _render_account_sidebar()
    st.navigation(list(_PAGES.values()), position="sidebar").run()


main()
