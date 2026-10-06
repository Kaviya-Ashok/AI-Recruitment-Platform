"""Admin user management — list, create, deactivate, reactivate (CLAUDE.md §23).

ADMIN ONLY. Every function takes ``acting_user_id`` and, as its first act, runs
:func:`_require_admin`: ``require_internal_user`` (unknown, malformed and inactive
users are refused exactly as everywhere else), then the SYSTEM rejection, then the
role gate — in that order, so an unknown caller learns nothing about roles. The
same pattern as ``final_decision_service._require_decider``. The Users page is also
absent from every non-admin's navigation, but this check is what actually protects
the data.

WHAT IS NOT HERE
----------------
No delete (accounts are disabled, never removed — audit history and foreign keys
must stay intact), no password reset, no role change, no invitation e-mail. Those
are separate, deliberate follow-ups.

PASSWORDS
---------
The initial password is hashed with the same ``auth_service.hash_password`` the
login verifies against. It is checked (:func:`validate_password`), hashed, stored
as a hash and forgotten: it is never logged, never put in a message, an audit field
or a return value, and no view in this module carries a password hash.

``auth_service`` had no password rule beyond "not empty". This module adds one for
accounts created through the admin page: at least :data:`PASSWORD_MIN_CHARS`
characters, and at most :data:`PASSWORD_MAX_BYTES` bytes (bcrypt ignores everything
after 72 bytes, so a longer password would silently be a shorter one).

CREATION IS NOT ``auth_service.create_user``
--------------------------------------------
``create_user`` writes its ``USER_CREATED`` audit event with the new user's e-mail
and full name in the action text and the state snapshot, and records the NEW user as
the actor. That is fine for the first-account bootstrap but not for an admin action
that must audit structurally and name the acting admin. So :func:`create_user_as_admin`
builds the row itself (same ``hash_password``, same lower-cased e-mail rule) and
writes its own ``USER_CREATED`` event whose fields are structural only: the acting
user id, the target user id and the role. ``create_user`` and ``app/seed.py`` are
unchanged.

AUDIT (all in the SAME transaction as the change)
--------------------------------------------------
``USER_CREATED`` / ``USER_DEACTIVATED`` / ``USER_REACTIVATED`` — ``user_id`` = the
acting admin, ``entity_type`` = ``"user"``, ``entity_id`` = the target, and metadata
``{acting_user_id, target_user_id, role}``. No e-mail, no name, no password
material, ever.

PROTECTIONS
-----------
* The SYSTEM user is never listed and never a target.
* An admin cannot deactivate their own account.
* The last active ADMIN cannot be deactivated. The active admins are locked
  (``SELECT ... FOR UPDATE``) before the check, so two admins deactivating each
  other at the same moment cannot leave the platform with none.
* Deactivating an already inactive user (or reactivating an active one) is a calm
  no-op: no second event.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models.audit_event import AuditEventType
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
from app.services.audit_service import record_event
from app.services.auth_service import hash_password
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

#: Roles an admin may assign. SYSTEM is the automated pipeline and is never
#: creatable through the UI.
ASSIGNABLE_ROLES: tuple[UserRole, ...] = (
    UserRole.HR, UserRole.HIRING_MANAGER, UserRole.ADMIN,
)

PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_BYTES = 72          # bcrypt's limit

_EMAIL_MAX = 320
_NAME_MAX = 255
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --- user-safe messages (never interpolate an e-mail, a name or a password) ----

_NOT_ADMIN = "Only an administrator can manage users."
_SYSTEM_ACTOR = "The automated pipeline account cannot manage users."
_SYSTEM_TARGET = "The automated pipeline account cannot be changed."
_NO_SUCH_USER = "No such user."
_BAD_EMAIL = "Enter a valid email address."
_BAD_NAME = "Enter the person's full name."
_NAME_TOO_LONG = f"The name can be at most {_NAME_MAX} characters."
_BAD_ROLE = "Choose HR, Hiring manager or Admin."
_PASSWORD_SHORT = f"The password must be at least {PASSWORD_MIN_CHARS} characters."
_PASSWORD_LONG = (
    f"The password is too long: it can be at most {PASSWORD_MAX_BYTES} bytes."
)
_PASSWORD_MISMATCH = "The two passwords do not match."
_DUPLICATE = "A user with that email already exists."
_SELF_DEACTIVATE = "You can't deactivate your own account."
_LAST_ADMIN = (
    "This is the last active administrator. Another administrator must be active "
    "before this account can be deactivated."
)


# --- exceptions --------------------------------------------------------------------


class UserAdminError(Exception):
    """Base class. The message is always safe to show to the admin."""


class UserAdminPermissionError(UserAdminError):
    """The caller is not an administrator (or is the SYSTEM account)."""


class UserAdminValidationError(UserAdminError):
    """An input failed validation."""


class UserAdminConflictError(UserAdminError):
    """The e-mail is already registered."""


class UserAdminTargetError(UserAdminError):
    """The target user is missing, is the SYSTEM account, or the change is not
    allowed (self-deactivation, last administrator)."""


# --- views -----------------------------------------------------------------------------


@dataclass(frozen=True)
class UserView:
    """What the Users page may know about an account. Deliberately has NO password
    hash field, and is built from explicit columns so the hash is never loaded."""

    user_id: uuid.UUID
    full_name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime


_VIEW_COLUMNS = (
    User.id, User.full_name, User.email, User.role, User.is_active, User.created_at,
)


def _to_view(row) -> UserView:
    user_id, full_name, email, role, is_active, created_at = row
    return UserView(
        user_id=user_id, full_name=full_name, email=email,
        role=getattr(role, "value", role), is_active=bool(is_active),
        created_at=created_at,
    )


# --- guards and validation -----------------------------------------------------------------


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _is_system(user: User) -> bool:
    return user.role is UserRole.SYSTEM or user.id == SYSTEM_USER_ID


def _require_admin(db: Session, acting_user_id: uuid.UUID | str | None) -> User:
    """``require_internal_user``, then the SYSTEM rejection, then the ADMIN gate."""
    actor = require_internal_user(db, acting_user_id)
    if _is_system(actor):
        logger.warning("user_admin rejected: SYSTEM actor user_id=%s", actor.id)
        raise UserAdminPermissionError(_SYSTEM_ACTOR)
    if actor.role is not UserRole.ADMIN:
        logger.warning(
            "user_admin denied: role=%s user_id=%s", actor.role.value, actor.id
        )
        raise UserAdminPermissionError(_NOT_ADMIN)
    return actor


def validate_role(value: object) -> UserRole:
    """One of HR / HIRING_MANAGER / ADMIN as a :class:`UserRole`. SYSTEM and
    anything else (including free text) is refused."""
    raw = getattr(value, "value", value)
    for role in ASSIGNABLE_ROLES:
        if raw == role.value:
            return role
    raise UserAdminValidationError(_BAD_ROLE)


def validate_email(value: object) -> str:
    """The trimmed, lower-cased address (the same normalisation login uses)."""
    email = (value or "").strip().lower() if isinstance(value, str) else ""
    if not email or len(email) > _EMAIL_MAX or not _EMAIL_SHAPE.match(email):
        raise UserAdminValidationError(_BAD_EMAIL)
    return email


def validate_name(value: object) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        raise UserAdminValidationError(_BAD_NAME)
    if len(name) > _NAME_MAX:
        raise UserAdminValidationError(_NAME_TOO_LONG)
    return name


def validate_password(password: object, confirm: object) -> str:
    """The password, if it is long enough, fits bcrypt and matches its
    confirmation. The messages never repeat any part of it."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_CHARS:
        raise UserAdminValidationError(_PASSWORD_SHORT)
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise UserAdminValidationError(_PASSWORD_LONG)
    if password != confirm:
        raise UserAdminValidationError(_PASSWORD_MISMATCH)
    return password


