"""Tests for app.services.screening_question_service (Phase 4 Step 3).

Mocking boundary (project convention): patch
``app.services.screening_question_service.get_structured_response`` — the real
Claude API is never touched. Real Postgres via the savepoint-rollback ``db``
fixture. Inputs (rubric criteria, prequalification result, resume extraction,
screening session) are built directly with the ORM so a test does not have to
run three other AI tasks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.ai.schemas.screening_questions import (
    ScreeningQuestion as AIScreeningQuestion,
)
from app.ai.schemas.screening_questions import ScreeningQuestionSet
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
from app.database.models.screening_question import ScreeningQuestion
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services import screening_question_service as sqs
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.screening_question_service import (
    ScreeningAnswerNotAllowedError,
    ScreeningQuestionError,
    ScreeningRoundPreconditionError,
    abandon_screening,
    ensure_round_2_generated,
    generate_round_questions,
    get_round_questions,
    submit_answer,
)
from app.utils.authorization import UnauthorizedError

_PDF_MIME = "application/pdf"

_CRITERIA_SPECS = [
    ("MANDATORY", "Tech", "5+ years of professional Python development"),
    ("MANDATORY", "Distributed", "Hands-on experience with Apache Spark / PySpark"),
    ("PREFERRED", "Messaging", "Kafka or equivalent"),
    ("BEHAVIORAL", "Collab", "Has mentored junior engineers"),
    ("OTHER", "Logistics", "Available on-site in Berlin"),
]


def _hr_id(db):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="HR", role=UserRole.HR,
    ).id


def _seed_ready_session(db, *, prequal_results_override=None):
    """A screening session at READY_FOR_ROUND_1 with everything upstream in place."""
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
    for i, (rtype, cat, txt) in enumerate(_CRITERIA_SPECS, start=1):
        c = RubricCriterion(
            rubric_version_id=rubric.id, requirement_type=rtype, category=cat,
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
    application.status = ApplicationStatus.SCREENING_IN_PROGRESS
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

    results = prequal_results_override or [
        {
            "criterion_id": str(c.id), "criterion_index": i,
            "requirement_type": c.requirement_type, "category": c.category,
            "criterion_text": c.criterion_text,
            "result": ["PASS", "UNKNOWN", "PASS", "UNKNOWN", "FAIL"][i - 1],
            "evidence_summary": "ev", "reasoning": "why", "confidence": "MEDIUM",
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
        status=ScreeningSessionStatus.READY_FOR_ROUND_1,
    )
    db.add(session)
    db.flush()

    return SimpleNamespace(
        hr=hr, job=job, rubric=rubric, criteria=criteria,
        application=application, extraction=extraction, prequal=prequal,
        session=session,
    )


def _q_set(n, *, criterion_ids=None):
    cats = ["GAP", "CV", "JD", "BEHAVIORAL", "GAP", "CV", "JD", "BEHAVIORAL"]
    cids = criterion_ids or [None] * n
    return ScreeningQuestionSet(questions=[
        AIScreeningQuestion(
            category=cats[i % len(cats)],
            criterion_id=cids[i] if i < len(cids) else None,
            question_text=f"Question {i + 1}?",
            generated_reason=f"reason {i + 1}",
        )
        for i in range(n)
    ])


def _patch(mocker, return_value):
    return mocker.patch.object(sqs, "get_structured_response", return_value=return_value)


def _events(db, session_id, event_type):
    return db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == session_id,
            AuditEvent.event_type == event_type.value,
        )
    ).scalars().all()


def _count(db, model, **where):
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return db.execute(stmt).scalar_one()


# =====================================================================
# generate_round_questions — round 1
# =====================================================================


def test_round_1_happy_path(db, mocker):
    s = _seed_ready_session(db)
    spy = _patch(mocker, _q_set(5))

    rows = generate_round_questions(
        db, screening_session_id=s.session.id, round=1,
        requested_by_user_id=SYSTEM_USER_ID,
    )

    assert len(rows) == 5
    assert [r.sequence_index for r in rows] == [0, 1, 2, 3, 4]
    assert all(r.round == 1 for r in rows)
    assert all(r.ai_model for r in rows)
    assert spy.call_count == 1

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS

    evs = _events(db, s.session.id, AuditEventType.SCREENING_QUESTIONS_GENERATED)
    assert len(evs) == 1
    md = evs[0].new_state
    assert md["round"] == 1
    assert md["question_count"] == 5
    assert set(md["category_breakdown"]) == {"JD", "CV", "BEHAVIORAL", "GAP"}
    assert evs[0].user_id == SYSTEM_USER_ID
    # No question / reason text anywhere in the audit row.
    blob = f"{evs[0].action}{evs[0].previous_state}{md}"
    assert "Question 1?" not in blob and "reason 1" not in blob


def test_round_1_is_idempotent(db, mocker):
    s = _seed_ready_session(db)
    spy = _patch(mocker, _q_set(5))

    first = generate_round_questions(
        db, screening_session_id=s.session.id, round=1,
        requested_by_user_id=SYSTEM_USER_ID,
    )
    again = generate_round_questions(
        db, screening_session_id=s.session.id, round=1,
        requested_by_user_id=SYSTEM_USER_ID,
    )

    assert {r.id for r in first} == {r.id for r in again}
    assert spy.call_count == 1  # no second AI call
    assert _count(db, ScreeningQuestion, screening_session_id=s.session.id) == 5
    assert len(_events(db, s.session.id,
                       AuditEventType.SCREENING_QUESTIONS_GENERATED)) == 1


def test_round_1_wrong_status_raises_before_any_ai_call(db, mocker):
    s = _seed_ready_session(db)
    s.session.status = ScreeningSessionStatus.PENDING
    db.flush()
    spy = _patch(mocker, _q_set(5))

    with pytest.raises(ScreeningRoundPreconditionError):
        generate_round_questions(
            db, screening_session_id=s.session.id, round=1,
            requested_by_user_id=SYSTEM_USER_ID,
        )
    assert spy.call_count == 0
    assert _count(db, ScreeningQuestion, screening_session_id=s.session.id) == 0


def test_round_1_requires_internal_user(db, mocker):
    s = _seed_ready_session(db)
    _patch(mocker, _q_set(5))
    for bad in (None, "nope", uuid.uuid4()):
        with pytest.raises(UnauthorizedError):
            generate_round_questions(
                db, screening_session_id=s.session.id, round=1,
                requested_by_user_id=bad,
            )


@pytest.mark.parametrize("n", [0, 3, 9, 12])
def test_round_1_length_out_of_bounds_rejected(db, mocker, n):
    s = _seed_ready_session(db)
    _patch(mocker, _q_set(n))
    with pytest.raises(ScreeningQuestionError):
        generate_round_questions(
            db, screening_session_id=s.session.id, round=1,
            requested_by_user_id=SYSTEM_USER_ID,
        )
    assert _count(db, ScreeningQuestion, screening_session_id=s.session.id) == 0


def test_round_1_hallucinated_criterion_id_rejected(db, mocker):
    s = _seed_ready_session(db)
    _patch(mocker, _q_set(5, criterion_ids=[str(uuid.uuid4())] + [None] * 4))
    with pytest.raises(ScreeningQuestionError):
        generate_round_questions(
            db, screening_session_id=s.session.id, round=1,
            requested_by_user_id=SYSTEM_USER_ID,
        )
    assert _count(db, ScreeningQuestion, screening_session_id=s.session.id) == 0


def test_round_1_real_criterion_id_is_mapped(db, mocker):
    s = _seed_ready_session(db)
    real = str(s.criteria[1].id)
    _patch(mocker, _q_set(5, criterion_ids=[real] + [None] * 4))
    rows = generate_round_questions(
        db, screening_session_id=s.session.id, round=1,
        requested_by_user_id=SYSTEM_USER_ID,
    )
    assert str(rows[0].rubric_criterion_id) == real
    assert rows[1].rubric_criterion_id is None


@pytest.mark.parametrize("forced", ["PASS", "FAIL", "UNKNOWN"])
def test_prequalification_outcome_never_gates_generation(db, mocker, forced):
    override = None
    s = _seed_ready_session(db)
    s.prequal.results = [
        {**r, "result": forced} for r in s.prequal.results
    ]
    db.flush()
    _patch(mocker, _q_set(5))

    rows = generate_round_questions(
        db, screening_session_id=s.session.id, round=1,
        requested_by_user_id=SYSTEM_USER_ID,
    )
    assert len(rows) == 5
    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS


# =====================================================================
# submit_answer + round transitions
# =====================================================================


def _generate_r1(db, mocker, s, n=4):
    _patch(mocker, _q_set(n))
    return generate_round_questions(
        db, screening_session_id=s.session.id, round=1,
        requested_by_user_id=SYSTEM_USER_ID,
    )


def test_submit_answer_happy_path(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)

    a = submit_answer(db, screening_question_id=qs[0].id, answer_text="  my answer ")
    assert a.answer_text == "my answer"
    assert a.submitted_at is not None
    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS


def test_submit_answer_edit_while_round_open(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)

    submit_answer(db, screening_question_id=qs[0].id, answer_text="first")
    a = submit_answer(db, screening_question_id=qs[0].id, answer_text="second")
    assert a.answer_text == "second"
    assert _count(db, ScreeningAnswer, screening_question_id=qs[0].id) == 1


def test_submit_answer_empty_rejected(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)
    with pytest.raises(ScreeningQuestionError):
        submit_answer(db, screening_question_id=qs[0].id, answer_text="   ")


def test_last_round_1_answer_completes_round_and_triggers_round_2(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)
    # Round-2 generator returns 2 follow-ups.
    for i, q in enumerate(qs):
        if i < len(qs) - 1:
            submit_answer(db, screening_question_id=q.id, answer_text=f"a{i}")

    _patch(mocker, _q_set(2))  # round-2 generation
    submit_answer(db, screening_question_id=qs[-1].id, answer_text="last")

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.ROUND_2_IN_PROGRESS
    assert _count(db, ScreeningQuestion,
                  screening_session_id=s.session.id, round=2) == 2


def test_round_2_zero_questions_goes_straight_to_complete(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)
    for i, q in enumerate(qs[:-1]):
        submit_answer(db, screening_question_id=q.id, answer_text=f"a{i}")

    _patch(mocker, _q_set(0))  # round 2: nothing worth following up
    submit_answer(db, screening_question_id=qs[-1].id, answer_text="last")

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.SCREENING_COMPLETE
    db.refresh(s.application)
    assert s.application.status == ApplicationStatus.SCREENING_COMPLETED

    completed = _events(db, s.session.id, AuditEventType.AI_SCREENING_COMPLETED)
    assert len(completed) == 1
    assert completed[0].new_state["reason"] == "round_2_no_followups"
    assert _count(db, ScreeningQuestion,
                  screening_session_id=s.session.id, round=2) == 0


def _answer_all_but_last(db, qs):
    for i, q in enumerate(qs[:-1]):
        submit_answer(db, screening_question_id=q.id, answer_text=f"a{i}")


def test_double_submit_of_last_answer_does_not_double_transition_or_emit(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)
    _answer_all_but_last(db, qs)

    _patch(mocker, _q_set(0))
    submit_answer(db, screening_question_id=qs[-1].id, answer_text="last")
    # A rerun re-submits the same last answer.
    with pytest.raises(ScreeningAnswerNotAllowedError):
        submit_answer(db, screening_question_id=qs[-1].id, answer_text="last again")

    assert len(_events(db, s.session.id,
                       AuditEventType.AI_SCREENING_COMPLETED)) == 1
    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.SCREENING_COMPLETE


def test_answer_after_round_complete_is_rejected(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)
    _answer_all_but_last(db, qs)
    _patch(mocker, _q_set(0))
    submit_answer(db, screening_question_id=qs[-1].id, answer_text="last")  # completes

    with pytest.raises(ScreeningAnswerNotAllowedError):
        submit_answer(db, screening_question_id=qs[0].id, answer_text="edit later")


def test_last_round_2_answer_completes_screening_once(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)
    _answer_all_but_last(db, qs)
    _patch(mocker, _q_set(2))  # round 2 -> 2 questions
    submit_answer(db, screening_question_id=qs[-1].id, answer_text="last")

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.ROUND_2_IN_PROGRESS
    r2 = db.execute(
        select(ScreeningQuestion).where(
            ScreeningQuestion.screening_session_id == s.session.id,
            ScreeningQuestion.round == 2,
        ).order_by(ScreeningQuestion.sequence_index)
    ).scalars().all()

    submit_answer(db, screening_question_id=r2[0].id, answer_text="r2a0")
    submit_answer(db, screening_question_id=r2[1].id, answer_text="r2a1")

    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.SCREENING_COMPLETE
    completed = _events(db, s.session.id, AuditEventType.AI_SCREENING_COMPLETED)
    assert len(completed) == 1
    assert completed[0].new_state["reason"] == "round_2_answers_complete"


def test_round_2_generation_retry_after_failure(db, mocker):
    s = _seed_ready_session(db)
    qs = _generate_r1(db, mocker, s, n=4)
    _answer_all_but_last(db, qs)

    # Round-2 generation fails on the submit path.
    _patch(mocker, _q_set(2)).side_effect = sqs.AIError("boom")
    submit_answer(db, screening_question_id=qs[-1].id, answer_text="last")
    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.ROUND_1_COMPLETE  # parked

    # Page-load retry succeeds.
    _patch(mocker, _q_set(2))
    ensure_round_2_generated(db, screening_session_id=s.session.id)
    db.refresh(s.session)
    assert s.session.status == ScreeningSessionStatus.ROUND_2_IN_PROGRESS


# =====================================================================
# get_round_questions — candidate-safe projection
# =====================================================================


def test_get_round_questions_omits_reason_and_criterion(db, mocker):
    s = _seed_ready_session(db)
    real = str(s.criteria[0].id)
    _patch(mocker, _q_set(4, criterion_ids=[real, None, None, None]))
    generate_round_questions(
        db, screening_session_id=s.session.id, round=1,
        requested_by_user_id=SYSTEM_USER_ID,
    )
    submit_answer(
        db,
        screening_question_id=str(
            db.execute(select(ScreeningQuestion.id).where(
                ScreeningQuestion.screening_session_id == s.session.id
            ).order_by(ScreeningQuestion.sequence_index)).scalars().first()
        ),
        answer_text="done",
    )

    views = get_round_questions(db, screening_session_id=s.session.id, round=1)
    assert len(views) == 4
    fields = set(vars(views[0]))
    assert "generated_reason" not in fields
    assert "rubric_criterion_id" not in fields and "criterion_id" not in fields
    assert views[0].answered is True and views[0].answer_text == "done"
    assert views[1].answered is False and views[1].answer_text is None


# =====================================================================
# abandon_screening — explicit HR decision
# =====================================================================


def test_abandon_screening_sets_incomplete_not_rejected(db, mocker):
    s = _seed_ready_session(db)
    _generate_r1(db, mocker, s, n=4)
    hr = _hr_id(db)

    changed = abandon_screening(
        db, screening_session_id=s.session.id, requested_by_user_id=hr,
    )
    assert changed is True
    db.refresh(s.application)
    assert s.application.status == ApplicationStatus.SCREENING_INCOMPLETE
    assert "REJECTED" not in ApplicationStatus.ALL  # no such status exists

    evs = _events(db, s.session.id, AuditEventType.AI_SCREENING_INCOMPLETE)
    assert len(evs) == 1
    assert evs[0].user_id == hr  # a human decision, attributed to the HR user


def test_abandon_screening_is_idempotent(db, mocker):
    s = _seed_ready_session(db)
    _generate_r1(db, mocker, s, n=4)
    hr = _hr_id(db)

    assert abandon_screening(
        db, screening_session_id=s.session.id, requested_by_user_id=hr) is True
    assert abandon_screening(
        db, screening_session_id=s.session.id, requested_by_user_id=hr) is False
    assert len(_events(db, s.session.id,
                       AuditEventType.AI_SCREENING_INCOMPLETE)) == 1


def test_abandon_screening_is_hr_only(db):
    s = _seed_ready_session(db)
    for bad in (None, "x", uuid.uuid4()):
        with pytest.raises(UnauthorizedError):
            abandon_screening(
                db, screening_session_id=s.session.id, requested_by_user_id=bad,
            )


def test_abandon_screening_refused_once_complete(db, mocker):
    s = _seed_ready_session(db)
    s.session.status = ScreeningSessionStatus.SCREENING_COMPLETE
    db.flush()
    hr = _hr_id(db)
    with pytest.raises(ScreeningRoundPreconditionError):
        abandon_screening(
            db, screening_session_id=s.session.id, requested_by_user_id=hr,
        )
