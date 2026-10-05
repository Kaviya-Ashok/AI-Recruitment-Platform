"""Tests for app.services.final_scorecard_service (Phase 4 Step 10).

No AI anywhere — Step 10 makes no Claude call, so there is no mocking boundary
to establish. Real Postgres via the savepoint-rollback ``db`` fixture; every
upstream artefact is built directly with the ORM or through the owning service.

The first section is pure Python (no DB): the one deterministic score this
module derives, which CLAUDE.md §§10/20 require to be documented and
reproducible.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database.models.application import Application, ApplicationStatus
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.document import Document
from app.database.models.interview_feedback import (
    RATING_MAX,
    InterviewFeedback,
)
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
from app.database.models.screening_answer import ScreeningAnswer
from app.database.models.screening_evaluation import ScreeningEvaluation
from app.database.models.screening_question import (
    ScreeningQuestion,
    ScreeningQuestionCategory,
)
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services import final_scorecard_service as fss
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.final_scorecard_service import (
    DISAGREEMENT_NOT_ASSESSED,
    FINAL_DECISION_NOT_DECIDED,
    INSUFFICIENT_EVIDENCE,
    MULTIPLE_RUBRIC_VERSIONS_WARNING,
    FinalScorecardActorError,
    Provenance,
    compute_interview_score,
    get_final_scorecard,
)
from app.services.interview_feedback_service import create_interview_feedback
from app.services.job_service import create_job
from app.utils.authorization import UnauthorizedError

_PDF_MIME = "application/pdf"

_CRITERIA = [
    ("MANDATORY", "Core", "5+ years professional Python"),
    ("MANDATORY", "Data", "Hands-on Apache Spark / PySpark"),
    ("PREFERRED", "Messaging", "Kafka or equivalent"),
    ("BEHAVIORAL", "Collab", "Has mentored junior engineers"),
    ("EXPERIENCE", "Domain", "3+ years in fintech"),
    ("OTHER", "Misc", "Comfortable with on-call rotation"),
]

_NOTES = (
    "Candidate walked through the Spark pipeline in real detail and explained "
    "the partitioning trade-offs clearly."
)


# =====================================================================
# Section 1 — the one derived score (no DB, no AI)
# =====================================================================


def test_interview_score_is_none_without_ratings():
    """Notes-only feedback is legitimate; no ratings means "not scored", not
    "scored zero" (CLAUDE.md — unknown is not fail)."""
    assert compute_interview_score([]) is None
    assert compute_interview_score([None, "x", True]) is None  # type: ignore[list-item]


@pytest.mark.parametrize(
    "ratings, expected",
    [
        ([5], 10),
        ([5, 5, 5], 10),
        ([1], 2),          # bottom of a 1-5 scale is 1/5, deliberately not 0
        ([1, 1, 1], 2),
        ([3], 6),
        ([4, 3], 7),       # mean 3.5 -> 7.0
        ([2, 5, 3, 1], 6),  # mean 2.75 -> 5.5 -> round -> 6
    ],
)
def test_interview_score_formula(ratings, expected):
    """score = round(10 * mean / RATING_MAX), clamped to [0, 10]."""
    assert compute_interview_score(ratings) == expected


def test_interview_score_always_in_range():
    for combo in itertools.product(range(1, RATING_MAX + 1), repeat=3):
        assert 0 <= compute_interview_score(list(combo)) <= 10


def test_interview_score_takes_no_confidence_argument():
    """CLAUDE.md §21 separation — confidence is never blended into a score."""
    params = set(inspect.signature(compute_interview_score).parameters)
    assert params == {"ratings"}


# =====================================================================
# Section 2 — assembly (real DB)
# =====================================================================


def _hr(db, role=UserRole.HR):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="HR Person", role=role,
    )


def _rubric(db, job, hr, *, version_number=1,
            status=RubricVersionStatus.APPROVED):
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


def _rows(criteria, *, mandatory_result="PASS", other_result="PASS"):
    out = []
    for i, c in enumerate(criteria, start=1):
        result = (
            mandatory_result if c.requirement_type == RequirementType.MANDATORY
            else other_result
        )
        out.append({
            "criterion_id": str(c.id), "criterion_index": i,
            "requirement_type": c.requirement_type, "category": c.category,
            "criterion_text": c.criterion_text, "result": result,
            "evidence_summary": f"evidence for {c.criterion_text}",
            "reasoning": "reasoning", "confidence": "HIGH",
        })
    return out


def _seed(db, *, with_feedback=True, with_analysis=True, with_ranking=True,
          with_shortlist=True, with_guide=True, with_transcript=True,
          ratings=None, human_recommendation="PROCEED", notes=_NOTES,
          mandatory_result="PASS", other_result="PASS",
          prequal_rubric=None):
    """A fully-evaluated application. Later stages are individually optional so
    the missing-source tests can switch them off one at a time.

    ``with_transcript`` seeds two REAL ``screening_questions`` /
    ``screening_answers`` rows (one answered, one not) — deliberately a
    different count from ``len(_CRITERIA)`` (6), so a test asserting the
    transcript count also proves it is no longer reading the criterion-row
    count."""
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

    db.add(PrequalificationResult(
        application_id=app.id,
        rubric_version_id=(prequal_rubric or rubric).id,
        resume_extraction_id=extraction.id, results=_rows(criteria),
        ai_model="claude-sonnet-5",
    ))
    db.flush()

    session = ScreeningSession(
        application_id=app.id, access_token=f"tok-{uuid.uuid4().hex}",
        status=ScreeningSessionStatus.SCREENING_COMPLETE,
    )
    db.add(session)
    db.flush()

    if with_transcript:
        _add_transcript_entry(
            db, session.id, sequence_index=0,
            question_text="How long have you used Python?",
            answer_text="About five years, mostly backend services.",
        )
        _add_transcript_entry(
            db, session.id, sequence_index=1,
            category=ScreeningQuestionCategory.GAP,
            question_text="Can you describe your Kafka experience?",
            answer_text=None,
        )

    evaluation = ScreeningEvaluation(
        screening_session_id=session.id, rubric_version_id=rubric.id,
        results=_rows(criteria, mandatory_result=mandatory_result,
                      other_result=other_result),
        requirements_score=8, requirements_coverage=1.0,
        experience_score=7, experience_coverage=1.0,
        behavioral_score=6, behavioral_coverage=0.5,
        strengths=["Strong Python"], gaps=["No Kafka"], unknowns=["Team size"],
        overall_confidence="HIGH", ai_recommendation="PROCEED",
        ai_model="claude-sonnet-5",
    )
    db.add(evaluation)
    db.flush()

    if with_ranking:
        db.add(CandidateRanking(
            job_id=job.id, rubric_version_id=rubric.id, application_id=app.id,
            rank_position=2, overall_score=7.25, eligible=True,
            mandatory_unknown_flag=False,
            generated_at=datetime.now(timezone.utc),
            generation_batch_id=uuid.uuid4(),
        ))
        db.flush()

    entry = None
    if with_shortlist:
        entry = CandidateShortlistEntry(
            job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
            is_shortlisted=True, reason="Strong Spark evidence",
            rank_position_at_decision=2, decided_by_user_id=hr.id,
            decided_at=datetime.now(timezone.utc),
        )
        db.add(entry)
        db.flush()

    guide = None
    if with_guide and entry is not None:
        guide = InterviewGuide(
            job_id=job.id, application_id=app.id, shortlist_entry_id=entry.id,
            rubric_version_id=rubric.id, ai_model="claude-sonnet-5",
            generated_at=datetime.now(timezone.utc),
        )
        db.add(guide)
        db.flush()

    feedback = None
    if with_feedback and guide is not None:
        feedback = create_interview_feedback(
            db, user_id=hr.id, application_id=app.id,
            interview_guide_id=guide.id, interview_round=1,
            recommendation=human_recommendation, notes=notes,
            ratings=ratings if ratings is not None else [
                {"competency_label": "System design", "rating": 4,
                 "comment": "Clear trade-offs."},
                {"competency_label": "Communication", "rating": 3,
                 "comment": None},
            ],
        )

    analysis = None
    if with_analysis and feedback is not None and guide is not None:
        analysis = PostInterviewAnalysis(
            application_id=app.id, interview_feedback_id=feedback.id,
            interview_guide_id=guide.id, rubric_version_id=rubric.id,
            requested_by_user_id=hr.id,
            summary="AI consolidated summary.",
            strengths=["AI strength."], gaps=["AI gap."], unknowns=[],
            evidence_consistency_notes="Sources line up.",
            confidence="HIGH", ai_recommendation="PROCEED",
            human_recommendation_snapshot=human_recommendation,
            analyzed_only_latest_feedback=True, ai_model="claude-sonnet-5",
            status=PostInterviewAnalysisStatus.CURRENT,
        )
        db.add(analysis)
        db.flush()

    return {
        "hr": hr, "job": job, "rubric": rubric, "criteria": criteria,
        "app": app, "guide": guide, "feedback": feedback,
        "analysis": analysis, "evaluation": evaluation, "entry": entry,
        "session": session,
    }


def _add_transcript_entry(
    db, session_id, *, round_=1, sequence_index=0,
    category=ScreeningQuestionCategory.JD, question_text="Q?",
    answer_text="A.",
):
    """One real screening Q&A pair, via the actual
    ``screening_questions`` / ``screening_answers`` tables — the only way
    ``get_screening_transcript`` can ever return a non-empty result. Pass
    ``answer_text=None`` for an unanswered question."""
    q = ScreeningQuestion(
        screening_session_id=session_id, round=round_,
        sequence_index=sequence_index, category=category,
        rubric_criterion_id=None, question_text=question_text,
        generated_reason="probes a gap", ai_model="claude-sonnet-5",
    )
    db.add(q)
    db.flush()
    if answer_text is not None:
        db.add(ScreeningAnswer(
            screening_question_id=q.id, answer_text=answer_text,
            submitted_at=datetime.now(timezone.utc),
        ))
        db.flush()
    return q


def _card(db, s, user=None):
    return get_final_scorecard(
        db, s["app"].id, acting_user_id=(user or s["hr"]).id
    )


# --- all nine evidence sources -----------------------------------


def test_assembles_every_evidence_source(db):
    s = _seed(db)
    v = _card(db, s)

    assert v is not None
    assert v.candidate_name == "Casey Candidate"          # 1 candidate
    assert v.resume_evidence["skills"] == ["Python", "Spark"]  # 2 résumé
    assert v.mandatory_criteria                            # 3/4 prequal+screening
    assert v.requirements.score == 8                       # 4 screening eval
    assert v.has_screening_transcript is True               # 5 real transcript
    assert v.screening_question_count == 2                  # (seeded by _seed)
    assert v.ranking.rank_position == 2                    # 6 ranking
    assert v.ranking.is_shortlisted is True                # 7 shortlist
    assert v.interview_guide_question_count is not None    # 8 guide
    assert v.interview_round == 1                          # 9 human feedback
    assert v.post_interview_summary == "AI consolidated summary."  # Step 9
    assert v.missing_sources == ()


def test_returns_none_for_unknown_application(db):
    hr = _hr(db)
    assert get_final_scorecard(
        db, uuid.uuid4(), acting_user_id=hr.id
    ) is None


def test_uses_the_latest_human_feedback(db):
    """"Latest" is the Step 8 accessor's own created_at DESC ordering — this
    module does not redefine it as "highest round"."""
    s = _seed(db)
    s["feedback"].created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.flush()
    later = create_interview_feedback(
        db, user_id=s["hr"].id, application_id=s["app"].id,
        interview_guide_id=s["guide"].id, interview_round=2,
        recommendation="HOLD", notes="Second round.",
        ratings=[{"competency_label": "Depth", "rating": 5, "comment": None}],
    )
    v = _card(db, s)
    assert v.interview_round == 2
    assert v.human_recommendation == "HOLD"
    assert v.interview_ratings[0].competency_label == "Depth"
    assert later.id


def test_uses_only_the_current_post_interview_analysis(db):
    """Step 9's CURRENT/SUPERSEDED semantics are respected, not re-derived."""
    s = _seed(db)
    s["analysis"].status = PostInterviewAnalysisStatus.SUPERSEDED
    s["analysis"].superseded_at = datetime.now(timezone.utc)
    db.add(PostInterviewAnalysis(
        application_id=s["app"].id,
        interview_feedback_id=s["feedback"].id,
        interview_guide_id=s["guide"].id, rubric_version_id=s["rubric"].id,
        requested_by_user_id=s["hr"].id, summary="Newer summary.",
        strengths=[], gaps=[], unknowns=[],
        evidence_consistency_notes="n", confidence="MEDIUM",
        ai_recommendation="HOLD", human_recommendation_snapshot="PROCEED",
        analyzed_only_latest_feedback=True, ai_model="claude-sonnet-5",
        status=PostInterviewAnalysisStatus.CURRENT,
    ))
    db.flush()

    v = _card(db, s)
    assert v.post_interview_summary == "Newer summary."
    assert v.post_interview_ai_recommendation == "HOLD"
    assert v.post_interview_confidence == "MEDIUM"


