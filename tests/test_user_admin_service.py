"""Tests for app.services.user_admin_service (admin user management).

Real Postgres via the savepoint-rollback ``db`` fixture. The service is ADMIN ONLY:
every function is refused for HR, HIRING_MANAGER and SYSTEM callers; create /
deactivate / reactivate each write exactly one structural audit event in the same
transaction; and the password, e-mail and full name never reach an audit field, a
log line, a view, an exception message or a return value.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

import app.services.user_admin_service as uas
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.user import SYSTEM_USER_EMAIL, SYSTEM_USER_ID, User, UserRole
from app.services.auth_service import authenticate_user, create_user
from app.services.user_admin_service import (
    ASSIGNABLE_ROLES,
    PASSWORD_MIN_CHARS,
    UserAdminConflictError,
    UserAdminPermissionError,
    UserAdminTargetError,
    UserAdminValidationError,
    UserView,
    check_not_last_admin,
    create_user_as_admin,
    deactivate_user,
    list_users,
    reactivate_user,
)
from app.utils.authorization import UnauthorizedError, require_internal_user

_SERVICE = Path(__file__).resolve().parents[1] / "app" / "services" / "user_admin_service.py"
_PW = "A-long-enough-passphrase-1"


def _mk(db, role=UserRole.ADMIN, name="Test Person", email=None):
    return create_user(
        db=db, email=email or f"u-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only-123", full_name=name, role=role,
    )


def _create(db, admin, **kw):
    args = dict(
        email=f"new-{uuid.uuid4().hex}@x.test", full_name="New Person",
        role="HR", password=_PW, password_confirm=_PW, acting_user_id=admin.id,
    )
    args.update(kw)
    return create_user_as_admin(db, **args)


def _count(db, model=User):
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def _events(db, event_type):
    return db.execute(
        select(AuditEvent).where(AuditEvent.event_type == event_type.value)
    ).scalars().all()


@pytest.fixture
def admin(db):
    return _mk(db, UserRole.ADMIN, name="Ada Admin")


# =========================================================== gating ================

_CALLS = {
    "list": lambda db, who, target: list_users(db, acting_user_id=who),
    "create": lambda db, who, target: create_user_as_admin(
        db, email=f"n-{uuid.uuid4().hex}@x.test", full_name="N", role="HR",
        password=_PW, password_confirm=_PW, acting_user_id=who),
    "deactivate": lambda db, who, target: deactivate_user(
        db, target_user_id=target, acting_user_id=who),
    "reactivate": lambda db, who, target: reactivate_user(
        db, target_user_id=target, acting_user_id=who),
}


@pytest.mark.parametrize("call", sorted(_CALLS))
@pytest.mark.parametrize("role", [UserRole.HR, UserRole.HIRING_MANAGER])
def test_hr_and_hiring_manager_are_refused_with_a_specific_message(db, admin, call, role):
    caller = _mk(db, role)
    target = _mk(db, UserRole.HR)
    before_users, before_events = _count(db), _count(db, AuditEvent)
    with pytest.raises(UserAdminPermissionError) as exc:
        _CALLS[call](db, caller.id, target.id)
    assert str(exc.value) == "Only an administrator can manage users."
    assert (_count(db), _count(db, AuditEvent)) == (before_users, before_events)


@pytest.mark.parametrize("call", sorted(_CALLS))
def test_the_system_user_is_refused_as_an_actor(db, admin, call):
    target = _mk(db, UserRole.HR)
    with pytest.raises(UserAdminPermissionError) as exc:
        _CALLS[call](db, SYSTEM_USER_ID, target.id)
    assert "automated pipeline" in str(exc.value)


@pytest.mark.parametrize("call", sorted(_CALLS))
@pytest.mark.parametrize("who", [None, "not-a-uuid", "unknown"])
def test_missing_malformed_or_unknown_callers_are_unauthorized(db, admin, call, who):
    acting = uuid.uuid4() if who == "unknown" else who
    with pytest.raises(UnauthorizedError):
        _CALLS[call](db, acting, admin.id)


@pytest.mark.parametrize("call", sorted(_CALLS))
def test_an_inactive_admin_is_unauthorized(db, admin, call):
    admin.is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _CALLS[call](db, admin.id, admin.id)


def test_an_admin_is_allowed(db, admin):
    other = _mk(db, UserRole.HR)
    assert isinstance(list_users(db, acting_user_id=admin.id), list)
    assert _create(db, admin).is_active is True
    assert deactivate_user(db, target_user_id=other.id, acting_user_id=admin.id).is_active is False
    assert reactivate_user(db, target_user_id=other.id, acting_user_id=admin.id).is_active is True


def test_the_guard_runs_before_any_validation(db):
    """An unauthorised caller learns nothing about the inputs' validity."""
    caller = _mk(db, UserRole.HR)
    with pytest.raises(UserAdminPermissionError):
        create_user_as_admin(
            db, email="not an email", full_name="", role="SYSTEM", password="x",
            password_confirm="y", acting_user_id=caller.id,
        )


