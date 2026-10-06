"""Users — create staff accounts, deactivate and reactivate them (ADMIN only).

Registered in ``app/main.py`` for the ADMIN role only, so no other role ever sees
the entry; the page also refuses a non-admin who somehow reaches it, and — what
actually protects the data — every service call in
``app/services/user_admin_service.py`` refuses a non-admin itself.

Native elements only (no HTML). The page shows a user's name, e-mail, role, status
and creation date and nothing else: no password hash ever reaches it.

PASSWORDS
---------
The two password fields live in a form whose widget keys carry a version number. A
successful create bumps the version, so the next run draws a fresh, empty form (and
the old widget keys — the only place the typed password ever sat — are dropped). The
password is never displayed, logged, put in a message or kept anywhere else; on a
validation error the form keeps what was typed so it can be corrected.

Deactivation is reversible and never a delete. It asks for a confirmation tick
(the same pattern the other pages use for a consequential action). An admin sees
their own row as read-only, and the SYSTEM account is never listed.
"""

from __future__ import annotations

import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.database.models.user import UserRole
from app.services.user_admin_service import (
    ASSIGNABLE_ROLES,
    PASSWORD_MIN_CHARS,
    UserAdminError,
    UserView,
    create_user_as_admin,
    deactivate_user,
    list_users,
    reactivate_user,
)
from app.utils.authorization import UnauthorizedError
from app.utils.session import get_current_user
from app.utils.ui import label_for
from app.utils.ui_widgets import confirmed, load_error, page_header, success_toast

_FORM_VERSION_KEY = "users_form_ver"
_PICK_KEY = "users_manage_pick"

_NOT_ADMIN = "Only an administrator can manage users."
_SESSION_INVALID = "Your session looks invalid — please sign out and back in."
_INACTIVE = "Your account is no longer active — please contact an admin."
_DB_ERROR = "Couldn't complete that — please try again."
_UNEXPECTED = "Something went wrong. Please try again."
_CONFIRM_FIRST = (
    "Please confirm that you understand this person will no longer be able to "
    "sign in."
)


def status_text(is_active: bool) -> str:
    """Status as a word, never colour alone. Pure."""
    return "Active" if is_active else "Deactivated"


def account_option_label(user: UserView) -> str:
    """The selector's plain-text label for one account. Pure."""
    return f"{user.full_name} — {label_for(user.role)} — {status_text(user.is_active)}"


# --- actions ------------------------------------------------------------------------


def _form_keys(version: int) -> dict[str, str]:
    return {
        name: f"users_{name}_{version}"
        for name in ("email", "name", "role", "password", "confirm")
    }


def _run_create(
    version: int, acting_user_id, *, email, name, role, password, confirm
) -> None:
    try:
        with session_scope() as db:
            create_user_as_admin(
                db, email=email, full_name=name, role=role, password=password,
                password_confirm=confirm, acting_user_id=acting_user_id,
            )
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except UserAdminError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback (or a password)
        st.error(_UNEXPECTED)
        return

    # A fresh key version draws an empty form next run; drop the typed values now.
    for key in _form_keys(version).values():
        try:
            del st.session_state[key]
        except (KeyError, st.errors.StreamlitAPIException):
            pass
    st.session_state[_FORM_VERSION_KEY] = version + 1
    success_toast("User created.")
    st.rerun()


def _run_change(action, target_id: str, acting_user_id, done_message: str) -> None:
    try:
        with session_scope() as db:
            action(db, target_user_id=target_id, acting_user_id=acting_user_id)
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except UserAdminError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_DB_ERROR)
        return
    except Exception:  # noqa: BLE001
        st.error(_UNEXPECTED)
        return
    success_toast(done_message)
    st.rerun()


# --- sections ------------------------------------------------------------------------


