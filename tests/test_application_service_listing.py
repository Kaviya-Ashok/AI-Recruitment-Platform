"""Tests for the read-side paging extension on
``application_service.list_applications_for_job`` plus the new
``count_applications_for_job``.

Added for the Candidates page's "Show more" control (Phase C-2), mirroring the
Jobs-page precedent in ``tests/test_job_service_listing.py``. Read-only:
nothing here changes an application's status or any business rule.

ISOLATION: every assertion is scoped to a job this test just created, so
leftover rows in the shared development database cannot affect a count or an
ordering.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.database.models.application import Application
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.user import UserRole
from app.services.application_link_service import generate_link
from app.services.application_service import (
    count_applications_for_job,
    create_application,
    list_applications_for_job,
)
from app.services.auth_service import create_user
from app.services.job_service import create_job

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _hr_user(db):
    return create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )


def _job_with_link(db, user):
    job = create_job(
        db,
        title="Backend Engineer",
        department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="JD.",
        created_by_user_id=user.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    return job, link


def _application(db, job, link, *, name: str, created_at: datetime | None = None):
    """One application with a deterministic ``created_at``.

    Postgres ``now()`` is constant inside a transaction, so without the explicit
    UPDATE every row here would share a timestamp and ordering assertions would
    be meaningless.
    """
    app = create_application(
        db,
        job_id=job.id,
        application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com",
        full_name=name,
        phone=None,
    )
    if created_at is not None:
        db.execute(
            update(Application)
            .where(Application.id == app.id)
            .values(created_at=created_at)
        )
        db.flush()
        db.refresh(app)
    return app


# --- backward compatibility ------------------------------------------


def test_defaults_are_unchanged_all_applications_newest_first(db):
    """With no keyword arguments this behaves exactly as it always has."""
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    older = _application(db, job, link, name="Older", created_at=_T0)
    newer = _application(
        db, job, link, name="Newer", created_at=_T0 + timedelta(hours=1)
    )

    got = list_applications_for_job(db, job.id)

    assert [a.id for a in got] == [newer.id, older.id]


def test_still_scoped_to_the_requested_job(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    other_job, other_link = _job_with_link(db, user)
    mine = _application(db, job, link, name="Mine")
    _application(db, other_job, other_link, name="Theirs")

    assert [a.id for a in list_applications_for_job(db, job.id)] == [mine.id]


# --- limit / offset paging -------------------------------------------


def test_pagination_pages_are_distinct_and_cover_everything(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    for i in range(7):
        _application(
            db, job, link, name=f"A{i}", created_at=_T0 + timedelta(hours=i)
        )

    page1 = list_applications_for_job(db, job.id, limit=3)
    page2 = list_applications_for_job(db, job.id, limit=3, offset=3)
    page3 = list_applications_for_job(db, job.id, limit=3, offset=6)

    assert len(page1) == 3 and len(page2) == 3 and len(page3) == 1
    ids = [a.id for a in page1 + page2 + page3]
    assert len(ids) == len(set(ids)) == 7  # no overlap, nothing skipped
    # and the whole sequence is still newest-first
    assert ids == [a.id for a in list_applications_for_job(db, job.id)]


def test_pagination_is_stable_when_timestamps_tie(db):
    """All rows share created_at (the norm inside one transaction). The id
    tie-break must still give a total order so paging cannot repeat or drop."""
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    for i in range(6):
        _application(db, job, link, name=f"T{i}", created_at=_T0)

    first = list_applications_for_job(db, job.id, limit=2)
    second = list_applications_for_job(db, job.id, limit=2, offset=2)
    third = list_applications_for_job(db, job.id, limit=2, offset=4)

    ids = [a.id for a in first + second + third]
    assert len(ids) == len(set(ids)) == 6


def test_offset_beyond_the_end_returns_empty(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    _application(db, job, link, name="Only")

    assert list_applications_for_job(db, job.id, limit=5, offset=50) == []


def test_limit_plus_one_probe_detects_another_page(db):
    """The Candidates tab uses limit=shown+1 to decide whether to offer
    "Show more" without a second COUNT query."""
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    for i in range(4):
        _application(
            db, job, link, name=f"P{i}", created_at=_T0 + timedelta(hours=i)
        )

    assert len(list_applications_for_job(db, job.id, limit=3 + 1)) == 4
    assert len(list_applications_for_job(db, job.id, limit=10 + 1)) == 4


# --- count_applications_for_job --------------------------------------


def test_count_matches_the_unpaginated_list(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    for i in range(5):
        _application(db, job, link, name=f"C{i}")

    assert count_applications_for_job(db, job.id) == 5
    assert count_applications_for_job(db, job.id) == len(
        list_applications_for_job(db, job.id)
    )


def test_count_is_unaffected_by_paging_and_by_other_jobs(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    other_job, other_link = _job_with_link(db, user)
    for i in range(3):
        _application(db, job, link, name=f"M{i}")
    _application(db, other_job, other_link, name="Other")

    assert count_applications_for_job(db, job.id) == 3
    assert count_applications_for_job(db, other_job.id) == 1


def test_count_is_zero_for_a_job_with_no_applications(db):
    user = _hr_user(db)
    job, _link = _job_with_link(db, user)

    assert count_applications_for_job(db, job.id) == 0
