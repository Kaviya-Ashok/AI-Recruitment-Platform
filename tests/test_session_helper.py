"""Tests for app.utils.session — the non-UI auth/session-state logic.

The Streamlit UI in app/main.py is not unit-tested: it is thin glue over
``authenticate_user`` (covered in test_auth_service.py) and these helpers, and
driving Streamlit widgets in-process adds no real signal. The extracted helpers
below hold the only logic worth testing in isolation, and they accept a plain
mapping so a ``dict`` stands in for ``st.session_state``.
"""

from __future__ import annotations

import uuid

from app.database.models.user import User, UserRole
from app.utils.session import (
    SESSION_USER_KEY,
    build_session_user,
    clear_current_user,
    get_current_user,
    is_authenticated,
    set_current_user,
)


def _make_user() -> User:
    return User(
        id=uuid.uuid4(),
        email="Person@Example.Test",
        hashed_password="$2b$12$not-a-real-hash-value-000000000000000000000000",
        full_name="Test Person",
        role=UserRole.ADMIN,
    )


def test_build_session_user_projects_minimal_primitives():
    user = _make_user()

    data = build_session_user(user)

    assert data == {
        "id": str(user.id),
        "email": user.email,
        "full_name": "Test Person",
        "role": "ADMIN",
    }
    assert isinstance(data["id"], str)
    # Nothing sensitive leaks into session state.
    assert "hashed_password" not in data
    assert "password" not in data


def test_set_get_clear_roundtrip():
    state: dict = {}
    user = _make_user()

    assert is_authenticated(state) is False
    assert get_current_user(state) is None

    set_current_user(state, user)

    assert is_authenticated(state) is True
    assert get_current_user(state)["email"] == user.email
    assert state[SESSION_USER_KEY]["role"] == "ADMIN"

    clear_current_user(state)

    assert is_authenticated(state) is False
    assert get_current_user(state) is None


def test_clear_is_idempotent():
    state: dict = {}
    clear_current_user(state)  # nothing to clear
    clear_current_user(state)
    assert state == {}


def test_is_authenticated_false_for_empty_value():
    assert is_authenticated({SESSION_USER_KEY: None}) is False
    assert is_authenticated({SESSION_USER_KEY: {}}) is False
