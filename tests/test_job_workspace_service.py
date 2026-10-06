"""Tests for app.services.job_workspace_service (HR UI, Increment 2).

A READ-ONLY stage summary: counts per stage, a tick flag and an attention number.
Real Postgres via the savepoint-rollback ``db`` fixture; jobs, candidates, rounds
and decisions are built with the helpers from ``test_final_ranking_service``. Every
assertion is about rows this test created.
"""

from __future__ import annotations

import ast
import dataclasses
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database.models.audit_event import AuditEvent
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.document import Document
from app.database.models.job import JdInputMethod
from app.database.models.user import UserRole
from app.services.auth_service import create_user
from app.services.final_decision_service import record_final_decision
from app.services.job_service import create_job
from app.services.job_workspace_service import (
    STAGE_KEYS,
    JobStageSummary,
    StageSummary,
    default_stage,
    get_job_stage_summary,
)
from app.utils.authorization import UnauthorizedError
from tests.test_final_ranking_service import _candidate, _hr, _job

_SERVICE = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "job_workspace_service.py"
)
_WHY = "Strong evidence across both rounds; clear fit for the role."


def _manager(db):
    return create_user(
        db=db, email=f"m-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="Mia Manager",
        role=UserRole.HIRING_MANAGER,
    )


def _with_resume(db, cand):
    db.add(Document(
        application_id=cand["app"].id, drive_file_id=f"f-{uuid.uuid4().hex}",
        drive_folder_id="folder", original_filename="cv.pdf",
        mime_type="application/pdf", file_size_bytes=10,
    ))
    db.flush()


def _decide(db, cand, manager, decision="PROCEED"):
    return record_final_decision(
        db, application_id=cand["app"].id, decision=decision, rationale=_WHY,
        acting_user_id=manager.id,
    )


def _summary(db, w, user=None):
    return get_job_stage_summary(
        db, w["job"].id, acting_user_id=(user or w["hr"]).id
    )


# --- empty job -----------------------------------------------------------------


def test_a_brand_new_job_has_nothing_in_any_stage(db):
    hr = _hr(db)
    job = create_job(
        db, title="Empty Role", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
        created_by_user_id=hr.id,
    )
    s = get_job_stage_summary(db, job.id, acting_user_id=hr.id)
    assert s.header.job_id == job.id and s.header.title == "Empty Role"
    assert s.header.code == job.job_code and s.header.approved_rubric_version is None
    for key in STAGE_KEYS:
        assert s.stage(key) == StageSummary(count=0, complete=False, attention=0), key
    assert s.interviewed_count == 0
    assert default_stage(s) == "setup"


# --- mixed job: every count, flag and attention definition ---------------------


@pytest.fixture
def mixed(db):
    """Three applications on a job with an approved rubric v1, each with a screening
    ranking:
    A: résumé, shortlisted, one rated round, CURRENT decision
    B: NO résumé, shortlisted, one notes-only round (no ratings)
    C: NO résumé, not shortlisted, no feedback.
    """
    w = _job(db)
    a = _candidate(db, w, rounds={1: [4, 3]})
    b = _candidate(db, w, rounds={1: []})
    c = _candidate(db, w, rounds=None)
    _with_resume(db, a)
    mgr = _manager(db)
    _decide(db, a, mgr)
    return {"w": w, "a": a, "b": b, "c": c, "mgr": mgr}


def test_setup_counts_the_approved_rubric_version(db, mixed):
    s = _summary(db, mixed["w"])
    assert s.header.approved_rubric_version == 1
    assert s.setup == StageSummary(count=1, complete=True, attention=0)


def test_applicants_count_complete_flag_and_no_resume_attention(db, mixed):
    s = _summary(db, mixed["w"])
    # 3 applications; a ranking exists; B and C have no document.
    assert s.applicants == StageSummary(count=3, complete=True, attention=2)


def test_applicants_is_incomplete_without_a_screening_ranking(db):
    w = _job(db)
    _candidate(db, w, with_ranking=False, with_analysis=False)
    s = _summary(db, w)
    assert s.applicants.count == 1 and s.applicants.complete is False


def test_shortlist_counts_only_currently_shortlisted(db, mixed):
    w = mixed["w"]
    assert _summary(db, w).shortlist == StageSummary(2, True, 0)
    entry = db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.application_id == mixed["b"]["app"].id
        )
    ).scalar_one()
    entry.is_shortlisted = False
    db.flush()
    assert _summary(db, w).shortlist == StageSummary(1, True, 0)


def test_a_job_with_no_shortlist_is_incomplete(db):
    w = _job(db)
    _candidate(db, w, rounds=None)
    assert _summary(db, w).shortlist == StageSummary(0, False, 0)


def test_interviews_count_and_notes_only_round_needs_attention(db, mixed):
    s = _summary(db, mixed["w"])
    # A and B have a round; B's round carries no ratings.
    assert s.interviews == StageSummary(count=2, complete=False, attention=1)
    assert s.interviewed_count == 2