# =========================================================== list ================


def test_list_users_hides_the_system_user_and_never_carries_a_hash(db, admin):
    _mk(db, UserRole.HR, name="Hal HR")
    views = list_users(db, acting_user_id=admin.id)
    assert SYSTEM_USER_ID not in {v.user_id for v in views}
    assert SYSTEM_USER_EMAIL not in {v.email for v in views}
    assert all(v.role != "SYSTEM" for v in views)
    assert all(isinstance(v, UserView) for v in views)
    names = {f.name for f in dataclasses.fields(UserView)}
    assert names == {"user_id", "full_name", "email", "role", "is_active", "created_at"}
    assert not any("password" in n or "hash" in n for n in names)


def test_list_users_includes_inactive_accounts_and_is_frozen(db, admin):
    gone = _mk(db, UserRole.HR)
    deactivate_user(db, target_user_id=gone.id, acting_user_id=admin.id)
    by_id = {v.user_id: v for v in list_users(db, acting_user_id=admin.id)}
    assert by_id[gone.id].is_active is False and by_id[admin.id].is_active is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        by_id[gone.id].is_active = True                       # type: ignore[misc]


def test_list_users_is_read_only(db, admin):
    before = (_count(db), _count(db, AuditEvent))
    list_users(db, acting_user_id=admin.id)
    assert (_count(db), _count(db, AuditEvent)) == before


# =========================================================== create ================


@pytest.mark.parametrize("role", [r.value for r in ASSIGNABLE_ROLES])
def test_each_assignable_role_can_be_created_active(db, admin, role):
    view = _create(db, admin, role=role)
    assert view.role == role and view.is_active is True
    row = db.get(User, view.user_id)
    assert row.role is UserRole(role) and row.is_active is True


def test_role_may_be_given_as_the_enum(db, admin):
    assert _create(db, admin, role=UserRole.HIRING_MANAGER).role == "HIRING_MANAGER"


@pytest.mark.parametrize("bad", ["SYSTEM", UserRole.SYSTEM, "ROOT", "", None, "hr", "Admin ", 7])
def test_system_and_any_other_role_is_refused(db, admin, bad):
    before = _count(db)
    with pytest.raises(UserAdminValidationError) as exc:
        _create(db, admin, role=bad)
    assert str(exc.value) == "Choose HR, Hiring manager or Admin."
    assert _count(db) == before


def test_the_assignable_roles_are_exactly_hr_hiring_manager_admin():
    assert [r.value for r in ASSIGNABLE_ROLES] == ["HR", "HIRING_MANAGER", "ADMIN"]


def test_email_is_trimmed_and_lowercased_and_the_name_trimmed(db, admin):
    view = _create(db, admin, email="  Mixed.Case@X.Test ", full_name="  Mia  Manager ")
    assert view.email == "mixed.case@x.test" and view.full_name == "Mia  Manager"


@pytest.mark.parametrize("bad", ["", "   ", "plain", "a@b", "a b@x.test", "@x.test", "a@@x.test", None, 5])
def test_a_malformed_email_is_refused(db, admin, bad):
    with pytest.raises(UserAdminValidationError) as exc:
        _create(db, admin, email=bad)
    assert str(exc.value) == "Enter a valid email address."


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_empty_name_is_refused(db, admin, bad):
    with pytest.raises(UserAdminValidationError):
        _create(db, admin, full_name=bad)


