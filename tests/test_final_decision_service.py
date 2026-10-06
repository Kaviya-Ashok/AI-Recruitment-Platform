"""Tests for app.services.final_decision_service (Step 11).

No AI anywhere in this step. Real Postgres via the savepoint-rollback ``db``
fixture. Candidates, rankings, analyses and feedback are built with the helpers in
``test_final_ranking_service``; every query is scoped to rows this test created.
"""

from __future__ import annotations

import ast
import inspect
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, inspect as sa_inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.database.models.application import Application
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.final_decision import FinalDecision, FinalDecisionStatus
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services import final_decision_service as fds
from app.services.auth_service import create_user
from app.services.final_decision_service import (
    DECIDER_ROLES,
    RATIONALE_MAX_CHARS,
    RATIONALE_MIN_CHARS,
    FinalDecisionActorError,
    FinalDecisionConflictError,
    FinalDecisionPermissionError,
    FinalDecisionPreconditionError,
    FinalDecisionTargetNotFoundError,
    FinalDecisionValidationError,
    clean_rationale,
    get_current_final_decision,
    get_final_decision_staleness,
    list_current_decisions_for_job,
    list_final_decision_history,
    record_final_decision,
    role_may_decide,
    validate_decision,
)
from app.services.final_ranking_service import generate_final_ranking
from app.services.interview_feedback_service import create_interview_feedback
from app.utils.authorization import UnauthorizedError
from tests.test_final_ranking_service import _analysis, _candidate, _job

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
_WHY = "Strong evidence across both rounds; clear fit for the role."
D = Decimal

_SENT_RATIONALE = "RATIONALESENTINEL_9X the candidate was calm under pressure"
_SENT_CANDIDATE = "Zyxwvuts Sentinelcandidate"
_SENT_DECIDER = "Qwertyuiop Sentineldecider"


# --- helpers -----------------------------------------------------------------


def _user(db, role=UserRole.HIRING_MANAGER, name="Mia Manager"):
    return create_user(
        db=db, email=f"u-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name=name, role=role,
    )


def _decide(db, cand, user, decision="PROCEED", why=_WHY):
    return record_final_decision(
        db, application_id=cand["app"].id, decision=decision, rationale=why,
        acting_user_id=user.id,
    )


def _rows(db, cand):
    return db.execute(
        select(FinalDecision).where(FinalDecision.application_id == cand["app"].id)
        .order_by(FinalDecision.created_at, FinalDecision.id)
    ).scalars().all()


def _events(db, cand):
    return db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == cand["app"].id,
            AuditEvent.event_type == AuditEventType.FINAL_DECISION_SUBMITTED.value,
        )
    ).scalars().all()


def _setup(db, **kw):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4, 3]}, overall=7.25, **kw)
    return w, cand, _user(db)


# =====================================================================
# Pure rules
# =====================================================================


@pytest.mark.parametrize("value", ["PROCEED", "HOLD", "REJECT"])
def test_the_three_decisions_are_valid_and_a_human_may_reject(value):
    assert validate_decision(value) == value


@pytest.mark.parametrize("bad", ["proceed", "Hire", "", None, 1, "PROCEED ", "AUTOMATED"])
def test_anything_else_is_not_a_decision(bad):
    with pytest.raises(FinalDecisionValidationError, match="Proceed, Hold or Reject"):
        validate_decision(bad)


def test_rationale_is_trimmed_and_otherwise_stored_exactly_as_written():
    raw = "  Line one.\n\n  Line two — with *markdown*, <tags> & \"quotes\".  \n"
    assert clean_rationale(raw) == raw.strip()
    assert "\n\n  Line two" in clean_rationale(raw)           # interior untouched


@pytest.mark.parametrize("length, ok", [
    (RATIONALE_MIN_CHARS - 1, False), (RATIONALE_MIN_CHARS, True),
    (RATIONALE_MAX_CHARS, True), (RATIONALE_MAX_CHARS + 1, False),
])
def test_rationale_length_boundaries(length, ok):
    text_ = "x" * length
    if ok:
        assert clean_rationale(text_) == text_
    else:
        with pytest.raises(FinalDecisionValidationError):
            clean_rationale(text_)


