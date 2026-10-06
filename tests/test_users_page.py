"""The Users page (admin user management), through Streamlit's AppTest.

Services are stubbed (no database): the page's job is to call them with exactly what
was typed, show their messages calmly, clear the password fields after a success, and
never display a password. The service rules themselves are in
tests/test_user_admin_service.py. Navigation hiding is tested by running the real
``app.main`` and recording what ``st.navigation`` was given per role.

AppTest scripts rebind real page-module attributes process-wide, so the pristine
values are snapshotted at import time and restored around every test.
"""

from __future__ import annotations

import ast
import sys
import uuid
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.jobs as J
import app.pages.users as U

_TIMEOUT = 60
_ME = "11111111-1111-1111-1111-111111111111"
_OTHER = "22222222-2222-2222-2222-222222222222"
_GONE = "33333333-3333-3333-3333-333333333333"
_PW = "Zq9-page-SENTINEL-4417-pw"

_U_ATTRS = (
    "session_scope", "list_users", "create_user_as_admin", "deactivate_user",
    "reactivate_user",
)
_PRISTINE_U = {n: getattr(U, n) for n in _U_ATTRS}
_PRISTINE_RENDER = J.render_jobs_page


@pytest.fixture(autouse=True)
def _restore():
    for n, v in _PRISTINE_U.items():
        setattr(U, n, v)
    J.render_jobs_page = _PRISTINE_RENDER
    sys.modules.pop("app.main", None)
    try:
        yield
    finally:
        for n, v in _PRISTINE_U.items():
            setattr(U, n, v)
        J.render_jobs_page = _PRISTINE_RENDER
        sys.modules.pop("app.main", None)


_SCRIPT = '''
import contextlib
import datetime
import uuid

import streamlit as st

import app.pages.users as U
from app.services.user_admin_service import (
    UserAdminConflictError, UserAdminTargetError, UserAdminValidationError, UserView,
)
from app.utils.authorization import UnauthorizedError
from app.utils.session import SESSION_USER_KEY
from sqlalchemy.exc import SQLAlchemyError

st.session_state.setdefault("CALLS", [])
CALLS = st.session_state["CALLS"]
RAISE = __RAISE__            # None | exception source evaluated at call time
WHEN = datetime.datetime(2026, 10, 6, 9, 30, tzinfo=datetime.timezone.utc)


def view(i, name, email, role, active):
    return UserView(uuid.UUID(i), name, email, role, active, WHEN)


USERS = [
    view("__ME__", "Ada Admin", "ada@example.test", "ADMIN", True),
    view("__OTHER__", "Hal HR", "hal@example.test", "HR", True),
    view("__GONE__", "Gus Gone", "gus@example.test", "HIRING_MANAGER", False),
]


@contextlib.contextmanager
def _scope():
    yield "DB"


def _maybe_raise():
    if RAISE:
        raise eval(RAISE)


def _list(db, *, acting_user_id):
    CALLS.append(("list", acting_user_id))
    return list(USERS)


def _create(db, **kw):
    CALLS.append(("create", kw))
    _maybe_raise()
    return USERS[1]


def _deactivate(db, *, target_user_id, acting_user_id):
    CALLS.append(("deactivate", target_user_id, acting_user_id))
    _maybe_raise()
    return USERS[1]


def _reactivate(db, *, target_user_id, acting_user_id):
    CALLS.append(("reactivate", target_user_id, acting_user_id))
    _maybe_raise()
    return USERS[2]


U.session_scope = _scope
U.list_users = _list
U.create_user_as_admin = _create
U.deactivate_user = _deactivate
U.reactivate_user = _reactivate

st.session_state[SESSION_USER_KEY] = {
    "id": "__SESSION_ID__", "email": "ada@example.test", "full_name": "Ada Admin",
    "role": "__ROLE__",
}
U.render_users_page()
'''


