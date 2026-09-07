"""Tests for app.services.application_service (Phase 2).

No AI in this step — real Postgres via the savepoint-rollback ``db`` fixture.
The application flow only needs a job with an ACTIVE application link, so tests
drive ``application_link_service.generate_link`` (which requires an approved
rubric state) by setting ``job.status`` directly, mirroring
``test_application_link_service``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.user import UserRole
from app.services.application_link_service import generate_link
from app.services.application_service import (
    ApplicationTargetNotFoundError,
    DuplicateApplicationError,
    create_application,
    get_application,
    list_applications_for_job,
)
from app.services.auth_service import create_user
from app.services.candidate_service import get_candidate_by_email
from app.services.job_service import create_job


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
        title="Data Engineer",
        department="Data",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    return job, link


def _applicant(**over):
    base = dict(
        email=f"cand-{uuid.uuid4().hex}@example.com",
        full_name="Casey Candidate",
        phone="555-0100",
    )
    base.update(over)
    return base


# --- create_application: happy path ----------------------------------


def test_create_application_success(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    fields = _applicant()

    app_row = create_application(
        db, job_id=job.id, application_link_id=link.id, **fields
    )

    assert app_row.id is not None
    assert app_row.status == ApplicationStatus.APPLIED
    assert app_row.job_id == job.id
    assert app_row.application_link_id == link.id

    candidate = get_candidate_by_email(db, fields["email"])
    assert candidate is not None
    assert app_row.candidate_id == candidate.id


def test_create_application_writes_audit_event_same_transaction(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    fields = _applicant()

    app_row = create_application(
        db, job_id=job.id, application_link_id=link.id, **fields
    )

    ev = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == app_row.id,
            AuditEvent.event_type == AuditEventType.CANDIDATE_APPLIED.value,
        )
    ).scalars().one()
    assert ev.entity_type == "application"
    assert ev.user_id is None  # unauthenticated public flow
    assert ev.new_state["status"] == ApplicationStatus.APPLIED


def test_create_application_reuses_existing_candidate(db):
    user = _hr_user(db)
    job_a, link_a = _job_with_link(db, user)
    job_b, link_b = _job_with_link(db, user)
    email = f"reuse-{uuid.uuid4().hex}@example.com"

    a1 = create_application(
        db, job_id=job_a.id, application_link_id=link_a.id,
        email=email, full_name="Same Person", phone="1",
    )
    a2 = create_application(
        db, job_id=job_b.id, application_link_id=link_b.id,
        email=email.upper(), full_name="Same Person", phone="1",
    )

    assert a1.candidate_id == a2.candidate_id
    n = db.execute(
        select(func.count()).select_from(Application).where(
            Application.candidate_id == a1.candidate_id
        )
    ).scalar_one()
    assert n == 2


# --- create_application: duplicate ---------------------------------


def test_duplicate_application_raises(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    fields = _applicant()

    create_application(db, job_id=job.id, application_link_id=link.id, **fields)

    with pytest.raises(DuplicateApplicationError):
        create_application(
            db, job_id=job.id, application_link_id=link.id, **fields
        )

    # only one application row for that (candidate, job)
    candidate = get_candidate_by_email(db, fields["email"])
    n = db.execute(
        select(func.count()).select_from(Application).where(
            Application.candidate_id == candidate.id,
            Application.job_id == job.id,
        )
    ).scalar_one()
    assert n == 1


# --- create_application: FK / target integrity -------------------


def test_unknown_job_raises(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    with pytest.raises(ApplicationTargetNotFoundError):
        create_application(
            db, job_id=uuid.uuid4(), application_link_id=link.id, **_applicant()
        )


def test_unknown_application_link_raises(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    with pytest.raises(ApplicationTargetNotFoundError):
        create_application(
            db, job_id=job.id, application_link_id=uuid.uuid4(), **_applicant()
        )


def test_link_belonging_to_other_job_raises(db):
    user = _hr_user(db)
    job_a, link_a = _job_with_link(db, user)
    job_b, link_b = _job_with_link(db, user)
    with pytest.raises(ApplicationTargetNotFoundError):
        create_application(
            db, job_id=job_a.id, application_link_id=link_b.id, **_applicant()
        )


def test_no_application_or_candidate_row_on_target_failure(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    fields = _applicant()
    apps_before = db.execute(
        select(func.count()).select_from(Application)
    ).scalar_one()

    with pytest.raises(ApplicationTargetNotFoundError):
        create_application(
            db, job_id=job.id, application_link_id=uuid.uuid4(), **fields
        )

    assert db.execute(
        select(func.count()).select_from(Application)
    ).scalar_one() == apps_before
    assert get_candidate_by_email(db, fields["email"]) is None


# --- reads ------------------------------------------------------


def test_get_application(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    made = create_application(
        db, job_id=job.id, application_link_id=link.id, **_applicant()
    )
    assert get_application(db, made.id).id == made.id
    assert get_application(db, uuid.uuid4()) is None


def test_list_applications_for_job(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    other_job, other_link = _job_with_link(db, user)

    a1 = create_application(
        db, job_id=job.id, application_link_id=link.id, **_applicant()
    )
    a2 = create_application(
        db, job_id=job.id, application_link_id=link.id, **_applicant()
    )
    create_application(
        db, job_id=other_job.id, application_link_id=other_link.id, **_applicant()
    )

    got = list_applications_for_job(db, job.id)
    assert {a.id for a in got} == {a1.id, a2.id}


# --- privacy: no PII in audit metadata --------------------------


def test_candidate_applied_audit_has_no_pii(db):
    user = _hr_user(db)
    job, link = _job_with_link(db, user)
    email = "pii-check-person@example.com"
    full_name = "Very Unique Personname"
    phone = "555-987-6543"

    app_row = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=email, full_name=full_name, phone=phone,
    )

    ev = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == app_row.id,
            AuditEvent.event_type == AuditEventType.CANDIDATE_APPLIED.value,
        )
    ).scalars().one()

    blob = (
        f"{ev.action} || {ev.previous_state} || {ev.new_state} || "
        f"{ev.event_metadata}"
    )
    for pii in (email, full_name, phone, "pii-check-person", "987-6543"):
        assert pii not in blob, f"PII leaked into audit event: {blob!r}"

    # metadata references entities by id only
    assert ev.event_metadata == {
        "application_id": str(app_row.id),
        "candidate_id": str(app_row.candidate_id),
        "job_id": str(job.id),
        "application_link_id": str(link.id),
    }
