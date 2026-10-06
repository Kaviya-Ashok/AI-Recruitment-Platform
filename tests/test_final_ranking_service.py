"""Tests for app.services.final_ranking_service (Step 10b).

No AI anywhere in this step — nothing to mock at the Claude boundary. Real
Postgres via the savepoint-rollback ``db`` fixture. Every candidate here is built
directly with the ORM (application, screening evaluation, Step 5 ranking row,
shortlist entry, guide, feedback, optional analysis) so each test controls exactly
the inputs it cares about. Every query is scoped to rows this test created.
"""

from __future__ import annotations

import ast
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, inspect as sa_inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.final_ranking import (
    FinalRanking,
    FinalRankingEntry,
    FinalRankingEntryStatus,
    FinalRankingStatus,
)
from app.database.models.interview_guide import InterviewGuide
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysis,
    PostInterviewAnalysisStatus,
)
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
from app.services import final_ranking_service as frs
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.final_ranking_service import (
    FinalRankingActorError,
    FinalRankingTargetNotFoundError,
    generate_final_ranking,
    get_current_final_ranking,
    get_final_ranking_for_application,
    get_final_ranking_staleness,
    list_final_ranking_history,
)
from app.services.interview_feedback_service import create_interview_feedback
from app.services.job_service import create_job
from app.utils.authorization import UnauthorizedError

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
_NOTES = "A reasonably long set of interviewer notes about the conversation."
_SENT_NAME = "Zyxwvuts Sentinelcandidate"
_SENT_NOTES = "NOTESENTINEL_R7 " + _NOTES
D = Decimal


# --- seed helpers -----------------------------------------------------------


def _hr(db, name="Dana HR"):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name=name, role=UserRole.HR,
    )


def _rubric(db, job, hr, number=1):
    rv = RubricVersion(
        job_id=job.id, version_number=number, status=RubricVersionStatus.APPROVED,
        generated_from_requirements_version=1, created_by=hr.id,
    )
    db.add(rv)
    db.flush()
    db.add(RubricCriterion(
        rubric_version_id=rv.id, requirement_type="MANDATORY", category=None,
        criterion_text="5+ years Python", display_order=1,
    ))
    db.flush()
    return rv