def test_length_is_counted_after_trimming():
    assert clean_rationale(" " * 50 + "x" * 10 + " " * 50) == "x" * 10
    assert clean_rationale("x" * RATIONALE_MAX_CHARS + "   ") == "x" * RATIONALE_MAX_CHARS
    with pytest.raises(FinalDecisionValidationError):
        clean_rationale("   short   ")                          # 5 chars once trimmed


@pytest.mark.parametrize("bad", [None, "", "   ", "\n\t", 123])
def test_a_missing_rationale_is_rejected_with_a_clear_message(bad):
    with pytest.raises(FinalDecisionValidationError, match="rationale is required"):
        clean_rationale(bad)


def test_limits_are_ten_and_two_thousand():
    assert (RATIONALE_MIN_CHARS, RATIONALE_MAX_CHARS) == (10, 2000)


@pytest.mark.parametrize("role, allowed", [
    (UserRole.ADMIN, True), (UserRole.HIRING_MANAGER, True),
    (UserRole.HR, False), (UserRole.SYSTEM, False),
    ("ADMIN", True), ("HIRING_MANAGER", True), ("HR", False), ("SYSTEM", False),
    ("admin", False), ("", False), (None, False), ("SUPERUSER", False),
])
def test_role_matrix(role, allowed):
    assert role_may_decide(role) is allowed
    assert DECIDER_ROLES == {UserRole.HIRING_MANAGER, UserRole.ADMIN}


# =====================================================================
# Recording
# =====================================================================


def test_a_hiring_manager_can_record_a_decision(db):
    w, cand, mgr = _setup(db)
    v = _decide(db, cand, mgr, "REJECT", "  " + _WHY + "  ")
    assert v.decision == "REJECT" and v.status == "CURRENT"
    assert v.rationale == _WHY                                  # trimmed, nothing else
    assert v.decided_by_user_id == mgr.id and v.decided_by_name == "Mia Manager"
    assert v.superseded_at is None and v.application_id == cand["app"].id
    (row,) = _rows(db, cand)
    assert row.rationale == _WHY and row.status == "CURRENT"


def test_an_admin_can_record_a_decision(db):
    w, cand, _ = _setup(db)
    admin = _user(db, UserRole.ADMIN, "Ada Admin")
    assert _decide(db, cand, admin, "HOLD").decided_by_name == "Ada Admin"


def test_hr_may_not_decide_but_may_read(db):
    w, cand, mgr = _setup(db)
    hr = _user(db, UserRole.HR, "Hal HR")
    with pytest.raises(FinalDecisionPermissionError, match="hiring manager or an admin"):
        _decide(db, cand, hr)
    assert _rows(db, cand) == [] and _events(db, cand) == []
    _decide(db, cand, mgr)
    seen = get_current_final_decision(db, cand["app"].id, acting_user_id=hr.id)
    assert seen.decision == "PROCEED"
    assert len(list_final_decision_history(db, cand["app"].id, acting_user_id=hr.id)) == 1


def test_system_actor_is_rejected(db):
    w, cand, _ = _setup(db)
    with pytest.raises(FinalDecisionActorError):
        record_final_decision(
            db, application_id=cand["app"].id, decision="PROCEED", rationale=_WHY,
            acting_user_id=SYSTEM_USER_ID,
        )
    assert _rows(db, cand) == [] and _events(db, cand) == []


@pytest.mark.parametrize("who", [None, "unknown", "malformed"])
def test_unknown_or_missing_user_is_rejected(db, who):
    w, cand, _ = _setup(db)
    actor = {"unknown": uuid.uuid4(), "malformed": "nope", None: None}[who]
    with pytest.raises(UnauthorizedError):
        record_final_decision(
            db, application_id=cand["app"].id, decision="PROCEED", rationale=_WHY,
            acting_user_id=actor,
        )
    assert _rows(db, cand) == []


def test_inactive_user_is_rejected(db):
    w, cand, mgr = _setup(db)
    mgr.is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _decide(db, cand, mgr)
    assert _rows(db, cand) == []


def test_the_actor_gate_runs_before_everything_else(db):
    """Even a bogus application, decision and rationale do not mask the auth error."""
    hr = _user(db, UserRole.HR)
    with pytest.raises(FinalDecisionPermissionError):
        record_final_decision(
            db, application_id=uuid.uuid4(), decision="nope", rationale="",
            acting_user_id=hr.id,
        )
    with pytest.raises(UnauthorizedError):
        record_final_decision(
            db, application_id=uuid.uuid4(), decision="nope", rationale="",
            acting_user_id=uuid.uuid4(),
        )


