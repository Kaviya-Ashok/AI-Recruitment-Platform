"""Tests for app.services.job_service — real Postgres via the rollback fixture."""

from __future__ import annotations

import uuid

from datetime import datetime, timedelta, timezone

import pytest
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
