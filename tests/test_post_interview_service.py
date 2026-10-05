"""Tests for app.services.post_interview_service (Phase 4 Step 9).

Mocking boundary: patch
``app.services.post_interview_service.get_structured_response`` — the real
Claude API is never touched. Real Postgres via the savepoint-rollback ``db``
fixture; every upstream input is built directly with the ORM.

The first section is pure Python (no DB, no AI): the two deterministic rule
sets CLAUDE.md §§20-21 require to be documented and reproducible.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.ai.claude_client import AIError, AIOutputError
from app.ai.schemas.post_interview_analysis import PostInterviewAnalysisAssessment
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.document import Document
from app.database.models.interview_guide import InterviewGuide
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.job_requirement import RequirementType
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysis,
    PostInterviewAnalysisStatus,
)
from app.database.models.prequalification_result import PrequalificationResult
from app.database.models.resume_extraction import ResumeExtraction
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.screening_evaluation import (
    ScreeningEvaluation,
    ScreeningRecommendation,
)
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services import post_interview_service as pis
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.interview_feedback_service import create_interview_feedback
from app.services.job_service import create_job
from app.services.post_interview_service import (
    PostInterviewAnalysisActorError,
    PostInterviewAnalysisError,
    PostInterviewAnalysisPreconditionError,
    PostInterviewAnalysisTargetNotFoundError,
    compute_post_interview_confidence,
    compute_post_interview_recommendation,
    create_post_interview_analysis,
    get_current_post_interview_analysis,
    get_post_interview_analysis_history,
)
from app.services.shortlist_service import shortlist_candidate
from app.utils.authorization import UnauthorizedError

_PDF_MIME = "application/pdf"

_CRITERIA = [
    ("MANDATORY", "Core", "5+ years professional Python"),
    ("MANDATORY", "Data", "Hands-on Apache Spark / PySpark"),
    ("PREFERRED", "Messaging", "Kafka or equivalent"),
    ("BEHAVIORAL", "Collab", "Has mentored junior engineers"),
    ("EXPERIENCE", "Domain", "3+ years in fintech"),
]

# Long enough to clear MIN_NOTES_CHARS.
_SUBSTANTIVE_NOTES = (
    "The candidate talked through the Spark pipeline in real detail and "
    "explained the trade-offs they made on partitioning."
)


# =====================================================================
# Section 1 — deterministic rules (no DB, no AI)
# =====================================================================


def _conf(**over):
    kw = dict(
        screening_confidence="MEDIUM",
        has_substantive_notes=True,
        rating_count=1,
        unknown_count=0,
        transcript_used=False,
    )
    kw.update(over)
    return compute_post_interview_confidence(**kw)


def test_confidence_rule_1_no_notes_and_no_ratings_is_low():
    """The interview added nothing checkable."""
    assert _conf(has_substantive_notes=False, rating_count=0) == "LOW"
    # ...even when the screening itself was HIGH-confidence.
    assert (
        _conf(
            screening_confidence="HIGH",
            has_substantive_notes=False,
            rating_count=0,
        )
        == "LOW"
    )


def test_confidence_rule_1_either_notes_or_ratings_clears_it():
    assert _conf(has_substantive_notes=True, rating_count=0) != "LOW"
    assert _conf(has_substantive_notes=False, rating_count=2) != "LOW"


def test_confidence_rule_2_low_screening_with_unresolved_unknowns_is_low():
    assert _conf(screening_confidence="LOW", unknown_count=1) == "LOW"


def test_confidence_rule_2_low_screening_with_no_unknowns_is_not_low():
    """A weak base the interview DID close out is no longer weak."""
    assert _conf(screening_confidence="LOW", unknown_count=0) == "MEDIUM"


def test_confidence_rule_3_high_needs_everything():
    assert (
        _conf(
            screening_confidence="HIGH",
            has_substantive_notes=True,
            rating_count=1,
            unknown_count=0,
        )
        == "HIGH"
    )


@pytest.mark.parametrize(
    "over",
    [
        {"screening_confidence": "MEDIUM"},
        {"has_substantive_notes": False},
        {"rating_count": 0},
        {"unknown_count": 1},
    ],
)
def test_confidence_rule_3_any_missing_piece_drops_below_high(over):
    kw = dict(
        screening_confidence="HIGH",
        has_substantive_notes=True,
        rating_count=1,
        unknown_count=0,
        transcript_used=False,
    )
    kw.update(over)
    assert compute_post_interview_confidence(**kw) != "HIGH"


def test_confidence_rule_4_medium_is_the_default():
    assert _conf(screening_confidence="MEDIUM", unknown_count=3) == "MEDIUM"


def test_confidence_is_always_one_of_the_three_values():
    for sc, notes, ratings, unknowns in itertools.product(
        ("HIGH", "MEDIUM", "LOW"), (True, False), (0, 1, 4), (0, 1, 5)
    ):
        assert compute_post_interview_confidence(
            screening_confidence=sc,
            has_substantive_notes=notes,
            rating_count=ratings,
            unknown_count=unknowns,
            transcript_used=False,
        ) in {"HIGH", "MEDIUM", "LOW"}


def _rec(**over):
    kw = dict(
        screening_results=[
            {"requirement_type": RequirementType.MANDATORY, "result": "PASS"},
        ],
        post_interview_confidence="HIGH",
        unknown_count=0,
    )
    kw.update(over)
    return compute_post_interview_recommendation(**kw)


def test_recommendation_rule_1_mandatory_fail_holds():
    """A mandatory shortfall is not cured by an interview going well."""
    assert (
        _rec(
            screening_results=[
                {"requirement_type": RequirementType.MANDATORY, "result": "FAIL"},
                {"requirement_type": RequirementType.PREFERRED, "result": "PASS"},
            ]
        )
        == ScreeningRecommendation.HOLD
    )


def test_recommendation_rule_1_ignores_a_non_mandatory_fail():
    assert (
        _rec(
            screening_results=[
                {"requirement_type": RequirementType.PREFERRED, "result": "FAIL"},
                {"requirement_type": RequirementType.BEHAVIORAL, "result": "FAIL"},
            ]
        )
        == ScreeningRecommendation.PROCEED
    )


def test_recommendation_rule_2_low_confidence_holds():
    assert _rec(post_interview_confidence="LOW") == ScreeningRecommendation.HOLD


def test_recommendation_rule_3_remaining_unknowns_hold():
    assert _rec(unknown_count=1) == ScreeningRecommendation.HOLD


def test_recommendation_rule_4_proceed():
    assert _rec() == ScreeningRecommendation.PROCEED


def test_recommendation_never_returns_reject():
    """CLAUDE.md §§4, 11 — REJECT is a human decision, unreachable here."""
    for results, conf, unknowns in itertools.product(
        (
            [],
            [{"requirement_type": RequirementType.MANDATORY, "result": "FAIL"}],
            [{"requirement_type": RequirementType.MANDATORY, "result": "UNKNOWN"}],
            [{"requirement_type": RequirementType.PREFERRED, "result": "PASS"}],
        ),
        ("HIGH", "MEDIUM", "LOW"),
        (0, 3),
    ):
        out = compute_post_interview_recommendation(
            screening_results=results,
            post_interview_confidence=conf,
            unknown_count=unknowns,
        )
        assert out != ScreeningRecommendation.REJECT
        assert out in ScreeningRecommendation.AUTOMATED


def test_confidence_signature_is_the_old_one_plus_exactly_one_boolean():
    """Increment C added ``transcript_used`` and nothing else — in particular no
    human-recommendation parameter."""
    import inspect

    params = set(inspect.signature(compute_post_interview_confidence).parameters)
    assert params == {
        "screening_confidence", "has_substantive_notes", "rating_count",
        "unknown_count", "transcript_used",
    }


def test_recommendation_does_not_read_the_human_recommendation():
    """It takes no human-recommendation argument at all — echoing the human
    would make this a restatement, and comparing would be §8."""
    import inspect

    params = set(
        inspect.signature(compute_post_interview_recommendation).parameters
    )
    assert params == {
        "screening_results", "post_interview_confidence", "unknown_count"
    }


# =====================================================================
# Section 2 — service (real DB, mocked AI)
# =====================================================================


def _hr(db, role=UserRole.HR):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="HR Person", role=role,
    )


def _seed(db, *, mandatory_result="PASS", overall_confidence="HIGH"):
    """HR + job + approved rubric + fully-evaluated, shortlisted application +
    an interview guide. No interview feedback yet."""
    hr = _hr(db)
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
    for i, (rt, cat, txt) in enumerate(_CRITERIA, start=1):
        c = RubricCriterion(
            rubric_version_id=rubric.id, requirement_type=rt, category=cat,
            criterion_text=txt, display_order=i,
        )
        db.add(c)
        criteria.append(c)
    db.flush()

    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com", full_name="Casey Candidate",
        phone=None,
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

    def _rows(mandatory):
        out = []
        for i, c in enumerate(criteria, start=1):
            result = (
                mandatory if c.requirement_type == RequirementType.MANDATORY
                else "PASS"
            )
            out.append({
                "criterion_id": str(c.id), "criterion_index": i,
                "requirement_type": c.requirement_type, "category": c.category,
                "criterion_text": c.criterion_text, "result": result,
                "evidence_summary": "e", "reasoning": "r", "confidence": "HIGH",
            })
        return out

    db.add(PrequalificationResult(
        application_id=app.id, rubric_version_id=rubric.id,
        resume_extraction_id=extraction.id, results=_rows("PASS"),
        ai_model="claude-sonnet-5",
    ))
    db.flush()

    session = ScreeningSession(
        application_id=app.id, access_token=f"tok-{uuid.uuid4().hex}",
        status=ScreeningSessionStatus.SCREENING_COMPLETE,
    )
    db.add(session)
    db.flush()

    db.add(ScreeningEvaluation(
        screening_session_id=session.id, rubric_version_id=rubric.id,
        results=_rows(mandatory_result),
        requirements_score=8, requirements_coverage=1.0,
        experience_score=7, experience_coverage=1.0,
        behavioral_score=7, behavioral_coverage=1.0,
        strengths=["Strong Python"], gaps=[], unknowns=[],
        overall_confidence=overall_confidence, ai_recommendation="PROCEED",
        ai_model="claude-sonnet-5",
    ))
    db.flush()

    shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    entry = db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.application_id == app.id
        )
    ).scalar_one()

    guide = InterviewGuide(
        job_id=job.id, application_id=app.id, shortlist_entry_id=entry.id,
        rubric_version_id=rubric.id, ai_model="claude-sonnet-5",
        generated_at=datetime.now(timezone.utc),
    )
    db.add(guide)
    db.flush()

    return {
        "hr": hr, "job": job, "rubric": rubric, "criteria": criteria,
        "app": app, "guide": guide, "session": session,
    }


def _feedback(db, s, *, round_number=1, recommendation="PROCEED",
              notes=_SUBSTANTIVE_NOTES, ratings=None):
    return create_interview_feedback(
        db,
        user_id=s["hr"].id,
        application_id=s["app"].id,
        interview_guide_id=s["guide"].id,
        interview_round=round_number,
        recommendation=recommendation,
        notes=notes,
        ratings=ratings if ratings is not None else [
            {"competency_label": "System design", "rating": 4,
             "comment": "Clear trade-offs."},
        ],
    )


def _assessment(**over):
    kw = dict(
        summary="AI consolidated summary of the candidate.",
        strengths=["AI strength item."],
        gaps=["AI gap item."],
        unknowns=[],
        evidence_consistency_notes="AI evidence consistency prose.",
    )
    kw.update(over)
    return PostInterviewAnalysisAssessment(**kw)


def _patch(mocker, assessment=None, **kw):
    if assessment is not None:
        kw.setdefault("return_value", assessment)
    return mocker.patch.object(pis, "get_structured_response", **kw)


def _rows_for(db, application_id):
    return db.execute(
        select(PostInterviewAnalysis)
        .where(PostInterviewAnalysis.application_id == application_id)
        .order_by(PostInterviewAnalysis.created_at, PostInterviewAnalysis.id)
    ).scalars().all()


def _events(db, application_id):
    return db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == application_id,
            AuditEvent.event_type
            == AuditEventType.POST_INTERVIEW_ANALYSIS_COMPLETED.value,
        ).order_by(AuditEvent.timestamp)
    ).scalars().all()


# --- happy path --------------------------------------------------


def test_analysis_is_created_and_stored(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )

    assert out.summary == "AI consolidated summary of the candidate."
    assert out.strengths == ["AI strength item."]
    assert out.gaps == ["AI gap item."]
    assert out.unknowns == []
    assert out.evidence_consistency_notes == "AI evidence consistency prose."
    assert out.status == PostInterviewAnalysisStatus.CURRENT
    assert out.superseded_at is None
    assert out.requested_by_user_id == s["hr"].id
    # Increment C: new rows read every round, so this is False (it is True only
    # on pre-Increment-C rows, which read one record and no transcripts).
    assert out.analyzed_only_latest_feedback is False
    assert out.ai_model


def test_rubric_version_is_frozen_from_the_guide(db, mocker):
    """interview_feedback has no rubric_version_id; the guide is the join path,
    and the value is frozen — never re-derived from the job's current rubric."""
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.rubric_version_id == s["guide"].rubric_version_id
    assert out.interview_guide_id == s["guide"].id