def test_unknown_application_is_a_clear_error(db):
    mgr = _user(db)
    with pytest.raises(FinalDecisionTargetNotFoundError):
        record_final_decision(
            db, application_id=uuid.uuid4(), decision="PROCEED", rationale=_WHY,
            acting_user_id=mgr.id,
        )


def test_an_application_without_interview_feedback_cannot_be_decided(db):
    w = _job(db)
    cand = _candidate(db, w, rounds=None)
    mgr = _user(db)
    with pytest.raises(FinalDecisionPreconditionError, match="interview round"):
        _decide(db, cand, mgr)
    assert _rows(db, cand) == [] and _events(db, cand) == []


def test_shortlist_and_final_ranking_status_are_not_required(db):
    w, cand, mgr = _setup(db)
    db.execute(text("UPDATE candidate_shortlist_entries SET is_shortlisted = false "
                    "WHERE application_id = :a"), {"a": cand["app"].id})
    v = _decide(db, cand, mgr)                                    # no ranking run yet
    assert v.final_ranking_entry_id is None and v.final_score is None


@pytest.mark.parametrize("decision, why", [
    ("MAYBE", _WHY), ("proceed", _WHY), ("PROCEED", ""), ("PROCEED", "too short"),
    ("PROCEED", "x" * (RATIONALE_MAX_CHARS + 1)),
])
def test_validation_failures_persist_nothing(db, decision, why):
    w, cand, mgr = _setup(db)
    with pytest.raises(FinalDecisionValidationError):
        _decide(db, cand, mgr, decision, why)
    assert _rows(db, cand) == [] and _events(db, cand) == []


def test_a_failed_revision_leaves_the_current_decision_untouched(db):
    w, cand, mgr = _setup(db)
    first = _decide(db, cand, mgr, "PROCEED")
    with pytest.raises(FinalDecisionValidationError):
        _decide(db, cand, mgr, "HOLD", "no")
    (row,) = _rows(db, cand)
    assert row.id == first.decision_id and row.status == "CURRENT"
    assert len(_events(db, cand)) == 1


# =====================================================================
# Revision, history, per-job helper
# =====================================================================


def test_revising_supersedes_and_keeps_the_old_decision(db):
    w, cand, mgr = _setup(db)
    first = _decide(db, cand, mgr, "PROCEED", "First rationale, long enough.")
    second = _decide(db, cand, mgr, "HOLD", "Second rationale, long enough.")

    rows = {r.id: r for r in _rows(db, cand)}
    assert len(rows) == 2                                      # nothing deleted
    old, new = rows[first.decision_id], rows[second.decision_id]
    assert old.status == "SUPERSEDED" and old.superseded_at is not None
    assert old.rationale == "First rationale, long enough."    # old text preserved
    assert new.status == "CURRENT" and new.superseded_at is None
    assert new.created_at > old.created_at
    assert old.superseded_at == new.created_at                  # one clock reading


def test_exactly_one_current_decision_after_repeated_revisions(db):
    w, cand, mgr = _setup(db)
    for choice in ("PROCEED", "HOLD", "REJECT", "PROCEED"):
        _decide(db, cand, mgr, choice)
    rows = _rows(db, cand)
    assert [r.status for r in rows].count("CURRENT") == 1 and len(rows) == 4
    assert get_current_final_decision(
        db, cand["app"].id, acting_user_id=mgr.id).decision == "PROCEED"


def test_history_is_newest_first_and_uses_explicit_timestamps(db):
    """Two decisions in ONE transaction would share ``now()``; the service sets
    created_at itself, strictly increasing, so order never depends on a tie-break."""
    w, cand, mgr = _setup(db)
    a = _decide(db, cand, mgr, "PROCEED")
    b = _decide(db, cand, mgr, "HOLD")
    c = _decide(db, cand, mgr, "REJECT")
    assert a.created_at < b.created_at < c.created_at
    history = list_final_decision_history(db, cand["app"].id, acting_user_id=mgr.id)
    assert [h.decision_id for h in history] == [c.decision_id, b.decision_id, a.decision_id]
    assert [h.status for h in history] == ["CURRENT", "SUPERSEDED", "SUPERSEDED"]


