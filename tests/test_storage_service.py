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
        self.trashed: set[str] = set()              # folder ids marked trashed
        self.usable_calls: list[str] = []

    def folder_usable(self, service, folder_id):
        """Stand-in for ``_drive_folder_usable``: exists and not trashed."""
        self.usable_calls.append(folder_id)
        return folder_id in self.folders.values() and folder_id not in self.trashed

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
    mocker.patch.object(
        storage_service, "_drive_folder_usable", fake.folder_usable
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
    # Folders are named "<job_code> - <title>", no longer by job UUID.
    assert doc.drive_folder_id == fake_drive.folders[f"{job.job_code} - Data Engineer"]
    assert str(job.id) not in fake_drive.folders
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


# =====================================================================
# Job codes + Drive folder naming
# =====================================================================
#
# ``_drive_folder_usable`` is patched for every test by an autouse fixture in
# conftest.py, so the REAL function is captured here at import time (before any
# fixture runs) for the tests of its own logic.

_REAL_FOLDER_USABLE = storage_service._drive_folder_usable
_REAL_FIND_FOLDER = storage_service._drive_find_folder


class _StubFiles:
    """Records the kwargs of a Drive ``files().list/get(...)`` call and either
    returns ``result`` or raises ``exc`` from ``.execute()``."""

    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc
        self.list_kwargs = self.get_kwargs = None

    def list(self, **kw):
        self.list_kwargs = kw
        return self

    def get(self, **kw):
        self.get_kwargs = kw
        return self

    def execute(self):
        if self.exc is not None:
            raise self.exc
        return self.result


class _StubService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def _http_error(status):
    import httplib2
    from googleapiclient.errors import HttpError

    return HttpError(httplib2.Response({"status": status}), b"")


# --- folder name format ------------------------------------------------


@pytest.mark.parametrize(
    "code, title, expected",
    [
        ("V_001", "Junior Data Engineer", "V_001 - Junior Data Engineer"),
        # quotes are legal in a Drive name; they are escaped at QUERY time
        ("V_002", "O'Brien \"Lead\"", "V_002 - O'Brien \"Lead\""),
        # slashes and backslashes are removed, not turned into path separators
        ("V_003", "Dev\\Ops", "V_003 - DevOps"),
        ("V_004", "QA/Test / Lead", "V_004 - QATest Lead"),
        # unicode survives untouched
        (
            "V_005",
            "Ingénieur Données – São Paulo 数据工程师",
            "V_005 - Ingénieur Données – São Paulo 数据工程师",
        ),
        # whitespace collapses; real control characters are dropped without
        # gluing neighbouring words together
        ("V_006", "  Data\tEngineer\n\x00\x07 II  ", "V_006 - Data Engineer II"),
        # overflowing counter widens rather than truncates
        ("V_1000", "Analyst", "V_1000 - Analyst"),
    ],
)
def test_job_folder_name_format(code, title, expected):
    assert storage_service._job_folder_name(code, title) == expected


def test_job_folder_name_caps_a_very_long_title():
    name = storage_service._job_folder_name("V_007", "A" * 200)
    assert name == "V_007 - " + "A" * 80


def test_job_folder_name_never_ends_in_a_space_after_the_cap():
    # "word " x 40 -> the 80-char cut lands right after a space.
    name = storage_service._job_folder_name("V_008", "word " * 40)
    assert name == name.rstrip()
    assert len(name) <= len("V_008 - ") + 80


@pytest.mark.parametrize(
    "title", ["", None, "   ", "///", "\\\\", "\x00\x01 ", "/ \\ /"]
)
def test_job_folder_name_falls_back_to_the_bare_code(title):
    """Nothing usable left after sanitising -> just the job code."""
    assert storage_service._job_folder_name("V_009", title) == "V_009"


# --- Drive query escaping (the backslash bug) ---------------------------


def test_find_folder_escapes_backslash_before_quote():
    files = _StubFiles(result={"files": []})
    _REAL_FIND_FOLDER(_StubService(files), "root-1", "V_1 - a\\b'c")
    # one backslash -> two; the quote gets its own single escaping backslash
    assert r"name = 'V_1 - a\\b\'c'" in files.list_kwargs["q"]


def test_find_folder_trailing_backslash_no_longer_escapes_the_closing_quote():
    """Before the fix a name ending in a backslash produced a query whose last
    backslash escaped the closing quote, so the query was malformed."""
    files = _StubFiles(result={"files": []})
    _REAL_FIND_FOLDER(_StubService(files), "root-1", "V_1 - x\\")
    assert r"name = 'V_1 - x\\' and 'root-1' in parents" in files.list_kwargs["q"]


# --- the stored-folder verification seam --------------------------------


def test_folder_usable_true_for_a_live_folder():
    files = _StubFiles(result={
        "id": "f1", "trashed": False,
        "mimeType": "application/vnd.google-apps.folder",
    })
    assert _REAL_FOLDER_USABLE(_StubService(files), "f1") is True
    assert files.get_kwargs["fileId"] == "f1"


def test_folder_usable_false_when_trashed():
    files = _StubFiles(result={
        "id": "f1", "trashed": True,
        "mimeType": "application/vnd.google-apps.folder",
    })
    assert _REAL_FOLDER_USABLE(_StubService(files), "f1") is False


def test_folder_usable_false_when_the_id_is_not_a_folder():
    files = _StubFiles(result={
        "id": "f1", "trashed": False, "mimeType": "application/pdf",
    })
    assert _REAL_FOLDER_USABLE(_StubService(files), "f1") is False


def test_folder_usable_false_on_404():
    files = _StubFiles(exc=_http_error(404))
    assert _REAL_FOLDER_USABLE(_StubService(files), "gone") is False


@pytest.mark.parametrize("status", [403, 500, 503])
def test_folder_usable_raises_on_any_other_http_error(status):
    """A transient failure must NOT read as "folder missing" - that would make
    the caller create a second folder for a job that already has one."""
    files = _StubFiles(exc=_http_error(status))
    with pytest.raises(DriveUploadError):
        _REAL_FOLDER_USABLE(_StubService(files), "f1")


def test_folder_usable_raises_on_a_non_http_failure():
    files = _StubFiles(exc=ConnectionError("network down"))
    with pytest.raises(DriveUploadError):
        _REAL_FOLDER_USABLE(_StubService(files), "f1")


# --- folder resolution through upload_document --------------------------


def _another_application(db, job):
    """A second application to the SAME job (and so the same Drive folder)."""
    from app.database.models.application_link import ApplicationLink

    link = db.execute(
        select(ApplicationLink).where(ApplicationLink.job_id == job.id)
    ).scalars().first()
    return create_application(
        db,
        job_id=job.id,
        application_link_id=link.id,
        email=f"cand-{uuid.uuid4().hex}@example.com",
        full_name="Second Candidate",
        phone="555-0101",
    )


def _application_titled(db, title):
    user = create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only", full_name="HR Tester",
        role=UserRole.HR,
    )
    job = create_job(
        db, title=title, department="Data",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    app_row = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"cand-{uuid.uuid4().hex}@example.com",
        full_name="Casey Candidate", phone="555-0100",
    )
    return job, app_row