# --- provenance ---------------------------------------------------


# --- screening transcript (wired to the real accessor) -----------


def test_transcript_absent_even_with_an_evaluation_present(db):
    """Proves the old ``evaluation is not None`` proxy is gone: a screening
    evaluation exists here, but zero real transcript rows do, so the field
    must now be False/0 rather than True (as the old proxy would report)."""
    s = _seed(db, with_transcript=False)
    v = _card(db, s)

    assert s["evaluation"] is not None               # evaluation DOES exist
    assert v.has_screening_transcript is False
    assert v.screening_question_count == 0
    assert v.screening_transcript == ()


def test_transcript_count_reflects_real_entries_not_criterion_rows(db):
    """The seeded transcript (2 entries) and the rubric criteria (6, via
    ``_CRITERIA``) are deliberately different counts — this proves the field
    now reads the real transcript, not ``len(criterion_rows)``."""
    s = _seed(db)
    v = _card(db, s)

    assert len(_CRITERIA) == 6                 # the old (wrong) source value
    assert v.screening_question_count == 2     # the real transcript count
    assert v.screening_question_count != len(_CRITERIA)
    assert v.has_screening_transcript is True


def test_transcript_entries_carry_real_question_and_answer_content(db):
    """Not fabricated or placeholder text — the actual seeded Q&A, verbatim,
    via the real ``get_screening_transcript`` accessor."""
    s = _seed(db)
    v = _card(db, s)

    texts = {e.question_text: e.answer_text for e in v.screening_transcript}
    assert texts["How long have you used Python?"] == (
        "About five years, mostly backend services."
    )
    assert texts["Can you describe your Kafka experience?"] is None
    answered_flags = {e.question_text: e.answered for e in v.screening_transcript}
    assert answered_flags["How long have you used Python?"] is True
    assert answered_flags["Can you describe your Kafka experience?"] is False


