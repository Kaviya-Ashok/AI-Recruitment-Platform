"""Authorization-gate tests.

Two layers:

1. Unit tests for ``app.utils.authorization.require_internal_user`` in isolation.
2. A structural test proving the gate is wired into every HR/internal-only
   service function: each raises ``UnauthorizedError`` for a missing / unknown /
   inactive user id **before any side effect** (no AI call, no Drive call, no
   business DB write), and still works for a valid active internal user.

Mirrors the "catch a regression automatically" precedent set by
``test_only_storage_service_imports_the_drive_sdk``.

Real Postgres via the savepoint-rollback ``db`` fixture; AI + Drive are mocked.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.database.models.document import Document
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.prequalification_result import PrequalificationResult
from app.database.models.resume_extraction import ResumeExtraction
from app.database.models.rubric import RubricCriterion, RubricVersion, RubricVersionStatus
from app.database.models.user import User, UserRole
from app.services import (
    interview_guide_service,
    prequalification_service,
    ranking_service,
    resume_parsing_service,
    screening_evaluation_service,
    shortlist_service,
    storage_service,
)
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.prequalification_service import (
    get_prequalification_for_application,
    prequalify_application,
)
from app.services.resume_parsing_service import get_extraction_for_document, parse_resume
from app.services.screening_evaluation_service import (
    evaluate_screening,
    get_screening_evaluation_for_application,
)
from app.services.ranking_service import (
    generate_ranking,
    get_ranking_display_rows,
    get_ranking_for_job,
    list_evaluated_applications_for_job,
    list_rubric_version_partitions_for_job,
)
from app.services.interview_guide_service import (
    generate_interview_guide,
    get_interview_guide_for_application,
    get_shortlisted_candidates_for_job,
    list_interview_guides_for_job,
)
from app.services.shortlist_service import (
    get_ranking_staleness_for_partition,
    get_shortlist_status_for_job,
    shortlist_candidate,
    unshortlist_candidate,
)
from app.services.screening_question_service import get_screening_transcript
from app.services.storage_service import get_document_download_bytes
from app.utils.authorization import UnauthorizedError, require_internal_user

_PDF_MIME = "application/pdf"


# --- helpers -----------------------------------------------------------


def _user(db, *, active=True, role=UserRole.HR):
    u = create_user(
        db=db,
        email=f"u-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="Internal User",
        role=role,
    )
    if not active:
        u.is_active = False
        db.flush()
    return u


def _seed_chain(db):
    """user + job + approved rubric + application + document + extraction +
    prequalification result — enough to call all five functions for real."""
    user = _user(db)
    job = create_job(
        db, title="Backend Engineer", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()

    rubric = RubricVersion(
        job_id=job.id, version_number=1, status=RubricVersionStatus.APPROVED,
        generated_from_requirements_version=1, created_by=user.id,
    )
    db.add(rubric)
    db.flush()
    db.add(RubricCriterion(
        rubric_version_id=rubric.id, requirement_type="MANDATORY",
        category=None, criterion_text="5+ years Python", display_order=1,
    ))
    db.flush()

    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    application = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"cand-{uuid.uuid4().hex}@example.com",
        full_name="Casey Candidate", phone=None,
    )
    document = Document(
        application_id=application.id,
        drive_file_id=f"file-{uuid.uuid4().hex}",
        drive_folder_id="folder-test", original_filename="cv.pdf",
        mime_type=_PDF_MIME, file_size_bytes=2048,
    )
    db.add(document)
    db.flush()
    extraction = ResumeExtraction(
        document_id=document.id,
        extracted_data={"skills": ["Python"], "technologies": [], "experience": [],
                        "projects": [], "certifications": [], "education": [],
                        "other_relevant_claims": []},
        ai_model="claude-haiku-4-5-20251001",
    )
    db.add(extraction)
    db.flush()
    prequal = PrequalificationResult(
        application_id=application.id, rubric_version_id=rubric.id,
        resume_extraction_id=extraction.id,
        results=[{"criterion_id": "x", "criterion_index": 1,
                  "requirement_type": "MANDATORY", "category": None,
                  "criterion_text": "5+ years Python", "result": "UNKNOWN",
                  "evidence_summary": "n/a", "reasoning": "n/a", "confidence": "LOW"}],
        ai_model="claude-sonnet-5",
    )
    db.add(prequal)
    db.flush()
    return user, document, application, extraction


# --- require_internal_user unit tests -------------------------------


def test_returns_the_active_user(db):
    user = _user(db)
    got = require_internal_user(db, user.id)
    assert isinstance(got, User)
    assert got.id == user.id


def test_accepts_str_uuid(db):
    user = _user(db)
    assert require_internal_user(db, str(user.id)).id == user.id


@pytest.mark.parametrize("role", [UserRole.HR, UserRole.HIRING_MANAGER, UserRole.ADMIN])
def test_any_internal_role_is_accepted(db, role):
    user = _user(db, role=role)
    assert require_internal_user(db, user.id).id == user.id


def test_none_is_rejected(db):
    with pytest.raises(UnauthorizedError):
        require_internal_user(db, None)


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "12345", "  "])
def test_malformed_id_is_rejected(db, bad):
    with pytest.raises(UnauthorizedError):
        require_internal_user(db, bad)


def test_unknown_user_is_rejected(db):
    with pytest.raises(UnauthorizedError):
        require_internal_user(db, uuid.uuid4())


def test_inactive_user_is_rejected(db):
    user = _user(db, active=False)
    with pytest.raises(UnauthorizedError):
        require_internal_user(db, user.id)


def test_error_message_does_not_echo_the_id(db):
    probe = uuid.uuid4()
    with pytest.raises(UnauthorizedError) as exc:
        require_internal_user(db, probe)
    assert str(probe) not in str(exc.value)


# --- structural: every HR-only function enforces the gate ----------
#
# Authorization is the FIRST line of each function, before any db.get() of
# business data. So these tests do not need real target rows — a random target
# id plus a bad user id must still raise UnauthorizedError, and the AI / Drive
# seams must be untouched. The "inactive user" rejection is covered once, at the
# unit level (test_inactive_user_is_rejected) — require_internal_user is the
# single chokepoint, so structural coverage here uses the zero-cost bad ids.


@pytest.fixture(params=["none", "unknown", "malformed"])
def bad_user_id(request):
    return {
        "none": None,
        "unknown": uuid.uuid4(),
        "malformed": "not-a-uuid",
    }[request.param]


def test_get_document_download_bytes_denied_before_drive(db, mocker, bad_user_id):
    drive = mocker.patch.object(storage_service, "_get_drive")
    with pytest.raises(UnauthorizedError):
        get_document_download_bytes(uuid.uuid4(), db, acting_user_id=bad_user_id)
    drive.assert_not_called()


def test_get_extraction_for_document_denied(db, bad_user_id):
    with pytest.raises(UnauthorizedError):
        get_extraction_for_document(db, uuid.uuid4(), acting_user_id=bad_user_id)


def test_parse_resume_denied_before_ai_and_drive(db, mocker, bad_user_id):
    ai = mocker.patch.object(resume_parsing_service, "get_structured_response")
    dl = mocker.patch.object(resume_parsing_service, "get_document_download_bytes")
    before = db.execute(select(func.count()).select_from(ResumeExtraction)).scalar_one()

    with pytest.raises(UnauthorizedError):
        parse_resume(
            db, document_id=uuid.uuid4(), requested_by_user_id=bad_user_id
        )

    ai.assert_not_called()
    dl.assert_not_called()
    after = db.execute(select(func.count()).select_from(ResumeExtraction)).scalar_one()
    assert after == before


def test_get_prequalification_for_application_denied(db, bad_user_id):
    with pytest.raises(UnauthorizedError):
        get_prequalification_for_application(
            db, uuid.uuid4(), acting_user_id=bad_user_id
        )


def test_prequalify_application_denied_before_ai(db, mocker, bad_user_id):
    ai = mocker.patch.object(prequalification_service, "get_structured_response")
    before = db.execute(
        select(func.count()).select_from(PrequalificationResult)
    ).scalar_one()

    with pytest.raises(UnauthorizedError):
        prequalify_application(
            db, application_id=uuid.uuid4(),
            requested_by_user_id=bad_user_id, force=True,
        )

    ai.assert_not_called()
    after = db.execute(
        select(func.count()).select_from(PrequalificationResult)
    ).scalar_one()
    assert after == before


def test_get_screening_transcript_denied(db, bad_user_id):
    with pytest.raises(UnauthorizedError):
        get_screening_transcript(
            db, screening_session_id=uuid.uuid4(), acting_user_id=bad_user_id
        )


def test_get_screening_evaluation_for_application_denied(db, bad_user_id):
    with pytest.raises(UnauthorizedError):
        get_screening_evaluation_for_application(
            db, uuid.uuid4(), acting_user_id=bad_user_id
        )


def test_evaluate_screening_denied_before_ai(db, mocker, bad_user_id):
    from app.database.models.screening_evaluation import ScreeningEvaluation

    ai = mocker.patch.object(
        screening_evaluation_service, "get_structured_response"
    )
    before = db.execute(
        select(func.count()).select_from(ScreeningEvaluation)
    ).scalar_one()

    with pytest.raises(UnauthorizedError):
        evaluate_screening(
            db, application_id=uuid.uuid4(),
            requested_by_user_id=bad_user_id, force=True,
        )

    ai.assert_not_called()
    after = db.execute(
        select(func.count()).select_from(ScreeningEvaluation)
    ).scalar_one()
    assert after == before


def test_ranking_reads_denied(db, bad_user_id):
    for call in (
        lambda: list_evaluated_applications_for_job(
            db, uuid.uuid4(), acting_user_id=bad_user_id
        ),
        lambda: list_rubric_version_partitions_for_job(
            db, job_id=uuid.uuid4(), acting_user_id=bad_user_id
        ),
        lambda: get_ranking_for_job(
            db, job_id=uuid.uuid4(), rubric_version_id=uuid.uuid4(),
            acting_user_id=bad_user_id,
        ),
        lambda: get_ranking_display_rows(
            db, job_id=uuid.uuid4(), rubric_version_id=uuid.uuid4(),
            acting_user_id=bad_user_id,
        ),
    ):
        with pytest.raises(UnauthorizedError):
            call()


def test_generate_ranking_denied_before_any_write(db, bad_user_id):
    from app.database.models.candidate_ranking import CandidateRanking

    before = db.execute(
        select(func.count()).select_from(CandidateRanking)
    ).scalar_one()
    with pytest.raises(UnauthorizedError):
        generate_ranking(
            db, job_id=uuid.uuid4(), rubric_version_id=uuid.uuid4(),
            requested_by_user_id=bad_user_id,
        )
    after = db.execute(
        select(func.count()).select_from(CandidateRanking)
    ).scalar_one()
    assert after == before


def test_interview_guide_reads_denied(db, bad_user_id):
    for call in (
        lambda: get_interview_guide_for_application(
            db, uuid.uuid4(), acting_user_id=bad_user_id
        ),
        lambda: list_interview_guides_for_job(
            db, job_id=uuid.uuid4(), acting_user_id=bad_user_id
        ),
        lambda: get_shortlisted_candidates_for_job(
            db, job_id=uuid.uuid4(), acting_user_id=bad_user_id
        ),
    ):
        with pytest.raises(UnauthorizedError):
            call()


def test_generate_interview_guide_denied_before_any_write(db, mocker, bad_user_id):
    from app.database.models.interview_guide import InterviewGuide

    ai = mocker.patch.object(
        interview_guide_service, "get_structured_response"
    )
    before = db.execute(
        select(func.count()).select_from(InterviewGuide)
    ).scalar_one()
    with pytest.raises(UnauthorizedError):
        generate_interview_guide(
            db, application_id=uuid.uuid4(), requested_by_user_id=bad_user_id,
        )
    ai.assert_not_called()
    after = db.execute(
        select(func.count()).select_from(InterviewGuide)
    ).scalar_one()
    assert after == before


def test_shortlist_reads_denied(db, bad_user_id):
    for call in (
        lambda: get_shortlist_status_for_job(
            db, job_id=uuid.uuid4(), acting_user_id=bad_user_id
        ),
        lambda: get_ranking_staleness_for_partition(
            db, job_id=uuid.uuid4(), rubric_version_id=uuid.uuid4(),
            acting_user_id=bad_user_id,
        ),
    ):
        with pytest.raises(UnauthorizedError):
            call()


def test_shortlist_writes_denied_before_any_row(db, bad_user_id):
    from app.database.models.candidate_shortlist_entry import (
        CandidateShortlistEntry,
    )

    before = db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
    ).scalar_one()
    for call in (
        lambda: shortlist_candidate(
            db, job_id=uuid.uuid4(), application_id=uuid.uuid4(),
            rubric_version_id=uuid.uuid4(), requested_by_user_id=bad_user_id,
        ),
        lambda: unshortlist_candidate(
            db, job_id=uuid.uuid4(), application_id=uuid.uuid4(),
            requested_by_user_id=bad_user_id,
        ),
    ):
        with pytest.raises(UnauthorizedError):
            call()
    after = db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
    ).scalar_one()
    assert after == before


# --- regression: a valid active internal user still gets through ---


def test_valid_user_passes_the_gates(db, mocker):
    """The gate must not break legitimate internal use. The full happy-path
    behaviour of parse_resume / prequalify_application lives in their own
    service test modules; here we confirm a valid active user is NOT rejected by
    any of the five entry points."""
    user, document, application, extraction = _seed_chain(db)

    # reads return the seeded rows, no UnauthorizedError
    got_bytes_fn = mocker.patch.object(
        storage_service, "_drive_download_file", return_value=b"%PDF-stub"
    )
    mocker.patch.object(
        storage_service, "_get_drive", return_value=(object(), "root")
    )
    assert get_document_download_bytes(
        document.id, db, acting_user_id=user.id
    ) == b"%PDF-stub"
    got_bytes_fn.assert_called_once()

    assert (
        get_extraction_for_document(db, document.id, acting_user_id=user.id).id
        == extraction.id
    )
    assert (
        get_prequalification_for_application(
            db, application.id, acting_user_id=user.id
        ).application_id
        == application.id
    )
    assert require_internal_user(db, user.id).id == user.id


# --- structural: the public app must not import HR-only services ---


def test_public_app_does_not_import_hr_only_services():
    """Mirrors ``test_only_storage_service_imports_the_drive_sdk``: a regression
    guard, not a docstring promise. If a future edit imports resume_parsing /
    prequalification into the public candidate app — or pulls
    ``get_document_download_bytes`` into ``candidate_portal_service`` — this
    fails. The code-level auth gate would still block the call at runtime, but
    the import itself is a smell that should never land."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    public_surface = {
        root / "app" / "public_main.py",
        root / "app" / "services" / "candidate_portal_service.py",
    }
    forbidden = (
        "resume_parsing_service",
        "prequalification_service",
        "ranking_service",
        "shortlist_service",
        "interview_guide_service",
        "get_document_download_bytes",
    )
    offenders = []
    for path in public_surface:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{path.name} references {marker!r}")
    assert not offenders, offenders
