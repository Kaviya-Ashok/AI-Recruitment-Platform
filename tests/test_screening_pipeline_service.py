"""Tests for app.services.screening_pipeline_service (CLAUDE.md §2A items 1, 6, 7).

Mocking boundaries (project convention):
* ``app.services.resume_parsing_service.get_structured_response`` and
  ``app.services.prequalification_service.get_structured_response`` are patched
  — the real Claude API is never touched.
* Google Drive is faked via the same ``_drive_*`` seams as
  ``test_resume_parsing_service`` / ``test_storage_service``.

Real Postgres via the savepoint-rollback ``db`` fixture.

Step 2 stage order (SESSION FIRST):
    1. SCREENING_SESSION  — row created immediately, status PENDING, token minted,
                            AI_SCREENING_STARTED emitted
    2. RESUME_PARSING
    3. PREQUALIFICATION
    4. FINALIZE           — session -> READY_FOR_ROUND_1, application ->
                            SCREENING_IN_PROGRESS

The load-bearing properties under test:
1. idempotency across FIVE paths — single flow, rerun/refresh, resumed-via-link,
   concurrent overlap, HR manual recovery — none duplicate a row / AI call /
   audit event;
2. resumability — a failed stage is retried, completed stages are not redone;
3. prequalification never gates progression to READY_FOR_ROUND_1;
4. every audit event carries the SYSTEM actor, never ``user_id=None``;
5. the ``access_token`` never leaks (audit metadata, resolver outcomes, logs).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.ai.schemas.prequalification import PrequalificationAssessment
from app.ai.schemas.resume_parsing import ResumeExtractionResult
from app.ai.schemas.screening_questions import (
    ScreeningQuestion as AIScreeningQuestion,
)
from app.ai.schemas.screening_questions import ScreeningQuestionSet
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.prequalification_result import PrequalificationResult
from app.database.models.resume_extraction import ResumeExtraction
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
from app.services import (
    prequalification_service,
    resume_parsing_service,
    screening_pipeline_service,
    screening_question_service,
    storage_service,
)
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.screening_pipeline_service import (
    STALLED_PIPELINE_THRESHOLD,
    PipelineApplicationNotFoundError,
    PipelineOutcome,
    PipelineStage,
    ScreeningAccessOutcome,
    advance_screening_pipeline,
    get_candidate_screening_token,
    get_pipeline_state,
    get_screening_session_for_application,
    list_stalled_screening_applications,
    resolve_screening_access_token,
    resume_stalled_pipeline,
    run_screening_pipeline,
)
from app.services.storage_service import upload_document
from app.utils.authorization import UnauthorizedError

_PDF_MIME = "application/pdf"

_CRITERIA_SPECS = [
    ("MANDATORY", "Technical Skill", "5+ years of professional Python development"),
    ("MANDATORY", "Distributed Systems", "Hands-on experience with Apache Spark / PySpark"),
    ("PREFERRED", "Messaging", "Kafka or an equivalent event-streaming platform"),
    ("BEHAVIORAL", "Collaboration", "Has mentored or coached junior engineers"),
    ("OTHER", "Logistics", "Available to work on-site in Berlin"),
]

_ALL_STAGES = (
    PipelineStage.SCREENING_SESSION,
    PipelineStage.RESUME_PARSING,
    PipelineStage.PREQUALIFICATION,
    PipelineStage.FINALIZE,
    PipelineStage.ROUND_1_QUESTIONS,
)


# --- fake drive (same seams as test_resume_parsing_service) -----------


class _FakeDrive:
    def __init__(self) -> None:
        self.folders: dict[str, str] = {}
        self.files: dict[str, tuple[str, bytes, str]] = {}

    def find_folder(self, service, root_folder_id, name):
        return self.folders.get(name)

    def create_folder(self, service, root_folder_id, name):
        fid = f"folder-{len(self.folders) + 1}"
        self.folders[name] = fid
        return fid

    def upload_file(self, service, folder_id, name, file_bytes, mime_type):
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


# --- seeding --------------------------------------------------------


def _seed(db, fake_drive, sample_pdf_bytes, *, with_document=True):
    """A job with an approved rubric, an open link, and one application whose
    résumé has been uploaded (but not parsed)."""
    user = create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )
    job = create_job(
        db,
        title="Backend Engineer",
        department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()

    rubric = RubricVersion(
        job_id=job.id,
        version_number=1,
        status=RubricVersionStatus.APPROVED,
        generated_from_requirements_version=1,
        created_by=user.id,
    )
    db.add(rubric)
    db.flush()

    for i, (rtype, cat, txt) in enumerate(_CRITERIA_SPECS, start=1):
        db.add(
            RubricCriterion(
                rubric_version_id=rubric.id,
                requirement_type=rtype,
                category=cat,
                criterion_text=txt,
                display_order=i,
            )
        )
    db.flush()

    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    application = create_application(
        db,
        job_id=job.id,
        application_link_id=link.id,
        email=f"cand-{uuid.uuid4().hex}@example.com",
        full_name="Casey Candidate",
        phone=None,
    )

    if with_document:
        upload_document(
            db,
            application_id=application.id,
            job_id=job.id,
            file_bytes=sample_pdf_bytes,
            original_filename="cv.pdf",
            mime_type=_PDF_MIME,
        )

    return application


def _hr_user_id(db):
    return create_user(
        db=db,
        email=f"hr2-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Two",
        role=UserRole.HR,
    ).id


def _round_1_question_set(n: int = 5) -> ScreeningQuestionSet:
    cats = ["GAP", "CV", "JD", "BEHAVIORAL", "GAP", "CV", "JD", "BEHAVIORAL"]
    return ScreeningQuestionSet(
        questions=[
            AIScreeningQuestion(
                category=cats[i % len(cats)],
                criterion_id=None,
                question_text=f"Round 1 question {i + 1}: tell us more.",
                generated_reason=f"probes an open point ({i + 1}).",
            )
            for i in range(n)
        ]
    )


def _patch_both_ai(mocker, ai_response_dict, *, prequal_fixture="prequalification_valid.json"):
    """Patch every AI-task ``get_structured_response`` import site.

    Returns ``(resume_spy, prequal_spy, sq_spy)`` — the third being the
    screening-question generator, which the pipeline now calls as its final
    stage. Tests assert call counts to prove "no duplicate AI call".
    """
    resume_result = ResumeExtractionResult.model_validate(
        ai_response_dict("resume_parsing_valid.json")
    )
    prequal_result = PrequalificationAssessment.model_validate(
        ai_response_dict(prequal_fixture)
    )
    resume_spy = mocker.patch.object(
        resume_parsing_service, "get_structured_response",
        return_value=resume_result,
    )
    prequal_spy = mocker.patch.object(
        prequalification_service, "get_structured_response",
        return_value=prequal_result,
    )
    sq_spy = mocker.patch.object(
        screening_question_service, "get_structured_response",
        return_value=_round_1_question_set(),
    )
    return resume_spy, prequal_spy, sq_spy


# --- counting helpers ----------------------------------------------


def _session_count(db, application_id) -> int:
    return db.execute(
        select(func.count()).select_from(ScreeningSession)
        .where(ScreeningSession.application_id == application_id)
    ).scalar_one()


def _prequal_count(db, application_id) -> int:
    return db.execute(
        select(func.count()).select_from(PrequalificationResult)
        .where(PrequalificationResult.application_id == application_id)
    ).scalar_one()


def _extraction_count(db, application_id) -> int:
    from app.database.models.document import Document

    return db.execute(
        select(func.count())
        .select_from(ResumeExtraction)
        .join(Document, Document.id == ResumeExtraction.document_id)
        .where(Document.application_id == application_id)
    ).scalar_one()


def _started_events(db, application_id):
    session_ids = db.execute(
        select(ScreeningSession.id).where(
            ScreeningSession.application_id == application_id
        )
    ).scalars().all()
    if not session_ids:
        return []
    return db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.AI_SCREENING_STARTED.value,
            AuditEvent.entity_id.in_(session_ids),
        )
    ).scalars().all()


def _session(db, application_id):
    return db.execute(
        select(ScreeningSession).where(
            ScreeningSession.application_id == application_id
        )
    ).scalar_one_or_none()


def _round_question_count(db, session_id, round_) -> int:
    from app.database.models.screening_question import ScreeningQuestion

    return db.execute(
        select(func.count()).select_from(ScreeningQuestion).where(
            ScreeningQuestion.screening_session_id == session_id,
            ScreeningQuestion.round == round_,
        )
    ).scalar_one()


def _questions_generated_events(db, session_id):
    return db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type
            == AuditEventType.SCREENING_QUESTIONS_GENERATED.value,
            AuditEvent.entity_id == session_id,
        )
    ).scalars().all()


# =====================================================================
# HAPPY PATH — new ordering
# =====================================================================


def test_full_pipeline_runs_five_stages_session_first_round1_last(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)

    result = run_screening_pipeline(db, application_id=application.id)

    assert result.outcome == PipelineOutcome.COMPLETED
    assert result.is_complete
    assert result.stages_run == _ALL_STAGES
    assert set(result.state.completed_stages) == set(_ALL_STAGES)

    assert _extraction_count(db, application.id) == 1
    assert _prequal_count(db, application.id) == 1
    assert _session_count(db, application.id) == 1
    assert resume_spy.call_count == 1
    assert prequal_spy.call_count == 1
    assert sq_spy.call_count == 1  # round-1 questions generated exactly once

    session = _session(db, application.id)
    # Automatic pipeline ends with round-1 questions ready and the candidate
    # answering them.
    assert session.status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    assert _round_question_count(db, session.id, 1) == 5

    db.refresh(application)
    assert application.status == ApplicationStatus.SCREENING_IN_PROGRESS


def test_session_and_token_exist_after_the_very_first_stage(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    """The whole point of Step 2: durable row + credential before any AI work."""
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    first = advance_screening_pipeline(db, application_id=application.id)

    assert first.stages_run == (PipelineStage.SCREENING_SESSION,)
    assert first.outcome == PipelineOutcome.IN_PROGRESS
    assert _session_count(db, application.id) == 1
    assert _extraction_count(db, application.id) == 0  # no AI work yet
    assert _prequal_count(db, application.id) == 0

    session = _session(db, application.id)
    assert session.status == ScreeningSessionStatus.PENDING
    # secrets.token_urlsafe(32) -> 43 URL-safe chars.
    assert len(session.access_token) == 43
    assert set(session.access_token) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )

    db.refresh(application)
    assert application.status == ApplicationStatus.APPLIED  # not advanced yet


def test_advance_runs_exactly_one_stage_per_call_new_order(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    seen = []
    for _ in range(5):
        r = advance_screening_pipeline(db, application_id=application.id)
        seen.append(r.stages_run)

    assert seen == [
        (PipelineStage.SCREENING_SESSION,),
        (PipelineStage.RESUME_PARSING,),
        (PipelineStage.PREQUALIFICATION,),
        (PipelineStage.FINALIZE,),
        (PipelineStage.ROUND_1_QUESTIONS,),
    ]
    assert advance_screening_pipeline(
        db, application_id=application.id
    ).outcome == PipelineOutcome.COMPLETED
    assert _session(db, application.id).status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS


def test_finalize_only_fires_once_both_ai_stages_are_done(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    advance_screening_pipeline(db, application_id=application.id)  # session
    advance_screening_pipeline(db, application_id=application.id)  # resume
    # resume done, prequal NOT done -> next stage is PREQUALIFICATION, not FINALIZE
    state = get_pipeline_state(db, application_id=application.id)
    assert state.next_stage == PipelineStage.PREQUALIFICATION
    assert _session(db, application.id).status == ScreeningSessionStatus.PENDING


# =====================================================================
# AUDIT — the system actor, never user_id=None; token never leaks
# =====================================================================


def test_screening_started_event_is_emitted_at_session_creation(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    advance_screening_pipeline(db, application_id=application.id)  # session only

    events = _started_events(db, application.id)
    assert len(events) == 1
    event = events[0]
    assert event.user_id == SYSTEM_USER_ID
    assert event.user_id is not None
    assert event.entity_type == "screening_session"
    # Session created BEFORE any pipeline work -> application still APPLIED.
    assert event.previous_state["application_status"] == ApplicationStatus.APPLIED
    assert event.new_state["application_status"] == ApplicationStatus.APPLIED
    assert event.new_state["screening_session_status"] == ScreeningSessionStatus.PENDING


def test_finalize_emits_no_ai_screening_completed_event(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    """AI_SCREENING_COMPLETED is RESERVED for real round completion."""
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    run_screening_pipeline(db, application_id=application.id)

    session_ids = db.execute(
        select(ScreeningSession.id).where(
            ScreeningSession.application_id == application.id
        )
    ).scalars().all()
    completed = db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.AI_SCREENING_COMPLETED.value,
            AuditEvent.entity_id.in_(session_ids),
        )
    ).scalar_one()
    assert completed == 0
    # Exactly one STARTED, no more.
    assert len(_started_events(db, application.id)) == 1


def test_no_pipeline_audit_event_has_a_null_actor(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    run_screening_pipeline(db, application_id=application.id)

    pipeline_types = [
        AuditEventType.RESUME_PROCESSED.value,
        AuditEventType.PREQUALIFICATION_COMPLETED.value,
        AuditEventType.AI_SCREENING_STARTED.value,
    ]
    events = db.execute(
        select(AuditEvent).where(AuditEvent.event_type.in_(pipeline_types))
    ).scalars().all()
    mine = [
        e for e in events
        if e.new_state and (
            e.new_state.get("application_id") == str(application.id)
            or e.entity_id == application.id
        )
    ]
    assert mine
    for event in mine:
        assert event.user_id == SYSTEM_USER_ID


def test_access_token_never_appears_in_audit_metadata(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    run_screening_pipeline(db, application_id=application.id)
    session = _session(db, application.id)
    event = _started_events(db, application.id)[0]

    blob = f"{event.action}{event.previous_state}{event.new_state}{event.event_metadata}"
    assert session.access_token not in blob
    assert event.new_state["access_token_present"] is True


# =====================================================================
# IDEMPOTENCY PATH 1 — normal single flow, re-run to completion
# =====================================================================


def test_rerunning_after_full_success_does_no_work_and_duplicates_nothing(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)

    run_screening_pipeline(db, application_id=application.id)
    first = _session(db, application.id)
    first_token, first_id = first.access_token, first.id

    second = run_screening_pipeline(db, application_id=application.id)

    assert second.outcome == PipelineOutcome.COMPLETED
    assert second.stages_run == ()
    assert resume_spy.call_count == 1
    assert prequal_spy.call_count == 1
    assert sq_spy.call_count == 1  # round-1 questions not re-generated
    assert _extraction_count(db, application.id) == 1
    assert _prequal_count(db, application.id) == 1
    assert _session_count(db, application.id) == 1
    assert len(_started_events(db, application.id)) == 1
    assert len(_questions_generated_events(db, first_id)) == 1
    assert _round_question_count(db, first_id, 1) == 5

    again = _session(db, application.id)
    assert again.id == first_id
    assert again.access_token == first_token
    assert again.status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS


# =====================================================================
# IDEMPOTENCY PATH 2 — browser refresh / repeated advance
# =====================================================================


def test_repeated_advance_after_completion_is_a_no_op(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)
    run_screening_pipeline(db, application_id=application.id)

    for _ in range(6):
        r = advance_screening_pipeline(db, application_id=application.id)
        assert r.outcome == PipelineOutcome.COMPLETED
        assert r.stages_run == ()

    assert resume_spy.call_count == 1
    assert prequal_spy.call_count == 1
    assert sq_spy.call_count == 1
    assert _session_count(db, application.id) == 1
    assert len(_started_events(db, application.id)) == 1


def test_refresh_mid_pipeline_never_duplicates(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    """Interleave get_pipeline_state (page load) with advance (the work),
    several times, as a real browser rerun loop would."""
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)

    for _ in range(10):
        get_pipeline_state(db, application_id=application.id)          # page load
        r = advance_screening_pipeline(db, application_id=application.id)
        if r.outcome == PipelineOutcome.COMPLETED:
            break

    assert resume_spy.call_count == 1
    assert prequal_spy.call_count == 1
    assert _session_count(db, application.id) == 1
    assert _extraction_count(db, application.id) == 1
    assert _prequal_count(db, application.id) == 1
    assert len(_started_events(db, application.id)) == 1


# =====================================================================
# IDEMPOTENCY PATH 3 — resumed via saved ?screening= link
# =====================================================================


def test_resume_via_access_token_resolves_and_finishes_without_duplication(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)

    # Candidate's browser only gets as far as creating the session, then closes.
    advance_screening_pipeline(db, application_id=application.id)
    token = get_candidate_screening_token(db, application_id=application.id)
    assert token is not None

    # Later: they open the saved link. The resolver maps token -> application.
    resolution = resolve_screening_access_token(db, token)
    assert resolution.is_valid
    assert resolution.application_id == application.id

    # Resuming from there completes the pipeline, no duplicate work.
    result = run_screening_pipeline(
        db, application_id=resolution.application_id
    )
    assert result.outcome == PipelineOutcome.COMPLETED
    assert PipelineStage.SCREENING_SESSION not in result.stages_run  # already existed
    assert resume_spy.call_count == 1
    assert prequal_spy.call_count == 1
    assert _session_count(db, application.id) == 1
    assert len(_started_events(db, application.id)) == 1
    # Same token throughout — the credential the candidate saved still works.
    assert get_candidate_screening_token(db, application_id=application.id) == token


# =====================================================================
# IDEMPOTENCY PATH 4 — concurrent overlapping session creation
# =====================================================================


def test_concurrent_session_creation_returns_state_not_spurious_failed(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    """A second caller that loses the race on uq_screening_sessions_application
    must adopt the winner's row and return a correct in-progress state — never
    FAILED, never a duplicate row."""
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    # Call 1 creates the session normally (savepoint-released, survives a later
    # rollback in call 2).
    first = advance_screening_pipeline(db, application_id=application.id)
    assert first.stages_run == (PipelineStage.SCREENING_SESSION,)
    winner = _session(db, application.id)

    # Force call 2 to believe no session exists (both _build_state and
    # _ensure_screening_session read through this one function), so it proceeds
    # to INSERT and hits the real UNIQUE constraint from the row above.
    real_getter = screening_pipeline_service.get_screening_session_for_application
    calls = {"n": 0}

    def blind_then_real(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:  # the _build_state read + the _ensure pre-check
            return None
        return real_getter(*args, **kwargs)

    mocker.patch.object(
        screening_pipeline_service,
        "get_screening_session_for_application",
        side_effect=blind_then_real,
    )

    second = advance_screening_pipeline(db, application_id=application.id)

    assert second.outcome != PipelineOutcome.FAILED
    assert second.outcome == PipelineOutcome.IN_PROGRESS
    assert second.stages_run == ()  # the competitor's row, not our work
    assert _session_count(db, application.id) == 1  # exactly one
    assert _session(db, application.id).id == winner.id
    assert len(_started_events(db, application.id)) == 1  # no second STARTED


def test_unique_violation_helper_only_matches_our_constraint():
    from sqlalchemy.exc import IntegrityError

    class _Diag:
        constraint_name = "uq_screening_sessions_application"

    class _Orig:
        diag = _Diag()

    ours = IntegrityError("stmt", {}, Exception("boom"))
    ours.orig = _Orig()
    assert screening_pipeline_service._is_unique_application_violation(ours)

    class _OtherDiag:
        constraint_name = "some_other_unique_ix"

    class _OtherOrig:
        diag = _OtherDiag()

    other = IntegrityError("stmt", {}, Exception("boom"))
    other.orig = _OtherOrig()
    assert not screening_pipeline_service._is_unique_application_violation(other)


# =====================================================================
# IDEMPOTENCY PATH 5 — HR manual recovery of a stalled pipeline
# =====================================================================


def test_hr_manual_recovery_is_system_attributed_and_idempotent(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)
    hr_id = _hr_user_id(db)

    # Candidate's browser stalls after session creation.
    advance_screening_pipeline(db, application_id=application.id)

    result = resume_stalled_pipeline(
        db, application_id=application.id, requested_by_user_id=hr_id
    )
    assert result.outcome == PipelineOutcome.COMPLETED

    # SYSTEM, never the HR user, on every event.
    pipeline_events = db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type.in_([
                AuditEventType.RESUME_PROCESSED.value,
                AuditEventType.PREQUALIFICATION_COMPLETED.value,
                AuditEventType.AI_SCREENING_STARTED.value,
                AuditEventType.SCREENING_QUESTIONS_GENERATED.value,
            ])
        )
    ).scalars().all()
    mine = [
        e for e in pipeline_events
        if e.new_state and (
            e.new_state.get("application_id") == str(application.id)
            or e.entity_id == application.id
        )
    ]
    assert mine
    for e in mine:
        assert e.user_id == SYSTEM_USER_ID
        assert e.user_id != hr_id

    # The round-1 SCREENING_QUESTIONS_GENERATED event is SYSTEM-attributed too,
    # even though a human kicked the recovery.
    session = _session(db, application.id)
    for e in _questions_generated_events(db, session.id):
        assert e.user_id == SYSTEM_USER_ID
        assert e.user_id != hr_id

    # Recovery is idempotent: a second click does nothing.
    again = resume_stalled_pipeline(
        db, application_id=application.id, requested_by_user_id=hr_id
    )
    assert again.outcome == PipelineOutcome.COMPLETED
    assert again.stages_run == ()
    assert resume_spy.call_count == 1
    assert prequal_spy.call_count == 1
    assert sq_spy.call_count == 1
    assert _session_count(db, application.id) == 1
    assert len(_started_events(db, application.id)) == 1
    assert len(_questions_generated_events(db, session.id)) == 1


def test_resume_stalled_pipeline_requires_an_active_internal_user(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    for bad in (None, "not-a-uuid", uuid.uuid4()):
        with pytest.raises(UnauthorizedError):
            resume_stalled_pipeline(
                db, application_id=application.id, requested_by_user_id=bad
            )
    # Nothing ran.
    assert _session_count(db, application.id) == 0


# --- stalled query -------------------------------------------------


def test_list_stalled_only_returns_pending_sessions_past_the_threshold(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    fresh = _seed(db, fake_drive, sample_pdf_bytes)
    stale = _seed(db, fake_drive, sample_pdf_bytes)
    done = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)
    hr_id = _hr_user_id(db)

    # fresh: session just created -> PENDING, recent
    advance_screening_pipeline(db, application_id=fresh.id)
    # stale: session created, then backdate updated_at well past the threshold
    advance_screening_pipeline(db, application_id=stale.id)
    stale_session = _session(db, stale.id)
    stale_session.updated_at = datetime.now(timezone.utc) - (
        STALLED_PIPELINE_THRESHOLD + timedelta(minutes=5)
    )
    db.flush()
    # done: fully finished (round-1 questions ready, ROUND_1_IN_PROGRESS). It IS
    # a stallable status — so backdate it and expect it flagged as MID_SCREENING.
    run_screening_pipeline(db, application_id=done.id)
    done_session = _session(db, done.id)
    done_session.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
    db.flush()

    stalled = list_stalled_screening_applications(db, acting_user_id=hr_id)
    by_id = {s.application_id: s for s in stalled}

    assert stale.id in by_id
    assert by_id[stale.id].is_pre_round_1 is True
    assert fresh.id not in by_id     # too recent
    assert done.id in by_id          # ROUND_1_IN_PROGRESS is a mid-screening stall
    assert by_id[done.id].is_pre_round_1 is False
    assert by_id[done.id].status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    # No token on the DTO.
    for s in stalled:
        assert not hasattr(s, "access_token")


def test_list_stalled_is_hr_only(db):
    for bad in (None, "nope", uuid.uuid4()):
        with pytest.raises(UnauthorizedError):
            list_stalled_screening_applications(db, acting_user_id=bad)


def test_stalled_threshold_is_a_named_constant():
    assert isinstance(STALLED_PIPELINE_THRESHOLD, timedelta)
    assert STALLED_PIPELINE_THRESHOLD == timedelta(minutes=15)


# =====================================================================
# access_token RESOLVER — one generic outcome for every failure
# =====================================================================


def test_resolver_returns_application_id_for_a_valid_token(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)
    advance_screening_pipeline(db, application_id=application.id)
    token = get_candidate_screening_token(db, application_id=application.id)

    resolution = resolve_screening_access_token(db, token)
    assert resolution.outcome == ScreeningAccessOutcome.VALID
    assert resolution.is_valid
    assert resolution.application_id == application.id


@pytest.mark.parametrize(
    "bad_token",
    [
        None,
        "",
        "   ",
        "short",                                    # too short for the shape
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",         # right length, illegal chars
        "x" * 200,                                    # too long
        "Zm9vYmFyYmF6cXV4Y29ycXV1eGNvcnJlY3Rob3JzZQ",  # well-formed but unknown
    ],
)
def test_resolver_gives_one_generic_invalid_for_every_failure(db, bad_token):
    resolution = resolve_screening_access_token(db, bad_token)
    assert resolution.outcome == ScreeningAccessOutcome.INVALID
    assert not resolution.is_valid
    assert resolution.application_id is None


def test_resolver_never_returns_the_session_or_token(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)
    advance_screening_pipeline(db, application_id=application.id)
    token = get_candidate_screening_token(db, application_id=application.id)

    resolution = resolve_screening_access_token(db, token)
    # Only outcome + application_id — no token, no ScreeningSession.
    assert set(vars(resolution)) == {"outcome", "application_id"}
    assert token not in str(vars(resolution))


def test_get_candidate_screening_token_is_none_before_the_session_exists(
    db, fake_drive, sample_pdf_bytes
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    assert get_candidate_screening_token(db, application_id=application.id) is None


# =====================================================================
# RESUMABILITY — failed stages retried, completed ones not redone
# =====================================================================


def test_retry_after_a_failed_ai_stage_resumes_at_that_stage(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)

    advance_screening_pipeline(db, application_id=application.id)  # session
    advance_screening_pipeline(db, application_id=application.id)  # resume OK
    assert _extraction_count(db, application.id) == 1

    prequal_spy.side_effect = prequalification_service.AIError("boom")
    failed = advance_screening_pipeline(db, application_id=application.id)
    assert failed.outcome == PipelineOutcome.FAILED
    assert failed.failed_stage == PipelineStage.PREQUALIFICATION
    assert _prequal_count(db, application.id) == 0
    assert _session_count(db, application.id) == 1        # session survived
    assert _session(db, application.id).status == ScreeningSessionStatus.PENDING

    prequal_spy.side_effect = None
    recovered = run_screening_pipeline(db, application_id=application.id)
    assert recovered.outcome == PipelineOutcome.COMPLETED
    assert PipelineStage.SCREENING_SESSION not in recovered.stages_run
    assert PipelineStage.RESUME_PARSING not in recovered.stages_run
    assert resume_spy.call_count == 1
    assert _session_count(db, application.id) == 1
    assert len(_started_events(db, application.id)) == 1


def test_resume_parsing_failure_leaves_session_but_no_downstream_rows(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, _, sq_spy = _patch_both_ai(mocker, ai_response_dict)
    resume_spy.side_effect = resume_parsing_service.AIError("down")

    result = run_screening_pipeline(db, application_id=application.id)

    assert result.outcome == PipelineOutcome.FAILED
    assert result.failed_stage == PipelineStage.RESUME_PARSING
    # Session IS created first now — that is the Step 2 guarantee.
    assert _session_count(db, application.id) == 1
    assert _session(db, application.id).status == ScreeningSessionStatus.PENDING
    assert _extraction_count(db, application.id) == 0
    assert _prequal_count(db, application.id) == 0
    db.refresh(application)
    assert application.status == ApplicationStatus.APPLIED


def test_application_without_a_resume_creates_session_then_fails_clearly(
    db, fake_drive, sample_pdf_bytes
):
    application = _seed(db, fake_drive, sample_pdf_bytes, with_document=False)

    result = run_screening_pipeline(db, application_id=application.id)

    assert result.outcome == PipelineOutcome.FAILED
    assert result.failed_stage == PipelineStage.RESUME_PARSING
    assert result.internal_reason == "NO_DOCUMENT"
    assert "résumé" in result.message
    assert _session_count(db, application.id) == 1  # session still created first


# =====================================================================
# PREQUALIFICATION DOES NOT GATE ROUND-1 QUESTIONS (CLAUDE.md §2A item 7)
# =====================================================================


def _force_prequal_result(mocker, ai_response_dict, forced: str):
    base = ai_response_dict("prequalification_valid.json")
    doc = {"assessments": [{**a, "result": forced} for a in base["assessments"]]}
    mocker.patch.object(
        resume_parsing_service, "get_structured_response",
        return_value=ResumeExtractionResult.model_validate(
            ai_response_dict("resume_parsing_valid.json")
        ),
    )
    mocker.patch.object(
        prequalification_service, "get_structured_response",
        return_value=PrequalificationAssessment.model_validate(doc),
    )
    mocker.patch.object(
        screening_question_service, "get_structured_response",
        return_value=_round_1_question_set(),
    )


def test_mandatory_fail_still_reaches_round_1_questions(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _force_prequal_result(mocker, ai_response_dict, "FAIL")

    result = run_screening_pipeline(db, application_id=application.id)

    assert result.outcome == PipelineOutcome.COMPLETED
    session = _session(db, application.id)
    assert session.status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    assert _round_question_count(db, session.id, 1) == 5
    db.refresh(application)
    assert application.status == ApplicationStatus.SCREENING_IN_PROGRESS

    stored = db.execute(
        select(PrequalificationResult).where(
            PrequalificationResult.application_id == application.id
        )
    ).scalar_one()
    mandatory = [r for r in stored.results if r["requirement_type"] == "MANDATORY"]
    assert mandatory and all(r["result"] == "FAIL" for r in mandatory)


@pytest.mark.parametrize("forced", ["PASS", "FAIL", "UNKNOWN"])
def test_every_prequalification_outcome_reaches_round_1_identically(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict, forced
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _force_prequal_result(mocker, ai_response_dict, forced)

    result = run_screening_pipeline(db, application_id=application.id)
    assert result.outcome == PipelineOutcome.COMPLETED
    assert result.state.completed_stages == _ALL_STAGES
    assert _session(db, application.id).status == (
        ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    )


# =====================================================================
# AUTH / STATE / EDGE
# =====================================================================


def test_pipeline_runs_end_to_end_with_only_the_system_actor(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)

    result = run_screening_pipeline(db, application_id=application.id)
    assert result.outcome == PipelineOutcome.COMPLETED
    assert get_screening_session_for_application(
        db, application.id, acting_user_id=SYSTEM_USER_ID
    ) is not None


def test_screening_session_read_is_hr_only(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)
    run_screening_pipeline(db, application_id=application.id)

    for bad in (None, "not-a-uuid", uuid.uuid4()):
        with pytest.raises(UnauthorizedError):
            get_screening_session_for_application(
                db, application.id, acting_user_id=bad
            )


def test_pipeline_fails_safely_when_system_user_missing(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, _, sq_spy = _patch_both_ai(mocker, ai_response_dict)
    db.execute(User.__table__.delete().where(User.id == SYSTEM_USER_ID))
    db.flush()

    result = advance_screening_pipeline(db, application_id=application.id)

    assert result.outcome == PipelineOutcome.FAILED
    assert result.internal_reason == "SystemUserMissingError"
    assert "c9a4f1d7b208" not in result.message
    assert "alembic" not in result.message.lower()
    assert resume_spy.call_count == 0
    assert _session_count(db, application.id) == 0


def test_get_pipeline_state_is_read_only_and_starts_at_session_stage(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    resume_spy, prequal_spy, sq_spy = _patch_both_ai(mocker, ai_response_dict)

    state = get_pipeline_state(db, application_id=application.id)

    assert state.completed_stages == ()
    assert state.next_stage == PipelineStage.SCREENING_SESSION
    assert state.is_complete is False
    assert state.screening_session_id is None
    assert state.screening_session_status is None
    assert resume_spy.call_count == 0
    assert prequal_spy.call_count == 0
    assert _session_count(db, application.id) == 0
    db.refresh(application)
    assert application.status == ApplicationStatus.APPLIED


def test_unknown_application_raises(db):
    with pytest.raises(PipelineApplicationNotFoundError):
        advance_screening_pipeline(db, application_id=uuid.uuid4())
    with pytest.raises(PipelineApplicationNotFoundError):
        get_pipeline_state(db, application_id=uuid.uuid4())
    with pytest.raises(PipelineApplicationNotFoundError):
        run_screening_pipeline(db, application_id=uuid.uuid4())


# =====================================================================
# STEP 3 — round-1 auto-trigger + resolver on terminal/abandoned sessions
# =====================================================================


def test_round_1_generation_fires_once_from_the_pipeline_system_attributed(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _, _, sq_spy = _patch_both_ai(mocker, ai_response_dict)

    run_screening_pipeline(db, application_id=application.id)
    session = _session(db, application.id)

    assert sq_spy.call_count == 1
    assert _round_question_count(db, session.id, 1) == 5
    events = _questions_generated_events(db, session.id)
    assert len(events) == 1
    assert events[0].user_id == SYSTEM_USER_ID
    assert events[0].new_state["round"] == 1
    assert events[0].new_state["question_count"] == 5
    # Safe metadata only — no question text anywhere.
    blob = f"{events[0].action}{events[0].previous_state}{events[0].new_state}"
    assert "Round 1 question" not in blob

    # Re-running the pipeline never re-generates.
    run_screening_pipeline(db, application_id=application.id)
    assert sq_spy.call_count == 1
    assert _round_question_count(db, session.id, 1) == 5
    assert len(_questions_generated_events(db, session.id)) == 1


def test_round_1_generation_failure_is_retryable_and_not_a_dup(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _, _, sq_spy = _patch_both_ai(mocker, ai_response_dict)
    sq_spy.side_effect = screening_question_service.AIError("down")

    failed = run_screening_pipeline(db, application_id=application.id)
    assert failed.outcome == PipelineOutcome.FAILED
    assert failed.failed_stage == PipelineStage.ROUND_1_QUESTIONS
    session = _session(db, application.id)
    assert session.status == ScreeningSessionStatus.READY_FOR_ROUND_1
    assert _round_question_count(db, session.id, 1) == 0

    sq_spy.side_effect = None
    recovered = run_screening_pipeline(db, application_id=application.id)
    assert recovered.outcome == PipelineOutcome.COMPLETED
    assert _round_question_count(db, session.id, 1) == 5
    assert len(_questions_generated_events(db, session.id)) == 1


def test_resolver_completed_token_is_identical_to_a_fabricated_token(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    """CLAUDE.md §2A / anti-enumeration: a SCREENING_COMPLETE session's token
    must resolve exactly like a token that never existed."""
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)
    run_screening_pipeline(db, application_id=application.id)

    session = _session(db, application.id)
    real_token = session.access_token
    # Drive the session to the terminal state directly.
    session.status = ScreeningSessionStatus.SCREENING_COMPLETE
    db.flush()

    fabricated = "A" * 43  # well-formed, never issued
    completed_res = resolve_screening_access_token(db, real_token)
    fabricated_res = resolve_screening_access_token(db, fabricated)

    assert completed_res.outcome == fabricated_res.outcome == ScreeningAccessOutcome.INVALID
    assert completed_res.application_id is fabricated_res.application_id is None
    assert vars(completed_res) == vars(fabricated_res)


def test_resolver_abandoned_application_is_identical_to_a_fabricated_token(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)
    run_screening_pipeline(db, application_id=application.id)
    session = _session(db, application.id)
    real_token = session.access_token

    db.refresh(application)
    application.status = ApplicationStatus.SCREENING_INCOMPLETE
    db.flush()

    abandoned_res = resolve_screening_access_token(db, real_token)
    fabricated_res = resolve_screening_access_token(db, "B" * 43)
    assert vars(abandoned_res) == vars(fabricated_res)
    assert abandoned_res.outcome == ScreeningAccessOutcome.INVALID


@pytest.mark.parametrize(
    "status",
    [
        ScreeningSessionStatus.PENDING,
        ScreeningSessionStatus.READY_FOR_ROUND_1,
        ScreeningSessionStatus.ROUND_1_IN_PROGRESS,
        ScreeningSessionStatus.ROUND_1_COMPLETE,
        ScreeningSessionStatus.ROUND_2_IN_PROGRESS,
    ],
)
def test_resolver_still_resumes_every_in_progress_state(
    db, mocker, fake_drive, sample_pdf_bytes, ai_response_dict, status
):
    application = _seed(db, fake_drive, sample_pdf_bytes)
    _patch_both_ai(mocker, ai_response_dict)
    advance_screening_pipeline(db, application_id=application.id)  # PENDING session
    session = _session(db, application.id)
    session.status = status
    db.flush()

    res = resolve_screening_access_token(db, session.access_token)
    assert res.is_valid
    assert res.application_id == application.id