def test_transcript_entries_are_frozen_and_session_safe(db):
    s = _seed(db)
    v = _card(db, s)
    db.expunge_all()
    assert len(v.screening_transcript) == 2
    entry = v.screening_transcript[0]
    assert entry.question_text and entry.round == 1
    with pytest.raises(Exception):
        entry.answer_text = "mutated"


def test_other_evidence_sources_unaffected_by_transcript_fix(db):
    """Regression guard: every OTHER field on the view is unchanged in shape
    and content now that the transcript fields read real data. Mirrors the
    original assertions in ``test_assembles_every_evidence_source`` for the
    sources this fix did not touch."""
    s = _seed(db)
    v = _card(db, s)

    assert v.candidate_name == "Casey Candidate"
    assert v.resume_evidence["skills"] == ["Python", "Spark"]
    assert len(v.mandatory_criteria) == 2
    assert len(v.preferred_criteria) == 1
    assert v.requirements.score == 8
    assert v.experience.score == 7
    assert v.behavioral.score == 6
    assert v.ranking.rank_position == 2
    assert v.ranking.is_shortlisted is True
    assert v.interview_guide_question_count is not None
    assert v.interview_round == 1
    assert v.post_interview_summary == "AI consolidated summary."
    assert v.screening_ai_recommendation == "PROCEED"
    assert v.post_interview_ai_recommendation == "PROCEED"
    assert v.human_recommendation == "PROCEED"
    assert v.disagreement_status == DISAGREEMENT_NOT_ASSESSED
    assert v.final_decision_status == FINAL_DECISION_NOT_DECIDED
    assert v.has_multiple_rubric_versions is False
    assert v.missing_sources == ()