def test_the_anchor_and_snapshot_come_from_the_most_recent_record(db, mocker):
    """Increment C reversed "latest only" for WHAT IS READ, but "most recent
    record" keeps its Step 8 meaning for the anchor column and the snapshot."""
    s = _seed(db)
    earlier = _feedback(db, s, round_number=1, recommendation="HOLD")
    # Every row in this test shares one transaction, and Postgres ``now()`` is
    # transaction-start time — so without this the two rows carry an identical
    # ``created_at`` and "latest" falls back to a random-UUID tie-break. In
    # production each submission commits its own transaction and the timestamps
    # genuinely differ; this reproduces that timeline.
    earlier.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.flush()
    latest = _feedback(db, s, round_number=2, recommendation="PROCEED")
    _patch(mocker, _assessment())

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.interview_feedback_id == latest.id
    assert out.analyzed_only_latest_feedback is False
    assert out.human_recommendation_snapshot == "PROCEED"


def test_human_recommendation_is_snapshotted_verbatim(db, mocker):
    """Including REJECT, which the AI path can never produce itself."""
    s = _seed(db)
    _feedback(db, s, recommendation="REJECT")
    _patch(mocker, _assessment())

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.human_recommendation_snapshot == "REJECT"
    assert out.ai_recommendation in ScreeningRecommendation.AUTOMATED


