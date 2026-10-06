"""Tests for the candidate-page readers in app.services.job_workspace_service
(HR UI, Increment 3): ``get_candidate_header``, ``list_job_candidates``,
``list_interview_overview`` and ``list_candidate_activity``.

Real Postgres via the savepoint-rollback ``db`` fixture; data is built with the
helpers from ``test_final_ranking_service`` / ``test_job_workspace_service``. All of
these readers are READ-ONLY: row counts and the audit table are asserted unchanged,
and the module is checked structurally for writes and for any read of an audit
event's free text or metadata.
"""

from __future__ import annotations

import ast
import dataclasses
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, func, select

from app.database.models.audit_event import AuditEvent
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.final_decision import FinalDecision
from app.database.models.final_ranking import FinalRankingEntryStatus
from app.database.models.interview_feedback import InterviewFeedback
from app.database.models.interview_transcript import (
    InterviewTranscript,
    InterviewTranscriptStatus,
)
from app.database.models.post_interview_analysis import PostInterviewAnalysis
from app.services.job_workspace_service import (
    ActivityItem,
    CandidateHeader,
    InterviewOverviewRow,
    JobCandidate,
    get_candidate_header,
    list_candidate_activity,
    list_interview_overview,
    list_job_candidates,
)
from app.utils.authorization import UnauthorizedError
from tests.test_final_ranking_service import _candidate, _generate, _hr, _job
from tests.test_job_workspace_service import _decide, _manager

_SERVICE = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "job_workspace_service.py"
)


def _header(db, w, cand, user=None):
    return get_candidate_header(
        db, w["job"].id, cand["app"].id, acting_user_id=(user or w["hr"]).id
    )


@pytest.fixture
def complete(db):
    """One candidate with everything: ranking, shortlist, a rated round, an
    analysis, a CURRENT final ranking and a CURRENT decision."""
    w = _job(db)
    cand = _candidate(db, w, name="Ada Complete", rounds={1: [4, 5]}, overall=7.5)
    _generate(db, w)
    mgr = _manager(db)
    _decide(db, cand, mgr, "PROCEED")
    return {"w": w, "cand": cand, "mgr": mgr}


# --- a complete candidate --------------------------------------------------------


def test_a_complete_candidate_header(db, complete):
    h = _header(db, complete["w"], complete["cand"])
    assert isinstance(h, CandidateHeader)
    assert h.candidate_name == "Ada Complete"
    assert h.candidate_email.endswith("@x.com")
    assert h.job_code == complete["w"]["job"].job_code
    assert h.job_title == "Backend Engineer"
    assert h.application_status == "SCREENING_EVALUATED"
    assert h.screened is True and h.is_shortlisted is True
    assert h.has_current_final_ranking is True
    assert h.entry_status == FinalRankingEntryStatus.RANKED
    assert h.final_rank == 1 and h.final_ranked_count == 1
    assert h.final_score is not None and h.interview_score is not None
    assert h.screening_score == Decimal("7.5") or h.screening_score == Decimal("7.50")
    assert h.rounds_count == 1 and h.has_unrated_round is False
    assert h.transcripts_count == 0
    assert h.has_analysis is True
    assert h.decision == "PROCEED"
    assert h.decided_by_name == "Mia Manager"
    assert h.decided_at is not None


def test_scores_are_the_stored_ones_not_recomputed(db, complete):
    from app.database.models.final_ranking import FinalRankingEntry

    entry = db.execute(
        select(FinalRankingEntry).where(
            FinalRankingEntry.application_id == complete["cand"]["app"].id
        )
    ).scalar_one()
    entry.final_score = Decimal("1.23")          # deliberately not what the rules give
    db.flush()
    h = _header(db, complete["w"], complete["cand"])
    assert h.final_score == Decimal("1.23")


def test_a_transcript_counts_only_while_current(db, complete):
    fb = complete["cand"]["feedback"]
    db.add_all([
        InterviewTranscript(
            interview_feedback_id=fb.id, uploaded_by_user_id=complete["w"]["hr"].id,
            drive_file_id=f"d-{uuid.uuid4().hex}", drive_folder_id="f",
            file_name="a.pdf", mime_type="application/pdf", file_size_bytes=5,
            text_extractable=True, status=InterviewTranscriptStatus.SUPERSEDED,
        ),
        InterviewTranscript(
            interview_feedback_id=fb.id, uploaded_by_user_id=complete["w"]["hr"].id,
            drive_file_id=f"d-{uuid.uuid4().hex}", drive_folder_id="f",
            file_name="b.pdf", mime_type="application/pdf", file_size_bytes=5,
            text_extractable=True, status=InterviewTranscriptStatus.CURRENT,
        ),
    ])
    db.flush()
    assert _header(db, complete["w"], complete["cand"]).transcripts_count == 1