def test_every_displayed_group_carries_provenance(db):
    s = _seed(db)
    v = _card(db, s)

    assert v.overall_score_provenance == Provenance.RANKING
    for bucket in (v.requirements, v.experience, v.behavioral, v.interview):
        assert bucket.provenance == Provenance.SYSTEM_SCORE
    for c in v.mandatory_criteria + v.preferred_criteria + v.other_criteria:
        assert c.provenance in Provenance.ALL


def test_screening_and_post_interview_narratives_stay_separate(db):
    """AI prose from two different stages is never merged into one list."""
    s = _seed(db)
    v = _card(db, s)
    assert v.screening_strengths == ("Strong Python",)
    assert v.post_interview_strengths == ("AI strength.",)
    assert v.screening_gaps == ("No Kafka",)
    assert v.post_interview_gaps == ("AI gap.",)


def test_human_feedback_is_never_labeled_as_ai(db):
    s = _seed(db)
    v = _card(db, s)
    assert v.interview_notes == _NOTES
    assert v.interviewer_name == "HR Person"
    # The interviewer's words are not in any AI-provenanced field.
    for item in v.screening_strengths + v.post_interview_strengths:
        assert _NOTES not in item


# --- scores read as-is, never recalculated ------------------------


def test_bucket_scores_match_the_originating_row_exactly(db):
    """Structural: the service reinterprets nothing."""
    s = _seed(db)
    ev = s["evaluation"]
    v = _card(db, s)

    assert v.requirements.score == ev.requirements_score
    assert v.requirements.coverage == ev.requirements_coverage
    assert v.experience.score == ev.experience_score
    assert v.experience.coverage == ev.experience_coverage
    assert v.behavioral.score == ev.behavioral_score
    assert v.behavioral.coverage == ev.behavioral_coverage
    assert v.screening_confidence == ev.overall_confidence
    assert v.screening_ai_recommendation == ev.ai_recommendation