def test_interviews_complete_when_every_round_is_rated(db):
    w = _job(db)
    _candidate(db, w, rounds={1: [4], 2: [5, 3]})
    s = _summary(db, w)
    assert s.interviews == StageSummary(count=1, complete=True, attention=0)


def test_final_ranking_counts_current_decisions_against_interviewed(db, mixed):
    s = _summary(db, mixed["w"])
    # A decided; B interviewed but undecided; C never interviewed.
    assert s.final_ranking == StageSummary(count=1, complete=False, attention=1)


def test_a_superseded_decision_is_not_counted_twice(db, mixed):
    _decide(db, mixed["a"], mixed["mgr"], "HOLD")      # supersedes the first
    s = _summary(db, mixed["w"])
    assert s.final_ranking.count == 1


def test_final_ranking_is_complete_when_every_interviewed_one_is_decided(db, mixed):
    _decide(db, mixed["b"], mixed["mgr"], "REJECT")
    s = _summary(db, mixed["w"])
    assert s.final_ranking == StageSummary(count=2, complete=True, attention=0)


def test_default_stage_is_the_first_incomplete_one(db, mixed):
    assert default_stage(_summary(db, mixed["w"])) == "interviews"


def test_another_jobs_rows_are_never_counted(db, mixed):
    other = _job(db)
    s = _summary(db, other)
    assert s.applicants.count == 0 and s.shortlist.count == 0
    assert s.interviews.count == 0 and s.final_ranking.count == 0
    # ... and the first job is unchanged by the other's existence.
    assert _summary(db, mixed["w"]).applicants.count == 3


def test_the_summary_is_immutable(db, mixed):
    s = _summary(db, mixed["w"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.shortlist = StageSummary(9, True, 0)            # type: ignore[misc]
    assert isinstance(s, JobStageSummary)


# --- unknown job ------------------------------------------------------------------


@pytest.mark.parametrize("bad", [uuid.uuid4(), "not-a-uuid", "", "123", None])
def test_an_unknown_or_malformed_job_is_none(db, bad):
    hr = _hr(db)
    assert get_job_stage_summary(db, bad, acting_user_id=hr.id) is None


def test_a_string_job_id_works(db, mixed):
    s = get_job_stage_summary(
        db, str(mixed["w"]["job"].id), acting_user_id=mixed["w"]["hr"].id
    )
    assert s is not None and s.applicants.count == 3


# --- guard ---------------------------------------------------------------------------


def test_an_inactive_user_is_refused(db, mixed):
    mixed["w"]["hr"].is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _summary(db, mixed["w"])


@pytest.mark.parametrize("who", [None, "not-a-uuid", "unknown"])
def test_a_missing_or_unknown_user_is_refused(db, mixed, who):
    acting = uuid.uuid4() if who == "unknown" else who
    with pytest.raises(UnauthorizedError):
        get_job_stage_summary(db, mixed["w"]["job"].id, acting_user_id=acting)


def test_the_guard_runs_before_the_job_lookup(db):
    with pytest.raises(UnauthorizedError):
        get_job_stage_summary(db, uuid.uuid4(), acting_user_id=uuid.uuid4())


@pytest.mark.parametrize("role", [UserRole.HR, UserRole.HIRING_MANAGER, UserRole.ADMIN])
def test_every_internal_role_may_read(db, mixed, role):
    user = create_user(
        db=db, email=f"r-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name="Reader", role=role,
    )
    assert _summary(db, mixed["w"], user).applicants.count == 3


# --- read-only -----------------------------------------------------------------------


def _audit_count(db):
    return db.execute(select(func.count()).select_from(AuditEvent)).scalar_one()


def test_a_summary_writes_nothing_and_emits_no_audit_event(db, mixed):
    db.flush()
    before = _audit_count(db)
    docs = db.execute(select(func.count()).select_from(Document)).scalar_one()
    _summary(db, mixed["w"])
    assert not db.new and not db.dirty and not db.deleted
    assert _audit_count(db) == before
    assert db.execute(select(func.count()).select_from(Document)).scalar_one() == docs


def test_a_refused_call_writes_no_audit_event(db, mixed):
    before = _audit_count(db)
    with pytest.raises(UnauthorizedError):
        get_job_stage_summary(db, mixed["w"]["job"].id, acting_user_id=None)
    assert _audit_count(db) == before


# --- structural ---------------------------------------------------------------------------


def test_the_module_has_no_write_ai_or_audit_code():
    tree = ast.parse(_SERVICE.read_text(encoding="utf-8"))
    forbidden_calls = {"add", "add_all", "delete", "commit", "flush", "merge", "rollback"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls, node.func.attr
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [a.name for a in node.names]
            )
            for name in names:
                low = name.lower()
                assert "audit_service" not in low and "claude" not in low, name
                assert not low.startswith("app.ai"), name
    # reads only: no sqlalchemy DML constructs are imported
    imported = {
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "sqlalchemy"
        for a in n.names
    }
    assert imported <= {"exists", "func", "select"}, imported