# --- other shapes ------------------------------------------------------------------


def test_an_ineligible_candidate_carries_the_status_and_reason(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]}, eligible=False)
    _generate(db, w)
    h = _header(db, w, cand)
    assert h.entry_status == FinalRankingEntryStatus.NOT_RANKED_INELIGIBLE
    assert h.entry_reason
    assert h.final_rank is None and h.final_ranked_count is None
    assert h.has_current_final_ranking is True


def test_a_notes_only_round_is_flagged_as_missing_ratings(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [], 2: [4]})
    _generate(db, w)
    h = _header(db, w, cand)
    assert h.rounds_count == 2 and h.has_unrated_round is True
    assert h.entry_status == FinalRankingEntryStatus.INCOMPLETE_INTERVIEW or (
        h.entry_status == FinalRankingEntryStatus.RANKED
    )


def test_a_candidate_with_no_feedback(db):
    w = _job(db)
    cand = _candidate(db, w, rounds=None)
    h = _header(db, w, cand)
    assert h.rounds_count == 0 and h.has_unrated_round is False
    assert h.transcripts_count == 0 and h.has_analysis is False
    assert h.is_shortlisted is False and h.decision is None
    assert h.decided_by_name is None and h.decided_at is None
    assert h.has_current_final_ranking is False
    assert h.final_score is None and h.final_rank is None and h.entry_status is None


def test_screening_numbers_fall_back_to_the_screening_ranking(db):
    """No final ranking yet: the screening score and rank are the stored ones."""
    w = _job(db)
    cand = _candidate(db, w, rounds=None, overall=6.25)
    h = _header(db, w, cand)
    assert h.screening_score == Decimal("6.25")
    assert h.screening_rank == 1
    assert h.has_current_final_ranking is False


def test_no_screening_ranking_means_no_screening_numbers(db):
    w = _job(db)
    cand = _candidate(db, w, rounds=None, with_ranking=False, with_analysis=False)
    h = _header(db, w, cand)
    assert h.screening_score is None and h.screening_rank is None
    assert h.screened is True            # an evaluation exists, only the ranking doesn't


def test_a_candidate_with_no_analysis(db):
    w = _job(db)
    cand = _candidate(db, w, rounds={1: [4]}, with_analysis=False)
    assert _header(db, w, cand).has_analysis is False


def test_a_superseded_analysis_is_not_counted(db, complete):
    row = db.execute(
        select(PostInterviewAnalysis).where(
            PostInterviewAnalysis.application_id == complete["cand"]["app"].id
        )
    ).scalar_one()
    row.status = "SUPERSEDED"
    db.flush()
    assert _header(db, complete["w"], complete["cand"]).has_analysis is False


def test_superseded_decisions_are_not_double_counted(db, complete):
    _decide(db, complete["cand"], complete["mgr"], "HOLD")      # supersedes PROCEED
    _decide(db, complete["cand"], complete["mgr"], "REJECT")    # supersedes HOLD
    h = _header(db, complete["w"], complete["cand"])
    assert h.decision == "REJECT"
    rows = db.execute(
        select(func.count()).select_from(FinalDecision).where(
            FinalDecision.application_id == complete["cand"]["app"].id
        )
    ).scalar_one()
    assert rows == 3                                  # history kept; header shows one


def test_an_unshortlisted_candidate_is_not_shortlisted(db, complete):
    entry = db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.application_id == complete["cand"]["app"].id
        )
    ).scalar_one()
    entry.is_shortlisted = False
    db.flush()
    assert _header(db, complete["w"], complete["cand"]).is_shortlisted is False


# --- scoping and unknowns ---------------------------------------------------------


def test_an_application_of_another_job_returns_none(db, complete):
    other = _job(db)
    assert get_candidate_header(
        db, other["job"].id, complete["cand"]["app"].id,
        acting_user_id=other["hr"].id,
    ) is None


@pytest.mark.parametrize("bad", [uuid.uuid4(), "not-a-uuid", "", "123", None])
def test_an_unknown_or_malformed_application_returns_none(db, complete, bad):
    assert get_candidate_header(
        db, complete["w"]["job"].id, bad, acting_user_id=complete["w"]["hr"].id
    ) is None


@pytest.mark.parametrize("bad", [uuid.uuid4(), "not-a-uuid", None])
def test_an_unknown_or_malformed_job_returns_none(db, complete, bad):
    assert get_candidate_header(
        db, bad, complete["cand"]["app"].id, acting_user_id=complete["w"]["hr"].id
    ) is None