def _app(role: str = "ADMIN", raise_: str | None = None) -> AppTest:
    script = (
        _SCRIPT.replace("__ROLE__", role).replace("__RAISE__", repr(raise_))
        .replace("__SESSION_ID__", _ME).replace("__ME__", _ME).replace("__OTHER__", _OTHER).replace("__GONE__", _GONE)
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _calls(at):
    return at.session_state["CALLS"]


def _text(at) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [e.value for e in at.error] + [i.value for i in at.info]
    parts += [w.value for w in at.warning] + [t.value for t in at.title]
    parts += [t.value for t in at.toast] + [s.value for s in at.subheader]
    return "\n".join(str(p) for p in parts)


def _input(at, label):
    return next(t for t in at.text_input if t.label == label)


def _fill(at, *, email="new.person@example.test", name="New Person", role="Hiring manager",
          password=_PW, confirm=_PW):
    _input(at, "Email").set_value(email)
    _input(at, "Full name").set_value(name)
    at.selectbox[0].set_value(next(
        k for k, v in {"HR": "HR", "HIRING_MANAGER": "Hiring manager", "ADMIN": "Admin"}.items()
        if v == role))
    _input(at, "Initial password").set_value(password)
    _input(at, "Confirm password").set_value(confirm)
    return at


def _submit(at):
    return next(b for b in at.button if b.label == "Create user")


# --- gating on the page ---------------------------------------------------------------------


@pytest.mark.parametrize("role", ["HR", "HIRING_MANAGER", "SYSTEM", "", "SOMETHING"])
def test_a_non_admin_who_reaches_the_page_sees_a_refusal_and_no_form(role):
    at = _app(role)
    assert "Only an administrator can manage users." in _text(at)
    assert not at.text_input and not at.selectbox and not at.dataframe
    assert _calls(at) == []                                    # no service call at all


def test_an_admin_sees_the_form_the_accounts_and_the_manager():
    at = _app("ADMIN")
    assert [t.value for t in at.title] == ["Users"]
    assert [t.label for t in at.text_input] == [
        "Email", "Full name", "Initial password", "Confirm password",
    ]
    password_kind = type(at.text_input[0].proto).Type.PASSWORD
    assert [t.proto.type for t in at.text_input if "password" in t.label.lower()] == [
        password_kind, password_kind]                        # masked, never echoed
    assert [t.proto.type for t in at.text_input if "password" not in t.label.lower()] == [
        type(at.text_input[0].proto).Type.DEFAULT] * 2
    assert list(at.selectbox[0].options) == ["HR", "Hiring manager", "Admin"]
    assert _submit(at).label == "Create user"
    assert ("list", uuid.UUID(_ME)) in _calls(at)


def test_the_role_choices_never_include_system():
    at = _app("ADMIN")
    assert "Automated pipeline" not in list(at.selectbox[0].options)
    assert not any("system" in str(o).lower() for o in at.selectbox[0].options)


def test_the_table_shows_name_email_role_status_and_created_date_in_words():
    at = _app("ADMIN")
    rows = at.dataframe[0].value
    assert list(rows.columns) == ["Name", "Email", "Role", "Status", "Created"]
    assert rows.to_dict("records") == [
        {"Name": "Ada Admin", "Email": "ada@example.test", "Role": "Admin",
         "Status": "Active", "Created": "2026-10-06"},
        {"Name": "Hal HR", "Email": "hal@example.test", "Role": "HR",
         "Status": "Active", "Created": "2026-10-06"},
        {"Name": "Gus Gone", "Email": "gus@example.test", "Role": "Hiring manager",
         "Status": "Deactivated", "Created": "2026-10-06"},
    ]


def test_an_invalid_session_user_is_told_so():
    script = (
        _SCRIPT.replace("__ROLE__", "ADMIN").replace("__RAISE__", "None")
        .replace("__SESSION_ID__", "not-a-uuid").replace("__ME__", _ME).replace("__OTHER__", _OTHER).replace("__GONE__", _GONE)
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert any("session looks invalid" in e.value for e in at.error)


# --- create ---------------------------------------------------------------------------------------


def test_creating_calls_the_service_once_with_exactly_what_was_typed():
    at = _fill(_app()).run()
    _submit(at).click().run()
    assert not at.exception
    (call,) = [c for c in _calls(at) if c[0] == "create"]
    assert call[1] == {
        "email": "new.person@example.test", "full_name": "New Person",
        "role": "HIRING_MANAGER", "password": _PW, "password_confirm": _PW,
        "acting_user_id": uuid.UUID(_ME),
    }


def test_nothing_is_created_on_load_or_on_widget_changes():
    at = _fill(_app()).run()
    assert not [c for c in _calls(at) if c[0] == "create"]


def test_after_a_success_the_form_is_empty_and_no_state_holds_the_password():
    at = _fill(_app()).run()
    _submit(at).click().run()
    assert at.session_state["users_form_ver"] == 1
    assert [t.value for t in at.text_input] == ["", "", "", ""]
    assert any("User created." in t.value for t in at.toast)
    leaked = [k for k, v in at.session_state.filtered_state.items()
              if isinstance(v, str) and _PW in v]
    assert leaked == []


def test_a_toast_confirms_without_naming_anyone_or_showing_a_password():
    at = _fill(_app()).run()
    _submit(at).click().run()
    for t in at.toast:
        assert "new.person" not in t.value and _PW not in t.value and "New Person" not in t.value


@pytest.mark.parametrize("exc, expected", [
    ('UserAdminValidationError("The password must be at least 12 characters.")',
     "The password must be at least 12 characters."),
    ('UserAdminConflictError("A user with that email already exists.")',
     "A user with that email already exists."),
    ('UnauthorizedError("no")', "no longer active"),
    ('SQLAlchemyError("boom")', "Couldn't complete that"),
])
def test_service_errors_are_shown_calmly_and_the_form_keeps_what_was_typed(exc, expected):
    at = _fill(_app(raise_=exc)).run()
    _submit(at).click().run()
    assert any(expected in e.value for e in at.error)
    assert "users_form_ver" not in at.session_state              # not bumped on failure
    assert _input(at, "Email").value == "new.person@example.test"


def test_an_unexpected_error_never_shows_the_password_even_if_it_is_in_the_exception():
    at = _fill(_app(raise_=f'RuntimeError("boom {_PW} new.person@example.test")')).run()
    _submit(at).click().run()
    assert not at.exception
    assert any("Something went wrong" in e.value for e in at.error)
    shown = _text(at)
    assert _PW not in shown and "new.person@example.test" not in shown.replace(
        "ada@example.test", "")


def test_no_rendered_message_ever_contains_the_password():
    for raise_ in (None, 'UserAdminValidationError("bad")', 'SQLAlchemyError("x")'):
        at = _fill(_app(raise_=raise_)).run()
        _submit(at).click().run()
        assert _PW not in _text(at)


# --- manage: deactivate / reactivate -------------------------------------------------------------------------


def _pick(at, user_id):
    at.selectbox(key="users_manage_pick").set_value(user_id).run()
    assert not at.exception
    return at


def test_the_selector_lists_accounts_in_plain_text_with_status():
    at = _app()
    assert list(at.selectbox(key="users_manage_pick").options) == [
        "Ada Admin — Admin — Active", "Hal HR — HR — Active",
        "Gus Gone — Hiring manager — Deactivated",
    ]


def test_choosing_your_own_account_offers_no_deactivation():
    at = _pick(_app(), _ME)
    assert "This is your own account — you can't deactivate it." in _text(at)
    assert not [b for b in at.button if "activate" in b.label.lower()]


def test_deactivating_needs_the_confirmation_tick():
    at = _pick(_app(), _OTHER)
    next(b for b in at.button if b.label == "Deactivate account").click().run()
    assert any("Please confirm that you understand" in e.value for e in at.error)
    assert not [c for c in _calls(at) if c[0] == "deactivate"]


def test_a_confirmed_deactivation_calls_the_service_for_that_account():
    at = _pick(_app(), _OTHER)
    next(c for c in at.checkbox if "no longer be able to sign in" in c.label).check().run()
    next(b for b in at.button if b.label == "Deactivate account").click().run()
    assert not at.exception
    (call,) = [c for c in _calls(at) if c[0] == "deactivate"]
    assert call == ("deactivate", _OTHER, uuid.UUID(_ME))
    assert any("Account deactivated." in t.value for t in at.toast)


def test_a_deactivated_account_offers_reactivation_without_a_confirmation():
    at = _pick(_app(), _GONE)
    assert not at.checkbox
    next(b for b in at.button if b.label == "Reactivate account").click().run()
    (call,) = [c for c in _calls(at) if c[0] == "reactivate"]
    assert call == ("reactivate", _GONE, uuid.UUID(_ME))
    assert any("Account reactivated." in t.value for t in at.toast)


@pytest.mark.parametrize("exc, expected", [
    ('UserAdminTargetError("You can\'t deactivate your own account.")', "own account"),
    ('UserAdminTargetError("This is the last active administrator.")', "last active administrator"),
    ('UnauthorizedError("no")', "no longer active"),
    ('SQLAlchemyError("boom")', "Couldn't complete that"),
])
def test_deactivation_errors_are_shown_calmly(exc, expected):
    at = _pick(_app(raise_=exc), _OTHER)
    next(c for c in at.checkbox if "no longer be able" in c.label).check().run()
    next(b for b in at.button if b.label == "Deactivate account").click().run()
    assert not at.exception
    assert any(expected in e.value for e in at.error)


def test_there_is_no_delete_anywhere_on_the_page():
    at = _app()
    for choice in (_ME, _OTHER, _GONE):
        at = _pick(at, choice)
        labels = " ".join(b.label.lower() for b in at.button)
        assert "delete" not in labels and "remove" not in labels


def test_the_page_makes_no_ai_call_and_no_html():
    source = Path(U.__file__).read_text(encoding="utf-8")
    assert "unsafe_allow_html" not in source and "<div" not in source and "<style" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] + [a.name for a in node.names])
            for n in names:
                assert not n.startswith("app.ai") and "anthropic" not in n.lower(), n
                assert n != "app.services.auth_service", n           # never hashes here