def test_no_disagreement_is_computed_or_stored(db, mocker):
    """§8 is a later step: nothing here compares the two recommendations, and
    the table has no column that could hold such a verdict."""
    s = _seed(db)
    _feedback(db, s, recommendation="REJECT")   # maximal divergence
    _patch(mocker, _assessment())

    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    columns = set(PostInterviewAnalysis.__table__.columns.keys())
    for forbidden in (
        "disagreement", "disagreement_flag", "has_disagreement",
        "ai_human_disagreement", "agrees_with_human",
    ):
        assert forbidden not in columns

    assert not db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == s["app"].id,
            AuditEvent.event_type
            == AuditEventType.AI_HUMAN_DISAGREEMENT_DETECTED.value,
        )
    ).scalars().all()


def test_python_computes_confidence_and_recommendation(db, mocker):
    """The AI returns prose only; these two values come from the rule sets."""
    s = _seed(db, overall_confidence="HIGH")
    _feedback(db, s)
    _patch(mocker, _assessment(unknowns=[]))

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.confidence == "HIGH"
    assert out.ai_recommendation == ScreeningRecommendation.PROCEED


def test_remaining_unknowns_pull_the_recommendation_to_hold(db, mocker):
    s = _seed(db, overall_confidence="HIGH")
    _feedback(db, s)
    _patch(mocker, _assessment(unknowns=["Team size led is still unclear."]))

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.unknowns == ["Team size led is still unclear."]
    assert out.ai_recommendation == ScreeningRecommendation.HOLD


