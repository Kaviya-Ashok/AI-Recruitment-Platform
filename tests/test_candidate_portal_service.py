"""Tests for the Phase 1.5 candidate portal (service layer only — no Streamlit).

Covers app/services/candidate_portal_service.py and the small
get_job_for_valid_token wrapper added to application_link_service.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.database.models.application import Application
from app.database.models.application_link import ApplicationLinkStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.document import Document
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.user import UserRole
from app.services import storage_service
from app.services.application_link_service import (
    close_job,
    generate_link,
    get_job_for_valid_token,
    revoke_link,
)
from app.services.application_service import (
    ApplicationTargetNotFoundError,
    get_application,
)
from app.services.auth_service import create_user
from app.services.candidate_portal_service import (
    PortalOutcome,
    SUBMISSION_FAILURE_FALLBACK,
    SubmissionOutcome,
    get_application_contact,
    submission_failure_message,
    looks_like_token,
    retry_resume_upload,
    submit_application_via_token,
    view_job_via_token,
)
from app.services.job_service import create_job
from app.services.storage_service import DriveUploadError

_RUBRIC_CRITERIA = [
    ("MANDATORY", "Knowledge & Compliance", "Solid grasp of core employment law"),
    ("MANDATORY", "Technical Skill", "Hands-on with an HRIS platform"),
    ("PREFERRED", "Certification", "PHR or SHRM-CP certification"),
    ("EXPERIENCE", None, "2-4 years in a People Operations role"),
    ("BEHAVIORAL", "Interpersonal", "Handles sensitive conversations with care"),
    ("OTHER", "Logistics", "Comfortable overlapping US Eastern hours"),
]

_ALL_CRITERION_TEXTS = {c[2] for c in _RUBRIC_CRITERIA}


def _hr_user(db):
    return create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )


def _job_with_approved_rubric(db, user, *, with_rubric: bool = True):
    """Create a job + (optionally) a directly-built APPROVED rubric."""
    job = create_job(
        db,
        title="HR Generalist",
        department="People",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()

    if with_rubric:
        rv = RubricVersion(
            job_id=job.id,
            version_number=1,
            status=RubricVersionStatus.APPROVED,
            generated_from_requirements_version=1,
            created_by=user.id,
            approved_by=user.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(rv)
        db.flush()
        for i, (rtype, cat, text) in enumerate(_RUBRIC_CRITERIA, start=1):
            db.add(
                RubricCriterion(
                    rubric_version_id=rv.id,
                    requirement_type=rtype,
                    category=cat,
                    criterion_text=text,
                    display_order=i,
                )
            )
        db.flush()
    return job


def _open_with_link(db, user, *, with_rubric: bool = True):
    job = _job_with_approved_rubric(db, user, with_rubric=with_rubric)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    return job, link


# --- looks_like_token -------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "short",
        "has spaces in it aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "has/slash/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "'; DROP TABLE application_links; --aaaaaaaaaaaaaaaa",
        "x" * 200,
        12345,
    ],
)
def test_looks_like_token_rejects_bad(bad):
    assert looks_like_token(bad) is False


def test_looks_like_token_accepts_real_token(db):
    user = _hr_user(db)
    _job, link = _open_with_link(db, user)
    assert looks_like_token(link.token) is True


# --- get_job_for_valid_token ----------------------------------------


def test_get_job_for_valid_token_valid(db):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)
    got = get_job_for_valid_token(db, link.token)
    assert got is not None and got.id == job.id


def test_get_job_for_valid_token_not_found(db):
    assert get_job_for_valid_token(db, "no-such-token-" + "a" * 40) is None


def test_get_job_for_valid_token_revoked_and_superseded(db):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)
    v2 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)  # supersedes link
    assert get_job_for_valid_token(db, link.token) is None  # SUPERSEDED
    revoke_link(db, link_id=v2.id, requested_by_user_id=user.id)
    assert get_job_for_valid_token(db, v2.token) is None  # REVOKED


def test_get_job_for_valid_token_closed_job(db):
    from app.services.application_link_service import close_job

    user = _hr_user(db)
    job, link = _open_with_link(db, user)
    close_job(db, job_id=job.id, requested_by_user_id=user.id)
    assert get_job_for_valid_token(db, link.token) is None


# --- view_job_via_token: happy path -------------------------------


def test_view_job_via_token_valid_builds_candidate_view(db):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)

    result = view_job_via_token(db, link.token)

    assert result.outcome == PortalOutcome.OK
    assert result.internal_reason == "VALID"
    view = result.view
    assert view.job_title == "HR Generalist"
    assert view.department == "People"
    assert view.details_available is True

    headings = [s.heading for s in view.sections]
    # plain-language headings, never internal type labels
    assert headings == [
        "What you'll need",
        "Nice to have",
        "Experience we're looking for",
        "How you work",
        "Also good to know",
    ]
    all_items = [item for s in view.sections for item in s.items]
    assert set(all_items) == _ALL_CRITERION_TEXTS
    blob = " ".join(headings + all_items)
    for internal in ("MANDATORY", "PREFERRED", "BEHAVIORAL", "Technical Skill"):
        assert internal not in blob


def test_view_job_via_token_writes_audit_without_token(db):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)

    view_job_via_token(db, link.token)

    events = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == link.id,
            AuditEvent.event_type == AuditEventType.APPLICATION_LINK_VIEWED.value,
        )
    ).scalars().all()
    assert len(events) == 1
    e = events[0]
    assert e.entity_type == "application_link"
    assert e.user_id is None
    assert e.event_metadata == {"job_id": str(job.id)}
    blob = f"{e.action} || {e.previous_state} || {e.new_state} || {e.event_metadata}"
    assert link.token not in blob


def test_view_job_via_token_multiple_views_multiple_events(db):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)
    view_job_via_token(db, link.token)
    view_job_via_token(db, link.token)
    n = db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_id == link.id,
            AuditEvent.event_type == AuditEventType.APPLICATION_LINK_VIEWED.value,
        )
    ).scalar_one()
    assert n == 2


# --- view_job_via_token: invalid paths --------------------------


def test_view_job_via_token_malformed_makes_no_db_call(db, mocker):
    spy = mocker.patch(
        "app.services.candidate_portal_service.resolve_link"
    )
    result = view_job_via_token(db, "not a valid token!!")
    assert result.outcome == PortalOutcome.MALFORMED_TOKEN
    assert result.internal_reason == "MALFORMED_TOKEN"
    assert result.view is None
    spy.assert_not_called()


def test_view_job_via_token_invalid_cases_uniform_outcome(db):
    user = _hr_user(db)

    # not found (well-formed but unknown)
    r_nf = view_job_via_token(db, "Zt" + "x" * 41)
    assert r_nf.outcome == PortalOutcome.INVALID_LINK
    assert r_nf.internal_reason == "NOT_FOUND"

    # revoked
    job, link = _open_with_link(db, user)
    revoke_link(db, link_id=link.id, requested_by_user_id=user.id)
    r_rev = view_job_via_token(db, link.token)
    assert r_rev.outcome == PortalOutcome.INVALID_LINK
    assert r_rev.internal_reason == "INACTIVE"

    # superseded
    job2, l1 = _open_with_link(db, user)
    generate_link(db, job_id=job2.id, requested_by_user_id=user.id)
    r_sup = view_job_via_token(db, l1.token)
    assert r_sup.outcome == PortalOutcome.INVALID_LINK
    assert r_sup.internal_reason == "INACTIVE"

    # closed job
    from app.services.application_link_service import close_job

    job3, l3 = _open_with_link(db, user)
    close_job(db, job_id=job3.id, requested_by_user_id=user.id)
    r_closed = view_job_via_token(db, l3.token)
    assert r_closed.outcome == PortalOutcome.INVALID_LINK
    assert r_closed.internal_reason == "JOB_CLOSED"

    # every invalid case has the same candidate-facing outcome
    assert {
        r_nf.outcome, r_rev.outcome, r_sup.outcome, r_closed.outcome
    } == {PortalOutcome.INVALID_LINK}


def test_view_job_via_token_invalid_writes_no_audit_event(db):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)
    revoke_link(db, link_id=link.id, requested_by_user_id=user.id)

    before = db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == AuditEventType.APPLICATION_LINK_VIEWED.value)
    ).scalar_one()

    view_job_via_token(db, link.token)  # INACTIVE

    after = db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == AuditEventType.APPLICATION_LINK_VIEWED.value)
    ).scalar_one()
    assert after == before


# --- defensive: no approved rubric --------------------------------


def test_view_job_via_token_no_approved_rubric(db):
    """Not reachable via a real link (generation requires an approved rubric),
    but the code must not crash — show a details-unavailable view, still audit."""
    user = _hr_user(db)
    job, link = _open_with_link(db, user, with_rubric=False)

    result = view_job_via_token(db, link.token)

    assert result.outcome == PortalOutcome.OK
    assert result.view.details_available is False
    assert result.view.job_title == "HR Generalist"
    assert result.view.sections == []
    # a valid link was still viewed -> audit event still written
    n = db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_id == link.id,
            AuditEvent.event_type == AuditEventType.APPLICATION_LINK_VIEWED.value,
        )
    ).scalar_one()
    assert n == 1


# =====================================================================
# Phase 2 — candidate application submission orchestrators
# =====================================================================

_RESUME_PDF = b"%PDF-1.4\n% tiny stub resume\n"
_PDF_MIME = "application/pdf"

# PII that must never appear in any audit row written by these flows.
_CAND_NAME = "Casey Uniquename Candidate"
_CAND_EMAIL = "casey.uniquename@example.com"
_CAND_PHONE = "555-0142"


class _FakeDrive:
    """In-memory stand-in for storage_service's four _drive_* seams (same shape
    as the one in tests/test_storage_service.py)."""

    def __init__(self) -> None:
        self.folders: dict[str, str] = {}
        self.files: dict[str, tuple[str, bytes, str]] = {}
        self.upload_calls = 0
        self.fail_upload = False

    def find_folder(self, service, root_folder_id, name):
        return self.folders.get(name)

    def create_folder(self, service, root_folder_id, name):
        fid = f"folder-{len(self.folders) + 1}"
        self.folders[name] = fid
        return fid

    def upload_file(self, service, folder_id, name, file_bytes, mime_type):
        self.upload_calls += 1
        if self.fail_upload:
            raise DriveUploadError("Drive upload failed: simulated.")
        fid = f"file-{len(self.files) + 1}"
        self.files[fid] = (name, file_bytes, mime_type)
        return fid, len(file_bytes)

    def download_file(self, service, drive_file_id):
        return self.files[drive_file_id][1]


@pytest.fixture
def fake_drive(mocker):
    fake = _FakeDrive()
    mocker.patch.object(
        storage_service, "_get_drive", return_value=(object(), "root-test")
    )
    mocker.patch.object(storage_service, "_drive_find_folder", fake.find_folder)
    mocker.patch.object(storage_service, "_drive_create_folder", fake.create_folder)
    mocker.patch.object(storage_service, "_drive_upload_file", fake.upload_file)
    mocker.patch.object(storage_service, "_drive_download_file", fake.download_file)
    return fake


def _apply_ready(db):
    """user + job + APPROVED rubric + an ACTIVE application link."""
    user = _hr_user(db)
    return _open_with_link(db, user)


def _count_events(db, entity_id, event_type: str) -> int:
    return db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.entity_id == entity_id,
            AuditEvent.event_type == event_type,
        )
    ).scalar_one()


def _applications_for_job(db, job_id):
    return db.execute(
        select(Application).where(Application.job_id == job_id)
    ).scalars().all()


def _submit(db, token, *, filename="casey_resume.pdf", data=_RESUME_PDF):
    return submit_application_via_token(
        db,
        token=token,
        full_name=_CAND_NAME,
        email=_CAND_EMAIL,
        phone=_CAND_PHONE,
        file_bytes=data,
        original_filename=filename,
    )


# --- success -------------------------------------------------------


def test_submit_application_success(db, fake_drive):
    job, link = _apply_ready(db)

    result = _submit(db, link.token)

    assert result.outcome == SubmissionOutcome.SUCCESS
    assert result.application_id

    app_row = get_application(db, result.application_id)
    assert app_row is not None and app_row.job_id == job.id

    doc = db.execute(
        select(Document).where(Document.application_id == app_row.id)
    ).scalar_one()
    assert doc.mime_type == _PDF_MIME
    assert doc.original_filename == "casey_resume.pdf"

    assert _count_events(db, app_row.id, AuditEventType.CANDIDATE_APPLIED.value) == 1
    assert _count_events(db, doc.id, AuditEventType.RESUME_UPLOADED.value) == 1

    rows = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id.in_([app_row.id, doc.id]))
    ).scalars().all()
    blob = " ".join(
        f"{e.action} {e.previous_state} {e.new_state} {e.event_metadata}"
        for e in rows
    )
    assert _CAND_NAME not in blob
    assert _CAND_EMAIL not in blob
    assert _CAND_PHONE not in blob
    assert link.token not in blob


def test_submit_application_optional_phone_blank(db, fake_drive):
    job, link = _apply_ready(db)
    result = submit_application_via_token(
        db, token=link.token, full_name="No Phone",
        email="no.phone@example.com", phone="   ",
        file_bytes=_RESUME_PDF, original_filename="r.pdf",
    )
    assert result.outcome == SubmissionOutcome.SUCCESS


# --- duplicate ----------------------------------------------------


def test_submit_application_duplicate(db, fake_drive):
    job, link = _apply_ready(db)

    first = _submit(db, link.token)
    assert first.outcome == SubmissionOutcome.SUCCESS

    again = _submit(db, link.token)
    assert again.outcome == SubmissionOutcome.DUPLICATE
    assert again.internal_reason == "DUPLICATE"
    assert again.application_id is None

    assert len(_applications_for_job(db, job.id)) == 1
    assert _count_events(
        db, first.application_id, AuditEventType.CANDIDATE_APPLIED.value
    ) == 1


# --- link invalid at submit time -------------------------------


def test_submit_application_link_revoked_at_submit(db, fake_drive):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)
    revoke_link(db, link_id=link.id, requested_by_user_id=user.id)
    before = db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.CANDIDATE_APPLIED.value
        )
    ).scalar_one()

    result = _submit(db, link.token)

    assert result.outcome == SubmissionOutcome.LINK_INVALID
    assert result.internal_reason == "INACTIVE"
    assert len(_applications_for_job(db, job.id)) == 0
    # delta, not absolute — the shared dev DB may already hold real events
    assert db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.CANDIDATE_APPLIED.value
        )
    ).scalar_one() == before


def test_submit_application_job_closed_at_submit(db, fake_drive):
    user = _hr_user(db)
    job, link = _open_with_link(db, user)
    close_job(db, job_id=job.id, requested_by_user_id=user.id)

    result = _submit(db, link.token)

    assert result.outcome == SubmissionOutcome.LINK_INVALID
    assert result.internal_reason == "JOB_CLOSED"
    assert len(_applications_for_job(db, job.id)) == 0


def test_submit_application_unknown_token(db, fake_drive):
    result = _submit(db, "Zt" + "x" * 41)
    assert result.outcome == SubmissionOutcome.LINK_INVALID
    assert result.internal_reason == "NOT_FOUND"


# --- Drive failure keeps the application; retry reuses it ------


def test_submit_drive_failure_keeps_application(db, fake_drive):
    job, link = _apply_ready(db)
    fake_drive.fail_upload = True
    before_uploaded = db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RESUME_UPLOADED.value
        )
    ).scalar_one()

    result = _submit(db, link.token)

    assert result.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED
    assert result.internal_reason == "DriveUploadError"
    assert result.application_id

    app_row = get_application(db, result.application_id)
    assert app_row is not None  # NOT rolled back (CLAUDE.md §27)
    assert db.execute(
        select(func.count()).select_from(Document).where(
            Document.application_id == app_row.id
        )
    ).scalar_one() == 0

    assert _count_events(db, app_row.id, AuditEventType.CANDIDATE_APPLIED.value) == 1
    # delta, not absolute — the shared dev DB may already hold real events
    assert db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RESUME_UPLOADED.value
        )
    ).scalar_one() == before_uploaded


def test_retry_resume_upload_reuses_same_application(db, fake_drive):
    job, link = _apply_ready(db)
    fake_drive.fail_upload = True
    failed = _submit(db, link.token)
    assert failed.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED
    app_id = failed.application_id

    fake_drive.fail_upload = False
    retried = retry_resume_upload(
        db, application_id=app_id,
        file_bytes=_RESUME_PDF, original_filename="casey_resume.pdf",
    )

    assert retried.outcome == SubmissionOutcome.SUCCESS
    assert retried.application_id == app_id

    assert len(_applications_for_job(db, job.id)) == 1
    doc = db.execute(
        select(Document).where(Document.application_id == app_id)
    ).scalar_one()
    assert _count_events(db, app_id, AuditEventType.CANDIDATE_APPLIED.value) == 1
    assert _count_events(db, doc.id, AuditEventType.RESUME_UPLOADED.value) == 1


def test_retry_resume_upload_still_failing(db, fake_drive):
    job, link = _apply_ready(db)
    fake_drive.fail_upload = True
    failed = _submit(db, link.token)

    again = retry_resume_upload(
        db, application_id=failed.application_id,
        file_bytes=_RESUME_PDF, original_filename="r.pdf",
    )
    assert again.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED
    assert again.application_id == failed.application_id


def test_retry_resume_upload_unknown_application(db, fake_drive):
    result = retry_resume_upload(
        db, application_id=uuid.uuid4(),
        file_bytes=_RESUME_PDF, original_filename="r.pdf",
    )
    assert result.outcome == SubmissionOutcome.ERROR
    assert result.internal_reason == "APPLICATION_NOT_FOUND"


def test_submit_application_target_not_found_is_generic_error(
    db, fake_drive, mocker
):
    job, link = _apply_ready(db)
    mocker.patch(
        "app.services.candidate_portal_service.create_application",
        side_effect=ApplicationTargetNotFoundError("simulated mismatch"),
    )

    result = _submit(db, link.token)

    assert result.outcome == SubmissionOutcome.ERROR
    assert result.internal_reason == "ApplicationTargetNotFoundError"
    assert result.application_id is None


# --- retry is refused once a résumé is already stored (defence in depth) ---
#
# ``resolve_resume_retry_token`` already blocks the public ``?retry=`` route
# once a document exists. These pin the SAME guarantee at the service boundary,
# so a caller that bypasses the resolver cannot replace a stored résumé either.


def _stored_document(db, application_id):
    return db.execute(
        select(Document).where(Document.application_id == application_id)
    ).scalar_one()


def _document_snapshot(doc):
    """Every persisted field, so a rejected retry can be proven inert."""
    return (
        doc.id, doc.application_id, doc.drive_file_id, doc.drive_folder_id,
        doc.original_filename, doc.mime_type, doc.file_size_bytes,
        doc.uploaded_at,
    )


def test_retry_succeeds_when_the_application_genuinely_has_no_resume(
    db, fake_drive
):
    """Property 1 — the legitimate path still works, and the document is really
    stored afterwards (not merely a SUCCESS outcome)."""
    job, link = _apply_ready(db)
    fake_drive.fail_upload = True
    failed = _submit(db, link.token)
    app_id = failed.application_id
    assert db.execute(
        select(func.count()).select_from(Document).where(
            Document.application_id == app_id
        )
    ).scalar_one() == 0

    fake_drive.fail_upload = False
    retried = retry_resume_upload(
        db, application_id=app_id,
        file_bytes=_RESUME_PDF, original_filename="casey_resume.pdf",
    )

    assert retried.outcome == SubmissionOutcome.SUCCESS
    assert str(_stored_document(db, app_id).application_id) == str(app_id)


def test_retry_is_refused_when_a_resume_is_already_stored(db, fake_drive):
    """Property 2 — the specific business outcome, not a generic failure."""
    job, link = _apply_ready(db)
    ok = _submit(db, link.token)
    assert ok.outcome == SubmissionOutcome.SUCCESS

    refused = retry_resume_upload(
        db, application_id=ok.application_id,
        file_bytes=_RESUME_PDF, original_filename="second_resume.pdf",
    )

    assert refused.outcome == SubmissionOutcome.ERROR
    assert refused.internal_reason == "RESUME_ALREADY_UPLOADED"
    assert refused.application_id == ok.application_id


def test_a_refused_retry_leaves_the_stored_document_untouched(db, fake_drive):
    """Property 3 — field-for-field, the existing document is unchanged, and no
    second document row appears."""
    job, link = _apply_ready(db)
    ok = _submit(db, link.token)
    app_id = ok.application_id
    before = _document_snapshot(_stored_document(db, app_id))

    retry_resume_upload(
        db, application_id=app_id,
        file_bytes=b"%PDF-1.4\nDIFFERENT CONTENT\n",
        original_filename="attacker_resume.pdf",
    )

    db.expire_all()
    assert db.execute(
        select(func.count()).select_from(Document).where(
            Document.application_id == app_id
        )
    ).scalar_one() == 1
    assert _document_snapshot(_stored_document(db, app_id)) == before


def test_a_refused_retry_never_reaches_google_drive(db, fake_drive):
    """Property 4 — the Drive upload seam is never invoked at all, proven by the
    call counter, not by discarding a result."""
    job, link = _apply_ready(db)
    ok = _submit(db, link.token)
    calls_after_first_upload = fake_drive.upload_calls
    assert calls_after_first_upload == 1

    retry_resume_upload(
        db, application_id=ok.application_id,
        file_bytes=_RESUME_PDF, original_filename="second_resume.pdf",
    )

    assert fake_drive.upload_calls == calls_after_first_upload
    assert len(fake_drive.files) == 1


def test_the_guard_does_not_disturb_the_failure_then_retry_flow(db, fake_drive):
    """Property 5 — repeated genuine failures still return RESUME_UPLOAD_FAILED
    (the guard must only fire when a document actually exists)."""
    job, link = _apply_ready(db)
    fake_drive.fail_upload = True
    failed = _submit(db, link.token)
    app_id = failed.application_id

    for _ in range(3):
        again = retry_resume_upload(
            db, application_id=app_id,
            file_bytes=_RESUME_PDF, original_filename="r.pdf",
        )
        assert again.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED
        assert again.internal_reason == "DriveUploadError"

    fake_drive.fail_upload = False
    assert retry_resume_upload(
        db, application_id=app_id,
        file_bytes=_RESUME_PDF, original_filename="r.pdf",
    ).outcome == SubmissionOutcome.SUCCESS


def test_the_guard_leaves_unknown_application_handling_unchanged(db, fake_drive):
    """Property 6 — the pre-existing precondition still fires first and still
    reports its own reason; the new guard never shadows it."""
    result = retry_resume_upload(
        db, application_id=uuid.uuid4(),
        file_bytes=_RESUME_PDF, original_filename="r.pdf",
    )
    assert result.outcome == SubmissionOutcome.ERROR
    assert result.internal_reason == "APPLICATION_NOT_FOUND"


def test_a_refused_retry_does_not_change_application_status_or_audit(
    db, fake_drive
):
    """Property 7 — no lifecycle movement and no new audit event from a refusal."""
    job, link = _apply_ready(db)
    ok = _submit(db, link.token)
    app_id = ok.application_id
    app_row = get_application(db, app_id)
    status_before = app_row.status
    uploaded_before = _count_events(
        db, _stored_document(db, app_id).id,
        AuditEventType.RESUME_UPLOADED.value,
    )
    applied_before = _count_events(
        db, app_id, AuditEventType.CANDIDATE_APPLIED.value
    )

    retry_resume_upload(
        db, application_id=app_id,
        file_bytes=_RESUME_PDF, original_filename="second_resume.pdf",
    )

    db.expire_all()
    assert get_application(db, app_id).status == status_before
    assert _count_events(
        db, _stored_document(db, app_id).id,
        AuditEventType.RESUME_UPLOADED.value,
    ) == uploaded_before
    assert _count_events(
        db, app_id, AuditEventType.CANDIDATE_APPLIED.value
    ) == applied_before


# --- C4: candidate-safe contact accessor ------------------------------
#
# Scoped to ONE already-known application_id. There is deliberately no lookup by
# name or email — this must never become a way to discover who applied.


def test_get_application_contact_returns_the_captured_details(db, fake_drive):
    job, link = _apply_ready(db)
    result = submit_application_via_token(
        db, token=link.token, full_name="Casey Candidate",
        email="casey-c4@example.com", phone=None,
        file_bytes=_RESUME_PDF, original_filename="casey_resume.pdf",
    )
    assert result.outcome == SubmissionOutcome.SUCCESS

    contact = get_application_contact(
        db, application_id=uuid.UUID(result.application_id)
    )
    assert contact is not None
    assert contact.full_name == "Casey Candidate"
    assert contact.email == "casey-c4@example.com"


def test_get_application_contact_is_none_for_an_unknown_application(db):
    assert get_application_contact(db, application_id=uuid.uuid4()) is None


def test_get_application_contact_never_crosses_between_applications(db, fake_drive):
    """Two applications, two candidates: each id returns only its own."""
    job, link = _apply_ready(db)
    a = submit_application_via_token(
        db, token=link.token, full_name="Alice A", email="alice-c4@example.com",
        phone=None, file_bytes=_RESUME_PDF, original_filename="a.pdf",
    )
    b = submit_application_via_token(
        db, token=link.token, full_name="Bob B", email="bob-c4@example.com",
        phone=None, file_bytes=_RESUME_PDF, original_filename="b.pdf",
    )
    assert a.application_id != b.application_id

    got_a = get_application_contact(db, application_id=uuid.UUID(a.application_id))
    got_b = get_application_contact(db, application_id=uuid.UUID(b.application_id))
    assert (got_a.full_name, got_a.email) == ("Alice A", "alice-c4@example.com")
    assert (got_b.full_name, got_b.email) == ("Bob B", "bob-c4@example.com")


def test_the_contact_accessor_exposes_no_search_capability():
    """Its only parameter is ``application_id`` — no name, email, job, or
    free-text criterion, so it cannot be used to enumerate candidates."""
    import inspect

    params = inspect.signature(get_application_contact).parameters
    assert list(params) == ["db", "application_id"]


# --- C6: candidate-safe submission failure messages -------------------


_ALL_MAPPED_REASONS = (
    "UNSUPPORTED_TYPE",
    "FILE_VALIDATION",
    "DriveUploadError",
    "StorageConfigError",
    "DriveAuthError",
    "APPLICATION_NOT_FOUND",
    "RESUME_ALREADY_UPLOADED",
)


@pytest.mark.parametrize("reason", _ALL_MAPPED_REASONS)
def test_every_enumerated_reason_maps_to_a_real_message(reason):
    message = submission_failure_message(reason)
    assert message and message != SUBMISSION_FAILURE_FALLBACK


def test_the_distinct_failure_causes_get_distinct_messages():
    """The three storage exceptions deliberately share one message (a candidate
    can do nothing different about them); every other cause is its own."""
    distinct = {
        submission_failure_message(r) for r in _ALL_MAPPED_REASONS
    }
    assert len(distinct) == 5  # 7 reasons, 3 storage ones collapsed into 1

    storage = {
        submission_failure_message(r)
        for r in ("DriveUploadError", "StorageConfigError", "DriveAuthError")
    }
    assert len(storage) == 1


@pytest.mark.parametrize(
    "reason", ["", None, "TOTALLY_UNKNOWN", "SomeFutureError", "SUCCESS"]
)
def test_an_unmapped_reason_falls_back_instead_of_crashing(reason):
    assert submission_failure_message(reason) == SUBMISSION_FAILURE_FALLBACK


@pytest.mark.parametrize(
    "reason", list(_ALL_MAPPED_REASONS) + ["", None, "TOTALLY_UNKNOWN"]
)
def test_the_reason_code_never_appears_in_the_message(reason):
    """Sentinel-style: ``internal_reason`` is documented logs/tests-only, so it
    must be a key here and never leak into candidate-facing copy."""
    message = submission_failure_message(reason)
    if reason:
        assert reason not in message
    # nor any raw exception-class naming convention
    for leak in ("Error", "Exception", "Traceback", "_"):
        assert leak not in message


def test_no_failure_message_promises_an_email():
    """Fix 2's standing invariant applies to this new copy too."""
    for reason in list(_ALL_MAPPED_REASONS) + [None]:
        blob = submission_failure_message(reason).lower()
        for forbidden in ("we'll email", "email you", "by email", "e-mail you"):
            assert forbidden not in blob