def _upload(db, app_row, job, pdf):
    return upload_document(
        db, application_id=app_row.id, job_id=job.id, file_bytes=pdf,
        original_filename="cv.pdf", mime_type=_PDF_MIME,
    )


def test_new_job_gets_a_new_style_folder_and_a_second_upload_reuses_it(
    db, fake_drive, sample_pdf_bytes
):
    job, first = _application(db)
    second = _another_application(db, job)

    d1 = _upload(db, first, job, sample_pdf_bytes)
    d2 = _upload(db, second, job, sample_pdf_bytes)

    expected = f"{job.job_code} - Data Engineer"
    assert list(fake_drive.folders) == [expected]
    assert fake_drive.create_folder_calls == 1          # created once, not twice
    assert d1.drive_folder_id == d2.drive_folder_id == fake_drive.folders[expected]


def test_second_upload_reuses_the_stored_folder_by_id_not_by_name(
    db, fake_drive, sample_pdf_bytes
):
    """The stored id is verified first; the name lookup is never needed."""
    job, first = _application(db)
    second = _another_application(db, job)
    d1 = _upload(db, first, job, sample_pdf_bytes)

    fake_drive.usable_calls.clear()
    d2 = _upload(db, second, job, sample_pdf_bytes)

    assert fake_drive.usable_calls == [d1.drive_folder_id]
    assert d2.drive_folder_id == d1.drive_folder_id