def test_mandatory_fail_holds_even_after_a_good_interview(db, mocker):
    s = _seed(db, mandatory_result="FAIL", overall_confidence="HIGH")
    _feedback(db, s, recommendation="PROCEED")
    _patch(mocker, _assessment(unknowns=[]))

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.ai_recommendation == ScreeningRecommendation.HOLD


def test_notes_only_feedback_still_produces_an_analysis(db, mocker):
    s = _seed(db)
    _feedback(db, s, ratings=[])
    _patch(mocker, _assessment())

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.confidence in {"HIGH", "MEDIUM", "LOW"}


def test_thin_feedback_yields_low_confidence(db, mocker):
    """No substantive notes and no ratings — the interview added nothing."""
    s = _seed(db, overall_confidence="HIGH")
    _feedback(db, s, notes=None, ratings=[])
    _patch(mocker, _assessment(unknowns=[]))

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert out.confidence == "LOW"
    assert out.ai_recommendation == ScreeningRecommendation.HOLD


# --- idempotency and non-destructive regeneration ----------------


def test_second_call_without_force_makes_no_ai_call(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    ai = _patch(mocker, _assessment())

    first = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    again = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )

    assert ai.call_count == 1
    assert again.id == first.id
    assert len(_rows_for(db, s["app"].id)) == 1


