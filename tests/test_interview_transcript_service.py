"""Tests for app.services.interview_transcript_service (Increment B).

Google Drive is replaced by an in-memory fake that, unlike the one in
``test_storage_service``, keys folders by ``(parent id, name)`` so a nested
``Transcripts`` subfolder can be represented. No real network call is made and
no AI is involved. Real Postgres via the savepoint-rollback ``db`` fixture; every
query is scoped to rows this test created.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.document import Document
from app.database.models.interview_feedback import InterviewFeedback
from app.database.models.interview_guide import InterviewGuide
from app.database.models.interview_transcript import (
    InterviewTranscript,
    InterviewTranscriptStatus,
)
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services import interview_transcript_service as svc
from app.services import storage_service
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.interview_feedback_service import create_interview_feedback
from app.services.interview_transcript_service import (
    MIN_EXTRACTABLE_CHARS,
    TRANSCRIPTS_FOLDER_NAME,
    InterviewTranscriptActorError,
    InterviewTranscriptError,
    InterviewTranscriptStorageError,
    InterviewTranscriptTargetNotFoundError,
    InterviewTranscriptValidationError,
    attach_interview_transcript,
    build_transcript_file_name,
    get_current_transcript_for_feedback,
    get_transcript_download_bytes,
    list_current_transcripts_for_application,
    list_transcripts_for_feedback,
    sanitize_candidate_name,
)
from app.services.job_service import create_job
from app.services.shortlist_service import shortlist_candidate
from app.utils.authorization import UnauthorizedError
from app.utils.validation import MAX_UPLOAD_BYTES

_PDF_MIME = "application/pdf"
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

_SENTINEL_NAME = "Zyxwvuts Sentinelcandidate"
_SENTINEL_ORIGINAL = "SENTINEL_original_upload_name.pdf"


# --- fake Drive (nested folders) -------------------------------------------


class NestedFakeDrive:
    def __init__(self) -> None:
        self.folders: dict[tuple[str, str], str] = {}   # (parent, name) -> id
        self.files: dict[str, dict] = {}                 # id -> metadata
        self.fail_upload = False
        self.fail_download = False
        self.upload_calls = 0

    def find_folder(self, service, parent_id, name):
        return self.folders.get((parent_id, name))

    def create_folder(self, service, parent_id, name):
        fid = f"folder-{len(self.folders) + 1}"
        self.folders[(parent_id, name)] = fid
        return fid

    def upload_file(self, service, folder_id, name, file_bytes, mime_type):
        self.upload_calls += 1
        if self.fail_upload:
            raise storage_service.DriveUploadError("Drive upload failed: simulated.")
        fid = f"file-{len(self.files) + 1}"
        self.files[fid] = {
            "name": name, "parent": folder_id, "bytes": file_bytes, "mime": mime_type,
        }
        return fid, len(file_bytes)

    def download_file(self, service, drive_file_id):
        if self.fail_download:
            raise storage_service.DriveDownloadError("Drive download failed: simulated.")
        return self.files[drive_file_id]["bytes"]


@pytest.fixture
def drive(mocker):
    fake = NestedFakeDrive()
    mocker.patch.object(
        storage_service, "_get_drive", return_value=(object(), "root-test")
    )
    mocker.patch.object(storage_service, "_drive_find_folder", fake.find_folder)
    mocker.patch.object(storage_service, "_drive_create_folder", fake.create_folder)
    mocker.patch.object(storage_service, "_drive_upload_file", fake.upload_file)
    mocker.patch.object(storage_service, "_drive_download_file", fake.download_file)
    return fake


# --- seed helpers ---------------------------------------------------------


def _hr(db, name="Dana Interviewer"):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name=name, role=UserRole.HR,
    )


def _seed(db, *, candidate_name="Ananya Rao"):
    hr = _hr(db)
    job = create_job(
        db, title="Backend Engineer", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
        created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    rv = RubricVersion(
        job_id=job.id, version_number=1, status=RubricVersionStatus.APPROVED,
        generated_from_requirements_version=1, created_by=hr.id,
    )
    db.add(rv)
    db.flush()
    db.add(RubricCriterion(
        rubric_version_id=rv.id, requirement_type="MANDATORY", category=None,
        criterion_text="5+ years Python", display_order=1,
    ))
    db.flush()
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com", full_name=candidate_name, phone=None,
    )
    app.status = ApplicationStatus.SCREENING_EVALUATED
    db.flush()
    entry = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rv.id,
        requested_by_user_id=hr.id,
    )
    guide = InterviewGuide(
        job_id=job.id, application_id=app.id, shortlist_entry_id=entry.id,
        rubric_version_id=rv.id, ai_model="claude-sonnet-5", generated_at=_T0,
    )
    db.add(guide)
    db.flush()
    return {"hr": hr, "job": job, "app": app, "guide": guide}


def _feedback(db, s, round_=1):
    return create_interview_feedback(
        db, user_id=s["hr"].id, application_id=s["app"].id,
        interview_guide_id=s["guide"].id, interview_round=round_,
        recommendation="PROCEED", notes="ok", ratings=[],
    )


def _attach(db, s, feedback, data, name="t.pdf", user=None):
    return attach_interview_transcript(
        db, interview_feedback_id=feedback.id, file_bytes=data,
        original_filename=name,
        acting_user_id=s["hr"].id if user is None else user,
    )


def _rows(db, feedback_id):
    return list(db.execute(
        select(InterviewTranscript)
        .where(InterviewTranscript.interview_feedback_id == feedback_id)
        .order_by(InterviewTranscript.created_at, InterviewTranscript.id)
    ).scalars().all())


def _audit_events(db, application_id):
    return list(db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.INTERVIEW_TRANSCRIPT_UPLOADED.value,
            AuditEvent.event_metadata["application_id"].astext == str(application_id),
        )
    ).scalars().all())


def _image_only_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(50, 50, 300, 300), fill=(0, 0, 0))
    data = doc.tobytes()
    doc.close()
    return data


# --- table / model ----------------------------------------------------------


def test_table_has_no_text_application_or_candidate_columns():
    cols = {c.name for c in InterviewTranscript.__table__.columns}
    assert cols == {
        "id", "interview_feedback_id", "uploaded_by_user_id", "drive_file_id",
        "drive_folder_id", "file_name", "mime_type", "file_size_bytes",
        "text_extractable", "status", "superseded_at", "created_at",
    }


def test_status_vocabulary_is_a_validated_string():
    assert InterviewTranscriptStatus.ALL == {"CURRENT", "SUPERSEDED"}
    assert InterviewTranscriptStatus.is_valid("CURRENT")
    assert not InterviewTranscriptStatus.is_valid("current")
    assert InterviewTranscript.__table__.c.status.type.__class__.__name__ == "String"


def test_foreign_keys_are_restrict(db):
    fks = {
        fk.parent.name: (fk.column.table.name, fk.ondelete)
        for fk in InterviewTranscript.__table__.foreign_keys
    }
    assert fks == {
        "interview_feedback_id": ("interview_feedback", "RESTRICT"),
        "uploaded_by_user_id": ("users", "RESTRICT"),
    }


def _raw_row(db, feedback, user, *, status, drive_file_id=None):
    row = InterviewTranscript(
        interview_feedback_id=feedback.id, uploaded_by_user_id=user.id,
        drive_file_id=drive_file_id or f"f-{uuid.uuid4().hex}",
        drive_folder_id="fo", file_name="x.pdf", mime_type=_PDF_MIME,
        file_size_bytes=1, text_extractable=True, status=status,
    )
    db.add(row)
    return row


def test_two_current_rows_for_one_feedback_fail_in_the_database(db):
    s = _seed(db)
    fb = _feedback(db, s)
    _raw_row(db, fb, s["hr"], status="CURRENT")
    db.flush()
    _raw_row(db, fb, s["hr"], status="CURRENT")
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.flush()


def test_many_superseded_rows_may_coexist_with_one_current(db):
    s = _seed(db)
    fb = _feedback(db, s)
    for _ in range(3):
        _raw_row(db, fb, s["hr"], status="SUPERSEDED")
    _raw_row(db, fb, s["hr"], status="CURRENT")
    db.flush()
    assert len(_rows(db, fb.id)) == 4


def test_two_feedback_rows_may_each_hold_a_current_row(db):
    s = _seed(db)
    f1, f2 = _feedback(db, s, 1), _feedback(db, s, 2)
    _raw_row(db, f1, s["hr"], status="CURRENT")
    _raw_row(db, f2, s["hr"], status="CURRENT")
    db.flush()


def test_drive_file_id_is_unique(db):
    s = _seed(db)
    f1, f2 = _feedback(db, s, 1), _feedback(db, s, 2)
    _raw_row(db, f1, s["hr"], status="CURRENT", drive_file_id="same")
    db.flush()
    _raw_row(db, f2, s["hr"], status="CURRENT", drive_file_id="same")
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.flush()


def test_the_feedback_row_cannot_be_deleted_while_a_transcript_points_at_it(db):
    s = _seed(db)
    fb = _feedback(db, s)
    _raw_row(db, fb, s["hr"], status="CURRENT")
    db.flush()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("DELETE FROM interview_feedback WHERE id = :i"), {"i": fb.id})


def test_the_uploader_cannot_be_deleted_while_a_transcript_points_at_them(db):
    s = _seed(db)
    fb = _feedback(db, s)
    uploader = _hr(db, "Uploader")
    _raw_row(db, fb, uploader, status="CURRENT")
    db.flush()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("DELETE FROM users WHERE id = :i"), {"i": uploader.id})


# --- file-name logic ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Ananya Rao", "Ananya_Rao"),
        ("  Ananya   Rao  ", "Ananya_Rao"),
        ("Zoë Müller", "Zoë_Müller"),
        ("அனன்யா ராவ்", "அனன்யா_ராவ்"),            # combining marks survive
        ("O'Brien, Pat (Jr.)", "OBrien_Pat_Jr"),
        ("../../etc/passwd", "etcpasswd"),
        ("a/b\\c", "abc"),
        ("Jean-Luc", "Jean-Luc"),
        ("", "Candidate"),
        (None, "Candidate"),
        ("!!! ???", "Candidate"),
        ("___", "Candidate"),
    ],
)
def test_candidate_name_sanitizing(raw, expected):
    assert sanitize_candidate_name(raw) == expected


def test_very_long_candidate_name_is_capped():
    out = sanitize_candidate_name("A" * 500)
    assert len(out) == 60


def test_name_with_control_characters_is_clean():
    assert sanitize_candidate_name("Ana\x00nya\nRao\t") == "Ananya_Rao"


def test_file_name_format_and_versions():
    assert build_transcript_file_name("Ananya Rao", 1, ".PDF", 1) == (
        "Ananya_Rao_Transcript_R1.pdf"
    )
    assert build_transcript_file_name("Ananya Rao", 2, ".docx", 2) == (
        "Ananya_Rao_Transcript_R2_v2.docx"
    )
    assert build_transcript_file_name("Ananya Rao", 1, ".pdf", 3) == (
        "Ananya_Rao_Transcript_R1_v3.pdf"
    )


# --- attach: success path -------------------------------------------------


def test_attach_stores_correct_name_subfolder_and_row(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s, round_=1)

    view = _attach(db, s, fb, sample_pdf_bytes, name="whatever it was called.pdf")

    assert view.file_name == "Ananya_Rao_Transcript_R1.pdf"
    assert view.interview_round == 1
    assert view.version_number == 1
    assert view.status == "CURRENT"
    assert view.mime_type == _PDF_MIME
    assert view.uploaded_by_name == "Dana Interviewer"
    assert view.text_extractable is True

    # Drive layout: <root>/<"V_001 - Title">/Transcripts/<file>
    job_folder = drive.folders[("root-test", f"{s['job'].job_code} - Backend Engineer")]
    transcripts = drive.folders[(job_folder, TRANSCRIPTS_FOLDER_NAME)]
    stored = next(iter(drive.files.values()))
    assert stored["parent"] == transcripts
    assert stored["name"] == "Ananya_Rao_Transcript_R1.pdf"
    assert stored["bytes"] == sample_pdf_bytes

    (row,) = _rows(db, fb.id)
    assert row.status == "CURRENT" and row.superseded_at is None
    assert row.drive_folder_id == transcripts
    assert row.uploaded_by_user_id == s["hr"].id
    assert row.file_size_bytes == len(sample_pdf_bytes)


def test_docx_is_accepted_with_its_own_mime_and_extension(db, drive, sample_docx_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    view = _attach(db, s, fb, sample_docx_bytes, name="x.DOCX")
    assert view.file_name == "Ananya_Rao_Transcript_R1.docx"
    assert view.mime_type == _DOCX_MIME
    assert view.text_extractable is True


def test_round_comes_from_the_feedback_row(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s, round_=3)
    view = _attach(db, s, fb, sample_pdf_bytes, name="R9_final_round_7.pdf")
    assert view.interview_round == 3
    assert view.file_name == "Ananya_Rao_Transcript_R3.pdf"


def test_the_transcripts_folder_is_created_once_and_reused(db, drive, sample_pdf_bytes):
    s = _seed(db)
    f1, f2 = _feedback(db, s, 1), _feedback(db, s, 2)
    _attach(db, s, f1, sample_pdf_bytes)
    _attach(db, s, f2, sample_pdf_bytes)
    names = [k[1] for k in drive.folders if k[1] == TRANSCRIPTS_FOLDER_NAME]
    assert len(names) == 1


def test_the_feedback_row_is_never_modified(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    before = (fb.interview_round, fb.notes, fb.recommendation, fb.created_at)
    _attach(db, s, fb, sample_pdf_bytes)
    db.refresh(fb)
    assert (fb.interview_round, fb.notes, fb.recommendation, fb.created_at) == before


def test_candidate_name_in_the_file_name_comes_from_the_candidate_row(
    db, drive, sample_pdf_bytes
):
    s = _seed(db, candidate_name="Ann/Marie O'Neil")
    fb = _feedback(db, s)
    view = _attach(db, s, fb, sample_pdf_bytes)
    assert view.file_name == "AnnMarie_ONeil_Transcript_R1.pdf"


# --- versioning --------------------------------------------------------------


def test_second_upload_becomes_v2_and_supersedes_the_first(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    first = _attach(db, s, fb, sample_pdf_bytes)
    second = _attach(db, s, fb, sample_pdf_bytes + b"\n% v2")

    assert second.file_name == "Ananya_Rao_Transcript_R1_v2.pdf"
    assert second.version_number == 2

    rows = _rows(db, fb.id)
    assert len(rows) == 2                                   # nothing deleted
    by_id = {r.id: r for r in rows}
    old, new = by_id[first.transcript_id], by_id[second.transcript_id]
    assert old.status == "SUPERSEDED" and old.superseded_at is not None
    assert new.status == "CURRENT" and new.superseded_at is None
    assert sum(r.status == "CURRENT" for r in rows) == 1

    # the old Drive file is still there, and so is the new one
    assert {f["name"] for f in drive.files.values()} == {
        "Ananya_Rao_Transcript_R1.pdf", "Ananya_Rao_Transcript_R1_v2.pdf",
    }


def test_third_upload_becomes_v3(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    for _ in range(3):
        view = _attach(db, s, fb, sample_pdf_bytes)
    assert view.file_name == "Ananya_Rao_Transcript_R1_v3.pdf"
    assert [r.status for r in _rows(db, fb.id)] == [
        "SUPERSEDED", "SUPERSEDED", "CURRENT",
    ]


def test_rounds_are_independent(db, drive, sample_pdf_bytes):
    s = _seed(db)
    f1, f2 = _feedback(db, s, 1), _feedback(db, s, 2)
    _attach(db, s, f1, sample_pdf_bytes)
    _attach(db, s, f2, sample_pdf_bytes)

    current = list_current_transcripts_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert set(current) == {1, 2}
    assert current[1].file_name == "Ananya_Rao_Transcript_R1.pdf"
    assert current[2].file_name == "Ananya_Rao_Transcript_R2.pdf"

    # replacing round 1 leaves round 2 alone
    _attach(db, s, f1, sample_pdf_bytes)
    current = list_current_transcripts_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert current[1].file_name == "Ananya_Rao_Transcript_R1_v2.pdf"
    assert current[2].file_name == "Ananya_Rao_Transcript_R2.pdf"
    assert all(r.status == "CURRENT" for r in _rows(db, f2.id))


# --- validation --------------------------------------------------------------


def _assert_nothing_persisted(db, drive, fb, app_id):
    assert _rows(db, fb.id) == []
    assert drive.upload_calls == 0 and drive.files == {}
    assert _audit_events(db, app_id) == []


def test_doc_files_are_rejected(db, drive):
    s = _seed(db)
    fb = _feedback(db, s)
    with pytest.raises(InterviewTranscriptValidationError):
        _attach(db, s, fb, b"\xd0\xcf\x11\xe0 old word", name="t.doc")
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


@pytest.mark.parametrize("name", ["t.txt", "t.exe", "t", "t.pdf.exe"])
def test_wrong_extensions_are_rejected(db, drive, sample_pdf_bytes, name):
    s = _seed(db)
    fb = _feedback(db, s)
    with pytest.raises(InterviewTranscriptValidationError):
        _attach(db, s, fb, sample_pdf_bytes, name=name)
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


def test_empty_file_is_rejected(db, drive):
    s = _seed(db)
    fb = _feedback(db, s)
    with pytest.raises(InterviewTranscriptValidationError):
        _attach(db, s, fb, b"")
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


def test_file_over_ten_mb_is_rejected(db, drive):
    s = _seed(db)
    fb = _feedback(db, s)
    with pytest.raises(InterviewTranscriptValidationError, match="too large"):
        _attach(db, s, fb, b"%PDF" + b"0" * MAX_UPLOAD_BYTES)
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


def test_a_renamed_text_file_is_rejected_by_the_magic_byte_check(db, drive):
    s = _seed(db)
    fb = _feedback(db, s)
    with pytest.raises(InterviewTranscriptValidationError, match="PDF"):
        _attach(db, s, fb, b"just plain text, not a pdf", name="t.pdf")
    with pytest.raises(InterviewTranscriptValidationError, match="DOCX"):
        _attach(db, s, fb, b"just plain text, not a docx", name="t.docx")
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


def test_a_pdf_renamed_to_docx_and_back_is_rejected(db, drive, sample_pdf_bytes, sample_docx_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    with pytest.raises(InterviewTranscriptValidationError):
        _attach(db, s, fb, sample_pdf_bytes, name="t.docx")
    with pytest.raises(InterviewTranscriptValidationError):
        _attach(db, s, fb, sample_docx_bytes, name="t.pdf")
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


def test_a_failed_replacement_leaves_the_current_transcript_untouched(
    db, drive, sample_pdf_bytes
):
    s = _seed(db)
    fb = _feedback(db, s)
    first = _attach(db, s, fb, sample_pdf_bytes)
    with pytest.raises(InterviewTranscriptValidationError):
        _attach(db, s, fb, b"not a pdf", name="t.pdf")
    (row,) = _rows(db, fb.id)
    assert row.id == first.transcript_id and row.status == "CURRENT"


def test_unknown_feedback_is_a_clear_error(db, drive, sample_pdf_bytes):
    s = _seed(db)
    with pytest.raises(InterviewTranscriptTargetNotFoundError):
        attach_interview_transcript(
            db, interview_feedback_id=uuid.uuid4(), file_bytes=sample_pdf_bytes,
            original_filename="t.pdf", acting_user_id=s["hr"].id,
        )
    assert drive.upload_calls == 0


# --- auth ----------------------------------------------------------------------


def test_system_actor_is_rejected(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    with pytest.raises(InterviewTranscriptActorError):
        _attach(db, s, fb, sample_pdf_bytes, user=SYSTEM_USER_ID)
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


@pytest.mark.parametrize("who", [None, "unknown", "malformed"])
def test_unknown_or_missing_user_is_rejected(db, drive, sample_pdf_bytes, who):
    s = _seed(db)
    fb = _feedback(db, s)
    actor = {"unknown": uuid.uuid4(), "malformed": "not-a-uuid", None: None}[who]
    with pytest.raises(UnauthorizedError):
        attach_interview_transcript(
            db, interview_feedback_id=fb.id, file_bytes=sample_pdf_bytes,
            original_filename="t.pdf", acting_user_id=actor,
        )
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


def test_inactive_user_is_rejected(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    s["hr"].is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _attach(db, s, fb, sample_pdf_bytes)
    _assert_nothing_persisted(db, drive, fb, s["app"].id)


def test_the_actor_check_runs_before_anything_else(db, drive):
    """Even an invalid file and a missing feedback row do not mask the auth error."""
    with pytest.raises(UnauthorizedError):
        attach_interview_transcript(
            db, interview_feedback_id=uuid.uuid4(), file_bytes=b"",
            original_filename="x.doc", acting_user_id=uuid.uuid4(),
        )


def test_accessors_and_download_are_guarded(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    view = _attach(db, s, fb, sample_pdf_bytes)
    nobody = uuid.uuid4()
    with pytest.raises(UnauthorizedError):
        list_transcripts_for_feedback(db, fb.id, acting_user_id=nobody)
    with pytest.raises(UnauthorizedError):
        get_current_transcript_for_feedback(db, fb.id, acting_user_id=None)
    with pytest.raises(UnauthorizedError):
        list_current_transcripts_for_application(db, s["app"].id, acting_user_id=nobody)
    with pytest.raises(UnauthorizedError):
        get_transcript_download_bytes(db, view.transcript_id, acting_user_id=nobody)


# --- Drive failure ----------------------------------------------------------------


def test_drive_failure_creates_no_row_and_leaves_the_feedback_alone(
    db, drive, sample_pdf_bytes
):
    s = _seed(db)
    fb = _feedback(db, s)
    drive.fail_upload = True
    with pytest.raises(InterviewTranscriptStorageError, match="nothing was saved"):
        _attach(db, s, fb, sample_pdf_bytes)
    assert _rows(db, fb.id) == []
    assert _audit_events(db, s["app"].id) == []
    assert db.get(InterviewFeedback, fb.id) is not None


def test_a_drive_failure_during_replacement_keeps_the_current_transcript(
    db, drive, sample_pdf_bytes
):
    s = _seed(db)
    fb = _feedback(db, s)
    first = _attach(db, s, fb, sample_pdf_bytes)
    drive.fail_upload = True
    with pytest.raises(InterviewTranscriptStorageError):
        _attach(db, s, fb, sample_pdf_bytes)
    (row,) = _rows(db, fb.id)
    assert row.id == first.transcript_id and row.status == "CURRENT"


def test_storage_misconfiguration_is_a_storage_error(db, mocker, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    mocker.patch.object(
        storage_service, "_get_drive",
        side_effect=storage_service.StorageConfigError("missing GOOGLE_X"),
    )
    with pytest.raises(InterviewTranscriptStorageError):
        _attach(db, s, fb, sample_pdf_bytes)
    assert _rows(db, fb.id) == []


def test_the_error_message_does_not_leak_drive_detail(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    drive.fail_upload = True
    with pytest.raises(InterviewTranscriptStorageError) as exc:
        _attach(db, s, fb, sample_pdf_bytes)
    assert "simulated" not in str(exc.value)


# --- audit -----------------------------------------------------------------------


def test_exactly_one_audit_event_per_attach_with_the_listed_metadata(
    db, drive, sample_pdf_bytes
):
    s = _seed(db)
    fb = _feedback(db, s, round_=2)
    first = _attach(db, s, fb, sample_pdf_bytes)
    (event,) = _audit_events(db, s["app"].id)
    assert event.user_id == s["hr"].id
    assert event.entity_type == "interview_transcript"
    assert event.entity_id == first.transcript_id
    assert event.event_metadata == {
        "application_id": str(s["app"].id),
        "interview_feedback_id": str(fb.id),
        "interview_round": 2,
        "transcript_id": str(first.transcript_id),
        "file_size_bytes": len(sample_pdf_bytes),
        "mime_type": _PDF_MIME,
        "version_number": 1,
        "superseded_transcript_id": None,
        "text_extractable": True,
    }

    second = _attach(db, s, fb, sample_pdf_bytes)
    events = _audit_events(db, s["app"].id)
    assert len(events) == 2
    newest = next(e for e in events if e.entity_id == second.transcript_id)
    assert newest.event_metadata["version_number"] == 2
    assert newest.event_metadata["superseded_transcript_id"] == str(first.transcript_id)


def test_no_name_ever_reaches_any_audit_field(db, drive, sample_pdf_bytes):
    s = _seed(db, candidate_name=_SENTINEL_NAME)
    fb = _feedback(db, s)
    view = _attach(db, s, fb, sample_pdf_bytes, name=_SENTINEL_ORIGINAL)
    _attach(db, s, fb, sample_pdf_bytes, name=_SENTINEL_ORIGINAL)

    assert "Zyxwvuts" in view.file_name        # the sentinel IS in the file name
    events = _audit_events(db, s["app"].id)
    assert len(events) == 2
    for e in events:
        blob = " ".join(
            str(v) for v in (
                e.action, e.entity_type, e.previous_state, e.new_state,
                e.event_metadata,
            )
        ).lower()
        for needle in (
            "zyxwvuts", "sentinelcandidate", "sentinel_original",
            "_transcript_r", ".pdf", view.file_name.lower(),
        ):
            assert needle not in blob, needle


def test_no_audit_event_is_written_when_the_attach_fails(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    drive.fail_upload = True
    with pytest.raises(InterviewTranscriptStorageError):
        _attach(db, s, fb, sample_pdf_bytes)
    assert _audit_events(db, s["app"].id) == []


# --- text_extractable --------------------------------------------------------------


def test_text_pdf_and_docx_are_extractable(db, drive, sample_pdf_bytes, sample_docx_bytes):
    s = _seed(db)
    f1, f2 = _feedback(db, s, 1), _feedback(db, s, 2)
    assert _attach(db, s, f1, sample_pdf_bytes).text_extractable is True
    assert _attach(db, s, f2, sample_docx_bytes, name="t.docx").text_extractable is True


def test_image_only_pdf_is_not_extractable_but_is_stored(db, drive):
    s = _seed(db)
    fb = _feedback(db, s)
    view = _attach(db, s, fb, _image_only_pdf())
    assert view.text_extractable is False
    assert len(_rows(db, fb.id)) == 1


def test_trivial_text_is_not_extractable(db, drive):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "x" * MIN_EXTRACTABLE_CHARS)   # == the limit
    data = doc.tobytes()
    doc.close()
    s = _seed(db)
    fb = _feedback(db, s)
    assert _attach(db, s, fb, data).text_extractable is False


def test_just_over_the_threshold_is_extractable(db, drive):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "x" * (MIN_EXTRACTABLE_CHARS + 1))
    data = doc.tobytes()
    doc.close()
    s = _seed(db)
    fb = _feedback(db, s)
    assert _attach(db, s, fb, data).text_extractable is True


def test_corrupt_file_with_valid_magic_bytes_is_stored_not_extractable_no_exception(
    db, drive
):
    s = _seed(db)
    f1, f2 = _feedback(db, s, 1), _feedback(db, s, 2)
    assert _attach(db, s, f1, b"%PDF-1.4 this is garbage").text_extractable is False
    assert _attach(
        db, s, f2, b"PK\x03\x04 not really a zip", name="t.docx"
    ).text_extractable is False


def test_extracted_text_is_never_logged_or_stored(db, drive, sample_pdf_bytes, caplog):
    s = _seed(db)
    fb = _feedback(db, s)
    with caplog.at_level("DEBUG"):
        _attach(db, s, fb, sample_pdf_bytes)
    assert "PySpark" not in caplog.text
    assert "PySpark" not in str(_rows(db, fb.id)[0].__dict__)


# --- accessors / download --------------------------------------------------------


def test_accessors_return_current_and_history_newest_first(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    assert get_current_transcript_for_feedback(db, fb.id, acting_user_id=s["hr"].id) is None
    assert list_transcripts_for_feedback(db, fb.id, acting_user_id=s["hr"].id) == []

    _attach(db, s, fb, sample_pdf_bytes)
    _attach(db, s, fb, sample_pdf_bytes)

    views = list_transcripts_for_feedback(db, fb.id, acting_user_id=s["hr"].id)
    assert [v.version_number for v in views] == [2, 1]
    assert [v.status for v in views] == ["CURRENT", "SUPERSEDED"]
    current = get_current_transcript_for_feedback(db, fb.id, acting_user_id=s["hr"].id)
    assert current.transcript_id == views[0].transcript_id


def test_accessors_tolerate_an_unknown_feedback_id(db):
    s = _seed(db)
    assert list_transcripts_for_feedback(db, uuid.uuid4(), acting_user_id=s["hr"].id) == []
    assert get_current_transcript_for_feedback(
        db, uuid.uuid4(), acting_user_id=s["hr"].id
    ) is None


def test_download_returns_bytes_name_and_mime(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    view = _attach(db, s, fb, sample_pdf_bytes)
    got = get_transcript_download_bytes(
        db, view.transcript_id, acting_user_id=s["hr"].id
    )
    assert got.content == sample_pdf_bytes
    assert got.file_name == "Ananya_Rao_Transcript_R1.pdf"
    assert got.mime_type == _PDF_MIME


def test_download_of_a_superseded_version_still_works(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    old = _attach(db, s, fb, sample_pdf_bytes)
    _attach(db, s, fb, sample_pdf_bytes + b"\n% v2")
    got = get_transcript_download_bytes(db, old.transcript_id, acting_user_id=s["hr"].id)
    assert got.content == sample_pdf_bytes


def test_download_errors_are_clear(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    view = _attach(db, s, fb, sample_pdf_bytes)
    with pytest.raises(InterviewTranscriptTargetNotFoundError):
        get_transcript_download_bytes(db, uuid.uuid4(), acting_user_id=s["hr"].id)
    drive.fail_download = True
    with pytest.raises(InterviewTranscriptStorageError):
        get_transcript_download_bytes(db, view.transcript_id, acting_user_id=s["hr"].id)


def test_all_error_types_share_one_base():
    for cls in (
        InterviewTranscriptActorError, InterviewTranscriptStorageError,
        InterviewTranscriptTargetNotFoundError, InterviewTranscriptValidationError,
    ):
        assert issubclass(cls, InterviewTranscriptError)


# --- structural: no AI reads transcripts; documents / résumé flow untouched --------

_APP = Path(__file__).resolve().parents[1] / "app"


def _imports_transcript_service(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    return (
        "interview_transcript_service" in src
        or "interview_transcript import" in src
        or "models.interview_transcript" in src
    )


def test_nothing_under_app_ai_imports_the_transcript_service_or_model():
    offenders = [
        str(p.relative_to(_APP)) for p in (_APP / "ai").rglob("*.py")
        if _imports_transcript_service(p)
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "module",
    [
        # post_interview_service.py is deliberately ABSENT: Increment C makes it
        # the ONE service allowed to import the transcript service (it reads the
        # transcript text for the AI). A dedicated test pins that it is the only
        # one -- see test_post_interview_analysis_all_rounds.
        "screening_evaluation_service.py",
        "screening_question_service.py", "interview_guide_service.py",
        "prequalification_service.py", "resume_parsing_service.py",
        "screening_pipeline_service.py", "candidate_portal_service.py",
    ],
)
def test_ai_driven_and_candidate_services_do_not_import_it(module):
    assert not _imports_transcript_service(_APP / "services" / module)


def test_candidate_facing_app_does_not_import_it():
    for p in (_APP / "pages").glob("*.py"):
        if p.name == "interviews.py":
            continue
        assert not _imports_transcript_service(p), p.name


def test_the_documents_table_is_unchanged():
    cols = {c.name for c in Document.__table__.columns}
    assert cols == {
        "id", "application_id", "drive_file_id", "drive_folder_id",
        "original_filename", "mime_type", "file_size_bytes", "uploaded_at",
    }


def test_attaching_a_transcript_never_creates_a_document_row(db, drive, sample_pdf_bytes):
    s = _seed(db)
    fb = _feedback(db, s)
    before = db.execute(
        select(func.count(Document.id)).where(Document.application_id == s["app"].id)
    ).scalar_one()
    _attach(db, s, fb, sample_pdf_bytes)
    after = db.execute(
        select(func.count(Document.id)).where(Document.application_id == s["app"].id)
    ).scalar_one()
    assert before == after == 0
    assert storage_service.list_documents_for_application(db, s["app"].id) == []


def test_upload_document_and_validate_uploaded_file_are_unmodified_in_behaviour(db):
    """The résumé path still rejects what it always did; this module added no
    magic-byte check to the shared validator."""
    from app.utils.validation import FileValidationError, validate_uploaded_file

    validate_uploaded_file("a.pdf", b"not really a pdf")        # no magic-byte check
    with pytest.raises(FileValidationError):
        validate_uploaded_file("a.doc", b"x")


# --- final scorecard: the additive transcript field ------------------------------


def test_scorecard_carries_the_latest_rounds_current_transcript_name(
    db, drive, sample_pdf_bytes
):
    from app.services.final_scorecard_service import get_final_scorecard

    s = _seed(db)
    f1 = _feedback(db, s, 1)
    f2 = _feedback(db, s, 2)
    # Same-transaction rows tie on now(); pin an order so "latest" is round 2.
    f1.created_at = _T0
    f2.created_at = _T0.replace(day=2)
    db.flush()
    _attach(db, s, f1, sample_pdf_bytes)

    def card():
        return get_final_scorecard(db, s["app"].id, acting_user_id=s["hr"].id)

    # Latest feedback (round 2) has none, even though round 1 does.
    assert card().interview_round == 2
    assert card().interview_transcript_file_name is None

    _attach(db, s, f2, sample_pdf_bytes)
    assert card().interview_transcript_file_name == "Ananya_Rao_Transcript_R2.pdf"
    _attach(db, s, f2, sample_pdf_bytes)
    assert card().interview_transcript_file_name == "Ananya_Rao_Transcript_R2_v2.pdf"


def test_a_missing_transcript_is_not_reported_as_a_missing_source(db):
    from app.services.final_scorecard_service import get_final_scorecard

    s = _seed(db)
    _feedback(db, s, 1)
    view = get_final_scorecard(db, s["app"].id, acting_user_id=s["hr"].id)
    assert view.interview_transcript_file_name is None
    assert not any("ranscript" in m and "creening" not in m for m in view.missing_sources)