def test_a_clock_that_goes_backwards_still_orders_correctly(db, mocker):
    w, cand, mgr = _setup(db)
    first = _decide(db, cand, mgr, "PROCEED")

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return first.created_at - timedelta(hours=1)

    mocker.patch.object(fds, "datetime", _Frozen)
    second = _decide(db, cand, mgr, "HOLD")
    assert second.created_at > first.created_at
    assert get_current_final_decision(
        db, cand["app"].id, acting_user_id=mgr.id).decision_id == second.decision_id


def test_current_decision_is_none_before_any_is_recorded(db):
    w, cand, mgr = _setup(db)
    assert get_current_final_decision(db, cand["app"].id, acting_user_id=mgr.id) is None
    assert list_final_decision_history(db, cand["app"].id, acting_user_id=mgr.id) == []


def test_per_job_helper_returns_only_current_decisions_of_that_job(db):
    w = _job(db)
    a = _candidate(db, w, rounds={1: [4]})
    b = _candidate(db, w, rounds={1: [3]})
    c = _candidate(db, w, rounds={1: [5]})                       # never decided
    other = _candidate(db, _job(db), rounds={1: [2]})
    mgr = _user(db)
    _decide(db, a, mgr, "PROCEED")
    _decide(db, b, mgr, "HOLD")
    _decide(db, b, mgr, "REJECT")                                # b revised
    _decide(db, other, mgr, "PROCEED")                           # other job
    got = list_current_decisions_for_job(db, w["job"].id, acting_user_id=mgr.id)
    assert set(got) == {a["app"].id, b["app"].id}
    assert got[b["app"].id].decision == "REJECT"
    assert c["app"].id not in got and other["app"].id not in got


def test_every_accessor_is_guarded(db):
    w, cand, mgr = _setup(db)
    _decide(db, cand, mgr)
    nobody = uuid.uuid4()
    with pytest.raises(UnauthorizedError):
        get_current_final_decision(db, cand["app"].id, acting_user_id=nobody)
    with pytest.raises(UnauthorizedError):
        list_final_decision_history(db, cand["app"].id, acting_user_id=None)
    with pytest.raises(UnauthorizedError):
        list_current_decisions_for_job(db, w["job"].id, acting_user_id=nobody)
    with pytest.raises(UnauthorizedError):
        get_final_decision_staleness(db, cand["app"].id, acting_user_id=nobody)


def test_views_carry_the_deciders_name_but_the_row_does_not_store_it(db):
    w, cand, _ = _setup(db)
    mgr = _user(db, name=_SENT_DECIDER)
    v = _decide(db, cand, mgr)
    assert v.decided_by_name == _SENT_DECIDER
    cols = {c.name for c in FinalDecision.__table__.columns}
    assert not any("name" in c or "email" in c or "notes" in c for c in cols)
    assert _SENT_DECIDER not in str(vars(_rows(db, cand)[0]))


# =====================================================================
# Snapshot
# =====================================================================


def test_snapshot_with_a_ranking_and_an_analysis(db):
    w, cand, mgr = _setup(db)
    (run,) = generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    fb2 = create_interview_feedback(
        db, user_id=w["hr"].id, application_id=cand["app"].id,
        interview_guide_id=cand["guide"].id, interview_round=2,
        recommendation="HOLD", notes="n" * 50, ratings=[],
    )
    v = _decide(db, cand, mgr, "HOLD")

    entry = db.execute(text(
        "SELECT id, final_score, rank, entry_status, final_confidence "
        "FROM final_ranking_entries WHERE final_ranking_id = :r AND application_id = :a"
    ), {"r": run.id, "a": cand["app"].id}).one()
    assert v.final_ranking_entry_id == entry.id
    assert v.final_score == entry.final_score == D("7.10")
    assert v.final_rank == entry.rank == 1
    assert v.entry_status == "RANKED" and v.final_confidence == entry.final_confidence
    assert v.rubric_version_id == w["rubric"].id
    assert v.post_interview_analysis_id == cand["analysis"].id
    assert v.ai_recommendation_snapshot == "PROCEED"
    assert set(v.interview_feedback_ids) == {
        str(cand["feedback"].id), str(fb2.id)}
    assert list(v.interview_feedback_ids) == sorted(v.interview_feedback_ids)


def test_snapshot_with_neither_a_ranking_nor_an_analysis(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]}, with_analysis=False)
    mgr = _user(db)
    v = _decide(db, cand, mgr)
    assert v.final_ranking_entry_id is None and v.final_score is None
    assert v.final_rank is None and v.entry_status is None
    assert v.final_confidence is None and v.rubric_version_id is None
    assert v.post_interview_analysis_id is None and v.ai_recommendation_snapshot is None
    assert v.interview_feedback_ids == (str(cand["feedback"].id),)