def test_force_supersedes_instead_of_deleting(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    ai = _patch(mocker, _assessment())

    first = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    ai.return_value = _assessment(summary="Second AI summary.")
    second = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id, force=True
    )

    assert ai.call_count == 2
    assert second.id != first.id

    rows = _rows_for(db, s["app"].id)
    assert len(rows) == 2                      # nothing deleted
    db.refresh(first)
    assert first.status == PostInterviewAnalysisStatus.SUPERSEDED
    assert first.superseded_at is not None
    assert first.summary == "AI consolidated summary of the candidate."
    assert second.status == PostInterviewAnalysisStatus.CURRENT
    assert second.superseded_at is None


def test_exactly_one_current_row_after_repeated_regeneration(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())

    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    for _ in range(3):
        create_post_interview_analysis(
            db, user_id=s["hr"].id, application_id=s["app"].id, force=True
        )

    rows = _rows_for(db, s["app"].id)
    assert len(rows) == 4
    current = [r for r in rows if r.status == PostInterviewAnalysisStatus.CURRENT]
    assert len(current) == 1


# --- audit -------------------------------------------------------


def test_exactly_one_audit_event_per_generation(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())

    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert len(_events(db, s["app"].id)) == 1

    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id, force=True
    )
    events = _events(db, s["app"].id)
    assert len(events) == 2
    assert events[1].new_state["regenerated"] is True
    assert events[1].new_state["superseded_analysis_id"]


def test_no_ai_prose_or_interviewer_text_reaches_the_audit_trail(db, mocker):
    """CLAUDE.md §§12, 22, 24 — audit metadata carries ids, enums and counts."""
    s = _seed(db)
    _feedback(db, s, notes=_SUBSTANTIVE_NOTES)
    _patch(mocker, _assessment(
        summary="SECRET-SUMMARY", strengths=["SECRET-STRENGTH"],
        gaps=["SECRET-GAP"], unknowns=["SECRET-UNKNOWN"],
        evidence_consistency_notes="SECRET-NOTES",
    ))

    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    blob = "".join(
        f"{e.action}{e.new_state}{e.previous_state}{e.event_metadata}"
        for e in _events(db, s["app"].id)
    )
    for secret in (
        "SECRET-SUMMARY", "SECRET-STRENGTH", "SECRET-GAP", "SECRET-UNKNOWN",
        "SECRET-NOTES", _SUBSTANTIVE_NOTES, "Casey Candidate",
    ):
        assert secret not in blob


def test_audit_event_carries_the_structural_facts(db, mocker):
    s = _seed(db)
    fb = _feedback(db, s, round_number=3)
    _patch(mocker, _assessment(strengths=["a", "b"], gaps=[], unknowns=["u"]))

    out = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    meta = _events(db, s["app"].id)[0].new_state
    assert meta["post_interview_analysis_id"] == str(out.id)
    assert meta["interview_feedback_id"] == str(fb.id)
    assert meta["interview_round"] == 3
    assert meta["strength_count"] == 2
    assert meta["gap_count"] == 0
    assert meta["unknown_count"] == 1
    assert meta["analyzed_only_latest_feedback"] is False
    assert meta["regenerated"] is False