def test_a_too_long_name_is_refused(db, admin):
    with pytest.raises(UserAdminValidationError):
        _create(db, admin, full_name="x" * 256)


def test_a_duplicate_email_is_refused_case_insensitively_without_echo(db, admin):
    _create(db, admin, email="dup@x.test")
    with pytest.raises(UserAdminConflictError) as exc:
        _create(db, admin, email="  DUP@x.test ")
    assert str(exc.value) == "A user with that email already exists."
    assert "dup" not in str(exc.value).lower()


def test_the_system_address_cannot_be_taken(db, admin):
    with pytest.raises(UserAdminConflictError):
        _create(db, admin, email=SYSTEM_USER_EMAIL.upper())


@pytest.mark.parametrize("weak", ["", "short", "x" * (PASSWORD_MIN_CHARS - 1), "12345678901"])
def test_a_weak_password_is_refused_and_nothing_is_created(db, admin, weak):
    before, events = _count(db), _count(db, AuditEvent)
    with pytest.raises(UserAdminValidationError) as exc:
        _create(db, admin, password=weak, password_confirm=weak)
    assert str(exc.value) == f"The password must be at least {PASSWORD_MIN_CHARS} characters."
    assert (_count(db), _count(db, AuditEvent)) == (before, events)


def test_the_minimum_length_is_accepted(db, admin):
    pw = "x" * PASSWORD_MIN_CHARS
    assert _create(db, admin, password=pw, password_confirm=pw).is_active


def test_a_password_longer_than_bcrypt_can_use_is_refused(db, admin):
    pw = "x" * 73
    with pytest.raises(UserAdminValidationError) as exc:
        _create(db, admin, password=pw, password_confirm=pw)
    assert "too long" in str(exc.value)
    assert pw not in str(exc.value)


def test_a_mismatched_confirmation_is_refused(db, admin):
    with pytest.raises(UserAdminValidationError) as exc:
        _create(db, admin, password=_PW, password_confirm=_PW + "x")
    assert str(exc.value) == "The two passwords do not match."


def test_the_new_user_can_sign_in_with_the_chosen_password_only(db, admin):
    view = _create(db, admin, email="signin@x.test", password=_PW, password_confirm=_PW)
    signed_in = authenticate_user(db, "signin@x.test", _PW)
    assert signed_in is not None and signed_in.id == view.user_id
    assert authenticate_user(db, "SIGNIN@x.test ", _PW) is not None      # same rule as login
    assert authenticate_user(db, "signin@x.test", _PW + "x") is None
    assert authenticate_user(db, "signin@x.test", "wrong-password-123") is None


def test_the_password_is_stored_only_as_a_bcrypt_hash(db, admin):
    view = _create(db, admin)
    stored = db.get(User, view.user_id).hashed_password
    assert stored.startswith("$2") and _PW not in stored


def test_the_created_account_is_a_normal_internal_user(db, admin):
    view = _create(db, admin, role="HIRING_MANAGER")
    assert require_internal_user(db, view.user_id).role is UserRole.HIRING_MANAGER


def test_a_failed_create_leaves_no_partial_state(db, admin):
    before = (_count(db), _count(db, AuditEvent))
    for kwargs in (dict(role="SYSTEM"), dict(password="short", password_confirm="short"),
                   dict(email="bad")):
        with pytest.raises(UserAdminValidationError):
            _create(db, admin, **kwargs)
    assert (_count(db), _count(db, AuditEvent)) == before


# =========================================================== deactivate / reactivate ================


def test_a_deactivated_user_cannot_sign_in_and_is_refused_by_the_guard(db, admin):
    target = _mk(db, UserRole.HR, email="leaver@x.test")
    assert authenticate_user(db, "leaver@x.test", "pw-for-tests-only-123") is not None
    deactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    assert authenticate_user(db, "leaver@x.test", "pw-for-tests-only-123") is None
    with pytest.raises(UnauthorizedError):
        require_internal_user(db, target.id)