def test_overall_score_is_the_existing_ranking_score(db):
    s = _seed(db)
    v = _card(db, s)
    assert v.overall_score == 7.25
    assert v.overall_score_unavailable_reason is None


def test_interview_score_is_derived_from_the_recorded_ratings(db):
    s = _seed(db)   # ratings 4 and 3 -> mean 3.5 -> round(10*3.5/5) = 7
    v = _card(db, s)
    assert v.interview.score == 7
    # Order follows the feedback accessor's own competency-label ordering, so
    # assert on content rather than position.
    assert set(v.interview.evidence) == {
        "System design: 4/5 — Clear trade-offs.",
        "Communication: 3/5",
    }


def test_unknown_criteria_keep_unknown_and_do_not_become_fail(db):
    s = _seed(db, mandatory_result="UNKNOWN")
    v = _card(db, s)
    results = {c.result for c in v.mandatory_criteria}
    assert results == {"UNKNOWN"}
    assert "FAIL" not in results


def test_confidence_is_carried_not_blended(db):
    """Changing confidence must not move any score."""
    s = _seed(db)
    before = _card(db, s)
    s["evaluation"].overall_confidence = "LOW"
    s["analysis"].confidence = "LOW"
    db.flush()
    after = _card(db, s)

    assert after.screening_confidence == "LOW"
    assert after.post_interview_confidence == "LOW"
    assert after.requirements.score == before.requirements.score
    assert after.experience.score == before.experience.score
    assert after.behavioral.score == before.behavioral.score
    assert after.interview.score == before.interview.score
    assert after.overall_score == before.overall_score