def _job(db):
    hr = _hr(db)
    job = create_job(
        db, title="Backend Engineer", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
        created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    rv = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    return {"hr": hr, "job": job, "rubric": rv, "link": link, "n": 0}


def _candidate(
    db, w, *, name=None, rounds=None, overall=7.0, eligible=True,
    mandatory_unknown=False, with_ranking=True, with_analysis=True,
    analysis_confidence="HIGH", analysis_rubric=None, rubric=None,
    screening_confidence="HIGH", notes=_NOTES, minutes=0, batch=None,
):
    """One interviewed candidate. ``rounds`` is ``{round: [ratings]}`` (an empty
    list is a notes-only round); ``rounds=None`` means NO interview feedback."""
    w["n"] += 1
    rubric = rubric or w["rubric"]
    app = create_application(
        db, job_id=w["job"].id, application_link_id=w["link"].id,
        email=f"c-{uuid.uuid4().hex}@x.com",
        full_name=name or f"Candidate {w['n']}", phone=None,
    )
    app.status = ApplicationStatus.SCREENING_EVALUATED
    app.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    db.flush()

    session = ScreeningSession(
        application_id=app.id, access_token=f"tok-{uuid.uuid4().hex}",
        status=ScreeningSessionStatus.SCREENING_COMPLETE,
    )
    db.add(session)
    db.flush()
    db.add(ScreeningEvaluation(
        screening_session_id=session.id, rubric_version_id=rubric.id,
        results=[], requirements_score=8, requirements_coverage=1.0,
        experience_score=7, experience_coverage=1.0, behavioral_score=6,
        behavioral_coverage=0.5, strengths=[], gaps=[], unknowns=[],
        overall_confidence=screening_confidence, ai_recommendation="PROCEED",
        ai_model="claude-sonnet-5",
    ))
    db.flush()

    ranking = None
    if with_ranking:
        ranking = CandidateRanking(
            job_id=w["job"].id, rubric_version_id=rubric.id, application_id=app.id,
            rank_position=1 if eligible else None, overall_score=overall,
            eligible=eligible, mandatory_unknown_flag=mandatory_unknown,
            generated_at=datetime.now(timezone.utc),
            generation_batch_id=batch or uuid.uuid4(),
        )
        db.add(ranking)
        db.flush()

    guide = feedback = None
    if rounds is not None:
        entry = CandidateShortlistEntry(
            job_id=w["job"].id, application_id=app.id, rubric_version_id=rubric.id,
            is_shortlisted=True, reason="r", rank_position_at_decision=1,
            decided_by_user_id=w["hr"].id, decided_at=datetime.now(timezone.utc),
        )
        db.add(entry)
        db.flush()
        guide = InterviewGuide(
            job_id=w["job"].id, application_id=app.id, shortlist_entry_id=entry.id,
            rubric_version_id=rubric.id, ai_model="claude-sonnet-5",
            generated_at=datetime.now(timezone.utc),
        )
        db.add(guide)
        db.flush()
        for number, ratings in sorted(rounds.items()):
            feedback = create_interview_feedback(
                db, user_id=w["hr"].id, application_id=app.id,
                interview_guide_id=guide.id, interview_round=number,
                recommendation="PROCEED", notes=notes,
                ratings=[
                    {"competency_label": f"Skill {i}", "rating": r, "comment": None}
                    for i, r in enumerate(ratings)
                ],
            )

    analysis = None
    if with_analysis and feedback is not None:
        analysis = _analysis(
            db, w, app, guide, feedback,
            confidence=analysis_confidence, rubric=analysis_rubric or rubric,
        )
    return {"app": app, "ranking": ranking, "guide": guide, "analysis": analysis,
            "feedback": feedback, "session": session, "w": w}


def _analysis(db, w, app, guide, feedback, *, confidence="HIGH", rubric=None,
              recommendation="PROCEED"):
    row = PostInterviewAnalysis(
        application_id=app.id, interview_feedback_id=feedback.id,
        interview_guide_id=guide.id, rubric_version_id=(rubric or w["rubric"]).id,
        requested_by_user_id=w["hr"].id, summary="AI summary.", strengths=[],
        gaps=[], unknowns=[], evidence_consistency_notes="n",
        confidence=confidence, ai_recommendation=recommendation,
        human_recommendation_snapshot="PROCEED",
        analyzed_only_latest_feedback=False, ai_model="claude-sonnet-5",
        status=PostInterviewAnalysisStatus.CURRENT,
    )
    db.add(row)
    db.flush()
    return row


def _generate(db, w, **kw):
    return generate_final_ranking(
        db, job_id=w["job"].id, requested_by_user_id=kw.get("user", w["hr"]).id
    )


def _entry(db, run, cand):
    return db.execute(
        select(FinalRankingEntry).where(
            FinalRankingEntry.final_ranking_id == run.id,
            FinalRankingEntry.application_id == cand["app"].id,
        )
    ).scalar_one()


def _runs(db, w):
    return db.execute(
        select(FinalRanking).where(FinalRanking.job_id == w["job"].id)
        .order_by(FinalRanking.created_at, FinalRanking.id)
    ).scalars().all()


def _events(db, w):
    return db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == w["job"].id,
            AuditEvent.event_type == AuditEventType.FINAL_RANKING_GENERATED.value,
        )
    ).scalars().all()


# =====================================================================
# Generation, numbers, and the breakdown
# =====================================================================


def test_generation_scores_ranks_and_stores_the_breakdown(db):
    w = _job(db)
    a = _candidate(db, w, rounds={1: [4, 3]}, overall=7.25)      # int 7.00
    b = _candidate(db, w, rounds={1: [5, 5]}, overall=8.00)      # int 10.00
    (run,) = _generate(db, w)

    ea, eb = _entry(db, run, a), _entry(db, run, b)
    # 0.4 x 7.25 + 0.6 x 7.00 = 2.90 + 4.20 = 7.10
    assert (ea.screening_score, ea.interview_score, ea.final_score) == (
        D("7.25"), D("7.00"), D("7.10"))
    # 0.4 x 8.00 + 0.6 x 10.00 = 3.20 + 6.00 = 9.20
    assert (eb.screening_score, eb.interview_score, eb.final_score) == (
        D("8.00"), D("10.00"), D("9.20"))
    assert (eb.rank, ea.rank) == (1, 2)
    assert ea.entry_status == FinalRankingEntryStatus.RANKED
    assert ea.rounds_used == [1] and ea.round_means == {"1": "3.50"}
    assert ea.screening_rank == 1 and ea.screening_confidence == "HIGH"
    assert len(ea.feedback_ids) == 1