def test_the_page_never_imports_a_password_hash_helper():
    source = Path(U.__file__).read_text(encoding="utf-8")
    assert "hash_password" not in source and "hashed_password" not in source


# --- navigation: the page exists for ADMIN only ---------------------------------------------------------------------

_NAV_SCRIPT = '''
import sys
import streamlit as st
import app.pages.users as U
from app.utils.session import SESSION_USER_KEY

U.render_users_page = lambda: st.write("USERS PAGE")
_orig = st.navigation

def _recording(pages, **kw):
    st.session_state["_nav"] = [(p.title, p.url_path) for p in pages]
    return _orig(pages, **kw)

st.navigation = _recording
sys.modules.pop("app.main", None)
st.session_state[SESSION_USER_KEY] = {
    "id": "%s", "email": "p@x.test", "full_name": "P", "role": "__ROLE__",
}
import app.main  # noqa: F401  (runs main())
''' % uuid.uuid4()


def _nav(role: str) -> list[tuple[str, str]]:
    at = AppTest.from_string(_NAV_SCRIPT.replace("__ROLE__", role), default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at.session_state["_nav"]


def test_only_an_admin_has_the_users_page_in_the_navigation():
    admin = _nav("ADMIN")
    assert ("Users", "users") in admin
    assert [t for t, _ in admin if t != "Users"] == ["Dashboard", "Jobs", "Candidates", "Interviews"]
    for role in ("HR", "HIRING_MANAGER", "SYSTEM", "", "SOMETHING"):
        assert "Users" not in [t for t, _ in _nav(role)], role


_LOGGED_OUT_SCRIPT = '''
import sys
import streamlit as st

_orig = st.navigation


def _recording(pages, **kw):
    st.session_state["_nav"] = [(p.title, p.url_path) for p in pages]
    return _orig(pages, **kw)


st.navigation = _recording
sys.modules.pop("app.main", None)
import app.main  # noqa: F401  (runs main() with nobody signed in)
'''


def test_the_logged_out_navigation_has_only_the_sign_in_page():
    at = AppTest.from_string(_LOGGED_OUT_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert [t for t, _ in at.session_state["_nav"]] == ["Sign in"]


def test_the_users_page_is_not_in_the_shared_page_table():
    """``_PAGES`` feeds the Dashboard's links and everyone's navigation; the Users
    page is added separately, for ADMIN only (``_pages_for_current_user``)."""
    tree = ast.parse((Path(U.__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    table = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_PAGES"
    )
    keys = [k.value for k in table.value.keys]
    assert keys == ["dashboard", "jobs", "candidates", "interviews"]


# --- the public candidate app can never reach user administration -----------------------------------------------------


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def test_the_public_candidate_app_never_imports_user_administration():
    root = Path(__file__).resolve().parents[1]
    seen: set[str] = set()
    todo = ["app.public_main"]
    while todo:
        mod = todo.pop()
        if mod in seen or not mod.startswith("app"):
            continue
        seen.add(mod)
        path = root / (mod.replace(".", "/") + ".py")
        if not path.exists():
            path = root / mod.replace(".", "/") / "__init__.py"
        if path.exists():
            todo.extend(n for n in _imports(path) if n.startswith("app"))
    assert not seen & {"app.pages.users", "app.services.user_admin_service"}, seen
