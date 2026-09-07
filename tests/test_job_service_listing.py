"""Tests for the read-side listing extensions on app.services.job_service
(status filter, search, sort, limit/offset paging, and count_jobs_by_status).

These were added for the Jobs-page tabs/search/sort/"Show more" redesign. They
are read-only: nothing here changes a job's status or any business rule.

ISOLATION: the suite runs against the shared development database, which can
contain jobs created by hand outside pytest. Every assertion below is therefore
scoped with ``created_by_user_id`` to a user this test just created, so leftover
rows can never affect a count or an ordering.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.database.models.job import JdInputMethod, Job, JobStatus
from app.database.models.user import UserRole
from app.services.auth_service import create_user
from app.services.job_service import (
    JobSort,
    count_jobs_by_status,
    create_job,
    list_jobs,
)

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _hr_user(db):
    return create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )


def _job(
    db,
    user,
    *,
    title: str,
    department: str | None = None,
    status: str = JobStatus.DRAFT,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Job:
    """One job owned by ``user``, with deterministic timestamps.

    Postgres ``now()`` is constant inside a transaction, so every row created by
    one test would otherwise share a ``created_at``; the explicit UPDATE is what
    makes ordering assertions meaningful.
    """
    job = create_job(
        db,
        title=title,
        department=department,
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="jd text",
        created_by_user_id=user.id,
    )
    job.status = status
    db.flush()
    values = {}
    if created_at is not None:
        values["created_at"] = created_at
    if updated_at is not None:
        values["updated_at"] = updated_at
    if values:
        db.execute(update(Job).where(Job.id == job.id).values(**values))
        db.flush()
        db.refresh(job)
    return job


def _titles(jobs) -> list[str]:
    return [j.title for j in jobs]


# --- backward compatibility -------------------------------------------


def test_defaults_are_unchanged_all_jobs_newest_first(db):
    """Called with no new arguments, list_jobs behaves exactly as before."""
    user = _hr_user(db)
    old = _job(db, user, title="Older", created_at=_T0)
    new = _job(db, user, title="Newer", created_at=_T0 + timedelta(hours=1))

    rows = list_jobs(db, created_by_user_id=user.id)

    assert _titles(rows) == ["Newer", "Older"]
    assert {j.id for j in rows} == {old.id, new.id}


def test_no_status_filter_returns_every_status(db):
    user = _hr_user(db)
    _job(db, user, title="D", status=JobStatus.DRAFT, created_at=_T0)
    _job(db, user, title="O", status=JobStatus.OPEN, created_at=_T0)
    _job(db, user, title="C", status=JobStatus.CLOSED, created_at=_T0)

    assert len(list_jobs(db, created_by_user_id=user.id)) == 3


# --- status filter ---------------------------------------------------


def test_status_filter_selects_only_those_statuses(db):
    user = _hr_user(db)
    _job(db, user, title="Draft one", status=JobStatus.DRAFT)
    _job(db, user, title="Analyzed", status=JobStatus.JD_ANALYZED)
    _job(db, user, title="Open one", status=JobStatus.OPEN)
    _job(db, user, title="Closed one", status=JobStatus.CLOSED)

    open_only = list_jobs(
        db, created_by_user_id=user.id, statuses={JobStatus.OPEN}
    )
    assert _titles(open_only) == ["Open one"]

    setup_group = list_jobs(
        db,
        created_by_user_id=user.id,
        statuses={JobStatus.DRAFT, JobStatus.JD_ANALYZED},
    )
    assert sorted(_titles(setup_group)) == ["Analyzed", "Draft one"]


def test_empty_status_iterable_matches_nothing(db):
    """An explicit empty set means 'none of these', not 'no filter'."""
    user = _hr_user(db)
    _job(db, user, title="Anything", status=JobStatus.OPEN)

    assert list_jobs(db, created_by_user_id=user.id, statuses=[]) == []


def test_status_groups_used_by_the_jobs_page_partition_every_status(db):
    """Each of the 7 JobStatus values lands in exactly one of the page's three
    tab groups — no job can become invisible."""
    from app.pages.jobs import _TABS

    union: set[str] = set()
    for _, _, statuses in _TABS:
        assert not (union & statuses), "tab groups must not overlap"
        union |= set(statuses)
    assert union == set(JobStatus.ALL)


# --- search ---------------------------------------------------------


def test_search_is_case_insensitive_and_partial_on_title(db):
    user = _hr_user(db)
    _job(db, user, title="Senior Backend Engineer")
    _job(db, user, title="Data Analyst")

    for term in ("backend", "BACKEND", "BaCkEnD", "end Eng"):
        assert _titles(
            list_jobs(db, created_by_user_id=user.id, search=term)
        ) == ["Senior Backend Engineer"], term


def test_search_matches_department_too(db):
    user = _hr_user(db)
    _job(db, user, title="Analyst", department="Finance")
    _job(db, user, title="Engineer", department="Platform")

    assert _titles(
        list_jobs(db, created_by_user_id=user.id, search="finan")
    ) == ["Analyst"]


def test_search_on_title_still_matches_when_department_is_null(db):
    """department is nullable — an OR against NULL must not drop the row."""
    user = _hr_user(db)
    _job(db, user, title="Nulldept Engineer", department=None)

    assert _titles(
        list_jobs(db, created_by_user_id=user.id, search="nulldept")
    ) == ["Nulldept Engineer"]


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_search_is_ignored(db, blank):
    user = _hr_user(db)
    _job(db, user, title="A", created_at=_T0)
    _job(db, user, title="B", created_at=_T0 + timedelta(hours=1))

    assert len(list_jobs(db, created_by_user_id=user.id, search=blank)) == 2


def test_search_escapes_like_wildcards(db):
    """A typed % or _ is literal text, not a wildcard."""
    user = _hr_user(db)
    _job(db, user, title="100% Remote Role")
    _job(db, user, title="Onsite Role")

    assert _titles(
        list_jobs(db, created_by_user_id=user.id, search="100%")
    ) == ["100% Remote Role"]
    # A bare '%' would match everything if it were treated as a wildcard.
    assert list_jobs(db, created_by_user_id=user.id, search="%zzz%") == []
    assert list_jobs(db, created_by_user_id=user.id, search="_nsite") == []


# --- sort -----------------------------------------------------------


def test_sort_newest_and_oldest(db):
    user = _hr_user(db)
    _job(db, user, title="First", created_at=_T0)
    _job(db, user, title="Second", created_at=_T0 + timedelta(hours=1))
    _job(db, user, title="Third", created_at=_T0 + timedelta(hours=2))

    assert _titles(
        list_jobs(db, created_by_user_id=user.id, sort=JobSort.NEWEST)
    ) == ["Third", "Second", "First"]
    assert _titles(
        list_jobs(db, created_by_user_id=user.id, sort=JobSort.OLDEST)
    ) == ["First", "Second", "Third"]


def test_sort_recently_updated_uses_updated_at_not_created_at(db):
    user = _hr_user(db)
    # Created newest-first as A, B — but B was touched most recently.
    _job(
        db, user, title="A",
        created_at=_T0 + timedelta(hours=2), updated_at=_T0,
    )
    _job(
        db, user, title="B",
        created_at=_T0, updated_at=_T0 + timedelta(hours=5),
    )

    assert _titles(
        list_jobs(db, created_by_user_id=user.id, sort=JobSort.NEWEST)
    ) == ["A", "B"]
    assert _titles(
        list_jobs(db, created_by_user_id=user.id, sort=JobSort.RECENTLY_UPDATED)
    ) == ["B", "A"]


def test_unknown_sort_falls_back_to_newest_without_raising(db):
    user = _hr_user(db)
    _job(db, user, title="Old", created_at=_T0)
    _job(db, user, title="New", created_at=_T0 + timedelta(hours=1))

    assert _titles(
        list_jobs(db, created_by_user_id=user.id, sort="NOT_A_SORT")
    ) == ["New", "Old"]


# --- limit / offset paging ------------------------------------------


def test_pagination_pages_are_distinct_and_cover_everything(db):
    user = _hr_user(db)
    for i in range(7):
        _job(db, user, title=f"J{i}", created_at=_T0 + timedelta(hours=i))

    page1 = list_jobs(db, created_by_user_id=user.id, limit=3)
    page2 = list_jobs(db, created_by_user_id=user.id, limit=3, offset=3)
    page3 = list_jobs(db, created_by_user_id=user.id, limit=3, offset=6)

    assert _titles(page1) == ["J6", "J5", "J4"]
    assert _titles(page2) == ["J3", "J2", "J1"]
    assert _titles(page3) == ["J0"]

    ids = [j.id for j in page1 + page2 + page3]
    assert len(ids) == len(set(ids)) == 7  # no overlap, nothing skipped


def test_pagination_is_stable_when_timestamps_tie(db):
    """All rows share created_at (the common case inside one transaction). The
    id tie-break must still give a total order, so paging cannot repeat or drop
    a row."""
    user = _hr_user(db)
    for i in range(6):
        _job(db, user, title=f"T{i}", created_at=_T0)

    first = list_jobs(db, created_by_user_id=user.id, limit=2)
    second = list_jobs(db, created_by_user_id=user.id, limit=2, offset=2)
    third = list_jobs(db, created_by_user_id=user.id, limit=2, offset=4)

    ids = [j.id for j in first + second + third]
    assert len(ids) == len(set(ids)) == 6


def test_offset_beyond_the_end_returns_empty(db):
    user = _hr_user(db)
    _job(db, user, title="Only")

    assert list_jobs(db, created_by_user_id=user.id, limit=5, offset=50) == []


def test_limit_plus_one_probe_detects_more_pages(db):
    """The page uses limit=shown+1 to decide whether to show 'Show more'."""
    user = _hr_user(db)
    for i in range(4):
        _job(db, user, title=f"P{i}", created_at=_T0 + timedelta(hours=i))

    assert len(list_jobs(db, created_by_user_id=user.id, limit=3 + 1)) == 4
    assert len(list_jobs(db, created_by_user_id=user.id, limit=10 + 1)) == 4


# --- combined -------------------------------------------------------


def test_status_search_sort_and_paging_compose(db):
    user = _hr_user(db)
    for i in range(5):
        _job(
            db, user, title=f"Open Engineer {i}", status=JobStatus.OPEN,
            department="Platform", created_at=_T0 + timedelta(hours=i),
        )
    _job(db, user, title="Open Analyst", status=JobStatus.OPEN)
    _job(db, user, title="Draft Engineer", status=JobStatus.DRAFT)

    rows = list_jobs(
        db,
        created_by_user_id=user.id,
        statuses={JobStatus.OPEN},
        search="engineer",
        sort=JobSort.OLDEST,
        limit=2,
        offset=1,
    )
    assert _titles(rows) == ["Open Engineer 1", "Open Engineer 2"]


# --- count_jobs_by_status -------------------------------------------


def test_count_jobs_by_status_groups_correctly(db):
    user = _hr_user(db)
    _job(db, user, title="d1", status=JobStatus.DRAFT)
    _job(db, user, title="d2", status=JobStatus.DRAFT)
    _job(db, user, title="o1", status=JobStatus.OPEN)

    counts = count_jobs_by_status(db, created_by_user_id=user.id)

    assert counts == {JobStatus.DRAFT: 2, JobStatus.OPEN: 1}
    # A status with no rows is simply absent, and .get() yields 0.
    assert counts.get(JobStatus.CLOSED, 0) == 0


def test_count_jobs_by_status_matches_list_jobs_for_each_status(db):
    user = _hr_user(db)
    _job(db, user, title="a", status=JobStatus.OPEN)
    _job(db, user, title="b", status=JobStatus.OPEN)
    _job(db, user, title="c", status=JobStatus.CLOSED)

    counts = count_jobs_by_status(db, created_by_user_id=user.id)
    for status in (JobStatus.OPEN, JobStatus.CLOSED, JobStatus.ARCHIVED):
        listed = list_jobs(
            db, created_by_user_id=user.id, statuses={status}
        )
        assert counts.get(status, 0) == len(listed), status