def _audit(
    db: Session, event_type: AuditEventType, actor: User, target_id: uuid.UUID,
    role: UserRole, action: str, *, previous_active: bool | None, now_active: bool,
) -> None:
    """One structural audit event: ids and role only."""
    record_event(
        db,
        event_type=event_type,
        action=action,
        entity_type="user",
        entity_id=target_id,
        user_id=actor.id,
        previous_state=(
            None if previous_active is None else {"is_active": previous_active}
        ),
        new_state={"is_active": now_active},
        metadata={
            "acting_user_id": str(actor.id),
            "target_user_id": str(target_id),
            "role": role.value,
        },
    )


# --- reads ------------------------------------------------------------------------------------


def list_users(
    db: Session, *, acting_user_id: uuid.UUID | str | None
) -> list[UserView]:
    """Every account except the SYSTEM user, newest first. ADMIN ONLY; read-only.
    The password hash column is never selected."""
    _require_admin(db, acting_user_id)
    rows = db.execute(
        select(*_VIEW_COLUMNS)
        .where(User.role != UserRole.SYSTEM, User.id != SYSTEM_USER_ID)
        .order_by(User.created_at.desc(), User.id)
    ).all()
    return [_to_view(r) for r in rows]


def _view_of(db: Session, user_id: uuid.UUID) -> UserView:
    return _to_view(db.execute(select(*_VIEW_COLUMNS).where(User.id == user_id)).one())