def test_audit_event_is_attributed_to_the_requesting_hr_user(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())

    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    assert _events(db, s["app"].id)[0].user_id == s["hr"].id


# --- authorization -----------------------------------------------


def test_unknown_actor_is_rejected(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    ai = _patch(mocker, _assessment())

    with pytest.raises(UnauthorizedError):
        create_post_interview_analysis(
            db, user_id=uuid.uuid4(), application_id=s["app"].id
        )
    assert ai.call_count == 0
    assert not _rows_for(db, s["app"].id)


def test_system_actor_is_rejected(db, mocker):
    """The pipeline account may not manufacture an analysis of a human's
    testimony — mirrors interview_feedback_service."""
    s = _seed(db)
    _feedback(db, s)
    ai = _patch(mocker, _assessment())

    with pytest.raises(PostInterviewAnalysisActorError):
        create_post_interview_analysis(
            db, user_id=SYSTEM_USER_ID, application_id=s["app"].id
        )
    assert ai.call_count == 0
    assert not _rows_for(db, s["app"].id)


def test_system_actor_rejection_message_is_user_safe(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())

    with pytest.raises(PostInterviewAnalysisActorError) as exc:
        create_post_interview_analysis(
            db, user_id=SYSTEM_USER_ID, application_id=s["app"].id
        )
    assert "signed-in HR user" in str(exc.value)
    assert "Casey Candidate" not in str(exc.value)


def test_reads_require_an_internal_user(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())
    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )

    with pytest.raises(UnauthorizedError):
        get_current_post_interview_analysis(
            db, s["app"].id, acting_user_id=uuid.uuid4()
        )
    with pytest.raises(UnauthorizedError):
        get_post_interview_analysis_history(
            db, s["app"].id, acting_user_id=uuid.uuid4()
        )


# --- preconditions -----------------------------------------------


def test_missing_application_is_rejected(db, mocker):
    _hr_user = _hr(db)
    ai = _patch(mocker, _assessment())
    with pytest.raises(PostInterviewAnalysisTargetNotFoundError):
        create_post_interview_analysis(
            db, user_id=_hr_user.id, application_id=uuid.uuid4()
        )
    assert ai.call_count == 0


def test_no_interview_feedback_is_rejected(db, mocker):
    s = _seed(db)                       # deliberately no feedback
    ai = _patch(mocker, _assessment())

    with pytest.raises(PostInterviewAnalysisPreconditionError) as exc:
        create_post_interview_analysis(
            db, user_id=s["hr"].id, application_id=s["app"].id
        )
    assert "interview feedback" in str(exc.value).lower()
    assert ai.call_count == 0
    assert not _rows_for(db, s["app"].id)