def test_reactivation_restores_sign_in_with_the_existing_password(db, admin):
    target = _mk(db, UserRole.HR, email="back@x.test")
    deactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    reactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    assert authenticate_user(db, "back@x.test", "pw-for-tests-only-123") is not None


def test_deactivation_changes_only_the_active_flag(db, admin):
    target = _mk(db, UserRole.HIRING_MANAGER, name="Keep Me")
    before = (target.email, target.full_name, target.role, target.hashed_password)
    deactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    db.refresh(target)
    assert (target.email, target.full_name, target.role, target.hashed_password) == before
    assert target.is_active is False


def test_nothing_is_ever_deleted(db, admin):
    target = _mk(db, UserRole.HR)
    before = _count(db)
    deactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    assert _count(db) == before and db.get(User, target.id) is not None


def test_repeating_a_change_is_a_calm_no_op_with_no_second_event(db, admin):
    target = _mk(db, UserRole.HR)
    reactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)       # already active
    assert _events(db, AuditEventType.USER_REACTIVATED) == []
    deactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    again = deactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    assert again.is_active is False
    assert len([e for e in _events(db, AuditEventType.USER_DEACTIVATED)
                if e.entity_id == target.id]) == 1


# --- protections --------------------------------------------------------------------------


def test_an_admin_cannot_deactivate_their_own_account(db, admin):
    _mk(db, UserRole.ADMIN)                                   # another admin exists
    with pytest.raises(UserAdminTargetError) as exc:
        deactivate_user(db, target_user_id=admin.id, acting_user_id=admin.id)
    assert str(exc.value) == "You can't deactivate your own account."
    db.refresh(admin)
    assert admin.is_active is True


def test_the_system_user_cannot_be_targeted(db, admin):
    for call in (deactivate_user, reactivate_user):
        with pytest.raises(UserAdminTargetError) as exc:
            call(db, target_user_id=SYSTEM_USER_ID, acting_user_id=admin.id)
        assert "automated pipeline" in str(exc.value)
    assert db.get(User, SYSTEM_USER_ID).is_active is True


@pytest.mark.parametrize("bad", [uuid.uuid4(), "not-a-uuid", "", None])
def test_an_unknown_or_malformed_target_is_a_calm_error(db, admin, bad):
    for call in (deactivate_user, reactivate_user):
        with pytest.raises(UserAdminTargetError) as exc:
            call(db, target_user_id=bad, acting_user_id=admin.id)
        assert str(exc.value) == "No such user."


def test_check_not_last_admin_is_pure_and_exact(db):
    a, b, hr = (_mk(db, UserRole.ADMIN), _mk(db, UserRole.ADMIN), _mk(db, UserRole.HR))
    with pytest.raises(UserAdminTargetError) as exc:
        check_not_last_admin([a], a)                          # only active admin: refused
    assert "last active administrator" in str(exc.value)
    with pytest.raises(UserAdminTargetError):
        check_not_last_admin([], a)
    check_not_last_admin([a, b], a)                           # another admin exists
    check_not_last_admin([a], hr)                             # a non-admin target is fine


def test_an_admin_may_deactivate_another_admin_while_two_are_active(db, admin):
    other = _mk(db, UserRole.ADMIN)
    assert deactivate_user(db, target_user_id=other.id, acting_user_id=admin.id).is_active is False


def test_the_last_active_admin_is_protected_against_a_stale_actor(db, mocker):
    """The race the lock exists for: the actor passed the guard, then stopped being
    an active admin before the deactivation ran. The locked re-check refuses it, so
    the platform can never be left with no active administrator."""
    actor, target = _mk(db, UserRole.ADMIN), _mk(db, UserRole.ADMIN)
    mocker.patch.object(uas, "require_internal_user", return_value=actor)
    actor.is_active = False
    db.flush()
    with pytest.raises(UserAdminPermissionError):
        deactivate_user(db, target_user_id=target.id, acting_user_id=actor.id)
    db.refresh(target)
    assert target.is_active is True