def test_snapshot_for_an_ineligible_candidate_keeps_the_score_and_no_rank(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [5]}, overall=10.0, eligible=False)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    v = _decide(db, cand, _user(db), "REJECT")
    assert v.entry_status == "NOT_RANKED_INELIGIBLE"
    assert v.final_rank is None and v.final_score == D("10.00")
    assert v.decision == "REJECT"                      # the human's call, recorded


def test_snapshot_for_an_incomplete_candidate_has_no_score(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: []}, overall=9.0)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    v = _decide(db, cand, _user(db), "HOLD")
    assert v.entry_status == "INCOMPLETE_INTERVIEW"
    assert v.final_score is None and v.final_rank is None and v.final_confidence is None
    assert v.final_ranking_entry_id is not None        # the entry still exists


def test_the_snapshot_is_not_updated_when_the_evidence_later_changes(db):
    w, cand, mgr = _setup(db)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    v = _decide(db, cand, mgr)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    again = get_current_final_decision(db, cand["app"].id, acting_user_id=mgr.id)
    assert again.final_ranking_entry_id == v.final_ranking_entry_id


# =====================================================================
# Staleness
# =====================================================================


def test_a_fresh_decision_is_not_stale_and_no_decision_is_not_stale(db):
    w, cand, mgr = _setup(db)
    assert not get_final_decision_staleness(db, cand["app"].id, acting_user_id=mgr.id).is_stale
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    _decide(db, cand, mgr)
    st = get_final_decision_staleness(db, cand["app"].id, acting_user_id=mgr.id)
    assert st.is_stale is False and st.reasons == ()


def test_regenerating_the_final_ranking_makes_a_decision_stale(db):
    w, cand, mgr = _setup(db)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    _decide(db, cand, mgr)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    st = get_final_decision_staleness(db, cand["app"].id, acting_user_id=mgr.id)
    assert st.is_stale and st.ranking_changed and not st.analysis_changed
    assert "final ranking was regenerated" in " ".join(st.reasons)


def test_a_new_analysis_makes_a_decision_stale(db):
    w, cand, mgr = _setup(db)
    _decide(db, cand, mgr)
    cand["analysis"].status = "SUPERSEDED"
    cand["analysis"].superseded_at = datetime.now(timezone.utc)
    _analysis(db, w, cand["app"], cand["guide"], cand["feedback"], confidence="LOW")
    st = get_final_decision_staleness(db, cand["app"].id, acting_user_id=mgr.id)
    assert st.is_stale and st.analysis_changed


def test_additional_feedback_makes_a_decision_stale(db):
    w, cand, mgr = _setup(db)
    _decide(db, cand, mgr)
    create_interview_feedback(
        db, user_id=w["hr"].id, application_id=cand["app"].id,
        interview_guide_id=cand["guide"].id, interview_round=2,
        recommendation="HOLD", notes="n" * 50, ratings=[],
    )
    st = get_final_decision_staleness(db, cand["app"].id, acting_user_id=mgr.id)
    assert st.is_stale and st.new_feedback_count == 1
    assert "1 interview feedback record(s) were added" in " ".join(st.reasons)


def test_a_revision_clears_the_stale_flag(db):
    w, cand, mgr = _setup(db)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    _decide(db, cand, mgr)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    assert get_final_decision_staleness(db, cand["app"].id, acting_user_id=mgr.id).is_stale
    _decide(db, cand, mgr, "HOLD")
    assert not get_final_decision_staleness(db, cand["app"].id, acting_user_id=mgr.id).is_stale


# =====================================================================
# The race backstop
# =====================================================================


def test_a_lost_race_is_a_calm_conflict_with_no_partial_state(db, mocker):
    """Simulate "someone else just recorded one": the service does not see the
    existing CURRENT row, tries to insert a second, and the partial unique index
    refuses it. Everything — including any supersede — must roll back."""
    w, cand, mgr = _setup(db)
    first = _decide(db, cand, mgr, "PROCEED")
    mocker.patch.object(fds, "_current_decision_row", return_value=None)

    with pytest.raises(FinalDecisionConflictError, match="Another decision was recorded"):
        _decide(db, cand, mgr, "REJECT")

    mocker.stopall()
    rows = _rows(db, cand)
    assert len(rows) == 1 and rows[0].id == first.decision_id
    assert rows[0].status == "CURRENT" and rows[0].decision == "PROCEED"
    assert len(_events(db, cand)) == 1                      # no event for the loser
    # and the application can still be decided afterwards
    assert _decide(db, cand, mgr, "HOLD").decision == "HOLD"


