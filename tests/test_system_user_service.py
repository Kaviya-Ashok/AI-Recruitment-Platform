"""Tests for the seeded SYSTEM actor (CLAUDE.md §2A item 2).

Real Postgres via the savepoint-rollback ``db`` fixture. The SYSTEM row itself
is seeded by migration ``c9a4f1d7b208`` and is therefore already present in the
test database — these tests read it, and any row they add/modify is rolled back.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database.models.user import (
    SYSTEM_USER_EMAIL,
    SYSTEM_USER_ID,
    User,
    UserRole,
)
from app.services.auth_service import authenticate_user
from app.services.system_user_service import (
    SystemUserMissingError,
    get_system_user,
    get_system_user_id,
)
from app.utils.authorization import UnauthorizedError, require_internal_user

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "app" / "database" / "migrations" / "versions"
    / "c9a4f1d7b208_add_system_user_role_and_system_user.py"
)


# --- the migration literals must match the app constants ---------------


def test_migration_literals_match_the_app_constants():
    """The migration hard-codes the id/email (a migration must not import app
    code that can move). This test is what stops the two from drifting."""
    source = _MIGRATION.read_text(encoding="utf-8")

    seeded_id = re.search(r"^SYSTEM_USER_ID = '([^']+)'", source, re.M).group(1)
    seeded_email = re.search(
        r"^SYSTEM_USER_EMAIL = '([^']+)'", source, re.M
    ).group(1)

    assert uuid.UUID(seeded_id) == SYSTEM_USER_ID
    assert seeded_email == SYSTEM_USER_EMAIL


# --- the seeded row ----------------------------------------------------


def test_system_user_is_seeded_with_the_fixed_identity(db):
    user = get_system_user(db)

    assert user.id == SYSTEM_USER_ID
    assert user.email == SYSTEM_USER_EMAIL
    assert user.role is UserRole.SYSTEM
    assert user.is_active is True


def test_get_system_user_id_returns_the_fixed_uuid(db):
    assert get_system_user_id(db) == SYSTEM_USER_ID


def test_exactly_one_system_user_exists(db):
    """Idempotent seed: the guarded INSERT can never produce a second row, no
    matter how many times the migration runs."""
    count = db.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.SYSTEM)
    ).scalar_one()
    assert count == 1


def test_reapplying_the_seed_statement_creates_no_second_row(db):
    """Re-run the migration's exact guarded INSERT and prove it is a no-op.

    Runs the real SQL rather than asserting about the Python, so a future edit
    that drops the WHERE NOT EXISTS guard fails here.
    """
    from sqlalchemy import text

    before = db.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.SYSTEM)
    ).scalar_one()

    for _ in range(2):
        db.execute(
            text(
                """
                INSERT INTO users (
                    id, email, hashed_password, full_name, role, is_active
                )
                SELECT
                    CAST(:id AS uuid), :email, :pw, :full_name,
                    CAST('SYSTEM' AS user_role), true
                WHERE NOT EXISTS (
                    SELECT 1 FROM users
                    WHERE id = CAST(:id AS uuid) OR email = :email
                )
                """
            ).bindparams(
                id=str(SYSTEM_USER_ID),
                email=SYSTEM_USER_EMAIL,
                pw="!",
                full_name="Automated Screening Pipeline",
            )
        )
    db.flush()

    after = db.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.SYSTEM)
    ).scalar_one()
    assert after == before == 1


# --- it is an actor, not a login --------------------------------------


def test_system_user_cannot_be_logged_into(db):
    """The seeded hash is the sentinel '!', not bcrypt — verify_password can
    never match it, so no password authenticates this account."""
    user = get_system_user(db)
    assert user.hashed_password == "!"

    for attempt in ("!", "", "password", "Automated Screening Pipeline"):
        assert authenticate_user(db, SYSTEM_USER_EMAIL, attempt) is None


# --- the guard accepts it with no special-casing -----------------------


def test_require_internal_user_accepts_the_system_user_unchanged(db):
    """CLAUDE.md §2A item 2: this is NOT a bypass — the guard accepts it by the
    same rule it accepts any active internal user."""
    resolved = require_internal_user(db, get_system_user_id(db))
    assert resolved.id == SYSTEM_USER_ID
    assert resolved.role is UserRole.SYSTEM


# --- failure modes are loud, and never self-healing --------------------


def test_missing_system_user_raises_actionable_error(db):
    """Simulated by deleting the row inside the test's rolled-back savepoint."""
    db.execute(
        User.__table__.delete().where(User.id == SYSTEM_USER_ID)
    )
    db.flush()

    with pytest.raises(SystemUserMissingError) as exc:
        get_system_user_id(db)

    message = str(exc.value)
    assert "alembic upgrade head" in message
    assert "c9a4f1d7b208" in message


def test_inactive_system_user_is_refused_not_silently_used(db):
    user = get_system_user(db)
    user.is_active = False
    db.flush()

    with pytest.raises(SystemUserMissingError):
        get_system_user_id(db)


def test_accessor_never_creates_the_row(db):
    """A missing SYSTEM user must stay missing — creating a privileged actor
    lazily from a public code path would be exactly the wrong behaviour."""
    db.execute(User.__table__.delete().where(User.id == SYSTEM_USER_ID))
    db.flush()

    with pytest.raises(SystemUserMissingError):
        get_system_user_id(db)

    still_absent = db.execute(
        select(func.count()).select_from(User).where(User.id == SYSTEM_USER_ID)
    ).scalar_one()
    assert still_absent == 0
