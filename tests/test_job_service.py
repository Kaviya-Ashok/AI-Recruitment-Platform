"""Tests for app.services.job_service — real Postgres via the rollback fixture."""

from __future__ import annotations

import re
import threading
import uuid

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select, update

from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.job import JdInputMethod, Job, JobStatus
from app.database.models.job_requirement import JobRequirement
from app.database.models.user import UserRole
from app.services.auth_service import create_user
from app.services.job_service import (
    JobValidationError,
    create_job,
    get_job,
    list_jobs,
)


def _hr_user(db):
    return create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )


def _count_job_created_events(db, job_id) -> int:
    return db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_type == "job",
            AuditEvent.entity_id == job_id,
            AuditEvent.event_type == AuditEventType.JOB_CREATED.value,
        )
    ).scalar_one()


# --- TEXT_PASTE ---------------------------------------------------------


def test_create_job_text_paste(db):
    user = _hr_user(db)
    jd = "  Senior Platform Engineer. Must know Kubernetes and Go.  "

    job = create_job(
        db,
        title="Platform Engineer",
        department="Infrastructure",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text=jd,
        created_by_user_id=user.id,
    )

    assert job.id is not None
    assert job.status == JobStatus.DRAFT
    assert job.jd_input_method is JdInputMethod.TEXT_PASTE
    assert job.jd_original_filename is None
    assert job.jd_source_text == jd.strip()
    assert job.created_by == user.id


def test_create_job_text_paste_accepts_string_method(db):
    user = _hr_user(db)
    job = create_job(
        db,
        title="Analyst",
        department=None,
        jd_input_method="TEXT_PASTE",
        jd_source_text="Data analyst role, SQL required.",
        created_by_user_id=user.id,
    )
    assert job.jd_input_method is JdInputMethod.TEXT_PASTE
    assert job.department is None


# --- FILE_UPLOAD -------------------------------------------------------


def test_create_job_pdf_upload(db, sample_pdf_bytes):
    user = _hr_user(db)
    job = create_job(
        db,
        title="Data Engineer",
        department="Data",
        jd_input_method=JdInputMethod.FILE_UPLOAD,
        uploaded_file_bytes=sample_pdf_bytes,
        uploaded_filename="../secret/Senior Data Engineer JD.pdf",
        created_by_user_id=user.id,
    )

    assert job.jd_input_method is JdInputMethod.FILE_UPLOAD
    assert job.jd_original_filename == "Senior Data Engineer JD.pdf"  # sanitised
    assert "Senior Data Engineer" in job.jd_source_text


def test_create_job_docx_upload(db, sample_docx_bytes):
    user = _hr_user(db)
    job = create_job(
        db,
        title="Data Engineer",
        department="Data",
        jd_input_method=JdInputMethod.FILE_UPLOAD,
        uploaded_file_bytes=sample_docx_bytes,
        uploaded_filename="jd.docx",
        created_by_user_id=user.id,
    )
    assert job.jd_original_filename == "jd.docx"
    assert "PySpark" in job.jd_source_text


# --- validation / failure paths --------------------------------------


@pytest.mark.parametrize("jd", ["", "   ", "\n\t  \n"])
def test_create_job_empty_paste_rejected_and_no_row(db, jd):
    user = _hr_user(db)
    before = db.execute(select(func.count()).select_from(Job)).scalar_one()

    with pytest.raises(JobValidationError):
        create_job(
            db,
            title="Ghost Job",
            department=None,
            jd_input_method=JdInputMethod.TEXT_PASTE,
            jd_source_text=jd,
            created_by_user_id=user.id,
        )

    after = db.execute(select(func.count()).select_from(Job)).scalar_one()
    assert after == before


def test_create_job_missing_title_rejected(db):
    user = _hr_user(db)
    with pytest.raises(JobValidationError):
        create_job(
            db,
            title="   ",
            department=None,
            jd_input_method=JdInputMethod.TEXT_PASTE,
            jd_source_text="valid jd text",
            created_by_user_id=user.id,
        )


def test_create_job_upload_without_file_rejected(db):
    user = _hr_user(db)
    with pytest.raises(JobValidationError):
        create_job(
            db,
            title="No File",
            department=None,
            jd_input_method=JdInputMethod.FILE_UPLOAD,
            uploaded_file_bytes=None,
            uploaded_filename=None,
            created_by_user_id=user.id,
        )


# --- audit -----------------------------------------------------------


def test_create_job_writes_exactly_one_audit_event(db, sample_pdf_bytes):
    user = _hr_user(db)
    job = create_job(
        db,
        title="Audited Job",
        department="QA",
        jd_input_method=JdInputMethod.FILE_UPLOAD,
        uploaded_file_bytes=sample_pdf_bytes,
        uploaded_filename="jd.pdf",
        created_by_user_id=user.id,
    )

    assert _count_job_created_events(db, job.id) == 1

    event = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_type == "job", AuditEvent.entity_id == job.id
        )
    ).scalar_one()
    assert event.event_type == AuditEventType.JOB_CREATED.value
    assert event.user_id == user.id
    assert event.new_state["status"] == JobStatus.DRAFT
    # JD text must NOT be duplicated into the audit row.
    assert "jd_source_text" not in event.new_state
    assert job.jd_source_text not in (event.action or "")


# --- get_job / list_jobs -------------------------------------------