def test_two_admins_cannot_deactivate_each_other_into_an_empty_admin_set(db):
    a, b = _mk(db, UserRole.ADMIN), _mk(db, UserRole.ADMIN)
    deactivate_user(db, target_user_id=b.id, acting_user_id=a.id)
    with pytest.raises(UnauthorizedError):                    # b is no longer active
        deactivate_user(db, target_user_id=a.id, acting_user_id=b.id)
    db.refresh(a)
    assert a.is_active is True


# =========================================================== audit ================

_ALLOWED_META = {"acting_user_id", "target_user_id", "role"}


def test_create_writes_one_structural_event_attributed_to_the_acting_admin(db, admin):
    view = _create(db, admin, role="HIRING_MANAGER")
    events = [e for e in _events(db, AuditEventType.USER_CREATED) if e.entity_id == view.user_id]
    assert len(events) == 1
    e = events[0]
    assert e.user_id == admin.id                              # the admin, not the new user
    assert (e.entity_type, e.entity_id) == ("user", view.user_id)
    assert e.event_metadata == {
        "acting_user_id": str(admin.id), "target_user_id": str(view.user_id),
        "role": "HIRING_MANAGER",
    }
    assert e.previous_state is None and e.new_state == {"is_active": True}
    assert e.action == "User account created by an administrator."


@pytest.mark.parametrize("call, event_type, was, now, text", [
    (deactivate_user, AuditEventType.USER_DEACTIVATED, True, False,
     "User account deactivated by an administrator."),
])
def test_deactivate_writes_one_structural_event(db, admin, call, event_type, was, now, text):
    target = _mk(db, UserRole.HR)
    call(db, target_user_id=target.id, acting_user_id=admin.id)
    (e,) = [x for x in _events(db, event_type) if x.entity_id == target.id]
    assert e.user_id == admin.id and (e.entity_type, e.entity_id) == ("user", target.id)
    assert set(e.event_metadata) == _ALLOWED_META
    assert e.event_metadata == {
        "acting_user_id": str(admin.id), "target_user_id": str(target.id), "role": "HR",
    }
    assert e.previous_state == {"is_active": was} and e.new_state == {"is_active": now}
    assert e.action == text


def test_reactivate_writes_one_structural_event(db, admin):
    target = _mk(db, UserRole.HR)
    deactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    reactivate_user(db, target_user_id=target.id, acting_user_id=admin.id)
    (e,) = [x for x in _events(db, AuditEventType.USER_REACTIVATED) if x.entity_id == target.id]
    assert e.user_id == admin.id
    assert e.previous_state == {"is_active": False} and e.new_state == {"is_active": True}
    assert set(e.event_metadata) == _ALLOWED_META


def test_the_new_event_types_are_plain_strings_in_the_canonical_list():
    assert AuditEventType.USER_DEACTIVATED.value == "USER_DEACTIVATED"
    assert AuditEventType.USER_REACTIVATED.value == "USER_REACTIVATED"
    assert AuditEventType.USER_CREATED.value == "USER_CREATED"


def test_refused_actions_write_no_event(db, admin):
    hr = _mk(db, UserRole.HR)
    before = _count(db, AuditEvent)
    for fn in (
        lambda: deactivate_user(db, target_user_id=admin.id, acting_user_id=admin.id),
        lambda: deactivate_user(db, target_user_id=SYSTEM_USER_ID, acting_user_id=admin.id),
        lambda: _create(db, admin, role="SYSTEM"),
        lambda: deactivate_user(db, target_user_id=admin.id, acting_user_id=hr.id),
    ):
        with pytest.raises(Exception):
            fn()
    assert _count(db, AuditEvent) == before


# --- the sentinel: password, e-mail and name never leak ------------------------------------------

_PW_SENTINEL = "Zq9-pw-SENTINEL-8841-xyz"
_EMAIL_SENTINEL = "sentinel-mailbox-7731@leakcheck.test"
_NAME_SENTINEL = "Sentinelle Nomenclature"


