"""Streamlit session-state helpers for authentication.

Why a plain dict and not the ``User`` ORM object
------------------------------------------------
CLAUDE.md §16: ``st.session_state`` is temporary UI state only. We store a small
dict of primitives describing who is logged in — **not** the SQLAlchemy
``User`` instance — because across Streamlit reruns that instance would be a
detached ORM object (lazy attribute access can raise), it would outlive its
session, and it carries fields the UI must never hold (``hashed_password``).

What we store: ``id`` (str), ``email``, ``full_name``, ``role`` (str). That is
enough to render the dashboard and attribute actions. Any later feature that
needs the live row re-queries the database by ``id``.

These functions take the state mapping as an argument (rather than importing
``st`` directly) so the logic is unit-testable with a plain ``dict``.
"""

from __future__ import annotations

from typing import Any, MutableMapping, Optional, TypedDict

from app.database.models.user import User

SESSION_USER_KEY = "auth_user"


class SessionUser(TypedDict):
    id: str
    email: str
    full_name: str
    role: str


def build_session_user(user: User) -> SessionUser:
    """Project a ``User`` row down to the minimal dict kept in session state."""
    role = getattr(user.role, "value", user.role)
    return {
        "id": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": str(role),
    }


def set_current_user(state: MutableMapping[str, Any], user: User) -> None:
    state[SESSION_USER_KEY] = build_session_user(user)


def get_current_user(
    state: MutableMapping[str, Any],
) -> Optional[SessionUser]:
    return state.get(SESSION_USER_KEY)


def is_authenticated(state: MutableMapping[str, Any]) -> bool:
    return bool(state.get(SESSION_USER_KEY))


def clear_current_user(state: MutableMapping[str, Any]) -> None:
    """Remove the logged-in user from session state (idempotent)."""
    state.pop(SESSION_USER_KEY, None)