def test_weights_are_stored_on_the_run(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [3]})
    (run,) = _generate(db, w)
    assert run.screening_weight == D("0.4000")
    assert run.interview_weight == D("0.6000")
    assert run.status == FinalRankingStatus.CURRENT and run.superseded_at is None


def test_all_rounds_count_equally_in_the_stored_score(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [5] * 8, 2: [1, 1]}, overall=5.0)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.interview_score == D("6.00")             # mean(5, 1) x 2, not pooled
    assert e.rounds_used == [1, 2]
    assert e.round_means == {"1": "5.00", "2": "1.00"}
    assert e.final_score == D("5.60")                  # 0.4 x 5 + 0.6 x 6


def test_a_notes_only_round_is_excluded_listed_and_not_zero(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4, 4], 2: []}, overall=6.0)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.interview_score == D("8.00")              # round 2 not averaged in
    assert e.rounds_used == [1]
    assert "Not scored (no ratings): round 2" in e.status_reason
    assert e.entry_status == FinalRankingEntryStatus.RANKED


def test_candidates_without_feedback_are_not_in_the_run(db):
    w = _job(db)
    interviewed = _candidate(db, w, rounds={1: [4]})
    not_interviewed = _candidate(db, w, rounds=None)
    (run,) = _generate(db, w)
    ids = {e.application_id for e in db.execute(
        select(FinalRankingEntry).where(FinalRankingEntry.final_ranking_id == run.id)
    ).scalars()}
    assert ids == {interviewed["app"].id}
    assert not_interviewed["app"].id not in ids


def test_shortlist_status_is_not_required(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    db.execute(text("UPDATE candidate_shortlist_entries SET is_shortlisted = false "
                    "WHERE application_id = :a"), {"a": c["app"].id})
    (run,) = _generate(db, w)
    assert _entry(db, run, c).entry_status == FinalRankingEntryStatus.RANKED


def test_nothing_to_rank_is_a_clear_error(db):
    w = _job(db)
    _candidate(db, w, rounds=None)
    with pytest.raises(FinalRankingTargetNotFoundError, match="interview feedback"):
        _generate(db, w)
    assert _runs(db, w) == []


def test_unknown_job_is_a_clear_error(db):
    hr = _hr(db)
    with pytest.raises(FinalRankingTargetNotFoundError, match="No such job"):
        generate_final_ranking(db, job_id=uuid.uuid4(), requested_by_user_id=hr.id)


# =====================================================================
# Statuses: incomplete, ineligible, unknown stays unknown
# =====================================================================


def test_no_screening_ranking_row_is_incomplete_screening(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, with_ranking=False)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.entry_status == FinalRankingEntryStatus.INCOMPLETE_SCREENING
    assert "Generate the screening ranking first" in e.status_reason
    assert "Ranking & Shortlist" in e.status_reason
    assert e.final_score is None and e.rank is None and e.final_confidence is None
    assert e.screening_score is None
    # the interview score is still reported; nothing is redistributed or zeroed
    assert e.interview_score == D("8.00")


def test_null_screening_score_is_incomplete_screening(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, overall=None)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.entry_status == FinalRankingEntryStatus.INCOMPLETE_SCREENING
    assert e.final_score is None and e.rank is None
    assert "No screening score" in e.status_reason


def test_no_rated_round_is_incomplete_interview(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [], 2: []}, overall=9.0)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.entry_status == FinalRankingEntryStatus.INCOMPLETE_INTERVIEW
    assert e.interview_score is None and e.final_score is None and e.rank is None
    assert e.screening_score == D("9.00")          # present, shown, never "scored 0"
    assert "Rounds with notes only are not scored" in e.status_reason
    assert e.rounds_used == []


def test_screening_incomplete_takes_precedence_over_interview_incomplete(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: []}, overall=None)
    (run,) = _generate(db, w)
    assert _entry(db, run, c).entry_status == FinalRankingEntryStatus.INCOMPLETE_SCREENING