def test_string_ids_work(db, complete):
    h = get_candidate_header(
        db, str(complete["w"]["job"].id), str(complete["cand"]["app"].id),
        acting_user_id=str(complete["w"]["hr"].id),
    )
    assert h is not None and h.candidate_name == "Ada Complete"


def test_the_header_is_immutable(db, complete):
    h = _header(db, complete["w"], complete["cand"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        h.decision = "HOLD"                           # type: ignore[misc]


# --- guards (all four readers) -----------------------------------------------------

_READERS = {
    "header": lambda db, w, c, who: get_candidate_header(
        db, w["job"].id, c["app"].id, acting_user_id=who),
    "candidates": lambda db, w, c, who: list_job_candidates(
        db, w["job"].id, acting_user_id=who),
    "overview": lambda db, w, c, who: list_interview_overview(
        db, w["job"].id, acting_user_id=who),
    "activity": lambda db, w, c, who: list_candidate_activity(
        db, c["app"].id, acting_user_id=who),
}


@pytest.mark.parametrize("reader", sorted(_READERS))
@pytest.mark.parametrize("who", [None, "not-a-uuid", "unknown"])
def test_a_missing_malformed_or_unknown_user_is_refused(db, complete, reader, who):
    acting = uuid.uuid4() if who == "unknown" else who
    with pytest.raises(UnauthorizedError):
        _READERS[reader](db, complete["w"], complete["cand"], acting)


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_an_inactive_user_is_refused(db, complete, reader):
    complete["w"]["hr"].is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _READERS[reader](db, complete["w"], complete["cand"], complete["w"]["hr"].id)


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_every_internal_role_may_read(db, complete, reader):
    for user in (complete["mgr"], _hr(db)):
        _READERS[reader](db, complete["w"], complete["cand"], user.id)


# --- query budget ------------------------------------------------------------------


def _count_statements(db, fn):
    seen: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    engine = db.get_bind().engine if hasattr(db.get_bind(), "engine") else db.get_bind()
    event.listen(engine, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before)
    return seen


def test_the_header_costs_a_handful_of_queries(db, complete):
    job_id, app_id = complete["w"]["job"].id, complete["cand"]["app"].id
    user_id = complete["w"]["hr"].id
    db.expire_all()           # ids are captured above, so only the reader's SQL counts
    seen = _count_statements(
        db,
        lambda: get_candidate_header(db, job_id, app_id, acting_user_id=user_id),
    )
    # guard + application + final entry + ranked count + decision
    assert 1 <= len(seen) <= 6, [s[:80] for s in seen]
    assert all(s.lstrip().upper().startswith("SELECT") for s in seen)


# --- read-only ------------------------------------------------------------------------


def _row_counts(db):
    from app.database.models.application import Application

    return {
        m.__name__: db.execute(select(func.count()).select_from(m)).scalar_one()
        for m in (
            Application, AuditEvent, FinalDecision, PostInterviewAnalysis,
            InterviewFeedback, CandidateShortlistEntry,
        )
    }


def test_no_reader_writes_a_row_or_an_audit_event(db, complete):
    before = _row_counts(db)
    for name in sorted(_READERS):
        _READERS[name](db, complete["w"], complete["cand"], complete["w"]["hr"].id)
    assert _row_counts(db) == before


def test_the_module_has_no_write_calls_and_never_reads_audit_free_text():
    tree = ast.parse(_SERVICE.read_text(encoding="utf-8"))
    forbidden_calls = {"add", "add_all", "delete", "merge", "commit", "flush",
                       "record_event", "insert", "update"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in forbidden_calls, f"write-like call: {name}"
    source = _SERVICE.read_text(encoding="utf-8")
    for column in ("event_metadata", "previous_state", "new_state", "AuditEvent.action"):
        assert column not in source, column
    assert "anthropic" not in source.lower() and "claude" not in source.lower().replace(
        "claude code", ""
    )


# --- list_job_candidates ---------------------------------------------------------------


def test_candidates_are_ordered_by_screening_rank_then_applied_time(db):
    w = _job(db)
    first = _candidate(db, w, name="Applied first", rounds=None, minutes=0,
                       eligible=False)           # rank None
    second = _candidate(db, w, name="Applied second", rounds=None, minutes=5,
                        eligible=False)           # rank None, later
    ranked = _candidate(db, w, name="Ranked", rounds=None, minutes=9)   # rank 1
    got = list_job_candidates(db, w["job"].id, acting_user_id=w["hr"].id)
    assert [c.candidate_name for c in got] == [
        "Ranked", "Applied first", "Applied second"
    ]
    assert got[0].screening_rank == 1 and got[1].screening_rank is None
    assert all(isinstance(c, JobCandidate) for c in got)
    assert {c.application_id for c in got} == {
        first["app"].id, second["app"].id, ranked["app"].id
    }


def test_candidates_never_include_another_jobs_applications(db, complete):
    other = _job(db)
    _candidate(db, other, name="Elsewhere", rounds=None)
    got = list_job_candidates(
        db, complete["w"]["job"].id, acting_user_id=complete["w"]["hr"].id
    )
    assert [c.candidate_name for c in got] == ["Ada Complete"]


@pytest.mark.parametrize("bad", [uuid.uuid4(), "not-a-uuid", None])
def test_candidates_of_an_unknown_job_are_empty(db, bad):
    hr = _hr(db)
    assert list_job_candidates(db, bad, acting_user_id=hr.id) == []


# --- list_interview_overview -------------------------------------------------------------


def test_the_interview_overview_rows(db):
    w = _job(db)
    rated = _candidate(db, w, name="Rated", rounds={1: [4, 5]}, minutes=0)
    notes_only = _candidate(db, w, name="Notes only", rounds={1: []}, minutes=1,
                            with_analysis=False)
    _candidate(db, w, name="Not interviewed", rounds=None, minutes=2)
    mgr = _manager(db)
    _decide(db, rated, mgr, "HOLD")
    rows = list_interview_overview(db, w["job"].id, acting_user_id=w["hr"].id)
    by_name = {r.candidate_name: r for r in rows}
    assert set(by_name) == {"Rated", "Notes only"}           # not-interviewed is omitted
    assert all(isinstance(r, InterviewOverviewRow) for r in rows)
    r = by_name["Rated"]
    assert (r.rounds_count, r.has_unrated_round, r.transcripts_count) == (1, False, 0)
    assert r.has_analysis is True and r.decision == "HOLD"
    n = by_name["Notes only"]
    assert n.has_unrated_round is True and n.has_analysis is False and n.decision is None


def test_the_interview_overview_is_set_based(db):
    w = _job(db)
    for i in range(4):
        _candidate(db, w, rounds={1: [3]}, minutes=i)
    db.expire_all()
    seen = _count_statements(
        db,
        lambda: list_interview_overview(db, w["job"].id, acting_user_id=w["hr"].id),
    )
    assert len(seen) <= 12, len(seen)       # constant in the number of candidates


# --- list_candidate_activity -------------------------------------------------------------


def test_activity_lists_events_about_this_application_only(db, complete):
    other = _candidate(db, complete["w"], name="Someone else", rounds={1: [3]})
    _decide(db, other, complete["mgr"], "HOLD")
    items = list_candidate_activity(
        db, complete["cand"]["app"].id, acting_user_id=complete["w"]["hr"].id
    )
    assert items and all(isinstance(i, ActivityItem) for i in items)
    types = [i.event_type for i in items]
    assert "FINAL_DECISION_SUBMITTED" in types
    assert types.count("FINAL_DECISION_SUBMITTED") == 1          # not the other one's
    stamps = [i.timestamp for i in items]
    assert stamps == sorted(stamps, reverse=True)
    decided = next(i for i in items if i.event_type == "FINAL_DECISION_SUBMITTED")
    assert decided.actor_name == "Mia Manager"


def test_activity_exposes_only_type_time_and_actor(db, complete):
    item = list_candidate_activity(
        db, complete["cand"]["app"].id, acting_user_id=complete["w"]["hr"].id
    )[0]
    assert [f.name for f in dataclasses.fields(item)] == [
        "event_type", "timestamp", "actor_name"
    ]


def test_activity_respects_the_limit(db, complete):
    items = list_candidate_activity(
        db, complete["cand"]["app"].id, acting_user_id=complete["w"]["hr"].id,
        limit=1,
    )
    assert len(items) == 1


@pytest.mark.parametrize("bad", [uuid.uuid4(), "not-a-uuid", None])
def test_activity_of_an_unknown_application_is_empty(db, bad):
    hr = _hr(db)
    assert list_candidate_activity(db, bad, acting_user_id=hr.id) == []


def test_the_module_reads_nothing_from_a_transcript_but_a_count():
    """The transcript guard (tests/test_post_interview_analysis_all_rounds.py) lets
    this module import the transcript MODEL only to count CURRENT rows. Pin that:
    the only transcript attributes it touches are the id, the status and the link
    to its feedback round — never a file name, a Drive id or any text."""
    tree = ast.parse(_SERVICE.read_text(encoding="utf-8"))
    touched = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "InterviewTranscript"
    }
    assert touched <= {"id", "status", "interview_feedback_id"}, touched
    assert "download" not in _SERVICE.read_text(encoding="utf-8").lower()