# --- create ------------------------------------------------------------------------------------


def create_user_as_admin(
    db: Session,
    *,
    email: str,
    full_name: str,
    role: UserRole | str,
    password: str,
    password_confirm: str,
    acting_user_id: uuid.UUID | str | None,
) -> UserView:
    """Create an ACTIVE account and audit it, in one transaction. ADMIN ONLY.

    Raises
    ------
    UnauthorizedError
        The caller is missing, unknown or inactive.
    UserAdminPermissionError
        The caller is not an administrator.
    UserAdminValidationError
        Bad e-mail, name, role (SYSTEM is refused), a short/long password, or a
        mismatched confirmation.
    UserAdminConflictError
        The e-mail is already registered (a business error, never an
        ``IntegrityError``).
    """
    actor = _require_admin(db, acting_user_id)
    email_norm = validate_email(email)
    name = validate_name(full_name)
    role_value = validate_role(role)
    secret = validate_password(password, password_confirm)

    if db.execute(select(User.id).where(User.email == email_norm)).first() is not None:
        raise UserAdminConflictError(_DUPLICATE)

    user = User(
        email=email_norm,
        hashed_password=hash_password(secret),
        full_name=name,
        role=role_value,
    )
    db.add(user)
    try:
        db.flush()                       # assigns the id; trips the unique constraint
    except IntegrityError as exc:
        db.rollback()
        raise UserAdminConflictError(_DUPLICATE) from exc

    _audit(
        db, AuditEventType.USER_CREATED, actor, user.id, role_value,
        "User account created by an administrator.",
        previous_active=None, now_active=True,
    )
    db.commit()
    logger.info(
        "user_created by=%s target=%s role=%s", actor.id, user.id, role_value.value
    )
    return _view_of(db, user.id)


# --- deactivate / reactivate -----------------------------------------------------------------------


def _load_target(db: Session, target_user_id: uuid.UUID | str) -> User:
    target_uuid = _as_uuid(target_user_id)
    target = db.get(User, target_uuid) if target_uuid is not None else None
    if target is None:
        raise UserAdminTargetError(_NO_SUCH_USER)
    if _is_system(target):
        raise UserAdminTargetError(_SYSTEM_TARGET)
    return target


def check_not_last_admin(active_admins: list[User], target: User) -> None:
    """Refuse when ``target`` is an ADMIN and no OTHER active ADMIN exists. Pure."""
    if target.role is not UserRole.ADMIN:
        return
    if not [a for a in active_admins if a.id != target.id]:
        raise UserAdminTargetError(_LAST_ADMIN)


def deactivate_user(
    db: Session,
    *,
    target_user_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str | None,
) -> UserView:
    """Deactivate an account (reversible; never a delete) and audit it, in one
    transaction. ADMIN ONLY. A user who is already inactive is returned unchanged
    with no second event.

    Raises
    ------
    UserAdminTargetError
        No such user; the SYSTEM account; the caller's own account; or the last
        active administrator.
    """
    actor = _require_admin(db, acting_user_id)
    target = _load_target(db, target_user_id)
    if target.id == actor.id:
        raise UserAdminTargetError(_SELF_DEACTIVATE)
    if not target.is_active:
        return _view_of(db, target.id)

    # Lock the active administrators BEFORE judging, so concurrent deactivations
    # serialise; then re-check that the actor is still one of them.
    admins = list(
        db.execute(
            select(User)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .order_by(User.id)
            .with_for_update()
        ).scalars().all()
    )
    if actor.id not in {a.id for a in admins}:
        raise UserAdminPermissionError(_NOT_ADMIN)
    check_not_last_admin(admins, target)

    target.is_active = False
    _audit(
        db, AuditEventType.USER_DEACTIVATED, actor, target.id, target.role,
        "User account deactivated by an administrator.",
        previous_active=True, now_active=False,
    )
    db.commit()
    logger.info("user_deactivated by=%s target=%s", actor.id, target.id)
    return _view_of(db, target.id)


def reactivate_user(
    db: Session,
    *,
    target_user_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str | None,
) -> UserView:
    """Reactivate an account and audit it, in one transaction. ADMIN ONLY. An
    already active user is returned unchanged with no second event."""
    actor = _require_admin(db, acting_user_id)
    target = _load_target(db, target_user_id)
    if target.is_active:
        return _view_of(db, target.id)

    target.is_active = True
    _audit(
        db, AuditEventType.USER_REACTIVATED, actor, target.id, target.role,
        "User account reactivated by an administrator.",
        previous_active=False, now_active=True,
    )
    db.commit()
    logger.info("user_reactivated by=%s target=%s", actor.id, target.id)
    return _view_of(db, target.id)
