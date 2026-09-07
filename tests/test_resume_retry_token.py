"""Tests for the résumé-retry credential (Phase D Fix 3).

``applications.resume_retry_token`` is a lazily-minted, per-application,
credential-equivalent token that gives a candidate a durable way back to their
own application when the résumé upload failed. Before it, the only pointer to
that application was ``st.session_state``, which a browser refresh clears —
leaving an application row with no document and no route to attach one.

These tests are about **security and lifecycle**, not "no exception raised":
minting happens on exactly one path and never elsewhere, the token resolves to
exactly one application, and none of the three identifiers the design
deliberately rejected (``application_links.token``, ``applications.id``,
``screening_sessions.access_token``) can be substituted for it.

Conventions follow ``tests/test_candidate_portal_service.py`` — the same
``fake_drive`` fixture and seed helpers, real Postgres via the savepoint
``db`` fixture. Every query is scoped to rows the test created.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import func, select
from streamlit.testing.v1 import AppTest

import app.public_main as P
from app.database.models.application import Application
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.document import Document
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.screening_session import ScreeningSession
from app.database.models.user import UserRole
from app.services import storage_service
from app.services.application_link_service import generate_link
from app.services.application_service import get_application
from app.services.auth_service import create_user
from app.services.candidate_portal_service import (
    ResumeRetryOutcome,
    SubmissionOutcome,
    get_resume_retry_token,
    resolve_resume_retry_token,
    retry_resume_upload,
    submit_application_via_token,
)
from app.services.job_service import create_job
from app.services.storage_service import DriveUploadError

_RESUME_PDF = b"%PDF-1.4\n%stub resume\n"

#: ``secrets.token_urlsafe(32)`` -> exactly 43 chars of the URL-safe alphabet.
_URLSAFE_43 = re.compile(r"^[A-Za-z0-9_-]{43}$")

#: AppTest script timeout, matching tests/test_public_page_routing.py.
_TIMEOUT = 60


# --- seed helpers (mirroring test_candidate_portal_service) -------------


def _hr_user(db):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only", full_name="HR Tester",
        role=UserRole.HR,
    )


def _apply_ready(db):
    """A job with an approved rubric and an ACTIVE application link."""
    user = _hr_user(db)
    job = create_job(
        db, title="HR Generalist", department="People",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    rv = RubricVersion(
        job_id=job.id, version_number=1, status=RubricVersionStatus.APPROVED,
        generated_from_requirements_version=1, created_by=user.id,
    )
    db.add(rv)
    db.flush()
    db.add(RubricCriterion(
        rubric_version_id=rv.id, requirement_type="MANDATORY", category=None,
        criterion_text="Solid grasp of core employment law", display_order=1,
    ))
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    return job, link


class _FakeDrive:
    """In-memory stand-in for storage_service's four ``_drive_*`` seams —
    the same shape as the fixtures in ``tests/test_candidate_portal_service.py``
    and ``tests/test_storage_service.py``."""

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


def _submit(db, token, *, email=None, filename="casey_resume.pdf"):
    return submit_application_via_token(
        db, token=token,
        full_name="Casey Candidate",
        email=email or f"casey-{uuid.uuid4().hex}@example.com",
        phone=None, file_bytes=_RESUME_PDF, original_filename=filename,
    )


def _token_of(db, application_id) -> str | None:
    return db.execute(
        select(Application.resume_retry_token).where(
            Application.id == uuid.UUID(str(application_id))
        )
    ).scalar_one()


def _doc_count(db, application_id) -> int:
    return db.execute(
        select(func.count(Document.id)).where(
            Document.application_id == uuid.UUID(str(application_id))
        )
    ).scalar_one()


def _failed_application(db, fake_drive):
    """Submit with Drive failing -> RESUME_UPLOAD_FAILED. Returns the result."""
    job, link = _apply_ready(db)
    fake_drive.fail_upload = True
    result = _submit(db, link.token)
    assert result.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED
    return job, link, result


# --- 1/2/3/4. minting lifecycle ------------------------------------------


def test_a_normally_created_application_has_a_null_retry_token(db, fake_drive):
    job, link = _apply_ready(db)
    result = _submit(db, link.token)
    assert result.outcome == SubmissionOutcome.SUCCESS
    assert _token_of(db, result.application_id) is None


def test_successful_upload_never_mints_a_token(db, fake_drive):
    """Belt and braces on the above: a successful retry must not mint one
    either."""
    job, link, failed = _failed_application(db, fake_drive)
    minted = _token_of(db, failed.application_id)
    assert minted is not None

    fake_drive.fail_upload = False
    retried = retry_resume_upload(
        db, application_id=failed.application_id,
        file_bytes=_RESUME_PDF, original_filename="casey_resume.pdf",
    )
    assert retried.outcome == SubmissionOutcome.SUCCESS
    # the success path changed nothing about the token
    assert _token_of(db, failed.application_id) == minted


def test_first_failure_mints_exactly_one_token(db, fake_drive):
    job, link, failed = _failed_application(db, fake_drive)
    token = _token_of(db, failed.application_id)
    assert token is not None
    # scoped: exactly one application in the DB carries this token
    assert db.execute(
        select(func.count(Application.id)).where(
            Application.resume_retry_token == token
        )
    ).scalar_one() == 1


def test_token_uses_the_repository_secure_mechanism(db, fake_drive):
    """``secrets.token_urlsafe(32)`` — 43 chars of the URL-safe alphabet, 256
    bits. Asserted on shape, and on non-repetition across applications."""
    job, link, failed = _failed_application(db, fake_drive)
    token = _token_of(db, failed.application_id)
    assert _URLSAFE_43.match(token), token[:4]

    # two independently failed applications never collide
    _, _, other = _failed_application(db, fake_drive)
    assert _token_of(db, other.application_id) != token


def test_a_duplicate_outcome_never_mints_a_token(db, fake_drive):
    """Only RESUME_UPLOAD_FAILED mints. DUPLICATE must not."""
    job, link = _apply_ready(db)
    email = f"casey-{uuid.uuid4().hex}@example.com"
    first = _submit(db, link.token, email=email)
    assert first.outcome == SubmissionOutcome.SUCCESS

    again = _submit(db, link.token, email=email)
    assert again.outcome == SubmissionOutcome.DUPLICATE
    assert again.application_id is None
    assert _token_of(db, first.application_id) is None


def test_an_invalid_link_outcome_never_mints_a_token(db, fake_drive):
    result = _submit(db, "Zt" + "x" * 41)
    assert result.outcome == SubmissionOutcome.LINK_INVALID
    assert result.application_id is None


# --- 5. idempotent reuse --------------------------------------------------


def test_repeated_failures_reuse_the_same_token(db, fake_drive):
    job, link, failed = _failed_application(db, fake_drive)
    first_token = _token_of(db, failed.application_id)

    for _ in range(3):
        again = retry_resume_upload(
            db, application_id=failed.application_id,
            file_bytes=_RESUME_PDF, original_filename="casey_resume.pdf",
        )
        assert again.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED

    assert _token_of(db, failed.application_id) == first_token


# --- 6/7. resolution ------------------------------------------------------


def test_a_valid_token_resolves_exactly_its_application(db, fake_drive):
    job, link, failed = _failed_application(db, fake_drive)
    token = _token_of(db, failed.application_id)

    resolution = resolve_resume_retry_token(db, token)
    assert resolution.is_valid
    assert resolution.outcome == ResumeRetryOutcome.VALID
    assert str(resolution.application_id) == str(failed.application_id)


@pytest.mark.parametrize(
    "bad",
    [
        None, "", "   ", "short", "x" * 200,
        "has spaces in it aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "!!!invalid-chars!!!aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_malformed_or_missing_tokens_resolve_invalid(db, bad):
    resolution = resolve_resume_retry_token(db, bad)
    assert not resolution.is_valid
    assert resolution.application_id is None


def test_an_unknown_wellformed_token_resolves_invalid(db):
    import secrets

    resolution = resolve_resume_retry_token(db, secrets.token_urlsafe(32))
    assert not resolution.is_valid
    assert resolution.application_id is None


def test_a_prefix_of_a_valid_token_does_not_resolve(db, fake_drive):
    """No partial / prefix matching — the lookup is strict equality."""
    job, link, failed = _failed_application(db, fake_drive)
    token = _token_of(db, failed.application_id)
    assert not resolve_resume_retry_token(db, token[:-1]).is_valid
    assert not resolve_resume_retry_token(db, token[:35]).is_valid


def test_the_token_stops_resolving_once_the_resume_is_attached(db, fake_drive):
    """A saved link cannot be replayed later to attach a second résumé."""
    job, link, failed = _failed_application(db, fake_drive)
    token = _token_of(db, failed.application_id)
    assert resolve_resume_retry_token(db, token).is_valid

    fake_drive.fail_upload = False
    retried = retry_resume_upload(
        db, application_id=failed.application_id,
        file_bytes=_RESUME_PDF, original_filename="casey_resume.pdf",
    )
    assert retried.outcome == SubmissionOutcome.SUCCESS
    assert _doc_count(db, failed.application_id) == 1

    assert not resolve_resume_retry_token(db, token).is_valid


# --- 8. cross-candidate isolation (the core security property) -----------


def test_one_candidates_token_never_resolves_another_application(db, fake_drive):
    """Application A's token resolves A and ONLY A; B's resolves B and ONLY B."""
    _, _, a = _failed_application(db, fake_drive)
    _, _, b = _failed_application(db, fake_drive)
    assert a.application_id != b.application_id

    token_a = _token_of(db, a.application_id)
    token_b = _token_of(db, b.application_id)
    assert token_a != token_b

    res_a = resolve_resume_retry_token(db, token_a)
    res_b = resolve_resume_retry_token(db, token_b)

    assert str(res_a.application_id) == str(a.application_id)
    assert str(res_a.application_id) != str(b.application_id)
    assert str(res_b.application_id) == str(b.application_id)
    assert str(res_b.application_id) != str(a.application_id)


def test_two_failed_applications_on_the_SAME_job_stay_isolated(db, fake_drive):
    """Sharing a job (and therefore an application_links.token) must not let
    one candidate's retry credential reach the other's application."""
    job, link = _apply_ready(db)
    fake_drive.fail_upload = True
    a = _submit(db, link.token, email=f"a-{uuid.uuid4().hex}@example.com")
    b = _submit(db, link.token, email=f"b-{uuid.uuid4().hex}@example.com")
    assert a.outcome == b.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED

    token_a = _token_of(db, a.application_id)
    assert str(
        resolve_resume_retry_token(db, token_a).application_id
    ) == str(a.application_id)
    assert str(
        resolve_resume_retry_token(db, token_a).application_id
    ) != str(b.application_id)


# --- 9/10/11/12. no fallback identifier is accepted ----------------------


def test_candidate_email_and_name_are_not_a_resolution_fallback(db, fake_drive):
    job, link, failed = _failed_application(db, fake_drive)
    app_row = get_application(db, failed.application_id)
    from app.database.models.candidate import Candidate

    candidate = db.get(Candidate, app_row.candidate_id)

    for value in (candidate.email, candidate.full_name, "Casey Candidate"):
        assert not resolve_resume_retry_token(db, value).is_valid


def test_application_id_is_not_a_resolution_fallback(db, fake_drive):
    """CLAUDE.md §2A item 5 rejects applications.id as a credential — the
    resolver must not accept it in either form."""
    job, link, failed = _failed_application(db, fake_drive)
    app_id = str(failed.application_id)

    assert not resolve_resume_retry_token(db, app_id).is_valid
    assert not resolve_resume_retry_token(db, app_id.replace("-", "")).is_valid


def test_the_job_link_token_cannot_be_substituted(db, fake_drive):
    """``application_links.token`` is job-scoped and shared by every candidate;
    it is well-formed, so this proves the resolver checks the right column."""
    job, link, failed = _failed_application(db, fake_drive)
    assert not resolve_resume_retry_token(db, link.token).is_valid


def test_no_screening_session_exists_at_this_stage_to_fall_back_to(db, fake_drive):
    """The design premise, asserted: the pipeline never starts when the upload
    fails, so ``screening_sessions.access_token`` does not exist here — there is
    nothing for a fallback to reach for even if one were attempted."""
    job, link, failed = _failed_application(db, fake_drive)
    assert db.execute(
        select(func.count(ScreeningSession.id)).where(
            ScreeningSession.application_id == uuid.UUID(str(failed.application_id))
        )
    ).scalar_one() == 0


# --- 13/14/17. audit + logging integrity ---------------------------------


def _events_for(db, application_id):
    return list(db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == uuid.UUID(str(application_id))
        )
    ).scalars().all())


def test_the_retry_token_never_appears_in_any_audit_field(db, fake_drive):
    """Sentinel-style, matching the Step 8 pattern: the minted token is the
    sentinel, and it must appear in no audit field anywhere."""
    job, link, failed = _failed_application(db, fake_drive)
    token = _token_of(db, failed.application_id)
    assert token  # the sentinel actually exists

    for ev in db.execute(select(AuditEvent)).scalars().all():
        blob = (
            f"{ev.action} || {ev.previous_state} || {ev.new_state} || "
            f"{ev.event_metadata} || {ev.entity_type}"
        )
        assert token not in blob


def test_minting_creates_no_audit_event_of_its_own(db, fake_drive):
    """A résumé failure is not an audited domain fact in this codebase, and
    minting must not invent one. Only CANDIDATE_APPLIED (from application
    creation) is expected for this application."""
    job, link, failed = _failed_application(db, fake_drive)
    types = {ev.event_type for ev in _events_for(db, failed.application_id)}
    assert types == {AuditEventType.CANDIDATE_APPLIED.value}


def test_no_false_pipeline_start_event_is_emitted(db, fake_drive):
    """Screening has not started — the audit trail must not claim it has."""
    job, link, failed = _failed_application(db, fake_drive)
    for forbidden in (
        AuditEventType.AI_SCREENING_STARTED,
        AuditEventType.AI_SCREENING_COMPLETED,
        AuditEventType.RESUME_PROCESSED,
        AuditEventType.PREQUALIFICATION_COMPLETED,
    ):
        assert db.execute(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.entity_id == uuid.UUID(str(failed.application_id)),
                AuditEvent.event_type == forbidden.value,
            )
        ).scalar_one() == 0


def test_the_token_never_appears_in_logged_or_returned_error_text(
    db, fake_drive, caplog
):
    import logging

    caplog.set_level(logging.DEBUG)
    job, link, failed = _failed_application(db, fake_drive)
    token = _token_of(db, failed.application_id)

    assert token not in caplog.text
    # ...nor in anything the service hands back to the caller
    assert token not in (failed.internal_reason or "")
    assert token not in str(failed)


# --- 15/16. existing retry behaviour preserved ---------------------------


def test_retry_after_failure_still_completes_end_to_end(db, fake_drive):
    job, link, failed = _failed_application(db, fake_drive)
    assert _doc_count(db, failed.application_id) == 0

    fake_drive.fail_upload = False
    retried = retry_resume_upload(
        db, application_id=failed.application_id,
        file_bytes=_RESUME_PDF, original_filename="casey_resume.pdf",
    )
    assert retried.outcome == SubmissionOutcome.SUCCESS
    assert str(retried.application_id) == str(failed.application_id)
    assert _doc_count(db, failed.application_id) == 1


def test_captured_candidate_details_survive_into_the_retried_flow(db, fake_drive):
    from app.database.models.candidate import Candidate

    job, link = _apply_ready(db)
    email = f"casey-{uuid.uuid4().hex}@example.com"
    fake_drive.fail_upload = True
    failed = _submit(db, link.token, email=email)

    token = _token_of(db, failed.application_id)
    resolved = resolve_resume_retry_token(db, token)

    app_row = get_application(db, resolved.application_id)
    candidate = db.get(Candidate, app_row.candidate_id)
    assert candidate.email == email
    assert candidate.full_name == "Casey Candidate"
    assert app_row.job_id == job.id
    assert app_row.application_link_id == link.id


# --- accessor -------------------------------------------------------------


def test_get_resume_retry_token_returns_none_for_a_clean_application(
    db, fake_drive
):
    job, link = _apply_ready(db)
    result = _submit(db, link.token)
    assert get_resume_retry_token(
        db, application_id=uuid.UUID(str(result.application_id))
    ) is None


def test_get_resume_retry_token_returns_the_minted_value(db, fake_drive):
    job, link, failed = _failed_application(db, fake_drive)
    assert get_resume_retry_token(
        db, application_id=uuid.UUID(str(failed.application_id))
    ) == _token_of(db, failed.application_id)


def test_get_resume_retry_token_is_none_for_an_unknown_application(db):
    assert get_resume_retry_token(db, application_id=uuid.uuid4()) is None


# --- 18/19/20. untouched existing behaviour ------------------------------


def test_the_clean_success_path_is_unchanged(db, fake_drive):
    job, link = _apply_ready(db)
    result = _submit(db, link.token)
    assert result.outcome == SubmissionOutcome.SUCCESS
    assert result.application_id
    assert _doc_count(db, result.application_id) == 1
    assert _token_of(db, result.application_id) is None
    assert db.execute(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.entity_id == uuid.UUID(str(result.application_id)),
            AuditEvent.event_type == AuditEventType.CANDIDATE_APPLIED.value,
        )
    ).scalar_one() == 1


# =====================================================================
# Fix 3 — ?retry= entry point routing
# =====================================================================


_RETRY_PATCHED_ATTRS = (
    "_render_retry_resume",
    "_render_processing",
    "_render_screening_resume",
    "_token_from_url",
    "_screening_token_from_url",
    "_retry_token_from_url",
    "_render_invalid",
    "_same_session_screening_is_complete",
)


@pytest.fixture(autouse=True)
def _restore_public_main_retry():
    saved = {name: getattr(P, name) for name in _RETRY_PATCHED_ATTRS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(P, name, value)


def _retry_routing_script(*, url_retry: str, session_retry: str | None,
                          processing_app: str | None) -> str:
    lines = []
    if session_retry is not None:
        lines.append(
            f"P.remember_retry_token(st.session_state, {session_retry!r})"
        )
    if processing_app is not None:
        lines.append(
            f"P.remember_processing_application(st.session_state, "
            f"{processing_app!r})"
        )
    seed = "\n".join(lines)

    return f'''
import streamlit as st
import app.public_main as P

st.session_state.setdefault("branch", None)

P._same_session_screening_is_complete = lambda app_id: False
P._retry_token_from_url = lambda: {url_retry!r}
P._token_from_url = lambda: ""
P._screening_token_from_url = lambda: ""
P._render_retry_resume = lambda token: st.session_state.__setitem__(
    "branch", f"retry:{{token}}"
)
P._render_processing = lambda app_id: st.session_state.__setitem__(
    "branch", f"processing:{{app_id}}"
)
P._render_screening_resume = lambda token: st.session_state.__setitem__(
    "branch", f"resume:{{token}}"
)
P._render_invalid = lambda: st.session_state.__setitem__("branch", "invalid")

{seed}
P._page()
# recorded inside the script, where session_state is a real mapping
st.session_state["retry_left"] = P.get_retry_token(st.session_state) or ""
'''


def _run_retry(**kwargs) -> AppTest:
    at = AppTest.from_string(
        _retry_routing_script(**kwargs), default_timeout=_TIMEOUT
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_a_retry_url_param_routes_to_the_retry_entry_point():
    at = _run_retry(url_retry="t" * 43, session_retry=None, processing_app=None)
    assert at.session_state["branch"] == "retry:" + "t" * 43


def test_the_retry_token_survives_a_dropped_url_param_via_session_state():
    at = _run_retry(url_retry="", session_retry="s" * 43, processing_app=None)
    assert at.session_state["branch"] == "retry:" + "s" * 43


def test_the_url_retry_token_wins_over_the_session_copy():
    at = _run_retry(url_retry="u" * 43, session_retry="s" * 43, processing_app=None)
    assert at.session_state["branch"] == "retry:" + "u" * 43


def test_once_the_resume_lands_the_candidate_goes_to_the_pipeline_view():
    """After a successful retry upload ``_apply_result`` sets the processing
    application, and the retry token has correctly stopped resolving. The
    candidate must hand over to the pipeline view — the same place the normal
    submission flow lands them.

    Two ways this could regress, both asserted: re-entering the retry branch
    would show "this link is no longer valid", and falling through to the
    job-link flow would show the invalid-application-link message (a ``?retry=``
    URL carries no ``?token=``). Either would greet a candidate who just
    succeeded with a failure message.
    """
    at = _run_retry(
        url_retry="t" * 43, session_retry="t" * 43, processing_app="app-1"
    )
    assert at.session_state["branch"] == "processing:app-1"


def test_the_spent_retry_token_is_cleared_from_session_state():
    """Housekeeping, so a later rerun cannot re-enter the retry branch with a
    credential that no longer resolves."""
    at = _run_retry(
        url_retry="t" * 43, session_retry="t" * 43, processing_app="app-1"
    )
    assert at.session_state["retry_left"] == ""


def test_no_retry_token_leaves_the_job_link_flow_untouched():
    at = _run_retry(url_retry="", session_retry=None, processing_app=None)
    assert at.session_state["branch"] == "invalid"