# --- recommendations: carried, never compared ---------------------


def test_all_three_recommendations_are_carried_separately(db):
    s = _seed(db, human_recommendation="REJECT")
    v = _card(db, s)
    assert v.screening_ai_recommendation == "PROCEED"
    assert v.post_interview_ai_recommendation == "PROCEED"
    assert v.human_recommendation == "REJECT"     # unmodified, un-normalised


def test_human_reject_is_preserved_verbatim(db):
    """The AI path can never produce REJECT; a human can, and it is not
    normalised toward the AI's value."""
    s = _seed(db, human_recommendation="REJECT")
    v = _card(db, s)
    assert v.human_recommendation == "REJECT"
    assert v.post_interview_ai_recommendation != v.human_recommendation


def test_disagreement_field_is_a_fixed_string_not_a_computation(db):
    """Maximal divergence must still produce the same fixed placeholder."""
    agree = _card(db, _seed(db, human_recommendation="PROCEED"))
    diverge = _card(db, _seed(db, human_recommendation="REJECT"))
    assert agree.disagreement_status == DISAGREEMENT_NOT_ASSESSED
    assert diverge.disagreement_status == DISAGREEMENT_NOT_ASSESSED
    assert agree.disagreement_status == diverge.disagreement_status
    for word in ("YES", "NO", "TRUE", "FALSE"):
        assert word not in DISAGREEMENT_NOT_ASSESSED.upper().split()


def test_final_decision_field_is_a_fixed_string(db):
    s = _seed(db)
    v = _card(db, s)
    assert v.final_decision_status == FINAL_DECISION_NOT_DECIDED
    assert v.final_decision_status not in ("PROCEED", "HOLD", "REJECT")


def test_no_view_field_holds_a_comparison_verdict(db):
    s = _seed(db, human_recommendation="REJECT")
    v = _card(db, s)
    for field in v.__dataclass_fields__:
        assert "disagree" not in field.lower() or field == "disagreement_status"
        assert "agree" not in field.lower() or field == "disagreement_status"


# --- ranking: displayed, never recalculated -----------------------


def test_existing_ranking_is_displayed_with_its_rubric_version(db):
    s = _seed(db)
    v = _card(db, s)
    assert v.ranking.rank_position == 2
    assert v.ranking.overall_score == 7.25
    assert v.ranking.eligible is True
    assert v.ranking.mandatory_unknown_flag is False
    assert v.ranking.rubric_version_id == s["rubric"].id
    assert v.ranking.rank_position_at_decision == 2
    assert v.ranking.shortlist_reason == "Strong Spark evidence"


def test_ranking_is_not_recalculated_from_interview_evidence(db):
    """Adding a glowing interview must not move the pre-interview ranking."""
    s = _seed(db, with_feedback=False, with_analysis=False)
    before = _card(db, s)
    create_interview_feedback(
        db, user_id=s["hr"].id, application_id=s["app"].id,
        interview_guide_id=s["guide"].id, interview_round=1,
        recommendation="PROCEED", notes=_NOTES,
        ratings=[{"competency_label": "All", "rating": 5, "comment": None}],
    )
    after = _card(db, s)

    assert after.ranking.rank_position == before.ranking.rank_position
    assert after.ranking.overall_score == before.ranking.overall_score
    assert after.overall_score == before.overall_score


