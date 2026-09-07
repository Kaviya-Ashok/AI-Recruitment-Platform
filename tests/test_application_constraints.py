"""DB-level constraint tests for the ``applications`` table (Phase 2 fix).

These assert the *database* behaviour, not the ORM: all three FKs on
``applications`` are ``ON DELETE RESTRICT``, so hard-deleting a ``candidate`` /
``job`` / ``application_link`` that still has a dependent application is rejected
by Postgres rather than silently cascading the application away.

Real Postgres via the savepoint-rollback ``db`` fixture. Each delete attempt is
wrapped in ``db.begin_nested()`` so the expected ``IntegrityError`` rolls back
only its own savepoint and the session stays usable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.user import UserRole
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job


def _setup_application(db):
    user = create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )
    job = create_job(
        db,
        title="Constrained Job",
        department="Data",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    app_row = create_application(
        db,
        job_id=job.id,
        application_link_id=link.id,
        email=f"cand-{uuid.uuid4().hex}@example.com",
        full_name="Casey Candidate",
        phone="555-0100",
    )
    return job, link, app_row


def _delete(db, table: str, row_id) -> None:
    with db.begin_nested():
        db.execute(
            text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id}
        )


def test_deleting_application_link_with_dependent_application_is_rejected(db):
    job, link, app_row = _setup_application(db)
    with pytest.raises(IntegrityError):
        _delete(db, "application_links", link.id)
    # application still present
    assert db.execute(
        text("SELECT count(*) FROM applications WHERE id = :id"),
        {"id": app_row.id},
    ).scalar_one() == 1


def test_deleting_job_with_dependent_application_is_rejected(db):
    job, link, app_row = _setup_application(db)
    with pytest.raises(IntegrityError):
        _delete(db, "jobs", job.id)
    assert db.execute(
        text("SELECT count(*) FROM applications WHERE id = :id"),
        {"id": app_row.id},
    ).scalar_one() == 1


def test_deleting_candidate_with_dependent_application_is_rejected(db):
    job, link, app_row = _setup_application(db)
    with pytest.raises(IntegrityError):
        _delete(db, "candidates", app_row.candidate_id)
    assert db.execute(
        text("SELECT count(*) FROM applications WHERE id = :id"),
        {"id": app_row.id},
    ).scalar_one() == 1


def test_deleting_parents_after_application_removed_is_allowed(db):
    """RESTRICT only blocks while a dependent application exists — the explicit
    'remove the application first' orchestration still works."""
    job, link, app_row = _setup_application(db)
    candidate_id = app_row.candidate_id

    db.execute(text("DELETE FROM applications WHERE id = :id"), {"id": app_row.id})
    db.flush()

    # now the parents can be removed
    _delete(db, "application_links", link.id)
    _delete(db, "candidates", candidate_id)
    db.flush()

    assert db.execute(
        text("SELECT count(*) FROM candidates WHERE id = :id"),
        {"id": candidate_id},
    ).scalar_one() == 0