def test_missing_screening_evaluation_is_rejected(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    db.execute(
        ScreeningEvaluation.__table__.delete().where(
            ScreeningEvaluation.screening_session_id == s["session"].id
        )
    )
    db.flush()
    ai = _patch(mocker, _assessment())

    with pytest.raises(PostInterviewAnalysisPreconditionError):
        create_post_interview_analysis(
            db, user_id=s["hr"].id, application_id=s["app"].id
        )
    assert ai.call_count == 0
    assert not _rows_for(db, s["app"].id)


def test_rubric_with_no_criteria_is_rejected(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    db.execute(
        RubricCriterion.__table__.delete().where(
            RubricCriterion.rubric_version_id == s["rubric"].id
        )
    )
    db.flush()
    ai = _patch(mocker, _assessment())

    with pytest.raises(PostInterviewAnalysisPreconditionError):
        create_post_interview_analysis(
            db, user_id=s["hr"].id, application_id=s["app"].id
        )
    assert ai.call_count == 0


# --- AI failure --------------------------------------------------


def test_ai_failure_persists_nothing(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, side_effect=AIError("upstream down"))

    with pytest.raises(PostInterviewAnalysisError) as exc:
        create_post_interview_analysis(
            db, user_id=s["hr"].id, application_id=s["app"].id
        )
    assert "upstream down" not in str(exc.value)
    assert not _rows_for(db, s["app"].id)
    assert not _events(db, s["app"].id)


def test_invalid_ai_output_persists_nothing(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, side_effect=AIOutputError(
        "post_interview_analysis", "{bad json}", "summary: field required"
    ))

    with pytest.raises(PostInterviewAnalysisError):
        create_post_interview_analysis(
            db, user_id=s["hr"].id, application_id=s["app"].id
        )
    assert not _rows_for(db, s["app"].id)
    assert not _events(db, s["app"].id)


def test_ai_failure_on_regeneration_leaves_the_current_analysis_intact(db, mocker):
    """CLAUDE.md §27 — a Claude failure preserves existing data."""
    s = _seed(db)
    _feedback(db, s)
    ai = _patch(mocker, _assessment())
    first = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )

    ai.side_effect = AIError("boom")
    with pytest.raises(PostInterviewAnalysisError):
        create_post_interview_analysis(
            db, user_id=s["hr"].id, application_id=s["app"].id, force=True
        )

    db.refresh(first)
    assert first.status == PostInterviewAnalysisStatus.CURRENT
    assert first.superseded_at is None
    assert len(_rows_for(db, s["app"].id)) == 1
    assert len(_events(db, s["app"].id)) == 1


# --- reads -------------------------------------------------------


def test_current_read_returns_none_before_any_analysis(db):
    s = _seed(db)
    assert get_current_post_interview_analysis(
        db, s["app"].id, acting_user_id=s["hr"].id
    ) is None
    assert get_post_interview_analysis_history(
        db, s["app"].id, acting_user_id=s["hr"].id
    ) == []


def test_current_read_returns_only_the_current_row(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    ai = _patch(mocker, _assessment())
    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    ai.return_value = _assessment(summary="Newest summary.")
    newest = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id, force=True
    )

    view = get_current_post_interview_analysis(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert view is not None
    assert view.analysis_id == newest.id
    assert view.summary == "Newest summary."
    assert view.status == PostInterviewAnalysisStatus.CURRENT
    assert view.requested_by_name == "HR Person"


def test_history_returns_every_row_newest_first(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    ai = _patch(mocker, _assessment())
    first = create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    # Distinct timestamps, for the same shared-transaction reason as above.
    first.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.flush()
    ai.return_value = _assessment(summary="Second.")
    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id, force=True
    )

    history = get_post_interview_analysis_history(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert len(history) == 2
    assert history[0].summary == "Second."
    assert history[0].status == PostInterviewAnalysisStatus.CURRENT
    assert history[1].status == PostInterviewAnalysisStatus.SUPERSEDED
    assert history[1].superseded_at is not None


def test_history_is_scoped_to_one_application(db, mocker):
    a = _seed(db)
    b = _seed(db)
    _feedback(db, a)
    _feedback(db, b)
    _patch(mocker, _assessment())
    create_post_interview_analysis(
        db, user_id=a["hr"].id, application_id=a["app"].id
    )

    assert len(get_post_interview_analysis_history(
        db, a["app"].id, acting_user_id=a["hr"].id
    )) == 1
    assert get_post_interview_analysis_history(
        db, b["app"].id, acting_user_id=b["hr"].id
    ) == []


def test_views_are_usable_after_the_session_closes(db, mocker):
    """Frozen primitives only — a Streamlit page renders these outside the
    session (the InterviewFeedbackView pattern)."""
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())
    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    view = get_current_post_interview_analysis(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    db.expunge_all()
    assert view.summary and view.confidence and view.ai_recommendation
    assert isinstance(view.strengths, list)
    with pytest.raises(Exception):
        view.summary = "mutated"        # frozen dataclass


# --- status vocabulary -------------------------------------------


def test_status_vocabulary_is_exactly_two_values():
    assert PostInterviewAnalysisStatus.ALL == {"CURRENT", "SUPERSEDED"}
    assert PostInterviewAnalysisStatus.is_valid("CURRENT")
    assert not PostInterviewAnalysisStatus.is_valid("REJECTED")


def test_stored_status_is_always_a_valid_value(db, mocker):
    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())
    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id
    )
    create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id, force=True
    )
    for row in _rows_for(db, s["app"].id):
        assert PostInterviewAnalysisStatus.is_valid(row.status)
        assert row.confidence in {"HIGH", "MEDIUM", "LOW"}
        assert row.ai_recommendation in ScreeningRecommendation.AUTOMATED
