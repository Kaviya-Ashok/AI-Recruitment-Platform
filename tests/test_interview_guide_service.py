"""Tests for app.services.interview_guide_service (Phase 4 Step 7).

Mocking boundary: patch
``app.services.interview_guide_service.get_structured_response`` — the real
Claude API is never touched. Real Postgres via the savepoint-rollback ``db``
fixture; every upstream input is built directly with the ORM.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.ai.schemas.interview_guide import (
    InterviewGuideAssessment,
    InterviewQuestionDraft,
)
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.document import Document
from app.database.models.interview_guide import InterviewGuide, InterviewQuestion
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.prequalification_result import PrequalificationResult
from app.database.models.resume_extraction import ResumeExtraction
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.screening_evaluation import ScreeningEvaluation
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services import interview_guide_service as igs
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.interview_guide_service import (
    InterviewGuideError,
    InterviewGuidePreconditionError,
    generate_interview_guide,
    get_interview_guide_for_application,
    get_shortlisted_candidates_for_job,
    list_interview_guides_for_job,
)
from app.services.shortlist_service import shortlist_candidate, unshortlist_candidate
from app.utils.authorization import UnauthorizedError

_PDF_MIME = "application/pdf"

_CRITERIA = [
    ("MANDATORY", "Core", "5+ years professional Python"),
    ("MANDATORY", "Data", "Hands-on Apache Spark / PySpark"),
    ("PREFERRED", "Messaging", "Kafka or equivalent"),
    ("BEHAVIORAL", "Collab", "Has mentored junior engineers"),
    ("EXPERIENCE", "Domain", "3+ years in fintech"),
]


def _hr(db):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="HR", role=UserRole.HR,
    )


def _rubric(db, job, hr, *, version_number=1, status=RubricVersionStatus.APPROVED):
    rv = RubricVersion(
        job_id=job.id, version_number=version_number, status=status,
        generated_from_requirements_version=1, created_by=hr.id,
    )
    db.add(rv)
    db.flush()
    crits = []
    for i, (rt, cat, txt) in enumerate(_CRITERIA, start=1):
        c = RubricCriterion(
            rubric_version_id=rv.id, requirement_type=rt, category=cat,
            criterion_text=txt, display_order=i,
        )
        db.add(c)
        crits.append(c)
    db.flush()
    return rv, crits


def _seed(db, *, shortlisted=True, rank_position=1, ranking_rubric=None):
    """HR + job + approved rubric + fully-evaluated application + (optionally) a
    shortlist entry + a candidate_rankings row."""
    hr = _hr(db)
    job = create_job(
        db, title="Backend Engineer", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
        created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    rubric, criteria = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com", full_name="Casey Candidate", phone=None,
    )
    app.status = ApplicationStatus.SCREENING_EVALUATED
    db.flush()

    doc = Document(
        application_id=app.id, drive_file_id=f"f-{uuid.uuid4().hex}",
        drive_folder_id="folder", original_filename="cv.pdf",
        mime_type=_PDF_MIME, file_size_bytes=1024,
    )
    db.add(doc)
    db.flush()
    extraction = ResumeExtraction(
        document_id=doc.id,
        extracted_data={"skills": ["Python", "Spark"], "technologies": [],
                        "experience": [], "projects": [], "certifications": [],
                        "education": [], "other_relevant_claims": []},
        ai_model="claude-haiku-4-5-20251001",
    )
    db.add(extraction)
    db.flush()

    prequal = PrequalificationResult(
        application_id=app.id, rubric_version_id=rubric.id,
        resume_extraction_id=extraction.id,
        results=[
            {"criterion_id": str(c.id), "criterion_index": i,
             "requirement_type": c.requirement_type, "category": c.category,
             "criterion_text": c.criterion_text, "result": "UNKNOWN",
             "evidence_summary": "prequal evidence", "reasoning": "prequal reasoning",
             "confidence": "LOW"}
            for i, c in enumerate(criteria, start=1)
        ],
        ai_model="claude-sonnet-5",
    )
    db.add(prequal)
    db.flush()

    session = ScreeningSession(
        application_id=app.id, access_token=f"tok-{uuid.uuid4().hex}",
        status=ScreeningSessionStatus.SCREENING_COMPLETE,
    )
    db.add(session)
    db.flush()

    ev = ScreeningEvaluation(
        screening_session_id=session.id, rubric_version_id=rubric.id,
        results=[
            {"criterion_id": str(c.id), "criterion_index": i,
             "requirement_type": c.requirement_type, "category": c.category,
             "criterion_text": c.criterion_text, "result": "UNKNOWN",
             "evidence_summary": "e", "reasoning": "r", "confidence": "LOW"}
            for i, c in enumerate(criteria, start=1)
        ],
        requirements_score=6, requirements_coverage=0.4,
        experience_score=None, experience_coverage=None,
        behavioral_score=5, behavioral_coverage=1.0,
        strengths=["5+ years professional Python: confirmed"],
        gaps=[], unknowns=["Kafka or equivalent: not addressed"],
        overall_confidence="LOW", ai_recommendation="HOLD",
        ai_model="claude-sonnet-5",
    )
    db.add(ev)
    db.flush()

    if shortlisted:
        rr_rubric = ranking_rubric or rubric
        if rank_position is not None:
            db.add(CandidateRanking(
                job_id=job.id, rubric_version_id=rr_rubric.id, application_id=app.id,
                rank_position=rank_position, overall_score=6.0, eligible=True,
                mandatory_unknown_flag=True, generated_at=datetime.now(timezone.utc),
                generation_batch_id=uuid.uuid4(),
            ))
            db.flush()
        shortlist_candidate(
            db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
            requested_by_user_id=hr.id,
        )

    return {
        "hr": hr, "job": job, "rubric": rubric, "criteria": criteria,
        "app": app, "session": session, "link": link,
    }


def _draft(n, *, criterion_id=None, category="REQUIREMENTS"):
    return InterviewGuideAssessment(questions=[
        InterviewQuestionDraft(
            category=category, rubric_criterion_id=criterion_id,
            question_text=f"AI question text {i}",
            evaluates=f"AI evaluates {i}",
            generated_reason=f"AI generated reason {i}",
        )
        for i in range(n)
    ])


def _patch(mocker, assessment):
    return mocker.patch.object(
        igs, "get_structured_response", return_value=assessment
    )


def _guide_events(db, application_id):
    return db.execute(
        select(AuditEvent)
        .where(
            AuditEvent.entity_id == application_id,
            AuditEvent.event_type == AuditEventType.INTERVIEW_GUIDE_GENERATED.value,
        )
        .order_by(AuditEvent.timestamp)
    ).scalars().all()


# --- precondition -------------------------------------------


def test_generation_blocked_when_not_shortlisted(db, mocker):
    s = _seed(db, shortlisted=False)
    ai = _patch(mocker, _draft(8))
    with pytest.raises(InterviewGuidePreconditionError):
        generate_interview_guide(
            db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
        )
    ai.assert_not_called()
    assert db.execute(
        select(func.count()).select_from(InterviewGuide)
        .where(InterviewGuide.application_id == s["app"].id)
    ).scalar_one() == 0


def test_generation_blocked_after_unshortlist(db, mocker):
    s = _seed(db)
    unshortlist_candidate(
        db, job_id=s["job"].id, application_id=s["app"].id,
        requested_by_user_id=s["hr"].id,
    )
    _patch(mocker, _draft(8))
    with pytest.raises(InterviewGuidePreconditionError):
        generate_interview_guide(
            db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
        )


# --- happy path --------------------------------------------


def test_happy_path_persists_guide_and_questions_and_one_event(db, mocker):
    s = _seed(db)
    cid = str(s["criteria"][0].id)
    _patch(mocker, _draft(8, criterion_id=cid))

    guide = generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    assert guide.rubric_version_id == s["rubric"].id
    assert guide.ai_model == "claude-sonnet-5"

    n_q = db.execute(
        select(func.count()).select_from(InterviewQuestion)
        .where(InterviewQuestion.interview_guide_id == guide.id)
    ).scalar_one()
    assert n_q == 8

    evs = _guide_events(db, s["app"].id)
    assert len(evs) == 1
    assert evs[0].user_id == s["hr"].id
    assert evs[0].user_id != SYSTEM_USER_ID
    assert evs[0].new_state["question_count"] == 8
    assert evs[0].new_state["rubric_version_id"] == str(s["rubric"].id)
    assert set(evs[0].new_state["by_category"]) == {
        "REQUIREMENTS", "EXPERIENCE", "BEHAVIORAL", "RESUME_VALIDATION", "PROBING",
    }


def test_view_returns_guide_grouped_by_category(db, mocker):
    s = _seed(db)
    _patch(mocker, InterviewGuideAssessment(questions=[
        InterviewQuestionDraft(category="PROBING", rubric_criterion_id=None,
                               question_text="p", evaluates="e", generated_reason="r"),
        InterviewQuestionDraft(category="REQUIREMENTS",
                               rubric_criterion_id=str(s["criteria"][0].id),
                               question_text="req", evaluates="e", generated_reason="r"),
        InterviewQuestionDraft(category="BEHAVIORAL", rubric_criterion_id=None,
                               question_text="b", evaluates="e", generated_reason="r"),
        InterviewQuestionDraft(category="REQUIREMENTS", rubric_criterion_id=None,
                               question_text="req2", evaluates="e", generated_reason="r"),
        InterviewQuestionDraft(category="EXPERIENCE", rubric_criterion_id=None,
                               question_text="x", evaluates="e", generated_reason="r"),
        InterviewQuestionDraft(category="RESUME_VALIDATION", rubric_criterion_id=None,
                               question_text="rv", evaluates="e", generated_reason="r"),
    ]))
    generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    view = get_interview_guide_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id,
    )
    cats = [q.category for q in view.questions]
    # category order: REQUIREMENTS, EXPERIENCE, BEHAVIORAL, RESUME_VALIDATION, PROBING
    assert cats == ["REQUIREMENTS", "REQUIREMENTS", "EXPERIENCE", "BEHAVIORAL",
                    "RESUME_VALIDATION", "PROBING"]
    # criterion_text is joined in for a mapped question
    mapped = [q for q in view.questions if q.rubric_criterion_id is not None]
    assert mapped and mapped[0].criterion_text == "5+ years professional Python"


# --- idempotency ------------------------------------------


def test_force_false_recall_is_idempotent(db, mocker):
    s = _seed(db)
    ai = _patch(mocker, _draft(7))
    g1 = generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    g2 = generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    assert g1.id == g2.id
    assert ai.call_count == 1
    assert db.execute(
        select(func.count()).select_from(InterviewGuide)
        .where(InterviewGuide.application_id == s["app"].id)
    ).scalar_one() == 1
    assert db.execute(
        select(func.count()).select_from(InterviewQuestion)
        .where(InterviewQuestion.interview_guide_id == g1.id)
    ).scalar_one() == 7
    assert len(_guide_events(db, s["app"].id)) == 1


def test_force_true_regeneration_replaces_questions_no_unique_violation(db, mocker):
    s = _seed(db)
    _patch(mocker, _draft(6))
    g1 = generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    old_guide_id = g1.id
    old_q_ids = set(db.execute(
        select(InterviewQuestion.id).where(
            InterviewQuestion.interview_guide_id == g1.id
        )
    ).scalars().all())

    _patch(mocker, _draft(9))
    g2 = generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id, force=True,
    )
    # one guide row for the application (UNIQUE respected)
    assert db.execute(
        select(func.count()).select_from(InterviewGuide)
        .where(InterviewGuide.application_id == s["app"].id)
    ).scalar_one() == 1
    new_q_ids = set(db.execute(
        select(InterviewQuestion.id).where(
            InterviewQuestion.interview_guide_id == g2.id
        )
    ).scalars().all())
    assert new_q_ids.isdisjoint(old_q_ids)
    assert len(new_q_ids) == 9
    # no orphan questions from the old guide — every old question id is gone
    assert db.execute(
        select(func.count()).select_from(InterviewQuestion)
        .where(InterviewQuestion.id.in_(old_q_ids))
    ).scalar_one() == 0
    evs = _guide_events(db, s["app"].id)
    assert len(evs) == 2
    # identify by content, not list order (shared txn timestamp under the fixture)
    forced_ev = next(e for e in evs if e.new_state["forced"] is True)
    assert forced_ev.new_state["replaced_guide_id"] == str(old_guide_id)
    assert any(e.new_state["forced"] is False for e in evs)


# --- rubric-version fidelity -----------------------------


def test_guide_generates_against_the_captured_superseded_version(db, mocker):
    s = _seed(db)
    job, hr, rv1 = s["job"], s["hr"], s["rubric"]

    # approve a v2 -> rv1 becomes SUPERSEDED
    rv1.status = RubricVersionStatus.SUPERSEDED
    db.flush()
    rv2, rv2_crits = _rubric(db, job, hr, version_number=2)
    db.flush()
    assert db.get(RubricVersion, rv1.id).status == "SUPERSEDED"

    # AI references a criterion from the CAPTURED (v1) version
    v1_crit_id = str(s["criteria"][1].id)
    _patch(mocker, _draft(8, criterion_id=v1_crit_id))

    guide = generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=hr.id,
    )
    assert guide.rubric_version_id == rv1.id  # the SUPERSEDED captured version

    view = get_interview_guide_for_application(
        db, s["app"].id, acting_user_id=hr.id,
    )
    assert view.rubric_version_status == "SUPERSEDED"
    assert view.rubric_version_number == 1

    # a v2-only criterion id would be rejected as hallucinated
    _patch(mocker, _draft(8, criterion_id=str(rv2_crits[0].id)))
    with pytest.raises(InterviewGuideError):
        generate_interview_guide(
            db, application_id=s["app"].id, requested_by_user_id=hr.id, force=True,
        )


def test_service_never_calls_get_approved_rubric():
    src = pathlib.Path(igs.__file__).read_text(encoding="utf-8")
    assert "get_approved_rubric" not in src
    assert "list_criteria" in src  # it DOES use the captured-id accessor


# --- validation ----------------------------------------


@pytest.mark.parametrize("n", [3, 5, 15, 30])
def test_question_count_bound_enforced(db, mocker, n):
    """Below MIN_QUESTIONS (6) or above MAX_QUESTIONS (14) -> unusable. (The
    empty-list case is rejected at the schema level — see
    test_interview_guide_schema.test_empty_question_list_rejected.)"""
    s = _seed(db)
    _patch(mocker, _draft(n))
    with pytest.raises(InterviewGuideError):
        generate_interview_guide(
            db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
        )
    assert db.execute(
        select(func.count()).select_from(InterviewGuide)
        .where(InterviewGuide.application_id == s["app"].id)
    ).scalar_one() == 0


def test_hallucinated_criterion_id_rejected(db, mocker):
    s = _seed(db)
    _patch(mocker, _draft(8, criterion_id=str(uuid.uuid4())))
    with pytest.raises(InterviewGuideError):
        generate_interview_guide(
            db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
        )
    assert db.execute(
        select(func.count()).select_from(InterviewGuide)
        .where(InterviewGuide.application_id == s["app"].id)
    ).scalar_one() == 0


# --- retained guide after unshortlist -----------------


def test_existing_guide_stays_readable_after_unshortlist(db, mocker):
    s = _seed(db)
    _patch(mocker, _draft(8))
    generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    unshortlist_candidate(
        db, job_id=s["job"].id, application_id=s["app"].id,
        requested_by_user_id=s["hr"].id,
    )
    view = get_interview_guide_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id,
    )
    assert view is not None
    assert len(view.questions) == 8
    # new generation is blocked
    with pytest.raises(InterviewGuidePreconditionError):
        generate_interview_guide(
            db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
            force=True,
        )


# --- audit safety -------------------------------------


def test_audit_metadata_has_no_free_text(db, mocker):
    s = _seed(db)
    _patch(mocker, InterviewGuideAssessment(questions=[
        InterviewQuestionDraft(
            category="PROBING", rubric_criterion_id=None,
            question_text="SENTINEL_QUESTION_TEXT",
            evaluates="SENTINEL_EVALUATES",
            generated_reason="SENTINEL_REASON",
        )
        for _ in range(6)
    ]))
    generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    ev = _guide_events(db, s["app"].id)[0]
    blob = f"{ev.action} || {ev.previous_state} || {ev.new_state} || {ev.event_metadata}"
    for sentinel in ("SENTINEL_QUESTION_TEXT", "SENTINEL_EVALUATES", "SENTINEL_REASON"):
        assert sentinel not in blob
    assert "question_count" in ev.new_state


# --- authorization -----------------------------------


@pytest.fixture(params=["none", "unknown", "malformed"])
def bad_user_id(request):
    return {"none": None, "unknown": uuid.uuid4(), "malformed": "nope"}[request.param]


def test_all_functions_denied_for_bad_user(db, mocker, bad_user_id):
    s = _seed(db)
    ai = _patch(mocker, _draft(8))
    for call in (
        lambda: generate_interview_guide(
            db, application_id=s["app"].id, requested_by_user_id=bad_user_id,
        ),
        lambda: get_interview_guide_for_application(
            db, s["app"].id, acting_user_id=bad_user_id,
        ),
        lambda: list_interview_guides_for_job(
            db, job_id=s["job"].id, acting_user_id=bad_user_id,
        ),
        lambda: get_shortlisted_candidates_for_job(
            db, job_id=s["job"].id, acting_user_id=bad_user_id,
        ),
    ):
        with pytest.raises(UnauthorizedError):
            call()
    ai.assert_not_called()


# --- get_shortlisted_candidates_for_job --------------


def test_shortlisted_view_partitions_and_reports_rank_and_guide(db, mocker):
    s = _seed(db, rank_position=3)
    rows = get_shortlisted_candidates_for_job(
        db, job_id=s["job"].id, acting_user_id=s["hr"].id,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.rubric_version_number == 1
    assert r.rubric_version_status == "APPROVED"
    assert r.rank_position_at_decision == 3
    assert r.current_rank_position == 3
    assert r.current_rank_available is True
    assert r.guide_exists is False

    _patch(mocker, _draft(6))
    generate_interview_guide(
        db, application_id=s["app"].id, requested_by_user_id=s["hr"].id,
    )
    rows = get_shortlisted_candidates_for_job(
        db, job_id=s["job"].id, acting_user_id=s["hr"].id,
    )
    assert rows[0].guide_exists is True
    assert rows[0].guide_id is not None


def test_shortlisted_view_current_rank_drift_and_missing_ranking(db):
    s = _seed(db, rank_position=2)
    # move the current ranking to #9 -> drift vs decision-time #2
    db.execute(
        select(CandidateRanking).where(CandidateRanking.application_id == s["app"].id)
    ).scalar_one().rank_position = 9
    db.flush()
    r = get_shortlisted_candidates_for_job(
        db, job_id=s["job"].id, acting_user_id=s["hr"].id,
    )[0]
    assert r.rank_position_at_decision == 2
    assert r.current_rank_position == 9

    # delete the ranking row entirely -> "no ranking regenerated"
    db.execute(
        CandidateRanking.__table__.delete().where(
            CandidateRanking.application_id == s["app"].id
        )
    )
    db.flush()
    r = get_shortlisted_candidates_for_job(
        db, job_id=s["job"].id, acting_user_id=s["hr"].id,
    )[0]
    assert r.current_rank_available is False
    assert r.current_rank_position is None


def test_shortlisted_view_never_mixes_rubric_versions(db):
    hr = _hr(db)
    job = create_job(
        db, title="J", department="E", jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="JD.", created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    rv1, c1 = _rubric(db, job, hr, version_number=1,
                      status=RubricVersionStatus.SUPERSEDED)
    rv2, c2 = _rubric(db, job, hr, version_number=2)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)

    def _evaluated(rubric):
        a = create_application(
            db, job_id=job.id, application_link_id=link.id,
            email=f"c-{uuid.uuid4().hex}@x.com", full_name="X", phone=None,
        )
        a.status = ApplicationStatus.SCREENING_EVALUATED
        db.flush()
        sess = ScreeningSession(
            application_id=a.id, access_token=f"tok-{uuid.uuid4().hex}",
            status=ScreeningSessionStatus.SCREENING_COMPLETE,
        )
        db.add(sess)
        db.flush()
        db.add(ScreeningEvaluation(
            screening_session_id=sess.id, rubric_version_id=rubric.id,
            results=[], requirements_score=5, requirements_coverage=1.0,
            experience_score=None, experience_coverage=None,
            behavioral_score=None, behavioral_coverage=None,
            strengths=[], gaps=[], unknowns=[], overall_confidence="MEDIUM",
            ai_recommendation="PROCEED", ai_model="claude-sonnet-5",
        ))
        db.flush()
        shortlist_candidate(db, job_id=job.id, application_id=a.id,
                            rubric_version_id=rubric.id, requested_by_user_id=hr.id)
        return a

    _evaluated(rv1)
    _evaluated(rv2)
    rows = get_shortlisted_candidates_for_job(
        db, job_id=job.id, acting_user_id=hr.id,
    )
    versions = [r.rubric_version_number for r in rows]
    assert sorted(versions) == [1, 2]
    # rows are ordered so the caller can section by version without interleaving
    assert versions == sorted(versions)