def test_ineligible_candidate_has_a_score_but_no_rank_and_is_not_above_ranked(db):
    w = _job(db)
    ok = _candidate(db, w, rounds={1: [3]}, overall=5.0)
    top = _candidate(db, w, rounds={1: [5]}, overall=10.0, eligible=False)
    (run,) = _generate(db, w)

    et, eo = _entry(db, run, top), _entry(db, run, ok)
    assert et.entry_status == FinalRankingEntryStatus.NOT_RANKED_INELIGIBLE
    assert et.final_score == D("10.00") and et.rank is None
    assert et.eligible is False                      # read as stored, not recomputed
    assert "not a rejection" in et.status_reason
    assert eo.rank == 1                               # the ineligible did not take #1

    view = get_current_final_ranking(db, job_id=w["job"].id, acting_user_id=w["hr"].id)[0]
    order = [e.application_id for e in view.entries]
    assert order.index(ok["app"].id) < order.index(top["app"].id)


def test_eligibility_is_read_as_stored_never_recomputed(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, eligible=True)
    # the evaluation says a mandatory FAIL, but Step 5's stored flag wins
    ev = db.execute(select(ScreeningEvaluation).where(
        ScreeningEvaluation.screening_session_id == c["session"].id)).scalar_one()
    ev.results = [{"requirement_type": "MANDATORY", "result": "FAIL"}]
    db.flush()
    (run,) = _generate(db, w)
    assert _entry(db, run, c).entry_status == FinalRankingEntryStatus.RANKED


def test_mandatory_unknown_is_flagged_and_does_not_exclude(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, mandatory_unknown=True)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.mandatory_unknown is True
    assert e.entry_status == FinalRankingEntryStatus.RANKED and e.rank == 1


# =====================================================================
# Ties
# =====================================================================


def test_equal_final_scores_share_a_rank_and_the_next_skips(db):
    w = _job(db)
    first = _candidate(db, w, rounds={1: [5]}, overall=10.0)       # 10.00
    t1 = _candidate(db, w, rounds={1: [3]}, overall=5.0, minutes=1)  # 2.0+3.6=...
    t2 = _candidate(db, w, rounds={1: [3]}, overall=5.0, minutes=2)
    last = _candidate(db, w, rounds={1: [1]}, overall=2.0)
    (run,) = _generate(db, w)
    ranks = {n: _entry(db, run, c).rank for n, c in
             (("first", first), ("t1", t1), ("t2", t2), ("last", last))}
    assert ranks == {"first": 1, "t1": 2, "t2": 2, "last": 4}
    view = get_current_final_ranking(db, job_id=w["job"].id, acting_user_id=w["hr"].id)[0]
    tied = {e.application_id for e in view.entries if e.tied}
    assert tied == {t1["app"].id, t2["app"].id}


def test_order_inside_a_tie_is_stable_screening_then_application_date(db):
    w = _job(db)
    # same final 6.00: (screening 6, interview 6) vs (screening 3, interview 8)
    low_screen = _candidate(db, w, rounds={1: [4]}, overall=3.0, minutes=1)   # 1.2+4.8=6.00
    high_screen = _candidate(db, w, rounds={1: [3]}, overall=6.0, minutes=9)  # 2.4+3.6=6.00
    (run,) = _generate(db, w)
    assert _entry(db, run, low_screen).final_score == D("6.00")
    assert _entry(db, run, high_screen).final_score == D("6.00")
    view = get_current_final_ranking(db, job_id=w["job"].id, acting_user_id=w["hr"].id)[0]
    assert [e.application_id for e in view.entries] == [
        high_screen["app"].id, low_screen["app"].id,
    ]
    assert {e.rank for e in view.entries} == {1}


# =====================================================================
# Rubric-version partitioning
# =====================================================================


def test_each_rubric_version_is_its_own_run_and_never_merged(db):
    w = _job(db)
    v2 = _rubric(db, w["job"], w["hr"], number=2)
    a = _candidate(db, w, rounds={1: [3]}, overall=5.0)
    b = _candidate(db, w, rounds={1: [5]}, overall=10.0, rubric=v2)
    runs = _generate(db, w)

    assert {r.rubric_version_id for r in runs} == {w["rubric"].id, v2.id}
    ea = _entry(db, next(r for r in runs if r.rubric_version_id == w["rubric"].id), a)
    eb = _entry(db, next(r for r in runs if r.rubric_version_id == v2.id), b)
    assert ea.rank == 1 and eb.rank == 1               # each is #1 of ITS version
    assert len(_events(db, w)) == 2