# =====================================================================
# Provenance safety: nothing here blocks Step 9 or Step 10b regeneration
# =====================================================================


def test_regenerating_the_final_ranking_still_works_after_a_decision(db):
    w, cand, mgr = _setup(db)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    v = _decide(db, cand, mgr)
    (second,) = generate_final_ranking(
        db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)     # no IntegrityError
    assert second.status == "CURRENT"
    # the decision's snapshot still points at the (now superseded) entry, intact
    assert db.execute(text("SELECT count(*) FROM final_ranking_entries WHERE id = :i"),
                      {"i": v.final_ranking_entry_id}).scalar_one() == 1


def test_regenerating_the_post_interview_analysis_still_works_after_a_decision(db, mocker):
    from app.services import post_interview_service as pis
    from tests.test_post_interview_service import (
        _assessment, _feedback, _patch, _seed,
    )

    s = _seed(db)
    _feedback(db, s)
    _patch(mocker, _assessment())
    first = pis.create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id)
    mgr = _user(db)
    v = record_final_decision(
        db, application_id=s["app"].id, decision="PROCEED", rationale=_WHY,
        acting_user_id=mgr.id,
    )
    assert v.post_interview_analysis_id == first.id

    second = pis.create_post_interview_analysis(
        db, user_id=s["hr"].id, application_id=s["app"].id, force=True)
    assert second.id != first.id
    # the decision still points at the superseded (kept, never deleted) analysis
    assert db.execute(text("SELECT count(*) FROM post_interview_analyses WHERE id = :i"),
                      {"i": first.id}).scalar_one() == 1
    st = get_final_decision_staleness(db, s["app"].id, acting_user_id=mgr.id)
    assert st.is_stale and st.analysis_changed


# =====================================================================
# Audit
# =====================================================================


def test_exactly_one_audit_event_per_decision_with_the_exact_fields(db):
    w, cand, mgr = _setup(db)
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    v = _decide(db, cand, mgr, "PROCEED")
    (event,) = _events(db, cand)
    assert event.user_id == mgr.id
    assert event.entity_type == "application" and event.entity_id == cand["app"].id
    assert event.new_state == {
        "application_id": str(cand["app"].id),
        "job_id": str(w["job"].id),
        "final_decision_id": str(v.decision_id),
        "decision": "PROCEED",
        "previous_decision_id": None,
        "previous_decision": None,
        "is_revision": False,
        "final_ranking_entry_id": str(v.final_ranking_entry_id),
        "post_interview_analysis_id": str(cand["analysis"].id),
        "rationale_length": len(_WHY),
        "feedback_count": 1,
    }


def test_a_revision_event_records_the_previous_decision(db):
    w, cand, mgr = _setup(db)
    first = _decide(db, cand, mgr, "PROCEED")
    second = _decide(db, cand, mgr, "REJECT", "A different and longer rationale.")
    events = {e.new_state["final_decision_id"]: e.new_state for e in _events(db, cand)}
    meta = events[str(second.decision_id)]
    assert meta["is_revision"] is True
    assert meta["previous_decision_id"] == str(first.decision_id)
    assert meta["previous_decision"] == "PROCEED"
    assert meta["rationale_length"] == len("A different and longer rationale.")
    assert events[str(first.decision_id)]["is_revision"] is False
    assert len(events) == 2