# --- rubric-version divergence ------------------------------------


def test_single_rubric_version_raises_no_warning(db):
    s = _seed(db)
    v = _card(db, s)
    assert v.has_multiple_rubric_versions is False
    assert {r.rubric_version_id for r in v.rubric_versions} == {s["rubric"].id}


def test_divergent_rubric_versions_are_flagged_and_both_preserved(db):
    """Prequalification against v1, everything else against v2 — the real
    divergence path. Neither version is picked, merged, or rewritten."""
    s = _seed(db)
    v1, _ = _rubric(
        db, s["job"], s["hr"], version_number=2,
        status=RubricVersionStatus.SUPERSEDED,
    )
    prequal = db.execute(
        select(PrequalificationResult).where(
            PrequalificationResult.application_id == s["app"].id
        )
    ).scalar_one()
    prequal.rubric_version_id = v1.id
    db.flush()

    v = _card(db, s)
    assert v.has_multiple_rubric_versions is True
    seen = {r.source_label: r.rubric_version_id for r in v.rubric_versions}
    assert seen["Prequalification"] == v1.id
    assert seen["Screening evaluation"] == s["rubric"].id
    assert MULTIPLE_RUBRIC_VERSIONS_WARNING


def test_divergence_does_not_rewrite_any_upstream_row(db):
    s = _seed(db)
    v1, _ = _rubric(
        db, s["job"], s["hr"], version_number=2,
        status=RubricVersionStatus.SUPERSEDED,
    )
    prequal = db.execute(
        select(PrequalificationResult).where(
            PrequalificationResult.application_id == s["app"].id
        )
    ).scalar_one()
    prequal.rubric_version_id = v1.id
    db.flush()
    before_results = list(prequal.results)

    _card(db, s)

    db.refresh(prequal)
    assert prequal.rubric_version_id == v1.id
    assert prequal.results == before_results


# --- missing sources ----------------------------------------------


def test_missing_later_stages_render_without_error(db):
    s = _seed(db, with_feedback=False, with_analysis=False)
    v = _card(db, s)

    assert v is not None
    assert v.interview_round is None
    assert v.post_interview_summary is None
    assert v.interview.score is None
    assert v.interview.unavailable_reason == "No interview feedback recorded yet"
    assert "Human interview feedback" in v.missing_sources
    assert "Post-interview AI analysis" in v.missing_sources
    # Earlier stages still render.
    assert v.requirements.score == 8


def test_missing_ranking_reports_itself(db):
    s = _seed(db, with_ranking=False)
    v = _card(db, s)
    assert v.overall_score is None
    assert v.overall_score_unavailable_reason
    assert "Pre-interview candidate ranking" in v.missing_sources


def test_feedback_without_ratings_gives_no_interview_score(db):
    s = _seed(db, ratings=[])
    v = _card(db, s)
    assert v.interview_round == 1
    assert v.interview.score is None
    assert v.interview.unavailable_reason == INSUFFICIENT_EVIDENCE
    assert v.interview_notes == _NOTES     # notes-only feedback still shows


def test_a_bare_application_still_produces_a_scorecard(db):
    s = _seed(db, with_feedback=False, with_analysis=False, with_ranking=False,
              with_shortlist=False, with_guide=False)
    v = _card(db, s)
    assert v is not None
    assert v.candidate_name == "Casey Candidate"
    assert len(v.missing_sources) >= 3


# --- authorization -------------------------------------------------


def test_unknown_actor_is_rejected(db):
    s = _seed(db)
    with pytest.raises(UnauthorizedError):
        get_final_scorecard(db, s["app"].id, acting_user_id=uuid.uuid4())


def test_system_actor_cannot_view_a_scorecard(db):
    s = _seed(db)
    with pytest.raises(FinalScorecardActorError) as exc:
        get_final_scorecard(db, s["app"].id, acting_user_id=SYSTEM_USER_ID)
    assert "signed-in HR user" in str(exc.value)
    assert "Casey Candidate" not in str(exc.value)