def _render_create_form(acting_user_id) -> None:
    version = int(st.session_state.get(_FORM_VERSION_KEY, 0))
    keys = _form_keys(version)
    with st.container(border=True):
        st.subheader("Create a user")
        with st.form(f"create_user_form_{version}", clear_on_submit=False):
            email = st.text_input("Email", key=keys["email"])
            name = st.text_input("Full name", key=keys["name"])
            role = st.selectbox(
                "Role",
                [r.value for r in ASSIGNABLE_ROLES],
                format_func=label_for,
                key=keys["role"],
            )
            password = st.text_input(
                "Initial password",
                type="password",
                key=keys["password"],
                help=f"At least {PASSWORD_MIN_CHARS} characters.",
            )
            confirm = st.text_input(
                "Confirm password", type="password", key=keys["confirm"]
            )
            submitted = st.form_submit_button("Create user")
            st.caption(
                "The account is active straight away and the person signs in with "
                "this password. Share it with them securely — it is never shown "
                "again."
            )
        if submitted:
            _run_create(
                version, acting_user_id, email=email, name=name, role=role,
                password=password, confirm=confirm,
            )


def _render_table(users: list[UserView]) -> None:
    st.subheader("Accounts")
    if not users:
        st.caption("No accounts yet.")
        return
    st.dataframe(
        [
            {
                "Name": u.full_name,
                "Email": u.email,
                "Role": label_for(u.role),
                "Status": status_text(u.is_active),
                "Created": f"{u.created_at:%Y-%m-%d}",
            }
            for u in users
        ],
        hide_index=True,
        width="stretch",
    )


def _render_manage(users: list[UserView], acting_user_id) -> None:
    with st.container(border=True):
        st.subheader("Manage an account")
        if not users:
            return
        by_id = {str(u.user_id): u for u in users}
        chosen = st.selectbox(
            "Account",
            list(by_id),
            index=None,
            placeholder="Choose an account",
            format_func=lambda key: account_option_label(by_id[key]),
            key=_PICK_KEY,
        )
        if chosen is None:
            st.caption(
                "Deactivating is reversible and never deletes anything: the "
                "person's history stays on record."
            )
            return
        target = by_id[chosen]
        if target.user_id == acting_user_id:
            st.info("This is your own account — you can't deactivate it.")
            return

        if target.is_active:
            st.caption(
                "A deactivated person cannot sign in, and anything they try while "
                "already signed in is refused. You can reactivate them at any time."
            )
            ok = confirmed(
                "I understand this person will no longer be able to sign in.",
                key=f"users_ok_{chosen}",
            )
            if st.button("Deactivate account", key=f"users_deactivate_{chosen}"):
                if not ok:
                    st.error(_CONFIRM_FIRST)
                else:
                    _run_change(
                        deactivate_user, chosen, acting_user_id, "Account deactivated."
                    )
        else:
            st.caption(
                "This account is deactivated. Reactivating lets the person sign in "
                "again with their existing password."
            )
            if st.button("Reactivate account", key=f"users_reactivate_{chosen}"):
                _run_change(
                    reactivate_user, chosen, acting_user_id, "Account reactivated."
                )


def render_users_page() -> None:
    current = get_current_user(st.session_state)
    if current is None:  # defensive: the gate is in main.py
        st.error("Please sign in.")
        return
    try:
        acting_user_id = uuid.UUID(current["id"])
    except (ValueError, KeyError, TypeError):
        st.error(_SESSION_INVALID)
        return

    page_header(
        "Users",
        "Create staff accounts and deactivate them when someone leaves. Accounts "
        "are never deleted.",
    )

    if current.get("role") != UserRole.ADMIN.value:   # defence in depth
        st.error(_NOT_ADMIN)
        return

    _render_create_form(acting_user_id)
    st.divider()

    try:
        with session_scope() as db:
            users = list_users(db, acting_user_id=acting_user_id)
    except UnauthorizedError:
        st.error(_INACTIVE)
        return
    except UserAdminError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        load_error("Couldn't load the accounts right now.")
        return

    _render_table(users)
    _render_manage(users, acting_user_id)
