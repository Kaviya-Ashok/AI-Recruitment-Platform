"""Quick find — one text box, up to eight matches, one click to open (HR UI,
Increment 4). Used on the Dashboard and on the Candidates page.

Submitting the form searches candidates (name or e-mail) and jobs (code or title);
there is no typeahead, so nothing is queried while typing. Fewer than two characters
is not searched — the box says so instead. The query is plain text: the service
binds it as a parameter and matches LIKE wildcards literally.

The submitted query is kept in ``st.session_state`` (UI state only) so the result
buttons are still drawn on the rerun that handles a click on one of them; a button
that vanished with the form's one-shot "submitted" flag would lose the click.
"""

from __future__ import annotations

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.services.overview_service import quick_find
from app.utils.authorization import UnauthorizedError
from app.utils.overview_helpers import (
    QUICK_FIND_MIN_CHARS,
    build_link,
    normalise_filter_text,
    normalise_query,
)
from app.utils.ui_widgets import go_to_jobs, load_error

_INACTIVE = "Your account is no longer active — please contact an admin."


def _state_key(scope: str) -> str:
    return f"quick_find_{scope}_query"


def _open(pages, match) -> None:
    if match.kind == "candidate":
        go_to_jobs(pages, build_link(match.job_id, candidate=match.application_id))
    else:
        go_to_jobs(pages, build_link(match.job_id))


def render_quick_find(pages, acting_user_id, *, scope: str) -> None:
    key = _state_key(scope)
    with st.container(border=True):
        st.markdown("**Quick find**")
        with st.form(f"quick_find_form_{scope}", border=False):
            cols = st.columns([5, 1], vertical_alignment="bottom")
            query = cols[0].text_input(
                "Quick find",
                key=f"quick_find_input_{scope}",
                placeholder="Candidate name or email, job code or title",
                label_visibility="collapsed",
            )
            submitted = cols[1].form_submit_button("Find", width="stretch")
        if submitted:
            st.session_state[key] = normalise_filter_text(query)

        stored = st.session_state.get(key)
        if not stored:
            return
        if normalise_query(stored) is None:
            st.caption(f"Type at least {QUICK_FIND_MIN_CHARS} characters to search.")
            return

        try:
            with session_scope() as db:
                matches = quick_find(db, stored, acting_user_id=acting_user_id)
        except UnauthorizedError:
            st.error(_INACTIVE)
            return
        except SQLAlchemyError:
            load_error("Couldn't search right now.")
            return

        if not matches:
            st.caption("No matches.")
        for index, match in enumerate(matches):
            if st.button(
                f"{match.label} — {match.detail}",
                key=f"quick_find_{scope}_{index}",
                icon=":material/person:" if match.kind == "candidate" else ":material/work:",
                type="tertiary",
            ):
                _open(pages, match)
        if st.button("Clear", key=f"quick_find_clear_{scope}", type="tertiary"):
            st.session_state.pop(key, None)
            st.rerun()
