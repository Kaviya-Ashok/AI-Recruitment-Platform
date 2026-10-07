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

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.database.models.user import UserRole
from app.pages.candidates_list import render_candidates_list_page
from app.pages.dashboard import render_dashboard_page
from app.pages.interviews import render_interviews_page
from app.pages.jobs import render_jobs_page
from app.pages.users import render_users_page
from app.services.auth_service import authenticate_user
from app.utils.session import (
    clear_current_user,
    get_current_user,
    is_authenticated,
    set_current_user,
)
from app.ui.theme import inject_theme
from app.utils.ui import label_for

st.set_page_config(
    page_title="Recruitment Intelligence Platform",
    page_icon=":material/hub:",
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


def _dashboard_page() -> None:
    render_dashboard_page(_PAGES)


def _candidates_page() -> None:
    render_candidates_list_page(_PAGES)


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
        _dashboard_page, title="Dashboard", icon=":material/dashboard:", default=True
    ),
    "jobs": st.Page(
        render_jobs_page, title="Jobs", icon=":material/work:", url_path="jobs"
    ),
    "candidates": st.Page(
        _candidates_page,
        title="Candidates",
        icon=":material/group:",
        url_path="candidates",
    ),
    "interviews": st.Page(
        render_interviews_page,
        title="Interviews",
        icon=":material/forum:",
        url_path="interviews",
    ),
}


#: The Users page is NOT in ``_PAGES``: it is registered for ADMIN only, so every
#: other role's navigation simply does not contain it (and ``/users`` falls back to
#: the default page for them). The service functions refuse non-admins as well.
_USERS_PAGE = st.Page(
    render_users_page,
    title="Users",
    icon=":material/manage_accounts:",
    url_path="users",
)


def _pages_for_current_user() -> list[st.Page]:
    pages = list(_PAGES.values())
    current = get_current_user(st.session_state) or {}
    if current.get("role") == UserRole.ADMIN.value:
        pages.append(_USERS_PAGE)
    return pages


def main() -> None:
    # Once per run, BEFORE either branch, so the login screen and every
    # authenticated page get the one static stylesheet (app/ui/theme.py).
    inject_theme()
    if not is_authenticated(st.session_state):
        st.navigation([st.Page(_login_page, title="Sign in")], position="hidden").run()
        return

    _render_account_sidebar()
    st.navigation(_pages_for_current_user(), position="sidebar").run()


main()