def test_legacy_uuid_named_folder_is_reused_and_no_second_folder_appears(
    db, fake_drive, sample_pdf_bytes
):
    """A job uploaded to BEFORE job codes existed has a UUID-named folder and a
    document row pointing at it. The next upload must use that folder (found by
    its stored id) - creating nothing - and the folder must keep its old name."""
    job, first = _application(db)
    second = _another_application(db, job)

    fake_drive.folders[str(job.id)] = "legacy-folder"
    db.add(Document(
        application_id=first.id, drive_file_id="legacy-file",
        drive_folder_id="legacy-folder", original_filename="old.pdf",
        mime_type=_PDF_MIME, file_size_bytes=10,
    ))
    db.flush()

    doc = _upload(db, second, job, sample_pdf_bytes)

    assert doc.drive_folder_id == "legacy-folder"
    assert fake_drive.create_folder_calls == 0
    assert list(fake_drive.folders) == [str(job.id)]     # still the old name
    assert fake_drive.folders[str(job.id)] == "legacy-folder"


def test_a_trashed_stored_folder_is_not_reused(db, fake_drive, sample_pdf_bytes):
    job, first = _application(db)
    second = _another_application(db, job)
    d1 = _upload(db, first, job, sample_pdf_bytes)

    fake_drive.trashed.add(d1.drive_folder_id)
    # A real Drive name query filters ``trashed = false``; this fake does not,
    # so re-key the entry to model "the trashed folder is invisible to a name
    # lookup" (kept in the dict so the fake's id counter keeps advancing).
    live_name = f"{job.job_code} - Data Engineer"
    fake_drive.folders["(trashed)"] = fake_drive.folders.pop(live_name)
    d2 = _upload(db, second, job, sample_pdf_bytes)

    assert d2.drive_folder_id != d1.drive_folder_id
    assert fake_drive.create_folder_calls == 2


def test_stored_folder_gone_but_a_new_style_folder_exists_is_found_by_name(
    db, fake_drive, sample_pdf_bytes
):
    job, first = _application(db)
    second = _another_application(db, job)
    db.add(Document(
        application_id=first.id, drive_file_id="gone-file",
        drive_folder_id="deleted-folder", original_filename="old.pdf",
        mime_type=_PDF_MIME, file_size_bytes=10,
    ))
    db.flush()
    fake_drive.folders[f"{job.job_code} - Data Engineer"] = "named-folder"

    doc = _upload(db, second, job, sample_pdf_bytes)

    assert doc.drive_folder_id == "named-folder"
    assert fake_drive.create_folder_calls == 0


def test_verification_failure_aborts_the_upload_without_creating_a_folder(
    db, fake_drive, sample_pdf_bytes, mocker
):
    """A transient error verifying the stored folder must fail the upload - not
    be read as "missing" and trigger a duplicate folder."""
    job, first = _application(db)
    second = _another_application(db, job)
    _upload(db, first, job, sample_pdf_bytes)
    folders_before = dict(fake_drive.folders)

    mocker.patch.object(
        storage_service, "_drive_folder_usable",
        side_effect=DriveUploadError("verify failed: simulated"),
    )
    with pytest.raises(DriveUploadError):
        _upload(db, second, job, sample_pdf_bytes)

    assert fake_drive.folders == folders_before
    assert fake_drive.create_folder_calls == 1
    assert db.execute(
        select(func.count()).select_from(Document)
        .where(Document.application_id == second.id)
    ).scalar_one() == 0


def test_upload_does_not_mutate_the_stored_job_title(
    db, fake_drive, sample_pdf_bytes
):
    job, app_row = _application_titled(db, "  Dev/Ops \\ Lead\t(Remote)  ")
    original = job.title
    _upload(db, app_row, job, sample_pdf_bytes)
    db.refresh(job)
    assert job.title == original
    assert f"{job.job_code} - DevOps Lead (Remote)" in fake_drive.folders


def test_upload_audit_metadata_never_contains_the_job_title_or_folder_name(
    db, fake_drive, sample_pdf_bytes
):
    """Sentinel: the folder name embeds the job title, so it must never leak
    into the upload's audit row (action, states or metadata)."""
    sentinel = "Zzsentineltitle"
    job, app_row = _application_titled(db, f"{sentinel} Engineer")

    doc = _upload(db, app_row, job, sample_pdf_bytes)

    ev = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == doc.id,
            AuditEvent.event_type == AuditEventType.RESUME_UPLOADED.value,
        )
    ).scalars().one()
    blob = (
        f"{ev.action} || {ev.previous_state} || {ev.new_state} || "
        f"{ev.event_metadata}"
    )
    assert sentinel not in blob
    assert "Engineer" not in blob
    assert f"{job.job_code} - " not in blob
