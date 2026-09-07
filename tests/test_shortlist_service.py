"""Tests for app.services.shortlist_service (Phase 4 Step 6).

No AI in this step — real Postgres via the savepoint-rollback ``db`` fixture.
Upstream state (job, approved rubric, applications, screening sessions,
screening_evaluations, candidate_rankings) is built directly with the ORM.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
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
from app.services import shortlist_service
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.job_service import create_job
from app.services.shortlist_service import (
    ShortlistError,
    ShortlistPreconditionError,
    ShortlistTargetNotFoundError,
    get_ranking_staleness_for_partition,
    get_shortlist_status_for_job,
    shortlist_candidate,
    unshortlist_candidate,
)
from app.utils.authorization import UnauthorizedError

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SENTINEL_REASON = "SENTINEL_REASON_strong culture add, met at meetup"


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
    db, job, hr, link, rubric,
    *,
    status=ApplicationStatus.SCREENING_EVALUATED,
    mandatory_results=("PASS",),
    evaluation_created_at=_T0,
):
    app = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com",
        full_name=f"Cand {uuid.uuid4().hex[:6]}", phone=None,
    )
    app.status = status
    db.flush()

    session = ScreeningSession(
        application_id=app.id, access_token=f"tok-{uuid.uuid4().hex}",
        status=ScreeningSessionStatus.SCREENING_COMPLETE,
    )
    db.add(session)
    db.flush()

    ev = ScreeningEvaluation(
        screening_session_id=session.id,
        rubric_version_id=rubric.id,
        results=[
            {"requirement_type": "MANDATORY", "result": r,
             "criterion_text": "c", "evidence_summary": "e", "reasoning": "r"}
            for r in mandatory_results
        ],
        requirements_score=8, requirements_coverage=1.0,
        experience_score=7, experience_coverage=1.0,
        behavioral_score=6, behavioral_coverage=1.0,
        strengths=[], gaps=[], unknowns=[],
        overall_confidence="MEDIUM", ai_recommendation="PROCEED",
        ai_model="claude-sonnet-5",
    )
    db.add(ev)
    db.flush()
    ev.created_at = evaluation_created_at
    db.flush()
    return app


def _ranking_row(db, job, rubric, app, *, rank_position, generated_at=_T0,
                 eligible=True):
    row = CandidateRanking(
        job_id=job.id, rubric_version_id=rubric.id, application_id=app.id,
        rank_position=rank_position, overall_score=7.0, eligible=eligible,
        mandatory_unknown_flag=False, generated_at=generated_at,
        generation_batch_id=uuid.uuid4(),
    )
    db.add(row)
    db.flush()
    return row


def _seed(db, *, ranked=True):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = _evaluated_application(db, job, hr, link, rubric)
    if ranked:
        _ranking_row(db, job, rubric, app, rank_position=1)
    return hr, job, rubric, link, app


def _events(db, application_id, event_type):
    return db.execute(
        select(AuditEvent)
        .where(
            AuditEvent.entity_id == application_id,
            AuditEvent.event_type == event_type.value,
        )
        .order_by(AuditEvent.timestamp)
    ).scalars().all()


# --- shortlist happy path -----------------------------------------


def test_shortlist_creates_row_and_emits_one_event_with_rank(db):
    hr, job, rubric, link, app = _seed(db)
    entry = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        reason="great communicator", requested_by_user_id=hr.id,
    )
    assert entry.is_shortlisted is True
    assert entry.rank_position_at_decision == 1
    assert entry.rubric_version_id == rubric.id
    assert entry.decided_by_user_id == hr.id
    assert entry.reason == "great communicator"

    evs = _events(db, app.id, AuditEventType.CANDIDATE_SHORTLISTED)
    assert len(evs) == 1
    assert evs[0].user_id == hr.id
    assert evs[0].user_id != SYSTEM_USER_ID
    assert evs[0].new_state["is_shortlisted"] is True
    assert evs[0].new_state["rank_position_at_decision"] == 1


def test_shortlist_captures_null_rank_when_no_ranking_row(db):
    hr, job, rubric, link, app = _seed(db, ranked=False)
    entry = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    assert entry.rank_position_at_decision is None
    assert entry.is_shortlisted is True


def test_shortlist_captures_null_rank_for_ineligible_candidate(db):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = _evaluated_application(
        db, job, hr, link, rubric, mandatory_results=("PASS", "FAIL"),
    )
    _ranking_row(db, job, rubric, app, rank_position=None, eligible=False)

    entry = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    assert entry.is_shortlisted is True
    assert entry.rank_position_at_decision is None


# --- idempotency -------------------------------------------------


def test_repeat_shortlist_is_a_noop_no_second_event_no_rewrite(db):
    hr, job, rubric, link, app = _seed(db)
    first = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    decided_at_1 = first.decided_at

    # move the ranking so a (wrong) re-capture would be visible
    db.execute(
        select(CandidateRanking).where(CandidateRanking.application_id == app.id)
    ).scalar_one().rank_position = 5
    db.flush()

    second = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        reason="new reason ignored", requested_by_user_id=hr.id,
    )
    assert second.id == first.id
    assert second.decided_at == decided_at_1  # not rewritten
    assert second.rank_position_at_decision == 1  # not re-captured
    assert second.reason != "new reason ignored"

    assert len(_events(db, app.id, AuditEventType.CANDIDATE_SHORTLISTED)) == 1
    assert db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
        .where(CandidateShortlistEntry.application_id == app.id)
    ).scalar_one() == 1


def test_unshortlist_when_never_shortlisted_is_a_noop(db):
    hr, job, rubric, link, app = _seed(db)
    result = unshortlist_candidate(
        db, job_id=job.id, application_id=app.id, requested_by_user_id=hr.id,
    )
    assert result is None
    assert _events(db, app.id, AuditEventType.CANDIDATE_UNSHORTLISTED) == []
    assert db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
        .where(CandidateShortlistEntry.application_id == app.id)
    ).scalar_one() == 0


def test_repeat_unshortlist_is_a_noop(db):
    hr, job, rubric, link, app = _seed(db)
    shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    unshortlist_candidate(
        db, job_id=job.id, application_id=app.id, requested_by_user_id=hr.id,
    )
    unshortlist_candidate(
        db, job_id=job.id, application_id=app.id, requested_by_user_id=hr.id,
    )
    assert len(_events(db, app.id, AuditEventType.CANDIDATE_UNSHORTLISTED)) == 1


# --- unshortlist happy path -------------------------------------


def test_unshortlist_updates_row_and_emits_one_event(db):
    hr, job, rubric, link, app = _seed(db)
    shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    entry = unshortlist_candidate(
        db, job_id=job.id, application_id=app.id,
        reason="role paused", requested_by_user_id=hr.id,
    )
    assert entry.is_shortlisted is False
    assert entry.reason == "role paused"
    # decision context is NOT cleared
    assert entry.rubric_version_id == rubric.id
    assert entry.rank_position_at_decision == 1

    evs = _events(db, app.id, AuditEventType.CANDIDATE_UNSHORTLISTED)
    assert len(evs) == 1
    assert evs[0].new_state["is_shortlisted"] is False
    assert evs[0].user_id == hr.id


# --- full reversibility cycle ---------------------------------


def test_full_cycle_shortlist_unshortlist_shortlist(db):
    hr, job, rubric, link, app = _seed(db)
    shortlist_candidate(db, job_id=job.id, application_id=app.id,
                        rubric_version_id=rubric.id, requested_by_user_id=hr.id)
    unshortlist_candidate(db, job_id=job.id, application_id=app.id,
                          requested_by_user_id=hr.id)
    final = shortlist_candidate(db, job_id=job.id, application_id=app.id,
                                rubric_version_id=rubric.id,
                                requested_by_user_id=hr.id)
    assert final.is_shortlisted is True

    # One event per real transition: 2 shortlist + 1 unshortlist. (Strict
    # ordering is not asserted — under the savepoint fixture every event in the
    # test shares one Postgres transaction_timestamp, so `ORDER BY timestamp`
    # is not deterministic; in production each record_event is its own txn.)
    all_evs = db.execute(
        select(AuditEvent)
        .where(AuditEvent.entity_id == app.id)
        .where(AuditEvent.event_type.in_((
            AuditEventType.CANDIDATE_SHORTLISTED.value,
            AuditEventType.CANDIDATE_UNSHORTLISTED.value,
        )))
    ).scalars().all()
    kinds = sorted(e.event_type for e in all_evs)
    assert kinds == [
        "CANDIDATE_SHORTLISTED", "CANDIDATE_SHORTLISTED", "CANDIDATE_UNSHORTLISTED",
    ]
    # still exactly one row
    assert db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
        .where(CandidateShortlistEntry.application_id == app.id)
    ).scalar_one() == 1


# --- precondition ---------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        ApplicationStatus.APPLIED,
        ApplicationStatus.PREQUALIFICATION_COMPLETED,
        ApplicationStatus.SCREENING_COMPLETED,
        ApplicationStatus.SCREENING_INCOMPLETE,
    ],
)
def test_shortlist_requires_screening_evaluated(db, status):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = _evaluated_application(db, job, hr, link, rubric, status=status)
    with pytest.raises(ShortlistPreconditionError):
        shortlist_candidate(
            db, job_id=job.id, application_id=app.id,
            rubric_version_id=rubric.id, requested_by_user_id=hr.id,
        )
    assert db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
        .where(CandidateShortlistEntry.application_id == app.id)
    ).scalar_one() == 0


def test_shortlist_application_not_in_job_raises(db):
    hr, job, rubric, link, app = _seed(db)
    other_job = _job(db, hr)
    with pytest.raises(ShortlistTargetNotFoundError):
        shortlist_candidate(
            db, job_id=other_job.id, application_id=app.id,
            rubric_version_id=rubric.id, requested_by_user_id=hr.id,
        )


# --- authorization ------------------------------------------


@pytest.fixture(params=["none", "unknown", "malformed"])
def bad_user_id(request):
    return {"none": None, "unknown": uuid.uuid4(), "malformed": "nope"}[request.param]


def test_all_functions_denied_for_bad_user(db, bad_user_id):
    hr, job, rubric, link, app = _seed(db)
    before = db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
    ).scalar_one()
    for call in (
        lambda: shortlist_candidate(
            db, job_id=job.id, application_id=app.id,
            rubric_version_id=rubric.id, requested_by_user_id=bad_user_id,
        ),
        lambda: unshortlist_candidate(
            db, job_id=job.id, application_id=app.id,
            requested_by_user_id=bad_user_id,
        ),
        lambda: get_shortlist_status_for_job(
            db, job_id=job.id, acting_user_id=bad_user_id,
        ),
        lambda: get_ranking_staleness_for_partition(
            db, job_id=job.id, rubric_version_id=rubric.id,
            acting_user_id=bad_user_id,
        ),
    ):
        with pytest.raises(UnauthorizedError):
            call()
    assert db.execute(
        select(func.count()).select_from(CandidateShortlistEntry)
    ).scalar_one() == before


def test_shortlist_attribution_is_the_real_hr_user_never_system(db):
    hr, job, rubric, link, app = _seed(db)
    entry = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    assert entry.decided_by_user_id == hr.id
    assert entry.decided_by_user_id != SYSTEM_USER_ID
    ev = _events(db, app.id, AuditEventType.CANDIDATE_SHORTLISTED)[0]
    assert ev.user_id == hr.id


# --- audit safety: reason never leaks ------------------------


def test_reason_never_appears_in_audit_metadata(db):
    hr, job, rubric, link, app = _seed(db)
    shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        reason=_SENTINEL_REASON, requested_by_user_id=hr.id,
    )
    unshortlist_candidate(
        db, job_id=job.id, application_id=app.id,
        reason=_SENTINEL_REASON + " (removed)", requested_by_user_id=hr.id,
    )
    for ev in db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == app.id)
    ).scalars().all():
        blob = (
            f"{ev.action} || {ev.previous_state} || {ev.new_state} || "
            f"{ev.event_metadata}"
        )
        assert "SENTINEL_REASON" not in blob
    # but the reason IS on the row
    row = db.execute(
        select(CandidateShortlistEntry)
        .where(CandidateShortlistEntry.application_id == app.id)
    ).scalars().one()
    assert "SENTINEL_REASON" in (row.reason or "")


# --- ApplicationStatus regression ---------------------------


def test_application_status_never_changes(db):
    hr, job, rubric, link, app = _seed(db)
    before = app.status
    shortlist_candidate(db, job_id=job.id, application_id=app.id,
                        rubric_version_id=rubric.id, requested_by_user_id=hr.id)
    db.refresh(app)
    assert app.status == before
    unshortlist_candidate(db, job_id=job.id, application_id=app.id,
                          requested_by_user_id=hr.id)
    db.refresh(app)
    assert app.status == before
    # repeat no-ops
    unshortlist_candidate(db, job_id=job.id, application_id=app.id,
                          requested_by_user_id=hr.id)
    shortlist_candidate(db, job_id=job.id, application_id=app.id,
                        rubric_version_id=rubric.id, requested_by_user_id=hr.id)
    shortlist_candidate(db, job_id=job.id, application_id=app.id,
                        rubric_version_id=rubric.id, requested_by_user_id=hr.id)
    db.refresh(app)
    assert app.status == before == ApplicationStatus.SCREENING_EVALUATED


# --- get_shortlist_status_for_job --------------------------


def test_status_view_includes_currently_and_previously_shortlisted(db):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    a_keep = _evaluated_application(db, job, hr, link, rubric)
    a_removed = _evaluated_application(db, job, hr, link, rubric)
    a_untouched = _evaluated_application(db, job, hr, link, rubric)

    shortlist_candidate(db, job_id=job.id, application_id=a_keep.id,
                        rubric_version_id=rubric.id, requested_by_user_id=hr.id)
    shortlist_candidate(db, job_id=job.id, application_id=a_removed.id,
                        rubric_version_id=rubric.id, requested_by_user_id=hr.id)
    unshortlist_candidate(db, job_id=job.id, application_id=a_removed.id,
                          requested_by_user_id=hr.id)

    view = get_shortlist_status_for_job(db, job_id=job.id, acting_user_id=hr.id)
    assert set(view) == {a_keep.id, a_removed.id}  # a_untouched has no row
    assert view[a_keep.id].is_shortlisted is True
    assert view[a_removed.id].is_shortlisted is False


# --- rubric-version isolation -----------------------------


def test_shortlist_in_one_partition_does_not_appear_in_another(db):
    hr = _hr(db)
    job = _job(db, hr)
    rv1 = _rubric(db, job, hr, version_number=1,
                  status=RubricVersionStatus.SUPERSEDED)
    rv2 = _rubric(db, job, hr, version_number=2)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app_v1 = _evaluated_application(db, job, hr, link, rv1)
    app_v2 = _evaluated_application(db, job, hr, link, rv2)

    shortlist_candidate(db, job_id=job.id, application_id=app_v1.id,
                        rubric_version_id=rv1.id, requested_by_user_id=hr.id)

    view = get_shortlist_status_for_job(db, job_id=job.id, acting_user_id=hr.id)
    # the job-level view has the v1 entry, tagged with its partition...
    assert view[app_v1.id].rubric_version_id == rv1.id
    # ...and nothing for the v2 candidate
    assert app_v2.id not in view


def test_superseded_rubric_candidate_can_still_be_shortlisted(db):
    hr = _hr(db)
    job = _job(db, hr)
    rv1 = _rubric(db, job, hr, version_number=1,
                  status=RubricVersionStatus.SUPERSEDED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = _evaluated_application(db, job, hr, link, rv1)
    entry = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rv1.id,
        requested_by_user_id=hr.id,
    )
    assert entry.is_shortlisted is True


# --- staleness ------------------------------------------


def test_staleness_counts_evaluations_after_the_latest_ranking(db):
    hr = _hr(db)
    job = _job(db, hr)
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)

    ranking_time = _T0 + timedelta(days=1)
    # 2 evaluated before the ranking, 3 after
    old = [
        _evaluated_application(db, job, hr, link, rubric,
                               evaluation_created_at=_T0 + timedelta(hours=i))
        for i in range(2)
    ]
    new = [
        _evaluated_application(db, job, hr, link, rubric,
                               evaluation_created_at=ranking_time + timedelta(hours=i + 1))
        for i in range(3)
    ]
    _ranking_row(db, job, rubric, old[0], rank_position=1,
                 generated_at=ranking_time)

    info = get_ranking_staleness_for_partition(
        db, job_id=job.id, rubric_version_id=rubric.id, acting_user_id=hr.id,
    )
    assert info.latest_ranking_generated_at == ranking_time
    assert info.stale_count == 3


def test_staleness_zero_when_no_new_evaluations(db):
    hr, job, rubric, link, app = _seed(db)  # ranking generated at _T0
    # the one evaluation was created at _T0, not strictly after
    info = get_ranking_staleness_for_partition(
        db, job_id=job.id, rubric_version_id=rubric.id, acting_user_id=hr.id,
    )
    assert info.stale_count == 0


def test_staleness_when_no_ranking_ever_generated(db):
    hr, job, rubric, link, app = _seed(db, ranked=False)
    info = get_ranking_staleness_for_partition(
        db, job_id=job.id, rubric_version_id=rubric.id, acting_user_id=hr.id,
    )
    assert info.latest_ranking_generated_at is None
    assert info.stale_count == 0


def test_staleness_is_partition_scoped(db):
    hr = _hr(db)
    job = _job(db, hr)
    rv1 = _rubric(db, job, hr, version_number=1,
                  status=RubricVersionStatus.SUPERSEDED)
    rv2 = _rubric(db, job, hr, version_number=2)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)

    ranking_time = _T0 + timedelta(days=1)
    _ranking_row(db, job, rv1,
                 _evaluated_application(db, job, hr, link, rv1,
                                        evaluation_created_at=_T0),
                 rank_position=1, generated_at=ranking_time)
    # a NEW v2 evaluation after rv1's ranking time — must NOT count as v1-stale
    _evaluated_application(db, job, hr, link, rv2,
                           evaluation_created_at=ranking_time + timedelta(hours=1))

    info = get_ranking_staleness_for_partition(
        db, job_id=job.id, rubric_version_id=rv1.id, acting_user_id=hr.id,
    )
    assert info.stale_count == 0


# --- structural: no AI, no ApplicationStatus write ------


def test_module_makes_no_ai_calls_and_no_status_writes():
    import pathlib
    src = pathlib.Path(shortlist_service.__file__).read_text(encoding="utf-8")
    assert "get_structured_response" not in src
    assert "claude_client" not in src
    # No assignment to any ``.status`` attribute (comparisons `== ` are fine).
    offenders = [
        ln for ln in src.splitlines()
        if ".status =" in ln.replace(".status ==", ".status EQ")
    ]
    assert offenders == []
