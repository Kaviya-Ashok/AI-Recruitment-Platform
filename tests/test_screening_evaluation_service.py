"""Tests for app.services.screening_evaluation_service (Phase 4 Step 4).

Mocking boundary (project convention): patch
``app.services.screening_evaluation_service.get_structured_response`` — the real
Claude API is never touched. Real Postgres via the savepoint-rollback ``db``
fixture. Upstream inputs (rubric, prequalification, resume extraction, screening
session + questions + answers) are built directly with the ORM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.ai.schemas.screening_evaluation import (
    CriterionEvaluation,
    ScreeningEvaluationAssessment,
)
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.document import Document
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.prequalification_result import PrequalificationResult
from app.database.models.resume_extraction import ResumeExtraction
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.screening_answer import ScreeningAnswer
from app.database.models.screening_evaluation import (
    ScreeningEvaluation,
    ScreeningRecommendation,
)
from app.database.models.screening_question import ScreeningQuestion
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services import screening_evaluation_service as sev
from app.services import screening_question_service as sqs
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.screening_evaluation_service import (
    ScreeningEvaluationError,
    ScreeningEvaluationPreconditionError,
    ScreeningEvaluationTargetNotFoundError,
    ensure_screening_evaluated,
    evaluate_screening,
    get_screening_evaluation_for_application,
)
from app.utils.authorization import UnauthorizedError

_PDF_MIME = "application/pdf"

# 5 criteria: (requirement_type, category, text). Prequal verdicts below.
_CRITERIA_SPECS = [
    ("MANDATORY", "Tech", "5+ years professional Python"),
    ("MANDATORY", "Distributed", "Hands-on Apache Spark / PySpark"),
    ("PREFERRED", "Messaging", "Kafka or equivalent"),
    ("BEHAVIORAL", "Collab", "Has mentored junior engineers"),
    ("EXPERIENCE", "Domain", "3+ years in fintech"),
]
_PREQUAL_RESULTS = ["PASS", "UNKNOWN", "PASS", "UNKNOWN", "FAIL"]


def _hr_id(db):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="HR", role=UserRole.HR,
    ).id


def _seed_complete_screening(
    db, *, prequal_results=None, questions_spec=None,
):
    """Everything upstream + a screening session at SCREENING_COMPLETE.

    ``questions_spec``: list of ``(round, criterion_idx_or_None, answer_or_None)``
    — the round-1/round-2 questions and (optional) answers to persist.
    Default: one answered question targeting criterion 2 (the UNKNOWN one).
    """
    hr = create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="HR", role=UserRole.HR,
    )
    job = create_job(
        db, title="Backend Engineer", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
        created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()

    rubric = RubricVersion(
        job_id=job.id, version_number=1, status=RubricVersionStatus.APPROVED,
        generated_from_requirements_version=1, created_by=hr.id,
    )
    db.add(rubric)
    db.flush()

    criteria = []
    for i, (rt, cat, txt) in enumerate(_CRITERIA_SPECS, start=1):
        c = RubricCriterion(
            rubric_version_id=rubric.id, requirement_type=rt, category=cat,
            criterion_text=txt, display_order=i,
        )
        db.add(c)
        criteria.append(c)
    db.flush()

    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    application = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com", full_name="Casey", phone=None,
    )
    application.status = ApplicationStatus.SCREENING_COMPLETED
    db.flush()

    document = Document(
        application_id=application.id, drive_file_id=f"f-{uuid.uuid4().hex}",
        drive_folder_id="folder", original_filename="cv.pdf",
        mime_type=_PDF_MIME, file_size_bytes=1024,
    )
    db.add(document)
    db.flush()

    extraction = ResumeExtraction(
        document_id=document.id,
        extracted_data={"skills": ["Python"], "technologies": ["Postgres"],
                        "experience": [], "projects": [], "certifications": [],
                        "education": [], "other_relevant_claims": []},
        ai_model="claude-haiku-4-5-20251001",
    )
    db.add(extraction)
    db.flush()

    verdicts = prequal_results or _PREQUAL_RESULTS
    results = [
        {
            "criterion_id": str(c.id), "criterion_index": i,
            "requirement_type": c.requirement_type, "category": c.category,
            "criterion_text": c.criterion_text,
            "result": verdicts[i - 1],
            "evidence_summary": f"prequal evidence for {c.criterion_text}",
            "reasoning": f"prequal reasoning for {c.criterion_text}",
            "confidence": "MEDIUM",
        }
        for i, c in enumerate(criteria, start=1)
    ]
    prequal = PrequalificationResult(
        application_id=application.id, rubric_version_id=rubric.id,
        resume_extraction_id=extraction.id, results=results,
        ai_model="claude-sonnet-5",
    )
    db.add(prequal)
    db.flush()

    session = ScreeningSession(
        application_id=application.id,
        access_token="t" + uuid.uuid4().hex + uuid.uuid4().hex[:10],
        status=ScreeningSessionStatus.SCREENING_COMPLETE,
    )
    db.add(session)
    db.flush()

    spec = questions_spec
    if spec is None:
        spec = [(1, 2, "I ran PySpark ETL pipelines for two years at Acme.")]

    seq_by_round: dict[int, int] = {}
    questions = []
    for (rnd, crit_idx, answer) in spec:
        seq = seq_by_round.get(rnd, 0)
        seq_by_round[rnd] = seq + 1
        q = ScreeningQuestion(
            screening_session_id=session.id, round=rnd, sequence_index=seq,
            category="GAP",
            rubric_criterion_id=(criteria[crit_idx - 1].id if crit_idx else None),
            question_text=f"Round {rnd} question about criterion {crit_idx}",
            generated_reason="targets an UNKNOWN",
            ai_model="claude-sonnet-5",
        )
        db.add(q)
        db.flush()
        questions.append(q)
        if answer is not None:
            db.add(ScreeningAnswer(
                screening_question_id=q.id, answer_text=answer,
                submitted_at=datetime.now(timezone.utc),
            ))
    db.flush()

    return SimpleNamespace(
        hr=hr, job=job, rubric=rubric, criteria=criteria,
        application=application, extraction=extraction, prequal=prequal,
        session=session, questions=questions,
    )


def _assessment(verdicts: list[str]) -> ScreeningEvaluationAssessment:
    """Build the mocked AI output: one CriterionEvaluation per criterion."""
    return ScreeningEvaluationAssessment(assessments=[
        CriterionEvaluation(
            criterion_index=i, result=v,
            evidence_summary=f"AI evidence for criterion {i}",
            reasoning=f"AI reasoning for criterion {i}",
        )
        for i, v in enumerate(verdicts, start=1)
    ])


def _patch(mocker, assessment):
    return mocker.patch.object(sev, "get_structured_response", return_value=assessment)


def _eval_row(db, application_id):
    return get_screening_evaluation_for_application(
        db, application_id, acting_user_id=SYSTEM_USER_ID
    )


def _score_events(db, application_id):
    return db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_type == "application",
            AuditEvent.entity_id == application_id,
            AuditEvent.event_type == AuditEventType.SCORE_GENERATED.value,
        )
    ).scalars().all()


# =====================================================================
# happy path + idempotency
# =====================================================================


def test_happy_path_creates_row_status_and_audit(db, mocker):
    s = _seed_complete_screening(db)
    # AI returns: criterion 2 resolved to PASS by the answer; the rest match
    # the prequalification verdicts.
    spy = _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))

    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )

    assert spy.call_count == 1
    assert ev.screening_session_id == s.session.id
    assert ev.rubric_version_id == s.rubric.id
    assert ev.ai_recommendation in ScreeningRecommendation.AUTOMATED
    assert ev.overall_confidence in ("HIGH", "MEDIUM", "LOW")
    assert len(ev.results) == 5

    db.refresh(s.application)
    assert s.application.status == ApplicationStatus.SCREENING_EVALUATED

    evs = _score_events(db, s.application.id)
    assert len(evs) == 1
    assert evs[0].user_id == s.hr.id


def test_idempotent_recall_no_second_ai_call_or_audit(db, mocker):
    s = _seed_complete_screening(db)
    spy = _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))

    first = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    again = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )

    assert first.id == again.id
    assert spy.call_count == 1
    assert _count_eval(db, s.session.id) == 1
    assert len(_score_events(db, s.application.id)) == 1


def _count_eval(db, session_id):
    return db.execute(
        select(func.count()).select_from(ScreeningEvaluation)
        .where(ScreeningEvaluation.screening_session_id == session_id)
    ).scalar_one()


def test_force_replaces_the_row(db, mocker):
    s = _seed_complete_screening(db)
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))
    first = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    first_id = first.id

    _patch(mocker, _assessment(["FAIL", "FAIL", "FAIL", "FAIL", "FAIL"]))
    second = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
        force=True,
    )
    assert second.id != first_id
    assert _count_eval(db, s.session.id) == 1
    # two SCORE_GENERATED events (original + re-run)
    assert len(_score_events(db, s.application.id)) == 2


# =====================================================================
# preconditions + auth
# =====================================================================


def test_precondition_wrong_status_raises_before_ai_call(db, mocker):
    s = _seed_complete_screening(db)
    s.session.status = ScreeningSessionStatus.ROUND_2_IN_PROGRESS
    db.flush()
    spy = _patch(mocker, _assessment(["PASS"] * 5))

    with pytest.raises(ScreeningEvaluationPreconditionError):
        evaluate_screening(
            db, application_id=s.application.id, requested_by_user_id=s.hr.id,
        )
    assert spy.call_count == 0
    assert _count_eval(db, s.session.id) == 0


def test_requires_internal_user(db, mocker):
    s = _seed_complete_screening(db)
    _patch(mocker, _assessment(["PASS"] * 5))
    for bad in (None, "nope", uuid.uuid4()):
        with pytest.raises(UnauthorizedError):
            evaluate_screening(
                db, application_id=s.application.id, requested_by_user_id=bad,
            )


def test_unknown_application_raises(db, mocker):
    _patch(mocker, _assessment(["PASS"] * 5))
    with pytest.raises(ScreeningEvaluationTargetNotFoundError):
        evaluate_screening(
            db, application_id=uuid.uuid4(), requested_by_user_id=SYSTEM_USER_ID,
        )


# =====================================================================
# RECONCILIATION (point 3)
# =====================================================================


def test_untargeted_criterion_passes_prequalification_through_verbatim(db, mocker):
    """A criterion with NO answered screening question must equal the
    prequalification verdict exactly — even if the AI returns something else."""
    # Only criterion 2 is targeted (default spec). The AI *tries* to flip
    # criteria 1, 4, 5 as well — those flips must be ignored.
    s = _seed_complete_screening(db)
    _patch(mocker, _assessment(["FAIL", "PASS", "FAIL", "PASS", "PASS"]))

    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    by_idx = {r["criterion_index"]: r for r in ev.results}

    # criterion 2 = targeted -> AI's verdict (PASS)
    assert by_idx[2]["result"] == "PASS"
    assert by_idx[2]["evidence_summary"] == "AI evidence for criterion 2"

    # criteria 1, 3, 4, 5 = untargeted -> prequalification verdict verbatim
    assert by_idx[1]["result"] == "PASS"    # prequal PASS, NOT the AI's FAIL
    assert by_idx[1]["evidence_summary"] == "prequal evidence for 5+ years professional Python"
    assert by_idx[3]["result"] == "PASS"
    assert by_idx[4]["result"] == "UNKNOWN"  # prequal UNKNOWN, NOT the AI's PASS
    assert by_idx[5]["result"] == "FAIL"     # prequal FAIL, NOT the AI's PASS


def test_targeted_criterion_can_get_a_reconciled_verdict(db, mocker):
    s = _seed_complete_screening(
        db,
        # criterion 2 (prequal UNKNOWN) is targeted and answered
        questions_spec=[(1, 2, "Yes, 3 years of production PySpark at Acme.")],
    )
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))

    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    by_idx = {r["criterion_index"]: r for r in ev.results}
    # prequal said UNKNOWN, the answer resolved it -> PASS
    assert by_idx[2]["result"] == "PASS"


def test_unknown_with_insufficient_answer_stays_unknown(db, mocker):
    s = _seed_complete_screening(
        db,
        questions_spec=[(1, 2, "I've heard of Spark but never used it much.")],
    )
    # AI, correctly, keeps criterion 2 UNKNOWN despite the (weak) answer.
    _patch(mocker, _assessment(["PASS", "UNKNOWN", "PASS", "UNKNOWN", "FAIL"]))

    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    by_idx = {r["criterion_index"]: r for r in ev.results}
    assert by_idx[2]["result"] == "UNKNOWN"  # never promoted to PASS/FAIL


def test_targeting_question_without_an_answer_is_treated_as_untargeted(db, mocker):
    s = _seed_complete_screening(
        db,
        questions_spec=[(1, 2, None)],  # question exists, no answer
    )
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))
    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    by_idx = {r["criterion_index"]: r for r in ev.results}
    # unanswered -> passthrough: prequal UNKNOWN wins over the AI's PASS
    assert by_idx[2]["result"] == "UNKNOWN"


@pytest.mark.parametrize("forced", ["PASS", "FAIL", "UNKNOWN"])
def test_prequalification_outcome_never_gates_evaluation(db, mocker, forced):
    s = _seed_complete_screening(db, prequal_results=[forced] * 5)
    _patch(mocker, _assessment([forced] * 5))

    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    assert ev is not None
    db.refresh(s.application)
    assert s.application.status == ApplicationStatus.SCREENING_EVALUATED


# =====================================================================
# scoring / bucketing on a real persisted row
# =====================================================================


def test_bucket_scores_use_requirement_type_not_category(db, mocker):
    # All 5 criteria have a "technical-sounding" category ("Tech", "Distributed",
    # "Domain", ...) — but the EXPERIENCE and BEHAVIORAL ones must land in their
    # own buckets, not Requirements, purely by requirement_type. Everything PASS
    # (prequal + no targeting) so each bucket that has criteria scores 10.
    s = _seed_complete_screening(db, prequal_results=["PASS"] * 5)
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "PASS", "PASS"]))
    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    # Requirements bucket = criteria 1,2,3 (2 MANDATORY + 1 PREFERRED), all PASS
    assert ev.requirements_score == 10
    assert ev.requirements_coverage == 1.0
    # Experience bucket = criterion 5 (EXPERIENCE), NOT Requirements despite its
    # "Domain" category.
    assert ev.experience_score == 10
    assert ev.experience_coverage == 1.0
    # Behavioral bucket = criterion 4 (BEHAVIORAL).
    assert ev.behavioral_score == 10


def test_all_unknown_bucket_is_null_not_zero(db, mocker):
    s = _seed_complete_screening(db, prequal_results=["UNKNOWN"] * 5)
    _patch(mocker, _assessment(["UNKNOWN"] * 5))
    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    assert ev.requirements_score is None and ev.requirements_coverage is None
    assert ev.experience_score is None
    assert ev.behavioral_score is None
    # all-UNKNOWN mandatory -> HOLD, LOW confidence
    assert ev.ai_recommendation == "HOLD"
    assert ev.overall_confidence == "LOW"


def test_strengths_gaps_unknowns_are_persisted_projections(db, mocker):
    s = _seed_complete_screening(db)
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))
    ev = evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    # 3 PASS, 1 UNKNOWN, 1 FAIL (after reconciliation)
    assert len(ev.strengths) == 3
    assert len(ev.gaps) == 1
    assert len(ev.unknowns) == 1
    assert all(": " in line for line in ev.strengths + ev.gaps + ev.unknowns)


# =====================================================================
# audit metadata safety
# =====================================================================


def test_score_generated_metadata_has_no_raw_candidate_content(db, mocker):
    s = _seed_complete_screening(
        db,
        questions_spec=[(1, 2, "SECRET_ANSWER_TEXT ran PySpark at Acme")],
    )
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))
    evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    event = _score_events(db, s.application.id)[0]
    blob = f"{event.action}{event.previous_state}{event.new_state}{event.event_metadata}"

    assert "SECRET_ANSWER_TEXT" not in blob
    assert "AI evidence for criterion" not in blob
    assert "AI reasoning for criterion" not in blob
    assert "prequal evidence" not in blob
    assert "5+ years professional Python" not in blob  # criterion_text
    # but the safe aggregates ARE there
    assert event.new_state["overall_confidence"] in ("HIGH", "MEDIUM", "LOW")
    assert event.new_state["ai_recommendation"] in ("PROCEED", "HOLD")
    assert set(event.new_state["by_result"]) == {"PASS", "FAIL", "UNKNOWN"}


# =====================================================================
# automatic trigger
# =====================================================================


def _r1_qset(n):
    from app.ai.schemas.screening_questions import (
        ScreeningQuestion as AIQ,
        ScreeningQuestionSet,
    )
    return ScreeningQuestionSet(questions=[
        AIQ(category="GAP", criterion_id=None,
            question_text=f"Q{i}", generated_reason=f"r{i}")
        for i in range(n)
    ])


def test_auto_trigger_on_zero_round_2(db, mocker):
    """generate_round_questions(round=2) -> zero questions -> SCREENING_COMPLETE
    -> evaluation fires automatically, SYSTEM-attributed."""
    s = _seed_complete_screening(db, questions_spec=[])
    # Put the session back to a state where round 2 can be generated.
    s.session.status = ScreeningSessionStatus.ROUND_1_COMPLETE
    db.flush()
    # one answered round-1 question so the round-2 precondition passes
    q = ScreeningQuestion(
        screening_session_id=s.session.id, round=1, sequence_index=0,
        category="GAP", rubric_criterion_id=None,
        question_text="Q1", generated_reason="r", ai_model="m",
    )
    db.add(q)
    db.flush()
    db.add(ScreeningAnswer(
        screening_question_id=q.id, answer_text="an answer",
        submitted_at=datetime.now(timezone.utc),
    ))
    db.flush()

    mocker.patch.object(
        sqs, "get_structured_response", return_value=_r1_qset(0),  # zero round-2
    )
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))

    sqs.generate_round_questions(
        db, screening_session_id=s.session.id, round=2,
        requested_by_user_id=SYSTEM_USER_ID,
    )

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.SCREENING_COMPLETE
    ev = _eval_row(db, s.application.id)
    assert ev is not None
    assert len(_score_events(db, s.application.id)) == 1
    assert _score_events(db, s.application.id)[0].user_id == SYSTEM_USER_ID


def test_auto_trigger_on_last_round_2_answer(db, mocker):
    s = _seed_complete_screening(db, questions_spec=[])
    s.session.status = ScreeningSessionStatus.ROUND_2_IN_PROGRESS
    db.flush()
    q = ScreeningQuestion(
        screening_session_id=s.session.id, round=2, sequence_index=0,
        category="GAP", rubric_criterion_id=None,
        question_text="R2 Q1", generated_reason="r", ai_model="m",
    )
    db.add(q)
    db.flush()

    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))
    sqs.submit_answer(db, screening_question_id=q.id, answer_text="my final answer")

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.SCREENING_COMPLETE
    assert _eval_row(db, s.application.id) is not None
    assert len(_score_events(db, s.application.id)) == 1


def test_auto_trigger_failure_is_retryable_and_not_a_dup(db, mocker):
    s = _seed_complete_screening(db, questions_spec=[])
    s.session.status = ScreeningSessionStatus.ROUND_2_IN_PROGRESS
    db.flush()
    q = ScreeningQuestion(
        screening_session_id=s.session.id, round=2, sequence_index=0,
        category="GAP", rubric_criterion_id=None,
        question_text="R2 Q1", generated_reason="r", ai_model="m",
    )
    db.add(q)
    db.flush()

    # Evaluation AI fails on the auto-trigger path.
    from app.ai.claude_client import AIRequestError
    _patch(mocker, _assessment(["PASS"] * 5)).side_effect = AIRequestError("down")
    sqs.submit_answer(db, screening_question_id=q.id, answer_text="final answer")

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.SCREENING_COMPLETE  # not corrupted
    assert _eval_row(db, s.application.id) is None
    assert len(_score_events(db, s.application.id)) == 0

    # Page-load retry succeeds; exactly one row + one event.
    _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))
    ensure_screening_evaluated(db, application_id=s.application.id)
    assert _eval_row(db, s.application.id) is not None
    assert _count_eval(db, s.session.id) == 1
    assert len(_score_events(db, s.application.id)) == 1


def test_ensure_screening_evaluated_is_noop_when_already_done(db, mocker):
    s = _seed_complete_screening(db)
    spy = _patch(mocker, _assessment(["PASS", "PASS", "PASS", "UNKNOWN", "FAIL"]))
    evaluate_screening(
        db, application_id=s.application.id, requested_by_user_id=s.hr.id,
    )
    ensure_screening_evaluated(db, application_id=s.application.id)
    ensure_screening_evaluated(db, application_id=s.application.id)
    assert spy.call_count == 1
    assert _count_eval(db, s.session.id) == 1


def test_ensure_screening_evaluated_noop_when_not_complete(db, mocker):
    s = _seed_complete_screening(db)
    s.session.status = ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    db.flush()
    spy = _patch(mocker, _assessment(["PASS"] * 5))
    ensure_screening_evaluated(db, application_id=s.application.id)
    assert spy.call_count == 0
    assert _count_eval(db, s.session.id) == 0
