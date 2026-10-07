"""Shortlist rubric version, ranking drift and the one-statement Shortlist read
(HR UI, Increment 5). Real Postgres via the savepoint-rollback ``db`` fixture.

Covers ``job_workspace_service.list_shortlist_overview`` and the shortlist fields that
``get_candidate_header`` now carries.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from sqlalchemy import event, func, select

from app.database.models.audit_event import AuditEvent
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.rubric import RubricVersionStatus
from app.services.job_workspace_service import (
    ShortlistOverviewRow,
    get_candidate_header,
    list_shortlist_overview,
)
from app.utils.authorization import UnauthorizedError
from app.utils.ranking_drift import ranking_drift
from tests.test_final_ranking_service import _candidate, _hr, _job, _rubric


def _rows(db, w, user=None):
    return list_shortlist_overview(db, w["job"].id, acting_user_id=(user or w["hr"]).id)


def _entry(db, cand):
    return db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.application_id == cand["app"].id
        )
    ).scalar_one()


def _header(db, w, cand):
    return get_candidate_header(
        db, w["job"].id, cand["app"].id, acting_user_id=w["hr"].id
    )


def _statements(db, fn):
    seen: list[str] = []

    def hook(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    engine = db.get_bind().engine if hasattr(db.get_bind(), "engine") else db.get_bind()
    event.listen(engine, "before_cursor_execute", hook)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    return seen


# --- list_shortlist_overview ----------------------------------------------------------


def test_a_shortlisted_candidate_row_carries_everything_the_stage_shows(db):
    w = _job(db)
    cand = _candidate(db, w, name="Ada Lovelace", rounds={1: [4, 3], 2: [5]})
    (row,) = _rows(db, w)
    assert isinstance(row, ShortlistOverviewRow)
    assert row.application_id == cand["app"].id and row.candidate_name == "Ada Lovelace"
    assert (row.rubric_version_number, row.rubric_version_status) == (1, "APPROVED")
    assert row.rank_position_at_decision == 1
    assert row.current_rank_position == 1 and row.current_rank_available is True
    assert row.guide_exists is True and row.rounds_count == 2


def test_a_candidate_who_is_not_shortlisted_is_not_listed(db):
    w = _job(db)
    kept = _candidate(db, w, name="Kept", rounds={1: [4]})
    gone = _candidate(db, w, name="Gone", rounds={1: [4]})
    _entry(db, gone).is_shortlisted = False
    db.flush()
    assert [r.application_id for r in _rows(db, w)] == [kept["app"].id]


def test_another_jobs_shortlist_is_never_listed(db):
    w1, w2 = _job(db), _job(db)
    _candidate(db, w1, name="Mine", rounds={1: [4]})
    _candidate(db, w2, name="Theirs", rounds={1: [4]})
    assert [r.candidate_name for r in _rows(db, w1)] == ["Mine"]


def test_a_moved_rank_reads_as_a_caution_through_the_helper(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    cand["ranking"].rank_position = 3
    db.flush()
    (row,) = _rows(db, w)
    d = ranking_drift(
        row.current_rank_available, row.current_rank_position,
        row.rank_position_at_decision,
    )
    assert d.caution and d.text == "Ranking changed — shortlisted at #1, now #3"


def test_no_ranking_row_for_the_version_is_not_available(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    db.delete(cand["ranking"])
    db.flush()
    (row,) = _rows(db, w)
    assert row.current_rank_available is False and row.current_rank_position is None


def test_a_ranking_row_without_a_position_is_available_but_unranked(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]}, eligible=False)
    (row,) = _rows(db, w)
    assert row.current_rank_available is True and row.current_rank_position is None
    assert cand["ranking"].rank_position is None


def test_a_candidate_with_no_guide_and_no_rounds_says_so(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={})
    db.delete(cand["guide"])
    db.flush()
    (row,) = _rows(db, w)
    assert row.guide_exists is False and row.rounds_count == 0


def test_the_rank_comes_from_the_candidates_own_rubric_version_only(db):
    w = _job(db)
    rv2 = _rubric(db, w["job"], w["hr"], number=2)
    old = _candidate(db, w, name="On v1", rounds={1: [4]})
    new = _candidate(db, w, name="On v2", rubric=rv2, rounds={1: [4]})
    # A ranking row for the OTHER version must never be picked up
    db.add(CandidateRanking(
        job_id=w["job"].id, rubric_version_id=rv2.id, application_id=old["app"].id,
        rank_position=9, overall_score=1, eligible=True, mandatory_unknown_flag=False,
        generated_at=old["ranking"].generated_at, generation_batch_id=uuid.uuid4(),
    ))
    db.delete(old["ranking"])
    db.flush()
    rows = {r.candidate_name: r for r in _rows(db, w)}
    assert rows["On v1"].current_rank_available is False       # not the v2 row's 9
    assert rows["On v1"].rubric_version_number == 1
    assert rows["On v2"].rubric_version_number == 2 and rows["On v2"].current_rank_position == 1
    assert new["app"].id == rows["On v2"].application_id


def test_rows_are_ordered_by_version_then_current_rank(db):
    w = _job(db)
    rv2 = _rubric(db, w["job"], w["hr"], number=2)
    b = _candidate(db, w, name="B v2 first", rubric=rv2, rounds={1: [4]})
    a = _candidate(db, w, name="A v1 second", rounds={1: [4]})
    c = _candidate(db, w, name="C v1 first", rounds={1: [4]})
    a["ranking"].rank_position, c["ranking"].rank_position = 2, 1
    b["ranking"].rank_position = 1
    db.flush()
    assert [r.candidate_name for r in _rows(db, w)] == [
        "C v1 first", "A v1 second", "B v2 first",
    ]


def test_a_superseded_version_keeps_its_status(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    w["rubric"].status = RubricVersionStatus.SUPERSEDED
    db.flush()
    (row,) = _rows(db, w)
    assert row.rubric_version_status == "SUPERSEDED"
    assert cand["app"].id == row.application_id


# --- guard, malformed input, read-only ------------------------------------------------


def test_an_inactive_user_is_refused(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    hr = _hr(db)
    hr.is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _rows(db, w, hr)


def test_an_unknown_user_is_refused(db):
    w = _job(db)
    with pytest.raises(UnauthorizedError):
        list_shortlist_overview(db, w["job"].id, acting_user_id=uuid.uuid4())


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "123"])
def test_a_malformed_job_id_gives_no_rows(db, bad):
    hr = _hr(db)
    assert list_shortlist_overview(db, bad, acting_user_id=hr.id) == []


def test_an_empty_shortlist_gives_no_rows(db):
    assert _rows(db, _job(db)) == []


def test_the_rows_are_immutable(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    (row,) = _rows(db, w)
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.candidate_name = "x"


def test_reading_writes_nothing_and_emits_no_audit_event(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    before = db.execute(select(func.count(AuditEvent.id))).scalar_one()
    _rows(db, w)
    db.flush()
    assert db.execute(select(func.count(AuditEvent.id))).scalar_one() == before


# --- the query count does not grow with the number of rows -------------------------------


def test_the_shortlist_read_costs_the_same_for_two_rows_and_for_twelve(db):
    w = _job(db)
    for i in range(2):
        _candidate(db, w, name=f"Few {i}", rounds={1: [4]})
    db.flush()
    hr_id, job_id = w["hr"].id, w["job"].id
    db.expire_all()
    few = _statements(
        db, lambda: list_shortlist_overview(db, job_id, acting_user_id=hr_id)
    )
    for i in range(10):
        _candidate(db, w, name=f"More {i}", rounds={1: [4]})
    db.flush()
    db.expire_all()
    many = _statements(
        db, lambda: list_shortlist_overview(db, job_id, acting_user_id=hr_id)
    )
    assert len(list_shortlist_overview(db, job_id, acting_user_id=hr_id)) == 12
    assert len(few) == len(many) == 2, ([s[:60] for s in few], [s[:60] for s in many])
    assert all(s.lstrip().upper().startswith("SELECT") for s in many)


# --- get_candidate_header: the shortlist context ------------------------------------------


def test_the_header_carries_the_shortlist_context(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    h = _header(db, w, cand)
    assert h.is_shortlisted is True
    assert (h.shortlist_rubric_version_number, h.shortlist_rubric_version_status) == (1, "APPROVED")
    assert h.rank_position_at_shortlisting == 1
    assert h.current_rank_position == 1 and h.current_rank_available is True


def test_the_header_shows_a_moved_rank(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    cand["ranking"].rank_position = 4
    db.flush()
    h = _header(db, w, cand)
    assert ranking_drift(
        h.current_rank_available, h.current_rank_position, h.rank_position_at_shortlisting
    ).text == "Ranking changed — shortlisted at #1, now #4"


def test_the_header_with_no_ranking_row_is_not_available(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    db.delete(cand["ranking"])
    db.flush()
    h = _header(db, w, cand)
    assert h.current_rank_available is False and h.current_rank_position is None


def test_a_candidate_who_is_not_shortlisted_has_no_shortlist_context(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    _entry(db, cand).is_shortlisted = False
    db.flush()
    h = _header(db, w, cand)
    assert h.is_shortlisted is False
    assert h.shortlist_rubric_version_number is None
    assert h.shortlist_rubric_version_status is None
    assert h.rank_position_at_shortlisting is None
    assert h.current_rank_position is None and h.current_rank_available is False


def test_a_candidate_who_was_never_shortlisted_has_no_shortlist_context(db):
    w = _job(db)
    cand = _candidate(db, w, rounds=None)
    h = _header(db, w, cand)
    assert h.is_shortlisted is False and h.shortlist_rubric_version_number is None


def test_the_header_reads_the_candidates_own_rubric_version(db):
    w = _job(db)
    rv2 = _rubric(db, w["job"], w["hr"], number=2)
    cand = _candidate(db, w, rubric=rv2, rounds={1: [4]})
    h = _header(db, w, cand)
    assert h.shortlist_rubric_version_number == 2


def test_the_header_never_reads_a_ranking_row_from_another_rubric_version(db):
    w = _job(db)
    rv2 = _rubric(db, w["job"], w["hr"], number=2)
    cand = _candidate(db, w, rounds={1: [4]})              # shortlisted on v1
    db.add(CandidateRanking(
        job_id=w["job"].id, rubric_version_id=rv2.id, application_id=cand["app"].id,
        rank_position=9, overall_score=1, eligible=True, mandatory_unknown_flag=False,
        generated_at=cand["ranking"].generated_at, generation_batch_id=uuid.uuid4(),
    ))
    db.delete(cand["ranking"])                             # nothing for v1 any more
    db.flush()
    h = _header(db, w, cand)
    assert h.current_rank_available is False and h.current_rank_position is None


def test_the_shortlist_context_adds_no_statement_to_the_header(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]})
    job_id, app_id, uid = w["job"].id, cand["app"].id, w["hr"].id
    db.expire_all()
    seen = _statements(
        db, lambda: get_candidate_header(db, job_id, app_id, acting_user_id=uid)
    )
    assert 1 <= len(seen) <= 6, [s[:80] for s in seen]
