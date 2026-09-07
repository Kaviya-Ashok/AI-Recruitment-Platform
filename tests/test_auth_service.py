"""Tests for app.services.auth_service.

No plaintext password literal is ever asserted against, printed, or logged;
each test binds its secret to a local variable and reuses that variable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.user import User, UserRole
from app.services.auth_service import (
    EmailAlreadyExistsError,
    authenticate_user,
    create_user,
    hash_password,
    verify_password,
)


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.test"


# --- hashing --------------------------------------------------------------


def test_hash_password_differs_from_plaintext_and_verifies():
    secret = "correct horse battery staple"
    hashed = hash_password(secret)

    assert hashed != secret
    assert hashed.startswith("$2b$")
    assert verify_password(secret, hashed) is True
    assert verify_password("not the secret", hashed) is False


def test_hash_password_is_salted_but_both_verify():
    secret = "correct horse battery staple"
    hash_a = hash_password(secret)
    hash_b = hash_password(secret)

    assert hash_a != hash_b  # distinct random salts
    assert verify_password(secret, hash_a) is True
    assert verify_password(secret, hash_b) is True


def test_hash_password_rejects_empty():
    with pytest.raises(ValueError):
        hash_password("")


# --- create_user ---------------------------------------------------------


def test_create_user_persists_hashed_not_plaintext(db):
    secret = "s3cret-value-A"
    email = _unique_email()

    user = create_user(
        db=db,
        email=email,
        plain_password=secret,
        full_name="Test Person",
        role=UserRole.HR,
    )

    # Re-read the raw column straight from the DB.
    stored_hash = db.execute(
        select(User.hashed_password).where(User.id == user.id)
    ).scalar_one()

    assert stored_hash != secret
    assert stored_hash.startswith("$2b$")
    assert secret not in stored_hash
    assert verify_password(secret, stored_hash) is True


def test_create_user_normalises_email_case(db):
    secret = "s3cret-value-B"
    email = _unique_email().upper()

    user = create_user(
        db=db,
        email=email,
        plain_password=secret,
        full_name="Case Test",
        role=UserRole.HR,
    )

    assert user.email == email.lower()


def test_create_user_writes_audit_event(db):
    secret = "s3cret-value-C"
    user = create_user(
        db=db,
        email=_unique_email(),
        plain_password=secret,
        full_name="Audited Person",
        role=UserRole.ADMIN,
    )

    events = db.execute(
        select(AuditEvent).where(AuditEvent.user_id == user.id)
    ).scalars().all()

    assert len(events) == 1
    event = events[0]
    assert event.event_type == AuditEventType.USER_CREATED.value
    assert event.entity_type == "user"
    assert event.entity_id == user.id
    assert event.new_state["email"] == user.email
    # The audit payload must not carry the password in any form.
    assert secret not in (event.action or "")
    assert secret not in str(event.new_state)


def test_create_user_duplicate_email_raises_domain_error(db):
    secret = "s3cret-value-D"
    email = _unique_email()
    create_user(
        db=db,
        email=email,
        plain_password=secret,
        full_name="First",
        role=UserRole.HR,
    )

    with pytest.raises(EmailAlreadyExistsError):
        create_user(
            db=db,
            email=email.upper(),  # same address, different case
            plain_password=secret,
            full_name="Second",
            role=UserRole.HR,
        )


@pytest.mark.parametrize(
    ("email", "password", "full_name"),
    [
        ("", "s3cret-value-E", "No Email"),
        (_unique_email(), "", "No Password"),
        (_unique_email(), "s3cret-value-E", ""),
        (_unique_email(), "s3cret-value-E", "   "),
    ],
)
def test_create_user_rejects_empty_fields(db, email, password, full_name):
    with pytest.raises(ValueError):
        create_user(
            db=db,
            email=email,
            plain_password=password,
            full_name=full_name,
            role=UserRole.HR,
        )


# --- authenticate_user -------------------------------------------------


def test_authenticate_user_succeeds_with_correct_credentials(db):
    secret = "s3cret-value-F"
    email = _unique_email()
    created = create_user(
        db=db,
        email=email,
        plain_password=secret,
        full_name="Login Ok",
        role=UserRole.HR,
    )

    result = authenticate_user(db=db, email=email.upper(), plain_password=secret)

    assert result is not None
    assert result.id == created.id


def test_authenticate_user_returns_none_for_wrong_password(db):
    secret = "s3cret-value-G"
    email = _unique_email()
    create_user(
        db=db,
        email=email,
        plain_password=secret,
        full_name="Wrong Pw",
        role=UserRole.HR,
    )

    assert authenticate_user(db=db, email=email, plain_password="not-it") is None


def test_authenticate_user_returns_none_for_unknown_email(db):
    assert (
        authenticate_user(
            db=db, email=_unique_email(), plain_password="s3cret-value-H"
        )
        is None
    )


def test_authenticate_user_returns_none_for_inactive_user(db):
    secret = "s3cret-value-I"
    email = _unique_email()
    user = create_user(
        db=db,
        email=email,
        plain_password=secret,
        full_name="Deactivated",
        role=UserRole.HR,
    )

    user.is_active = False
    db.commit()

    assert authenticate_user(db=db, email=email, plain_password=secret) is None


def test_authenticate_user_does_not_raise_on_blank_input(db):
    assert authenticate_user(db=db, email="", plain_password="") is None
