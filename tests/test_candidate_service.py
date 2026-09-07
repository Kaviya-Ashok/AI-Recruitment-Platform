"""Tests for app.services.candidate_service (Phase 2).

No AI in this step — real Postgres via the savepoint-rollback ``db`` fixture.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.database.models.candidate import Candidate
from app.services.candidate_service import (
    CandidateValidationError,
    get_candidate_by_email,
    get_or_create_candidate,
    normalise_email,
)


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(Candidate)).scalar_one()


def test_create_new_candidate_by_email(db):
    c = get_or_create_candidate(
        db, email="Alice@Example.com", full_name="  Alice A  ", phone=" 123 "
    )
    assert c.id is not None
    assert c.email == "alice@example.com"  # normalised
    assert c.full_name == "Alice A"       # trimmed
    assert c.phone == "123"


def test_blank_phone_stored_as_none(db):
    c = get_or_create_candidate(
        db, email="b@example.com", full_name="B", phone="   "
    )
    assert c.phone is None


def test_get_or_create_is_idempotent_on_repeat_email(db):
    first = get_or_create_candidate(
        db, email="dup@example.com", full_name="First Name", phone="111"
    )
    before = _count(db)

    second = get_or_create_candidate(
        db, email="  DUP@example.com ", full_name="Different Name", phone="999"
    )

    assert second.id == first.id
    assert _count(db) == before  # no duplicate person row
    # existing identity is NOT overwritten by the second call
    assert second.full_name == "First Name"
    assert second.phone == "111"


def test_get_candidate_by_email_normalises_and_misses_cleanly(db):
    assert get_candidate_by_email(db, "nobody@example.com") is None
    assert get_candidate_by_email(db, "") is None

    made = get_or_create_candidate(
        db, email="found@example.com", full_name="F", phone=None
    )
    assert get_candidate_by_email(db, "  FOUND@EXAMPLE.COM ").id == made.id


@pytest.mark.parametrize("email", ["", "   ", None])
def test_missing_email_rejected(db, email):
    with pytest.raises(CandidateValidationError):
        get_or_create_candidate(db, email=email, full_name="X", phone=None)


@pytest.mark.parametrize("name", ["", "   ", None])
def test_missing_name_rejected(db, name):
    with pytest.raises(CandidateValidationError):
        get_or_create_candidate(
            db, email="x@example.com", full_name=name, phone=None
        )


def test_normalise_email_helper():
    assert normalise_email("  Foo@Bar.COM ") == "foo@bar.com"
    assert normalise_email(None) == ""


def test_get_or_create_does_not_commit(db):
    """The helper must leave the transaction open for the caller to commit."""
    get_or_create_candidate(
        db, email="pending@example.com", full_name="P", phone=None
    )
    # row is visible in-session (flushed) but not committed
    assert get_candidate_by_email(db, "pending@example.com") is not None
    db.rollback()
    assert get_candidate_by_email(db, "pending@example.com") is None