def test_get_job(db):
    user = _hr_user(db)
    job = create_job(
        db,
        title="Findable",
        department=None,
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="jd",
        created_by_user_id=user.id,
    )
    assert get_job(db, job.id).id == job.id
    assert get_job(db, uuid.uuid4()) is None


def test_list_jobs_newest_first_and_filter(db):
    user_a = _hr_user(db)
    user_b = _hr_user(db)

    j1 = create_job(
        db, title="A1", department=None,
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="jd",
        created_by_user_id=user_a.id,
    )
    j2 = create_job(
        db, title="B1", department=None,
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="jd",
        created_by_user_id=user_b.id,
    )

    # Postgres now() is constant within a transaction, so both rows share a
    # created_at here. Push j1 into the past to get a deterministic ordering.
    db.execute(
        update(Job)
        .where(Job.id == j1.id)
        .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    db.flush()

    all_ids = [j.id for j in list_jobs(db)]
    assert j1.id in all_ids and j2.id in all_ids
    assert all_ids.index(j2.id) < all_ids.index(j1.id)  # newest first

    mine = list_jobs(db, created_by_user_id=user_a.id)
    assert [j.id for j in mine] == [j1.id]


def test_create_job_does_not_populate_job_requirements(db):
    # Phase 1.1's create_job must not create any requirement rows for the job
    # it creates (requirements come from analyze_jd in Phase 1.2). Scoped to
    # this job — the shared DB may hold real requirements from other jobs.
    user = _hr_user(db)
    job = create_job(
        db,
        title="No-Requirements Job",
        department=None,
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="A JD that has not been analysed.",
        created_by_user_id=user.id,
    )
    assert db.execute(
        select(func.count())
        .select_from(JobRequirement)
        .where(JobRequirement.job_id == job.id)
    ).scalar_one() == 0


# --- job_code (migration f7a8b9c0d1e2) -----------------------------------
#
# Codes come from a Postgres sequence evaluated by a column DEFAULT. A sequence
# is global and non-transactional, so these tests never assume an ABSOLUTE
# number (other tests, and rolled-back runs, advance it) - only format,
# uniqueness and "next = previous + 1".

_CODE_RE = re.compile(r"^V_\d{3,}$")


def _make_job(db, user, title="Platform Engineer"):
    return create_job(
        db,
        title=title,
        department=None,
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="A JD.",
        created_by_user_id=user.id,
    )


def _code_number(job) -> int:
    return int(job.job_code.removeprefix("V_"))


def test_create_job_assigns_a_correctly_formatted_code(db):
    job = _make_job(db, _hr_user(db))
    assert _CODE_RE.match(job.job_code), job.job_code


def test_codes_are_sequential_and_unique_back_to_back(db):
    user = _hr_user(db)
    jobs = [_make_job(db, user, title=f"Role {i}") for i in range(5)]

    numbers = [_code_number(j) for j in jobs]
    assert numbers == list(range(numbers[0], numbers[0] + 5))   # +1 each time
    assert len({j.job_code for j in jobs}) == 5


def test_code_is_zero_padded_to_at_least_three_digits(db):
    job = _make_job(db, _hr_user(db))
    digits = job.job_code.removeprefix("V_")
    assert len(digits) >= 3
    assert digits == f"{int(digits):03d}"      # pads to 3, never truncates


def test_a_directly_constructed_job_also_gets_a_code(db):
    """The code is a column DEFAULT, not Python-side: a bare ``Job(...)`` (as
    ``test_analyze_jd_empty_jd_raises`` builds) must not violate NOT NULL."""
    user = _hr_user(db)
    job = Job(
        title="Direct",
        department=None,
        status=JobStatus.DRAFT,
        jd_source_text="x",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        created_by=user.id,
    )
    db.add(job)
    db.flush()
    assert _CODE_RE.match(job.job_code)


def test_job_code_is_unique_at_the_database_level(db):
    user = _hr_user(db)
    first = _make_job(db, user)
    clash = Job(
        title="Clash", department=None, status=JobStatus.DRAFT,
        jd_source_text="x", jd_input_method=JdInputMethod.TEXT_PASTE,
        created_by=user.id, job_code=first.job_code,
    )
    db.add(clash)
    with pytest.raises(sa.exc.IntegrityError):
        with db.begin_nested():
            db.flush()


def test_the_code_is_never_changed_by_later_updates(db):
    job = _make_job(db, _hr_user(db))
    before = job.job_code
    job.status = JobStatus.JD_ANALYZED
    db.flush()
    db.refresh(job)
    assert job.job_code == before


def test_create_job_audit_event_carries_the_job_code(db):
    job = _make_job(db, _hr_user(db))
    event = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_type == "job", AuditEvent.entity_id == job.id
        )
    ).scalar_one()
    assert event.new_state["job_code"] == job.job_code


def test_concurrent_code_generation_never_hands_out_a_duplicate():
    """Real concurrency, on separate connections: ``next_job_code()`` is
    sequence-backed, so simultaneous callers can never receive the same value.
    Only a SELECT - no job rows are written to the shared dev database."""
    import app.database.database as app_db

    results: list[str] = []
    lock = threading.Lock()

    def worker():
        with app_db.engine.connect() as conn:
            for _ in range(10):
                code = conn.execute(sa.text("SELECT next_job_code()")).scalar_one()
                with lock:
                    results.append(code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 40
    assert len(set(results)) == 40
    assert all(_CODE_RE.match(c) for c in results)