def test_an_analysis_on_another_rubric_version_does_not_count(db):
    w = _job(db)
    v2 = _rubric(db, w["job"], w["hr"], number=2)
    c = _candidate(db, w, rounds={1: [4]}, analysis_confidence="LOW",
                   analysis_rubric=v2)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.post_interview_analysis_id is None
    # treated as ABSENT: capped at MEDIUM (not dragged down to the analysis's LOW)
    assert e.final_confidence == "MEDIUM"
    assert "different rubric version" in e.status_reason


# =====================================================================
# Confidence
# =====================================================================


def test_confidence_is_the_lowest_of_screening_and_analysis(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, screening_confidence="HIGH",
                   analysis_confidence="LOW")
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.final_confidence == "LOW"
    assert e.post_interview_analysis_id == c["analysis"].id


def test_without_an_analysis_confidence_is_capped_at_medium_and_says_so(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, screening_confidence="HIGH",
                   with_analysis=False)
    (run,) = _generate(db, w)
    e = _entry(db, run, c)
    assert e.final_confidence == "MEDIUM"
    assert e.post_interview_analysis_id is None
    assert "capped at MEDIUM" in e.status_reason


def test_confidence_never_changes_the_score(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, analysis_confidence="HIGH")
    (run1,) = _generate(db, w)
    before = _entry(db, run1, c).final_score
    c["analysis"].confidence = "LOW"
    db.flush()
    (run2,) = _generate(db, w)
    after = _entry(db, run2, c)
    assert after.final_score == before and after.final_confidence == "LOW"


# =====================================================================
# The AI recommendation / human recommendation never enter the score
# =====================================================================


def test_recommendations_do_not_move_any_number_or_trigger_anything(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]}, overall=6.0)
    (run1,) = _generate(db, w)
    base = _entry(db, run1, c)
    base_vals = (base.final_score, base.rank, base.entry_status)

    c["analysis"].ai_recommendation = "HOLD"
    db.execute(text("UPDATE interview_feedback SET recommendation = 'REJECT' "
                    "WHERE application_id = :a"), {"a": c["app"].id})
    status_before = db.get(Application, c["app"].id).status
    (run2,) = _generate(db, w)
    again = _entry(db, run2, c)
    assert (again.final_score, again.rank, again.entry_status) == base_vals
    assert db.get(Application, c["app"].id).status == status_before


def test_generation_never_changes_application_status(db):
    w = _job(db)
    cands = [_candidate(db, w, rounds={1: [r]}, eligible=(r != 1)) for r in (1, 3, 5)]
    before = {c["app"].id: db.get(Application, c["app"].id).status for c in cands}
    _generate(db, w)
    after = {c["app"].id: db.get(Application, c["app"].id).status for c in cands}
    assert before == after


# =====================================================================
# Supersession (non-destructive)
# =====================================================================


