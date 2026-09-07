"""Tests for app.services.ranking_service (Phase 4 Step 5).

No AI in this step — real Postgres via the savepoint-rollback ``db`` fixture.
Upstream state (job, approved rubric, applications, screening sessions,
screening_evaluations) is built directly with the ORM.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.job import JdInputMethod, JobStatus
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
from app.services import ranking_service
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.ranking_service import (
    RankingError,
    RankingTargetNotFoundError,
    generate_ranking,
    get_ranking_display_rows,
    get_ranking_for_job,
    list_evaluated_applications_for_job,
    list_rubric_version_partitions_for_job,
)
from app.utils.authorization import UnauthorizedError

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _hr(db):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="HR", role=UserRole.HR,
    )


def _job(db, hr):
    job = create_job(
        db, title="Backend Engineer", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
        created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    return job


def _rubric(db, job, hr, *, version_number=1, status=RubricVersionStatus.APPROVED):
    rv = RubricVersion(
        job_id=job.id, version_number=version_number, status=status,
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


def _evaluated_application(
    db,
    job,
    hr,
    link,
    rubric,
    *,
    created_at=_T0,
    req=8,
    exp=7,
    beh=6,
    req_cov=1.0,
    exp_cov=1.0,
    beh_cov=1.0,
    confidence="MEDIUM",
    mandatory_results=("PASS",),
    status=ApplicationStatus.SCREENING_EVALUATED,
):
    """One application + screening session + screening_evaluation row.

    ``mandatory_results`` becomes the MANDATORY criteria in ``results``.
    """
    app = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com", full_name=f"Cand {uuid.uuid4().hex[:6]}",
        phone=None,
    )
    app.status = status
    app.created_at = created_at
    db.flush()

    session = ScreeningSession(
        application_id=app.id,
        access_token=f"tok-{uuid.uuid4().hex}",
        status=ScreeningSessionStatus.SCREENING_COMPLETE,
    )
    db.add(session)
    db.flush()

    results = [
        {
            "criterion_id": str(uuid.uuid4()),
            "criterion_index": i + 1,
            "requirement_type": "MANDATORY",
            "category": None,
            "criterion_text": "mandatory crit",
            "result": r,
            "evidence_summary": "e",
            "reasoning": "r",
            "confidence": "MEDIUM",
        }
        for i, r in enumerate(mandatory_results)
    ]
    ev = ScreeningEvaluation(
        screening_session_id=session.id,
        rubric_version_id=rubric.id,
        results=results,
        requirements_score=req,
        requirements_coverage=req_cov,
        experience_score=exp,
        experience_coverage=exp_cov,
        behavioral_score=beh,
        behavioral_coverage=beh_cov,
        strengths=[],
        gaps=[],
        unknowns=[],
        overall_confidence=confidence,
        ai_recommendation="PROCEED",
        ai_model="claude-sonnet-5",
    )
    db.add(ev)
    db.flush()
    return app


def _seed(db, *, n=3):
    """HR + job + approved rubric + ``n`` evaluated applications with
    descending bucket scores (app 0 best)."""
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    apps = [
        _evaluated_application(
            db, job, hr, link, rubric,
            req=9 - i, exp=9 - i, beh=9 - i, created_at=_T0 + timedelta(hours=i),
        )
        for i in range(n)
    ]
    return hr, job, rubric, link, apps


# --- list_evaluated_applications_for_job ---------------------------


def test_only_screening_evaluated_applications_are_returned(db):
    hr, job, rubric, link, apps = _seed(db, n=2)
    # add one that is NOT evaluated yet
    _evaluated_application(
        db, job, hr, link, rubric, status=ApplicationStatus.SCREENING_COMPLETED,
    )
    rows = list_evaluated_applications_for_job(db, job.id, acting_user_id=hr.id)
    assert {r.application_id for r in rows} == {a.id for a in apps}


def test_list_evaluated_carries_evaluation_numbers(db):
    hr, job, rubric, link, apps = _seed(db, n=1)
    [row] = list_evaluated_applications_for_job(db, job.id, acting_user_id=hr.id)
    assert row.requirements_score == 9
    assert row.overall_confidence == "MEDIUM"
    assert row.rubric_version_id == rubric.id
    assert any(c["requirement_type"] == "MANDATORY" for c in row.results)


# --- generate_ranking: happy path -------------------------------


def test_generate_ranking_persists_ordered_rows(db):
    hr, job, rubric, link, apps = _seed(db, n=3)
    result = generate_ranking(
        db, job_id=job.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    assert [r.rank_position for r in result] == [1, 2, 3]
    # app 0 had the best scores
    assert result[0].application_id == apps[0].id
    stored = db.execute(
        select(CandidateRanking).where(CandidateRanking.job_id == job.id)
    ).scalars().all()
    assert len(stored) == 3
    assert len({r.generation_batch_id for r in stored}) == 1
    assert all(r.rubric_version_id == rubric.id for r in stored)


def test_generate_ranking_emits_one_ranking_generated_event_hr_attributed(db):
    hr, job, rubric, link, apps = _seed(db, n=2)
    generate_ranking(
        db, job_id=job.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    events = db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RANKING_GENERATED.value,
            AuditEvent.entity_id == job.id,
        )
    ).scalars().all()
    assert len(events) == 1
    ev = events[0]
    assert ev.user_id == hr.id
    assert ev.user_id != SYSTEM_USER_ID
    assert ev.new_state["candidate_count"] == 2
    assert ev.new_state["eligible_count"] == 2
    assert len(ev.new_state["positions"]) == 2


def test_ranking_generated_metadata_has_no_free_text_candidate_content(db):
    hr, job, rubric, link, apps = _seed(db, n=1)
    # plant a sentinel in the evaluation's free-text fields
    ev_row = db.execute(
        select(ScreeningEvaluation)
        .join(
            ScreeningSession,
            ScreeningSession.id == ScreeningEvaluation.screening_session_id,
        )
        .where(ScreeningSession.application_id == apps[0].id)
    ).scalars().one()
    ev_row.results = [
        {
            "requirement_type": "MANDATORY", "result": "PASS",
            "criterion_text": "SENTINEL_CRITERION_TEXT",
            "evidence_summary": "SENTINEL_EVIDENCE", "reasoning": "SENTINEL_REASONING",
        }
    ]
    db.flush()

    generate_ranking(
        db, job_id=job.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    ev = db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RANKING_GENERATED.value,
            AuditEvent.entity_id == job.id,
        )
    ).scalars().one()
    blob = f"{ev.action} || {ev.previous_state} || {ev.new_state} || {ev.event_metadata}"
    for sentinel in (
        "SENTINEL_CRITERION_TEXT", "SENTINEL_EVIDENCE", "SENTINEL_REASONING",
    ):
        assert sentinel not in blob
    # safe aggregates ARE present
    assert "generation_batch_id" in ev.new_state
    assert "eligible_count" in ev.new_state


# --- regeneration: atomic full replace -------------------------


def test_regeneration_replaces_partition_wholesale(db):
    hr, job, rubric, link, apps = _seed(db, n=2)
    first = generate_ranking(
        db, job_id=job.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    first_batch = first[0].generation_batch_id

    # a third candidate finishes evaluation, then HR regenerates
    _evaluated_application(
        db, job, hr, link, rubric, req=10, exp=10, beh=10,
        created_at=_T0 - timedelta(hours=1),
    )
    second = generate_ranking(
        db, job_id=job.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )

    stored = db.execute(
        select(CandidateRanking).where(CandidateRanking.job_id == job.id)
    ).scalars().all()
    assert len(stored) == 3  # old 2 gone, fresh 3 — never 5
    assert len({r.generation_batch_id for r in stored}) == 1
    assert stored[0].generation_batch_id != first_batch
    assert [r.rank_position for r in second] == [1, 2, 3]


def test_two_ranking_generated_events_after_a_regeneration(db):
    hr, job, rubric, link, apps = _seed(db, n=2)
    generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                     requested_by_user_id=hr.id)
    generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                     requested_by_user_id=hr.id)
    n = db.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RANKING_GENERATED.value,
            AuditEvent.entity_id == job.id,
        )
    ).scalar_one()
    assert n == 2


def test_regenerating_unchanged_data_same_order_new_batch_new_event(db):
    """Explicitly NOT the check-first idempotency of Steps 1-4: the ranking
    ORDER/SCORES are identical, but each call is a real regeneration — new
    generation_batch_id, new audit event."""
    hr, job, rubric, link, apps = _seed(db, n=3)
    first = generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                             requested_by_user_id=hr.id)
    second = generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                              requested_by_user_id=hr.id)

    assert [r.application_id for r in first] == [r.application_id for r in second]
    assert [r.overall_score for r in first] == [r.overall_score for r in second]
    assert [r.rank_position for r in first] == [r.rank_position for r in second]
    assert first[0].generation_batch_id != second[0].generation_batch_id

    events = db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type == AuditEventType.RANKING_GENERATED.value,
            AuditEvent.entity_id == job.id,
        )
    ).scalars().all()
    assert len(events) == 2
    assert (
        events[0].new_state["generation_batch_id"]
        != events[1].new_state["generation_batch_id"]
    )


def test_failure_before_commit_leaves_prior_ranking_intact(db, mocker):
    hr, job, rubric, link, apps = _seed(db, n=2)
    good = generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                            requested_by_user_id=hr.id)
    good_batch = good[0].generation_batch_id

    # make the audit write (which happens after the DELETE + INSERT, before the
    # commit) blow up on the next call
    mocker.patch.object(
        ranking_service, "record_event", side_effect=RuntimeError("boom")
    )
    with pytest.raises(RuntimeError):
        generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                         requested_by_user_id=hr.id)
    db.rollback()

    stored = db.execute(
        select(CandidateRanking).where(CandidateRanking.job_id == job.id)
    ).scalars().all()
    assert len(stored) == 2
    assert {r.generation_batch_id for r in stored} == {good_batch}


# --- partitioning: versions never mix -------------------------


def _two_partitions(db):
    hr = _hr(db)
    job = _job(db, hr)
    rv1 = _rubric(db, job, hr, version_number=1,
                  status=RubricVersionStatus.SUPERSEDED)
    rv2 = _rubric(db, job, hr, version_number=2,
                  status=RubricVersionStatus.APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    a_v1 = [
        _evaluated_application(db, job, hr, link, rv1, req=r)
        for r in (5, 6)
    ]
    a_v2 = [
        _evaluated_application(db, job, hr, link, rv2, req=r)
        for r in (7, 8, 9)
    ]
    return hr, job, rv1, rv2, a_v1, a_v2


def test_generate_ranking_ranks_only_the_requested_partition(db):
    hr, job, rv1, rv2, a_v1, a_v2 = _two_partitions(db)
    result = generate_ranking(
        db, job_id=job.id, rubric_version_id=rv2.id,
        requested_by_user_id=hr.id,
    )
    assert {r.application_id for r in result} == {a.id for a in a_v2}
    stored = db.execute(
        select(CandidateRanking).where(CandidateRanking.job_id == job.id)
    ).scalars().all()
    assert all(r.rubric_version_id == rv2.id for r in stored)
    assert len(stored) == 3


def test_get_ranking_for_job_returns_only_its_partition(db):
    hr, job, rv1, rv2, a_v1, a_v2 = _two_partitions(db)
    generate_ranking(db, job_id=job.id, rubric_version_id=rv1.id,
                     requested_by_user_id=hr.id)
    generate_ranking(db, job_id=job.id, rubric_version_id=rv2.id,
                     requested_by_user_id=hr.id)

    r1 = get_ranking_for_job(db, job_id=job.id, rubric_version_id=rv1.id,
                             acting_user_id=hr.id)
    r2 = get_ranking_for_job(db, job_id=job.id, rubric_version_id=rv2.id,
                             acting_user_id=hr.id)
    assert {r.application_id for r in r1} == {a.id for a in a_v1}
    assert {r.application_id for r in r2} == {a.id for a in a_v2}


def test_list_rubric_version_partitions_for_job(db):
    hr, job, rv1, rv2, a_v1, a_v2 = _two_partitions(db)
    parts = list_rubric_version_partitions_for_job(
        db, job_id=job.id, acting_user_id=hr.id
    )
    assert [p.version_number for p in parts] == [1, 2]
    by_v = {p.version_number: p for p in parts}
    assert by_v[1].evaluated_count == 2
    assert by_v[2].evaluated_count == 3
    assert by_v[1].ranking_generated_at is None

    generate_ranking(db, job_id=job.id, rubric_version_id=rv1.id,
                     requested_by_user_id=hr.id)
    parts = list_rubric_version_partitions_for_job(
        db, job_id=job.id, acting_user_id=hr.id
    )
    assert {p.version_number: p for p in parts}[1].ranking_generated_at is not None


# --- eligibility surfaces in persisted rows -------------------


def test_mandatory_fail_candidate_persisted_ineligible_no_rank(db):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    ok = _evaluated_application(db, job, hr, link, rubric, req=5,
                               mandatory_results=("PASS",))
    bad = _evaluated_application(db, job, hr, link, rubric, req=9,
                                 mandatory_results=("PASS", "FAIL"))

    generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                     requested_by_user_id=hr.id)
    rows = {
        r.application_id: r
        for r in db.execute(select(CandidateRanking)).scalars().all()
    }
    assert rows[ok.id].eligible is True
    assert rows[ok.id].rank_position == 1
    assert rows[bad.id].eligible is False
    assert rows[bad.id].rank_position is None


def test_mandatory_unknown_flag_persisted_without_exclusion(db):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    a = _evaluated_application(db, job, hr, link, rubric,
                              mandatory_results=("PASS", "UNKNOWN"))
    generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                     requested_by_user_id=hr.id)
    row = db.execute(
        select(CandidateRanking).where(CandidateRanking.job_id == job.id)
    ).scalars().one()
    assert row.application_id == a.id
    assert row.eligible is True
    assert row.mandatory_unknown_flag is True
    assert row.rank_position == 1


# --- display rows -------------------------------------------


def test_display_rows_carry_confidence_and_coverage(db):
    hr, job, rubric, link, apps = _seed(db, n=2)
    generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                     requested_by_user_id=hr.id)
    rows = get_ranking_display_rows(
        db, job_id=job.id, rubric_version_id=rubric.id, acting_user_id=hr.id
    )
    assert [r.rank_position for r in rows] == [1, 2]
    assert rows[0].overall_confidence == "MEDIUM"
    assert rows[0].requirements_coverage == 1.0
    assert rows[0].candidate_name  # present for display


# --- preconditions / errors --------------------------------


def test_unknown_rubric_version_for_job_raises(db):
    hr, job, rubric, link, apps = _seed(db, n=1)
    with pytest.raises(RankingTargetNotFoundError):
        generate_ranking(db, job_id=job.id, rubric_version_id=uuid.uuid4(),
                         requested_by_user_id=hr.id)


def test_partition_with_no_evaluated_candidates_raises(db):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    with pytest.raises(RankingTargetNotFoundError):
        generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                         requested_by_user_id=hr.id)


# --- authorization ----------------------------------------


@pytest.fixture(params=["none", "unknown", "malformed"])
def bad_user_id(request):
    return {"none": None, "unknown": uuid.uuid4(), "malformed": "nope"}[request.param]


def test_generate_ranking_denied_for_bad_user(db, bad_user_id):
    hr, job, rubric, link, apps = _seed(db, n=1)
    before = db.execute(
        select(func.count()).select_from(CandidateRanking)
    ).scalar_one()
    with pytest.raises(UnauthorizedError):
        generate_ranking(db, job_id=job.id, rubric_version_id=rubric.id,
                         requested_by_user_id=bad_user_id)
    after = db.execute(
        select(func.count()).select_from(CandidateRanking)
    ).scalar_one()
    assert after == before


def test_reads_denied_for_bad_user(db, bad_user_id):
    hr, job, rubric, link, apps = _seed(db, n=1)
    for call in (
        lambda: list_evaluated_applications_for_job(
            db, job.id, acting_user_id=bad_user_id
        ),
        lambda: get_ranking_for_job(
            db, job_id=job.id, rubric_version_id=rubric.id,
            acting_user_id=bad_user_id,
        ),
        lambda: get_ranking_display_rows(
            db, job_id=job.id, rubric_version_id=rubric.id,
            acting_user_id=bad_user_id,
        ),
        lambda: list_rubric_version_partitions_for_job(
            db, job_id=job.id, acting_user_id=bad_user_id
        ),
    ):
        with pytest.raises(UnauthorizedError):
            call()


# --- structural: ScreeningSessionStatus is never referenced -----


def test_ranking_service_never_references_screening_session_status():
    """Eligibility is ApplicationStatus.SCREENING_EVALUATED only. The ranking
    module must not reach for the screening-session lifecycle vocabulary."""
    src = (
        pathlib.Path(ranking_service.__file__).read_text(encoding="utf-8")
    )
    assert "ScreeningSessionStatus" not in src
    assert "screening_session.ScreeningSessionStatus" not in src
    # it DOES join ScreeningSession (by id) — that's allowed; only the *status*
    # vocabulary is forbidden.
    assert "ApplicationStatus.SCREENING_EVALUATED" in src
