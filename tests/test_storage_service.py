"""Tests for app.services.storage_service (Phase 2).

The Google Drive API is mocked **entirely** — no real network calls — following
the same discipline used for the Claude API. The four ``_drive_*`` seam
functions in ``storage_service`` are the only places that touch the SDK; here a
small in-memory ``FakeDrive`` replaces them, and ``_get_drive`` is patched so no
credentials are needed.

Real Postgres via the savepoint-rollback ``db`` fixture.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.document import Document
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.user import UserRole
from app.services import storage_service
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.storage_service import (
    DocumentTargetNotFoundError,
    DocumentValidationError,
    DriveAuthError,
    DriveUploadError,
    StorageConfigError,
    get_document,
    get_document_download_bytes,
    upload_document,
)
from app.utils.validation import MAX_UPLOAD_BYTES

_PDF_MIME = "application/pdf"
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class FakeDrive:
    """In-memory stand-in for the four ``storage_service._drive_*`` seams."""

    def __init__(self) -> None:
        self.folders: dict[str, str] = {}          # name -> folder id
        self.files: dict[str, tuple[str, bytes, str]] = {}  # id -> (name, bytes, mime)
        self.create_folder_calls = 0
        self.upload_calls = 0
        self.fail_upload = False

    def find_folder(self, service, root_folder_id, name):
        return self.folders.get(name)

    def create_folder(self, service, root_folder_id, name):
        self.create_folder_calls += 1
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
    fake = FakeDrive()
    mocker.patch.object(
        storage_service, "_get_drive", return_value=(object(), "root-test")
    )
    mocker.patch.object(storage_service, "_drive_find_folder", fake.find_folder)
    mocker.patch.object(
        storage_service, "_drive_create_folder", fake.create_folder
    )
    mocker.patch.object(storage_service, "_drive_upload_file", fake.upload_file)
    mocker.patch.object(
        storage_service, "_drive_download_file", fake.download_file
    )
    return fake


# --- test data helpers ---------------------------------------------


def _hr_user_id(db):
    """An active internal user id, for the auth gate on
    ``get_document_download_bytes``."""
    return create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    ).id


def _application(db):
    user = create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )
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
    app_row = create_application(
        db,
        job_id=job.id,
        application_link_id=link.id,
        email=f"cand-{uuid.uuid4().hex}@example.com",
        full_name="Casey Candidate",
        phone="555-0100",
    )
    return job, app_row


# --- successful upload -------------------------------------------


def test_upload_creates_document_row_and_audit(db, fake_drive, sample_pdf_bytes):
    job, app_row = _application(db)

    doc = upload_document(
        db,
        application_id=app_row.id,
        job_id=job.id,
        file_bytes=sample_pdf_bytes,
        original_filename="Jane_Smith_Resume.pdf",
        mime_type=_PDF_MIME,
    )

    assert doc.id is not None
    assert doc.application_id == app_row.id
    assert doc.drive_file_id in fake_drive.files
    assert doc.drive_folder_id == fake_drive.folders[str(job.id)]
    assert doc.original_filename == "Jane_Smith_Resume.pdf"  # sanitised base name
    assert doc.mime_type == _PDF_MIME
    assert doc.file_size_bytes == len(sample_pdf_bytes)

    # row is committed + persisted
    assert db.get(Document, doc.id) is not None

    ev = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == doc.id,
            AuditEvent.event_type == AuditEventType.RESUME_UPLOADED.value,
        )
    ).scalars().one()
    assert ev.entity_type == "document"
    assert ev.user_id is None
    assert ev.event_metadata == {
        "document_id": str(doc.id),
        "application_id": str(app_row.id),
        "job_id": str(job.id),
        "file_size_bytes": len(sample_pdf_bytes),
        "mime_type": _PDF_MIME,
    }


def test_audit_metadata_has_no_filename_pii(db, fake_drive, sample_pdf_bytes):
    job, app_row = _application(db)
    doc = upload_document(
        db,
        application_id=app_row.id,
        job_id=job.id,
        file_bytes=sample_pdf_bytes,
        original_filename="Very_Unique_Personname_CV.pdf",
        mime_type=_PDF_MIME,
    )
    ev = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == doc.id)
    ).scalars().one()
    blob = f"{ev.action} || {ev.previous_state} || {ev.new_state} || {ev.event_metadata}"
    assert "Personname" not in blob
    assert "Very_Unique" not in blob
    # but the documents table itself DOES keep the sanitised name
    assert db.get(Document, doc.id).original_filename == "Very_Unique_Personname_CV.pdf"


def test_docx_upload_allowed(db, fake_drive, sample_docx_bytes):
    job, app_row = _application(db)
    doc = upload_document(
        db,
        application_id=app_row.id,
        job_id=job.id,
        file_bytes=sample_docx_bytes,
        original_filename="resume.docx",
        mime_type=_DOCX_MIME,
    )
    assert doc.mime_type == _DOCX_MIME


# --- Drive upload failure => no DB row -------------------------


def test_drive_upload_failure_creates_no_document_row(
    db, fake_drive, sample_pdf_bytes
):
    job, app_row = _application(db)
    fake_drive.fail_upload = True
    before = db.execute(
        select(func.count()).select_from(Document)
    ).scalar_one()
    before_events = db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RESUME_UPLOADED.value
        )
    ).scalar_one()

    with pytest.raises(DriveUploadError):
        upload_document(
            db,
            application_id=app_row.id,
            job_id=job.id,
            file_bytes=sample_pdf_bytes,
            original_filename="resume.pdf",
            mime_type=_PDF_MIME,
        )

    assert db.execute(
        select(func.count()).select_from(Document)
    ).scalar_one() == before
    # no RESUME_UPLOADED audit event either (delta, not absolute — the shared
    # dev DB may already hold real RESUME_UPLOADED events)
    assert db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RESUME_UPLOADED.value
        )
    ).scalar_one() == before_events


# --- subfolder created once, reused ---------------------------


def test_job_subfolder_created_once_and_reused(db, fake_drive, sample_pdf_bytes):
    from app.services.application_link_service import get_active_link

    job, app_row_1 = _application(db)
    # a second application against the SAME job
    link = get_active_link(db, job.id)
    app_row_2 = create_application(
        db,
        job_id=job.id,
        application_link_id=link.id,
        email=f"cand2-{uuid.uuid4().hex}@example.com",
        full_name="Second Candidate",
        phone=None,
    )

    upload_document(
        db, application_id=app_row_1.id, job_id=job.id,
        file_bytes=sample_pdf_bytes, original_filename="a.pdf", mime_type=_PDF_MIME,
    )
    upload_document(
        db, application_id=app_row_2.id, job_id=job.id,
        file_bytes=sample_pdf_bytes, original_filename="b.pdf", mime_type=_PDF_MIME,
    )

    assert fake_drive.create_folder_calls == 1
    assert fake_drive.upload_calls == 2
    assert len(fake_drive.folders) == 1


# --- validation happens before any Drive call ---------------


def test_invalid_mime_rejected_before_drive(db, fake_drive, sample_pdf_bytes):
    job, app_row = _application(db)
    with pytest.raises(DocumentValidationError):
        upload_document(
            db, application_id=app_row.id, job_id=job.id,
            file_bytes=sample_pdf_bytes, original_filename="resume.pdf",
            mime_type="text/plain",
        )
    assert fake_drive.upload_calls == 0
    assert fake_drive.create_folder_calls == 0


def test_oversized_file_rejected_before_drive(db, fake_drive):
    job, app_row = _application(db)
    huge = b"%PDF-1.4\n" + b"0" * (MAX_UPLOAD_BYTES + 1)
    from app.utils.validation import FileValidationError

    with pytest.raises(FileValidationError):
        upload_document(
            db, application_id=app_row.id, job_id=job.id,
            file_bytes=huge, original_filename="huge.pdf", mime_type=_PDF_MIME,
        )
    assert fake_drive.upload_calls == 0


def test_unknown_application_rejected_before_drive(
    db, fake_drive, sample_pdf_bytes
):
    job, app_row = _application(db)
    with pytest.raises(DocumentTargetNotFoundError):
        upload_document(
            db, application_id=uuid.uuid4(), job_id=job.id,
            file_bytes=sample_pdf_bytes, original_filename="r.pdf",
            mime_type=_PDF_MIME,
        )
    assert fake_drive.upload_calls == 0


def test_job_id_mismatch_rejected(db, fake_drive, sample_pdf_bytes):
    job, app_row = _application(db)
    with pytest.raises(DocumentTargetNotFoundError):
        upload_document(
            db, application_id=app_row.id, job_id=uuid.uuid4(),
            file_bytes=sample_pdf_bytes, original_filename="r.pdf",
            mime_type=_PDF_MIME,
        )
    assert fake_drive.upload_calls == 0


# --- reads / round-trip -------------------------------------


def test_get_document_and_download_round_trip(db, fake_drive, sample_pdf_bytes):
    job, app_row = _application(db)
    doc = upload_document(
        db, application_id=app_row.id, job_id=job.id,
        file_bytes=sample_pdf_bytes, original_filename="r.pdf", mime_type=_PDF_MIME,
    )

    assert get_document(db, doc.id).id == doc.id
    assert get_document(db, uuid.uuid4()) is None

    got_bytes = get_document_download_bytes(
        doc.id, db, acting_user_id=_hr_user_id(db)
    )
    assert got_bytes == sample_pdf_bytes


def test_download_unknown_document_raises(db, fake_drive):
    with pytest.raises(DocumentTargetNotFoundError):
        get_document_download_bytes(
            uuid.uuid4(), db, acting_user_id=_hr_user_id(db)
        )


# --- CLAUDE.md §17 isolation: only storage_service imports the SDK ---


def test_only_storage_service_imports_the_drive_sdk():
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    markers = ("googleapiclient", "google.oauth2", "google_auth_httplib2")
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(m in text for m in markers) and path.name != "storage_service.py":
            offenders.append(str(path.relative_to(app_dir.parent)))
    assert not offenders, f"Drive SDK imported outside storage_service: {offenders}"


def test_no_service_account_import_remains():
    """The service-account auth path is fully removed, not just unused."""
    import pathlib

    # Built from fragments so a plain grep of this test file stays quiet.
    forbidden = ("google.oauth2." + "service_account", "service_account" + ".Credentials")
    root = pathlib.Path(__file__).resolve().parents[1]
    for sub in ("app", "scripts"):
        for path in (root / sub).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                assert marker not in text, f"{marker!r} still in {path}"


# --- OAuth config fail-fast (auth-layer swap) -----------------------

_VALID_OAUTH_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "test-secret",
    "GOOGLE_OAUTH_REFRESH_TOKEN": "1//test-refresh-token",
    "GOOGLE_DRIVE_ROOT_FOLDER_ID": "test-root-folder-id",
}


@pytest.mark.parametrize(
    "missing_var",
    [
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
    ],
)
def test_get_drive_missing_oauth_var_raises_named_config_error(mocker, missing_var):
    env = dict(_VALID_OAUTH_ENV)
    env.pop(missing_var)
    mocker.patch.dict("os.environ", env, clear=True)
    mocker.patch.object(storage_service, "_drive_cache", None)

    with pytest.raises(StorageConfigError) as excinfo:
        storage_service._get_drive()

    msg = str(excinfo.value)
    assert missing_var in msg
    # never echoes any (partial) secret value
    assert _VALID_OAUTH_ENV["GOOGLE_OAUTH_CLIENT_SECRET"] not in msg
    assert _VALID_OAUTH_ENV["GOOGLE_OAUTH_REFRESH_TOKEN"] not in msg


def test_get_drive_missing_refresh_token_points_at_authorize_script(mocker):
    env = dict(_VALID_OAUTH_ENV)
    env.pop("GOOGLE_OAUTH_REFRESH_TOKEN")
    mocker.patch.dict("os.environ", env, clear=True)
    mocker.patch.object(storage_service, "_drive_cache", None)

    with pytest.raises(StorageConfigError) as excinfo:
        storage_service._get_drive()
    assert "authorize_drive_oauth.py" in str(excinfo.value)


def test_get_drive_revoked_refresh_token_raises_drive_auth_error(mocker):
    from google.auth.exceptions import RefreshError

    mocker.patch.dict("os.environ", dict(_VALID_OAUTH_ENV), clear=True)
    mocker.patch.object(storage_service, "_drive_cache", None)
    # simulate Google rejecting the refresh token (invalid_grant / revoked)
    mocker.patch(
        "google.oauth2.credentials.Credentials.refresh",
        side_effect=RefreshError("invalid_grant: Token has been expired or revoked."),
    )

    with pytest.raises(DriveAuthError) as excinfo:
        storage_service._get_drive()
    assert "authorize_drive_oauth.py" in str(excinfo.value)
    assert "GOOGLE_OAUTH_REFRESH_TOKEN" in str(excinfo.value)
    # the token value must not appear in the error
    assert _VALID_OAUTH_ENV["GOOGLE_OAUTH_REFRESH_TOKEN"] not in str(excinfo.value)
