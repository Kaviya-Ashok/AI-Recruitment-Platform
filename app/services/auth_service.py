"""Authentication logic: password hashing, user creation, and login checks.

This module is backend-only and independently testable. It does not touch
Streamlit or session state.

Security rationale
------------------
* **bcrypt** (via passlib) hashes passwords: a deliberately slow, salted,
  adaptive hash built for passwords. We never roll our own hashing. The work
  factor (``rounds=12``) is a fixed, sensible MVP default — costly to
  brute-force, still fast enough for interactive login. It can be raised later
  without invalidating existing hashes: passlib stores the cost inside the hash
  string and :func:`verify_password` transparently handles any cost.
* **Generic authentication failure**: :func:`authenticate_user` returns
  ``None`` for every failure mode — unknown email, wrong password, or
  deactivated account. Callers/UI must show one generic message so a probing
  attacker cannot learn which accounts exist or are active. A dummy hash
  verification runs even when the email is unknown so response timing does not
  leak account existence.
* **Case-insensitive email**: addresses are normalised to lower-case and
  trimmed on both creation and lookup. No mainstream mail provider treats the
  address as case-sensitive, users type mixed case, and storing one canonical
  form lets the DB unique constraint actually block ``Foo@x.com`` /
  ``foo@x.com`` duplicates. Only case/whitespace is normalised — nothing else
  is rewritten.
* Plaintext passwords are never logged, printed, or persisted. Only the bcrypt
  hash string (``$2b$12$...``) is stored in ``users.hashed_password``.
"""

from __future__ import annotations

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.user import User, UserRole

# Fixed MVP work factor. Not worth an env var yet; raise here when hardware
# improves — existing hashes keep verifying.
_BCRYPT_ROUNDS = 12

_pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=_BCRYPT_ROUNDS,
)

# Verified against when the email is unknown, to keep authenticate_user timing
# roughly constant regardless of whether the account exists.
_DUMMY_HASH = _pwd_context.hash("timing-equalisation-placeholder")


class EmailAlreadyExistsError(Exception):
    """Raised by :func:`create_user` when the email is already registered."""


def _normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_password(plain_password: str) -> str:
    """Return a salted bcrypt hash of ``plain_password``.

    bcrypt only considers the first 72 bytes of the password (passlib truncates
    silently); acceptable for the MVP.
    """
    if not plain_password:
        raise ValueError("password must not be empty")
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return ``True`` iff ``plain_password`` matches ``hashed_password``.

    Never raises: a blank or malformed password/hash yields ``False``.
    """
    if not plain_password or not hashed_password:
        return False
    try:
        return _pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


def create_user(
    db: Session,
    email: str,
    plain_password: str,
    full_name: str,
    role: UserRole,
) -> User:
    """Hash the password, persist a :class:`User`, and record an audit event.

    Raises
    ------
    ValueError
        If ``email``, ``plain_password`` or ``full_name`` is empty, or ``role``
        is not a :class:`UserRole`.
    EmailAlreadyExistsError
        If a user with the (normalised) email already exists — raised instead of
        leaking a raw ``IntegrityError``.
    """
    email_norm = _normalise_email(email)
    if not email_norm:
        raise ValueError("email must not be empty")
    if not plain_password:
        raise ValueError("password must not be empty")
    if not full_name or not full_name.strip():
        raise ValueError("full_name must not be empty")
    if not isinstance(role, UserRole):
        raise ValueError("role must be a UserRole")

    # Friendly pre-check; the unique constraint below is the real guard.
    if db.execute(
        select(User.id).where(User.email == email_norm)
    ).first() is not None:
        raise EmailAlreadyExistsError(
            f"a user with email {email_norm!r} already exists"
        )

    user = User(
        email=email_norm,
        hashed_password=hash_password(plain_password),
        full_name=full_name.strip(),
        role=role,
    )
    db.add(user)
    try:
        db.flush()  # assign user.id and trigger the unique constraint
    except IntegrityError as exc:
        db.rollback()
        raise EmailAlreadyExistsError(
            f"a user with email {email_norm!r} already exists"
        ) from exc

    db.refresh(user)  # pull server defaults (is_active, created_at, ...)

    db.add(
        AuditEvent(
            user_id=user.id,
            event_type=AuditEventType.USER_CREATED.value,
            entity_type="user",
            entity_id=user.id,
            action=f"User account created: {email_norm} (role={role.value})",
            new_state={
                "email": email_norm,
                "full_name": user.full_name,
                "role": role.value,
                "is_active": user.is_active,
            },
        )
    )
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(
    db: Session,
    email: str,
    plain_password: str,
) -> User | None:
    """Return the :class:`User` on success, otherwise ``None``.

    ``None`` is returned identically for: unknown email, wrong password, or an
    inactive account. Never raises on bad credentials.
    """
    if not email or not plain_password:
        return None

    email_norm = _normalise_email(email)
    user = db.execute(
        select(User).where(User.email == email_norm)
    ).scalar_one_or_none()

    if user is None:
        # Spend comparable time so timing does not reveal the account is absent.
        verify_password(plain_password, _DUMMY_HASH)
        return None

    password_ok = verify_password(plain_password, user.hashed_password)
    if not password_ok or not user.is_active:
        return None

    return user