def test_nothing_sensitive_reaches_the_audit_trail_logs_or_other_tables(db, caplog):
    w = _job(db)
    cand = _candidate(db, w, name=_SENT_CANDIDATE, rounds={1: [4]})
    mgr = _user(db, name=_SENT_DECIDER)
    with caplog.at_level(logging.DEBUG):
        _decide(db, cand, mgr, "REJECT", _SENT_RATIONALE)
        _decide(db, cand, mgr, "HOLD", _SENT_RATIONALE + " revised")

    blob = "".join(
        f"{e.action}{e.entity_type}{e.previous_state}{e.new_state}{e.event_metadata}"
        for e in db.execute(select(AuditEvent).where(
            AuditEvent.entity_id == cand["app"].id)).scalars()
    )
    others = ""
    for table in ("final_ranking_entries", "final_rankings", "post_interview_analyses"):
        others += str(db.execute(text(f"SELECT * FROM {table}")).all()[-3:])
    for needle in ("RATIONALESENTINEL", _SENT_CANDIDATE, _SENT_DECIDER, "Zyxwvuts",
                   "Qwertyuiop"):
        assert needle not in blob, ("audit", needle)
        assert needle not in caplog.text, ("log", needle)
    assert "RATIONALESENTINEL" not in others
    # ...but the rationale IS stored, once, on its own row
    assert any("RATIONALESENTINEL" in r.rationale for r in _rows(db, cand))


def test_error_messages_never_contain_the_rationale(db):
    w, cand, mgr = _setup(db)
    hr = _user(db, UserRole.HR)
    with pytest.raises(FinalDecisionPermissionError) as exc:
        _decide(db, cand, hr, "PROCEED", _SENT_RATIONALE)
    assert "RATIONALESENTINEL" not in str(exc.value)
    with pytest.raises(FinalDecisionValidationError) as exc2:
        _decide(db, cand, mgr, "MAYBE", _SENT_RATIONALE)
    assert "RATIONALESENTINEL" not in str(exc2.value)


def test_no_event_is_written_for_a_rejected_attempt(db):
    w, cand, mgr = _setup(db)
    hr = _user(db, UserRole.HR)
    for user, decision in ((hr, "PROCEED"), (mgr, "BAD")):
        with pytest.raises(Exception):
            _decide(db, cand, user, decision)
    assert _events(db, cand) == []


# =====================================================================
# No side effects
# =====================================================================