def test_hiring_manager_and_admin_may_view(db):
    s = _seed(db)
    for role in (UserRole.HIRING_MANAGER, UserRole.ADMIN):
        user = _hr(db, role=role)
        assert _card(db, s, user=user) is not None


def test_public_candidate_app_cannot_reach_the_scorecard():
    """Structural: no candidate-facing module imports this service."""
    for name in ("app/public_main.py", "app/utils/candidate_ui.py",
                 "app/services/candidate_portal_service.py"):
        src = pathlib.Path(name).read_text(encoding="utf-8")
        assert "final_scorecard" not in src


# --- structural guarantees ----------------------------------------


def _service_ast():
    return ast.parse(
        pathlib.Path("app/services/final_scorecard_service.py").read_text(
            encoding="utf-8"
        )
    )


def test_service_performs_no_writes_and_no_audit():
    """No commit, no add, no delete, no audit event, no status change."""
    src = pathlib.Path(
        "app/services/final_scorecard_service.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "db.add(", "db.commit(", "db.delete(", "db.flush(",
        "record_event", "AuditEventType", ".status =", "ApplicationStatus",
    ):
        assert forbidden not in src, forbidden


def test_service_makes_no_ai_call():
    src = pathlib.Path(
        "app/services/final_scorecard_service.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("get_structured_response", "claude_client", "build_",
                      "Assessment"):
        assert forbidden not in src, forbidden


def test_service_contains_no_recommendation_comparison():
    """Mirrors Step 9's equivalent guard: the two recommendation values are
    never compared anywhere in this code path."""
    tree = _service_ast()
    rec_names = {
        "screening_ai_recommendation", "post_interview_ai_recommendation",
        "human_recommendation", "ai_recommendation", "recommendation",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            names = {
                n.attr if isinstance(n, ast.Attribute) else
                (n.id if isinstance(n, ast.Name) else "")
                for n in ast.walk(node)
            }
            assert len(names & rec_names) < 2, ast.dump(node)


def test_service_defines_no_disagreement_computation():
    tree = _service_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assert "disagree" not in node.name.lower()
            assert "compare" not in node.name.lower()


def test_only_one_score_is_derived_here():
    """compute_interview_score is the sole derivation; everything else is read
    verbatim. Guards against a scoring methodology creeping in."""
    tree = _service_ast()
    computing = [
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("compute_")
    ]
    assert computing == ["compute_interview_score"]


def test_upstream_rows_are_untouched_by_assembly(db):
    """Historical semantics: reading a scorecard mutates nothing upstream."""
    s = _seed(db)
    ev, fb, an = s["evaluation"], s["feedback"], s["analysis"]
    snapshot = (
        ev.requirements_score, ev.ai_recommendation, ev.overall_confidence,
        fb.recommendation, fb.notes, fb.interview_round,
        an.status, an.ai_recommendation, an.summary,
    )
    status_before = s["app"].status

    _card(db, s)
    _card(db, s)

    db.refresh(ev); db.refresh(fb); db.refresh(an); db.refresh(s["app"])
    assert (
        ev.requirements_score, ev.ai_recommendation, ev.overall_confidence,
        fb.recommendation, fb.notes, fb.interview_round,
        an.status, an.ai_recommendation, an.summary,
    ) == snapshot
    assert s["app"].status == status_before


def test_no_audit_event_is_emitted_by_viewing(db):
    from app.database.models.audit_event import AuditEvent
    s = _seed(db)
    before = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == s["app"].id)
    ).scalars().all()
    _card(db, s)
    after = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == s["app"].id)
    ).scalars().all()
    assert len(after) == len(before)


def test_view_is_frozen_and_session_safe(db):
    s = _seed(db)
    v = _card(db, s)
    db.expunge_all()
    assert v.candidate_name and v.requirements.label
    with pytest.raises(Exception):
        v.overall_score = 99.0