def test_a_new_run_supersedes_the_old_one_and_keeps_it(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    (first,) = _generate(db, w)
    (second,) = _generate(db, w)

    runs = {r.id: r for r in _runs(db, w)}
    assert len(runs) == 2                              # nothing deleted
    assert runs[first.id].status == FinalRankingStatus.SUPERSEDED
    assert runs[first.id].superseded_at is not None
    assert runs[second.id].status == FinalRankingStatus.CURRENT
    assert runs[second.id].superseded_at is None
    # the old run's entries are intact
    assert _entry(db, first, c).final_score == _entry(db, second, c).final_score
    assert _events(db, w)[-1].new_state["superseded_run_id"] in {str(first.id), None}


def test_exactly_one_current_run_per_partition_after_repeated_generation(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    for _ in range(3):
        _generate(db, w)
    current = [r for r in _runs(db, w) if r.status == FinalRankingStatus.CURRENT]
    assert len(current) == 1 and len(_runs(db, w)) == 3


def test_history_lists_every_run_newest_first(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    (first,) = _generate(db, w)
    first.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.flush()
    (second,) = _generate(db, w)
    history = list_final_ranking_history(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert [h.final_ranking_id for h in history] == [second.id, first.id]
    assert [h.status for h in history] == ["CURRENT", "SUPERSEDED"]


def test_step_5_can_still_regenerate_after_a_final_ranking_exists(db):
    """The reason there is NO foreign key into candidate_rankings: Step 5 deletes
    and re-inserts its rows, and must keep working."""
    from app.services.ranking_service import generate_ranking

    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    rows = generate_ranking(
        db, job_id=w["job"].id, rubric_version_id=w["rubric"].id,
        requested_by_user_id=w["hr"].id,
    )
    assert rows                                          # did not raise IntegrityError
    assert c["app"].id in {r.application_id for r in rows}


# =====================================================================
# Reads
# =====================================================================


def test_current_ranking_view_is_ordered_ranked_then_ineligible_then_incomplete(db):
    w = _job(db)
    inc = _candidate(db, w, rounds={1: []})
    inel = _candidate(db, w, rounds={1: [5]}, overall=10.0, eligible=False)
    ok = _candidate(db, w, rounds={1: [3]})
    _generate(db, w)
    (view,) = get_current_final_ranking(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert [e.application_id for e in view.entries] == [
        ok["app"].id, inel["app"].id, inc["app"].id,
    ]
    assert view.screening_weight == D("0.4000") and view.rubric_version_number == 1


def test_application_view_returns_its_entry_and_ranked_count(db):
    w = _job(db)
    a = _candidate(db, w, rounds={1: [4]}, overall=7.0)
    _candidate(db, w, rounds={1: [3]}, overall=6.0)
    assert get_final_ranking_for_application(
        db, a["app"].id, acting_user_id=w["hr"].id) is None
    _generate(db, w)
    got = get_final_ranking_for_application(db, a["app"].id, acting_user_id=w["hr"].id)
    assert got.entry.rank == 1 and got.ranked_count == 2
    assert got.screening_weight == D("0.4000") and got.interview_weight == D("0.6000")


def test_views_expose_display_names_but_runs_never_store_them(db):
    w = _job(db)
    c = _candidate(db, w, name=_SENT_NAME, rounds={1: [4]})
    _generate(db, w)
    (view,) = get_current_final_ranking(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert view.entries[0].candidate_name == _SENT_NAME
    cols = {c_.name for c_ in FinalRankingEntry.__table__.columns}
    assert not any("name" in n or "email" in n for n in cols)


# =====================================================================
# Staleness
# =====================================================================


def test_a_fresh_run_is_not_stale_and_no_run_is_not_stale(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    assert not get_final_ranking_staleness(
        db, job_id=w["job"].id, acting_user_id=w["hr"].id).is_stale
    _generate(db, w)
    st = get_final_ranking_staleness(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert st.is_stale is False and st.reasons == ()


def test_new_feedback_makes_it_stale(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    create_interview_feedback(
        db, user_id=w["hr"].id, application_id=c["app"].id,
        interview_guide_id=c["guide"].id, interview_round=2,
        recommendation="HOLD", notes=_NOTES, ratings=[],
    )
    st = get_final_ranking_staleness(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert st.is_stale and st.changed_feedback_count == 1
    assert "new interview feedback" in " ".join(st.reasons)


def test_a_new_analysis_makes_it_stale(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    c["analysis"].status = PostInterviewAnalysisStatus.SUPERSEDED
    c["analysis"].superseded_at = datetime.now(timezone.utc)
    _analysis(db, w, c["app"], c["guide"], c["feedback"], confidence="MEDIUM")
    st = get_final_ranking_staleness(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert st.is_stale and st.changed_analysis_count == 1


def test_a_new_screening_ranking_makes_it_stale(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    c["ranking"].generation_batch_id = uuid.uuid4()       # Step 5 regenerated
    db.flush()
    st = get_final_ranking_staleness(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert st.is_stale and st.changed_screening_count == 1


def test_a_newly_interviewed_candidate_makes_it_stale(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    _candidate(db, w, rounds={1: [3]})
    st = get_final_ranking_staleness(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert st.is_stale and st.new_candidate_count == 1


def test_regenerating_clears_staleness(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    _candidate(db, w, rounds={1: [3]})
    assert get_final_ranking_staleness(
        db, job_id=w["job"].id, acting_user_id=w["hr"].id).is_stale
    _generate(db, w)
    assert not get_final_ranking_staleness(
        db, job_id=w["job"].id, acting_user_id=w["hr"].id).is_stale


def test_checking_staleness_never_regenerates(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    _candidate(db, w, rounds={1: [3]})
    get_final_ranking_staleness(db, job_id=w["job"].id, acting_user_id=w["hr"].id)
    assert len(_runs(db, w)) == 1


def test_nothing_triggers_a_run_automatically(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    create_interview_feedback(
        db, user_id=w["hr"].id, application_id=c["app"].id,
        interview_guide_id=c["guide"].id, interview_round=2,
        recommendation="HOLD", notes=_NOTES, ratings=[],
    )
    assert _runs(db, w) == []


# =====================================================================
# Authorization
# =====================================================================


def test_system_actor_is_rejected(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    with pytest.raises(FinalRankingActorError):
        generate_final_ranking(
            db, job_id=w["job"].id, requested_by_user_id=SYSTEM_USER_ID
        )
    assert _runs(db, w) == [] and _events(db, w) == []


@pytest.mark.parametrize("who", [None, "unknown", "malformed"])
def test_unknown_or_missing_user_is_rejected(db, who):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    actor = {"unknown": uuid.uuid4(), "malformed": "not-a-uuid", None: None}[who]
    with pytest.raises(UnauthorizedError):
        generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=actor)
    assert _runs(db, w) == []


def test_inactive_user_is_rejected(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    w["hr"].is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _generate(db, w)


def test_every_accessor_is_guarded(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    _generate(db, w)
    nobody = uuid.uuid4()
    with pytest.raises(UnauthorizedError):
        get_current_final_ranking(db, job_id=w["job"].id, acting_user_id=nobody)
    with pytest.raises(UnauthorizedError):
        list_final_ranking_history(db, job_id=w["job"].id, acting_user_id=None)
    with pytest.raises(UnauthorizedError):
        get_final_ranking_for_application(db, c["app"].id, acting_user_id=nobody)
    with pytest.raises(UnauthorizedError):
        get_final_ranking_staleness(db, job_id=w["job"].id, acting_user_id=nobody)


# =====================================================================
# Audit
# =====================================================================


def test_exactly_one_audit_event_per_run_with_structural_fields(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    _candidate(db, w, rounds={1: [5]}, eligible=False)
    _candidate(db, w, rounds={1: []})
    (first,) = _generate(db, w)
    (event,) = _events(db, w)
    meta = event.new_state
    assert event.user_id == w["hr"].id and event.entity_type == "job"
    assert meta == {
        "job_id": str(w["job"].id),
        "final_ranking_id": str(first.id),
        "rubric_version_id": str(w["rubric"].id),
        "screening_weight": "0.40",
        "interview_weight": "0.60",
        "candidate_count": 3,
        "ranked_count": 1,
        "ineligible_count": 1,
        "incomplete_count": 1,
        "superseded_run_id": None,
    }
    (second,) = _generate(db, w)
    assert len(_events(db, w)) == 2
    newest = next(e for e in _events(db, w) if e.new_state["final_ranking_id"] == str(second.id))
    assert newest.new_state["superseded_run_id"] == str(first.id)


def test_no_name_or_text_reaches_the_audit_trail_logs_or_tables(db, caplog):
    w = _job(db)
    c = _candidate(db, w, name=_SENT_NAME, rounds={1: [4]}, notes=_SENT_NOTES)
    with caplog.at_level(logging.DEBUG):
        (run,) = _generate(db, w)

    sentinels = ("Zyxwvuts", "Sentinelcandidate", "NOTESENTINEL_R7")
    blob = "".join(
        f"{e.action}{e.entity_type}{e.previous_state}{e.new_state}{e.event_metadata}"
        for e in db.execute(select(AuditEvent).where(
            AuditEvent.entity_id == w["job"].id)).scalars()
    )
    stored = "".join(
        str(v) for v in vars(_entry(db, run, c)).values()
    ) + str(vars(run))
    for s in sentinels:
        assert s not in blob, ("audit", s)
        assert s not in caplog.text, ("log", s)
        assert s not in stored, ("stored", s)


def test_no_audit_event_for_a_failed_run(db):
    w = _job(db)
    _candidate(db, w, rounds=None)
    with pytest.raises(FinalRankingTargetNotFoundError):
        _generate(db, w)
    assert _events(db, w) == []


# =====================================================================
# Table constraints
# =====================================================================


def test_two_current_runs_for_one_partition_fail_in_the_database(db):
    w = _job(db)
    kw = dict(job_id=w["job"].id, rubric_version_id=w["rubric"].id,
              requested_by_user_id=w["hr"].id, screening_weight=D("0.4"),
              interview_weight=D("0.6"))
    db.add(FinalRanking(status="CURRENT", **kw))
    db.flush()
    db.add(FinalRanking(status="CURRENT", **kw))
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.flush()


def test_many_superseded_runs_may_coexist_with_one_current(db):
    w = _job(db)
    kw = dict(job_id=w["job"].id, rubric_version_id=w["rubric"].id,
              requested_by_user_id=w["hr"].id, screening_weight=D("0.4"),
              interview_weight=D("0.6"))
    for _ in range(3):
        db.add(FinalRanking(status="SUPERSEDED", **kw))
    db.add(FinalRanking(status="CURRENT", **kw))
    db.flush()


def test_an_entry_is_unique_per_run_and_application(db):
    w = _job(db)
    c = _candidate(db, w, rounds={1: [4]})
    (run,) = _generate(db, w)
    db.add(FinalRankingEntry(
        final_ranking_id=run.id, application_id=c["app"].id, eligible=True,
        entry_status="RANKED",
    ))
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.flush()


def test_foreign_keys_are_restrict_and_none_points_at_step_5_or_evaluations(db):
    inspector = sa_inspect(db.get_bind())
    for table in ("final_rankings", "final_ranking_entries"):
        for fk in inspector.get_foreign_keys(table):
            assert fk["options"].get("ondelete") == "RESTRICT", (table, fk)
    targets = {
        fk["referred_table"] for fk in inspector.get_foreign_keys("final_ranking_entries")
    }
    assert targets == {"final_rankings", "applications", "post_interview_analyses"}
    assert "candidate_rankings" not in targets and "screening_evaluations" not in targets


def test_the_run_cannot_be_deleted_while_entries_point_at_it(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    (run,) = _generate(db, w)
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("DELETE FROM final_rankings WHERE id = :i"), {"i": run.id})


def test_status_vocabularies_are_validated_strings_not_enums():
    assert FinalRankingStatus.ALL == {"CURRENT", "SUPERSEDED"}
    assert FinalRankingEntryStatus.ALL == {
        "RANKED", "NOT_RANKED_INELIGIBLE", "INCOMPLETE_SCREENING",
        "INCOMPLETE_INTERVIEW",
    }
    assert FinalRanking.__table__.c.status.type.__class__.__name__ == "String"
    assert FinalRankingEntry.__table__.c.entry_status.type.__class__.__name__ == "String"


# =====================================================================
# Structural
# =====================================================================


def _src(name):
    return (_APP_DIR / "services" / name).read_text(encoding="utf-8")


def test_nothing_in_the_ranking_path_imports_the_ai_layer():
    for name in ("final_ranking_service.py", "final_scoring.py"):
        tree = ast.parse(_src(name))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = " ".join(a.name for a in node.names)
                assert "app.ai" not in mod and "app.ai" not in names, name
                assert "claude_client" not in mod + names, name


def test_nothing_under_app_ai_imports_the_final_ranking_modules():
    for p in (_APP_DIR / "ai").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert "final_ranking" not in src and "final_scoring" not in src, p.name


def test_no_disagreement_logic_and_the_audit_member_stays_unemitted():
    for name in ("final_ranking_service.py", "final_scoring.py"):
        src = _src(name)
        assert "AI_HUMAN_DISAGREEMENT_DETECTED" not in src
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert "disagree" not in node.name.lower()
    emitters = [
        p.name for p in _APP_DIR.rglob("*.py")
        if "AuditEventType.AI_HUMAN_DISAGREEMENT_DETECTED" in p.read_text(encoding="utf-8")
    ]
    assert emitters == []


def test_the_service_never_writes_application_status_or_recommendations():
    src = _src("final_ranking_service.py")
    assert "ApplicationStatus" not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            assert "recommend" not in node.arg.lower(), node.arg
        # The ONLY ``.status`` it ever assigns is the previous RUN's, being
        # superseded -- never an application's.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    assert isinstance(target.value, ast.Name)
                    assert target.value.id == "previous", ast.dump(node)


def test_the_only_public_trigger_is_generate_final_ranking_and_nothing_calls_it_by_itself():
    callers = []
    for p in _APP_DIR.rglob("*.py"):
        if p.name in ("final_ranking_service.py",):
            continue
        if "generate_final_ranking(" in p.read_text(encoding="utf-8"):
            callers.append(str(p.relative_to(_APP_DIR)))
    assert callers == ["pages\\interviews.py"] or callers == ["pages/interviews.py"]