def test_recording_a_reject_changes_nothing_else(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    other = _candidate(db, w, rounds={1: [3]})
    generate_final_ranking(db, job_id=w["job"].id, requested_by_user_id=w["hr"].id)
    mgr = _user(db)

    def snapshot():
        return {
            "status": db.get(Application, cand["app"].id).status,
            "other_status": db.get(Application, other["app"].id).status,
            "shortlist": db.execute(text(
                "SELECT is_shortlisted FROM candidate_shortlist_entries "
                "WHERE application_id = :a"), {"a": cand["app"].id}).scalar_one(),
            "rank_rows": db.execute(text(
                "SELECT count(*), max(rank_position) FROM candidate_rankings "
                "WHERE job_id = :j"), {"j": w["job"].id}).one(),
            "final_rows": db.execute(text(
                "SELECT count(*), count(*) FILTER (WHERE status='CURRENT') "
                "FROM final_rankings WHERE job_id = :j"), {"j": w["job"].id}).one(),
            "entries": db.execute(text(
                "SELECT count(*) FROM final_ranking_entries")).scalar_one(),
            "other_events": db.execute(select(func.count(AuditEvent.id)).where(
                AuditEvent.event_type != AuditEventType.FINAL_DECISION_SUBMITTED.value,
                AuditEvent.entity_id.in_([cand["app"].id, w["job"].id]))).scalar_one(),
        }

    before = snapshot()
    _decide(db, cand, mgr, "REJECT")
    assert snapshot() == before


# =====================================================================
# Table constraints
# =====================================================================


def _raw(db, cand, user, status):
    row = FinalDecision(
        application_id=cand["app"].id, decided_by_user_id=user.id, decision="HOLD",
        rationale="a rationale", status=status, interview_feedback_ids=[],
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    return row


def test_two_current_rows_for_one_application_fail_in_the_database(db):
    w, cand, mgr = _setup(db)
    _raw(db, cand, mgr, "CURRENT")
    db.flush()
    _raw(db, cand, mgr, "CURRENT")
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.flush()


def test_many_superseded_rows_may_coexist_with_one_current(db):
    w, cand, mgr = _setup(db)
    for _ in range(3):
        _raw(db, cand, mgr, "SUPERSEDED")
    _raw(db, cand, mgr, "CURRENT")
    db.flush()


def test_foreign_keys_are_restrict_and_point_only_at_never_deleted_tables(db):
    inspector = sa_inspect(db.get_bind())
    fks = {
        fk["constrained_columns"][0]: (fk["referred_table"], fk["options"].get("ondelete"))
        for fk in inspector.get_foreign_keys("final_decisions")
    }
    assert fks == {
        "application_id": ("applications", "RESTRICT"),
        "decided_by_user_id": ("users", "RESTRICT"),
        "final_ranking_entry_id": ("final_ranking_entries", "RESTRICT"),
        "post_interview_analysis_id": ("post_interview_analyses", "RESTRICT"),
    }
    assert "candidate_rankings" not in {t for t, _ in fks.values()}


def test_a_decided_application_cannot_be_deleted(db):
    w, cand, mgr = _setup(db)
    _decide(db, cand, mgr)
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.execute(text("DELETE FROM applications WHERE id = :i"), {"i": cand["app"].id})


def test_status_vocabulary_is_a_validated_string_not_an_enum():
    assert FinalDecisionStatus.ALL == {"CURRENT", "SUPERSEDED"}
    assert FinalDecision.__table__.c.status.type.__class__.__name__ == "String"
    assert FinalDecision.__table__.c.decision.type.__class__.__name__ == "String"
    assert FinalDecision.__table__.c.created_at.server_default is None   # set in Python


# =====================================================================
# Structural
# =====================================================================


def _src():
    return (_APP_DIR / "services" / "final_decision_service.py").read_text(encoding="utf-8")


def test_the_service_imports_no_ai_and_no_notification_code():
    tree = ast.parse(_src())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = " ".join([getattr(node, "module", None) or ""] +
                             [a.name for a in node.names]).lower()
            for banned in ("app.ai", "claude", "anthropic", "smtplib", "email",
                           "notification", "notify", "sendgrid", "twilio", "requests"):
                assert banned not in names, (banned, names)


def test_the_service_never_writes_application_status_shortlist_or_ranking():
    """AST-based (docstrings may talk about shortlists; code must not touch them)."""
    tree = ast.parse(_src())
    banned = {
        "ApplicationStatus", "CandidateShortlistEntry", "CandidateRanking",
        "shortlist_candidate", "unshortlist_candidate", "generate_final_ranking",
        "generate_ranking", "generate_interview_guide",
    }
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            used.update(a.name for a in node.names)
    assert used.isdisjoint(banned), used & banned
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"delete", "execute_delete"}, ast.dump(node)
        # the only .status ever assigned is the previous DECISION's, superseded
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "status":
                    assert isinstance(target.value, ast.Name)
                    assert target.value.id == "previous", ast.dump(node)


def test_no_disagreement_logic_and_the_audit_member_is_unemitted_elsewhere():
    src = _src()
    assert "AI_HUMAN_DISAGREEMENT_DETECTED" not in src
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef):
            assert "disagree" not in node.name.lower() and "compare" not in node.name.lower()
    emitters = [
        p.name for p in _APP_DIR.rglob("*.py")
        if "AuditEventType.AI_HUMAN_DISAGREEMENT_DETECTED" in p.read_text(encoding="utf-8")
    ]
    assert emitters == []


def test_final_decision_submitted_is_emitted_only_by_this_service():
    users = sorted(
        p.name for p in _APP_DIR.rglob("*.py")
        if "AuditEventType.FINAL_DECISION_SUBMITTED" in p.read_text(encoding="utf-8")
    )
    assert users == ["final_decision_service.py"]


def test_nothing_under_app_ai_knows_about_decisions():
    for p in (_APP_DIR / "ai").rglob("*.py"):
        src = p.read_text(encoding="utf-8").lower()
        # (the bare word "rationale" appears in unrelated prompt comments)
        assert "final_decision" not in src and "finaldecision" not in src, p.name


def test_the_rationale_never_reaches_a_logger_call():
    for node in ast.walk(ast.parse(_src())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"):
            dumped = ast.dump(node)
            assert "rationale" not in dumped.lower(), dumped


def test_record_signature_has_no_ai_or_notification_parameters():
    params = set(inspect.signature(record_final_decision).parameters)
    assert params == {"db", "application_id", "decision", "rationale", "acting_user_id"}


def test_the_only_caller_of_record_final_decision_is_the_interviews_page_button():
    callers = [
        str(p.relative_to(_APP_DIR)).replace("\\", "/")
        for p in _APP_DIR.rglob("*.py")
        if "record_final_decision(" in p.read_text(encoding="utf-8")
        and p.name != "final_decision_service.py"
    ]
    assert callers == ["pages/interviews.py"]