def _audit_dump(db, *ids) -> str:
    """Every audit field of every event about ``ids`` (as actor or entity), as text."""
    rows = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id.in_(ids) | AuditEvent.user_id.in_(ids)
        )
    ).scalars().all()
    return json.dumps(
        [
            {
                "type": r.event_type, "action": r.action, "entity_type": r.entity_type,
                "entity_id": str(r.entity_id), "user_id": str(r.user_id),
                "previous": r.previous_state, "new": r.new_state, "meta": r.event_metadata,
            }
            for r in rows
        ],
        sort_keys=True,
    )


def test_password_email_and_name_never_reach_audit_logs_views_or_messages(db, admin, caplog):
    caplog.set_level(logging.DEBUG)
    view = create_user_as_admin(
        db, email=_EMAIL_SENTINEL, full_name=_NAME_SENTINEL, role="HR",
        password=_PW_SENTINEL, password_confirm=_PW_SENTINEL, acting_user_id=admin.id,
    )
    deactivate_user(db, target_user_id=view.user_id, acting_user_id=admin.id)
    reactivate_user(db, target_user_id=view.user_id, acting_user_id=admin.id)

    messages = []
    for kwargs in (
        dict(password="short", password_confirm="short"),
        dict(password=_PW_SENTINEL, password_confirm=_PW_SENTINEL + "x"),
        dict(email=_EMAIL_SENTINEL),                                  # duplicate
        dict(role="SYSTEM"),
        dict(email="not-an-email"),
    ):
        args = dict(
            email=f"x-{uuid.uuid4().hex}@x.test", full_name=_NAME_SENTINEL, role="HR",
            password=_PW_SENTINEL, password_confirm=_PW_SENTINEL, acting_user_id=admin.id,
        )
        args.update(kwargs)
        with pytest.raises(Exception) as exc:
            create_user_as_admin(db, **args)
        messages.append(str(exc.value))

    audit = _audit_dump(db, view.user_id, admin.id)
    assert audit != "[]"
    for secret in (_PW_SENTINEL, _EMAIL_SENTINEL, _NAME_SENTINEL, _EMAIL_SENTINEL.split("@")[0]):
        assert secret not in audit, f"{secret!r} leaked into an audit field"
        assert secret not in caplog.text, f"{secret!r} leaked into a log line"
        assert all(secret not in m for m in messages), f"{secret!r} leaked into a message"
    # the view legitimately carries the e-mail and name (the page displays them) —
    # but never anything password-related
    assert _PW_SENTINEL not in repr(view) and "hash" not in repr(view).lower()
    assert "$2" not in repr(view)


def test_nothing_is_printed(db, admin, capsys):
    view = _create(db, admin, password=_PW_SENTINEL, password_confirm=_PW_SENTINEL)
    deactivate_user(db, target_user_id=view.user_id, acting_user_id=admin.id)
    out = capsys.readouterr()
    assert _PW_SENTINEL not in out.out + out.err


# =========================================================== structural ================


def test_the_module_has_no_delete_no_ai_and_no_streamlit():
    tree = ast.parse(_SERVICE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in {"delete", "execute_delete", "truncate", "drop"}, name
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] + [a.name for a in node.names])
            for n in names:
                assert not n.startswith(("app.ai", "streamlit")), n
                assert "claude" not in n.lower() and "anthropic" not in n.lower(), n
    imported_sqlalchemy = {
        a.name for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "sqlalchemy" for a in n.names
    }
    assert "delete" not in imported_sqlalchemy
    source = _SERVICE.read_text(encoding="utf-8")
    assert "session.delete" not in source and "db.delete" not in source


def test_no_view_or_query_reads_the_password_hash():
    source = _SERVICE.read_text(encoding="utf-8")
    assert "User.hashed_password" not in source
    assert ".hashed_password" not in source
    assert [c.key for c in uas._VIEW_COLUMNS] == [
        "id", "full_name", "email", "role", "is_active", "created_at",
    ]


def test_the_original_create_user_and_seed_are_untouched():
    """create_user's own (e-mail-bearing) audit event is NOT this module's business:
    it is left as it was, and ``app/seed.py`` still uses it."""
    seed = (Path(__file__).resolve().parents[1] / "app" / "seed.py").read_text(encoding="utf-8")
    assert "create_user" in seed and "user_admin_service" not in seed
