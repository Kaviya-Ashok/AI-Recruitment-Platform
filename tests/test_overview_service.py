"""Tests for app.services.overview_service (HR UI, Increment 4): the dashboard
summary, the Jobs table, the Candidates list and Quick find.

Real Postgres via the savepoint-rollback ``db`` fixture. ``empty_db`` empties the
jobs / applications / candidates tables INSIDE the test's transaction (``TRUNCATE``
is transactional and is rolled back with everything else), so every count here is
absolute and independent of whatever is committed in the database.

Every reader is READ-ONLY, guarded like the other readers, and built from a number
of statements that does not grow with the number of rows.
"""

from __future__ import annotations

import ast
import dataclasses
import uuid
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text

import app.services.overview_service as O
from app.database.models.audit_event import AuditEvent
from app.database.models.final_decision import FinalDecision
from app.database.models.job import JdInputMethod, JobStatus
from app.services.application_service import create_application
from app.services.job_service import create_job
from app.services.job_workspace_service import (
    default_stage,
    get_candidate_header,
    get_job_stage_summary,
)
from app.services.overview_service import (
    QUICK_FIND_LIMIT,
    get_dashboard_summary,
    list_candidates_overview,
    list_job_options,
    list_jobs_overview,
    quick_find,
)
from app.utils.authorization import UnauthorizedError
from app.utils.candidate_progress import STAGE_LABELS, stage_name
from app.utils.overview_helpers import ATTENTION_ORDER, order_attention
from tests.test_final_ranking_service import _candidate, _generate, _hr, _job
from tests.test_job_workspace_service import _decide, _manager, _with_resume

_SERVICE = Path(O.__file__)


@pytest.fixture
def empty_db(db):
    db.execute(text("TRUNCATE jobs, applications, candidates RESTART IDENTITY CASCADE"))
    db.flush()
    return db


def _bare(db, w, name, *, minutes=0, email=None):
    """An application that has only just been submitted: no screening, no résumé."""
    app = create_application(
        db, job_id=w["job"].id, application_link_id=w["link"].id,
        email=email or f"{name.lower().replace(' ', '.')}-{uuid.uuid4().hex[:6]}@x.test",
        full_name=name, phone=None,
    )
    from datetime import datetime, timedelta, timezone

    app.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    db.flush()
    return app


def _open(w):
    w["job"].status = JobStatus.OPEN
    return w


def _summary(db, w):
    return get_dashboard_summary(db, acting_user_id=w["hr"].id)


def _attention(summary):
    return [(a.kind, a.job_code, a.candidate_name) for a in summary.attention]


def _count_statements(db, fn):
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


# =============================================================== dashboard ================


def test_an_empty_database_gives_an_empty_dashboard(empty_db):
    hr = _hr(empty_db)
    s = get_dashboard_summary(empty_db, acting_user_id=hr.id)
    assert (s.jobs, s.open_jobs, s.applications, s.interviewed, s.decided) == (0, 0, 0, 0, 0)
    assert s.attention == () and s.attention_total == 0 and s.pipelines == ()


@pytest.fixture
def mixed(empty_db):
    """Job A (OPEN): Ann complete+decided with résumé, Ben notes-only round, Cat
    screened only, Dan just applied. Job B (CLOSED): Eve interviewed, undecided,
    with résumé."""
    db = empty_db
    a = _open(_job(db))
    mgr = _manager(db)
    ann = _candidate(db, a, name="Ann", rounds={1: [4, 5]}, minutes=0)
    ben = _candidate(db, a, name="Ben", rounds={1: []}, minutes=1, with_analysis=False)
    cat = _candidate(db, a, name="Cat", rounds=None, minutes=2)
    dan = _bare(db, a, "Dan", minutes=3)
    _with_resume(db, ann)
    _decide(db, ann, mgr, "PROCEED")
    b = _job(db)
    b["job"].status = JobStatus.CLOSED
    eve = _candidate(db, b, name="Eve", rounds={1: [3]}, minutes=4)
    _with_resume(db, eve)
    db.flush()
    return dict(db=db, a=a, b=b, ann=ann, ben=ben, cat=cat, dan=dan, eve=eve, mgr=mgr)


def test_the_four_figures(mixed):
    s = _summary(mixed["db"], mixed["a"])
    assert s.jobs == 2 and s.open_jobs == 1
    assert s.applications == 5                      # 4 on A, 1 on B
    assert s.interviewed == 3                       # Ann, Ben, Eve have a recorded round
    assert s.decided == 1                           # only Ann


def test_a_superseded_decision_is_not_counted_twice(mixed):
    _decide(mixed["db"], mixed["ann"], mixed["mgr"], "HOLD")      # supersedes PROCEED
    _decide(mixed["db"], mixed["ann"], mixed["mgr"], "REJECT")
    assert _summary(mixed["db"], mixed["a"]).decided == 1


def test_pipelines_are_cumulative_counts_for_open_jobs_only(mixed):
    s = _summary(mixed["db"], mixed["a"])
    (p,) = s.pipelines                              # the closed job has none
    assert p.job_id == mixed["a"]["job"].id and p.job_code == mixed["a"]["job"].job_code
    assert (p.applicants, p.screened, p.shortlisted, p.interviewed, p.decided) == (4, 3, 2, 2, 1)


def test_pipeline_counts_never_exceed_the_applicants(mixed):
    for p in _summary(mixed["db"], mixed["a"]).pipelines:
        assert max(p.screened, p.shortlisted, p.interviewed, p.decided) <= p.applicants


def test_each_attention_type_appears_for_the_right_candidates(mixed):
    got = set(_attention(_summary(mixed["db"], mixed["a"])))
    code_a, code_b = mixed["a"]["job"].job_code, mixed["b"]["job"].job_code
    assert got == {
        ("RATINGS_MISSING", code_a, "Ben"),                  # round without ratings
        ("RESUME_MISSING", code_a, "Ben"), ("RESUME_MISSING", code_a, "Cat"),
        ("RESUME_MISSING", code_a, "Dan"),                   # Ann has a résumé
        ("DECISION_PENDING", code_a, "Ben"),                 # interviewed, undecided
        ("DECISION_PENDING", code_b, "Eve"),
    }


def test_an_application_with_everything_in_place_needs_no_attention(mixed):
    names = {a.candidate_name for a in _summary(mixed["db"], mixed["a"]).attention}
    assert "Ann" not in names


def test_attention_is_ordered_type_then_job_then_name_and_matches_the_pure_order(mixed):
    s = _summary(mixed["db"], mixed["a"])
    code_a, code_b = mixed["a"]["job"].job_code, mixed["b"]["job"].job_code
    got = _attention(s)
    assert [k for k, _c, _n in got] == sorted(
        [k for k, _c, _n in got], key=ATTENTION_ORDER.index)
    kinds = [k for k, _c, _n in got]
    assert kinds[0] == "RATINGS_MISSING" and kinds[-1] == "DECISION_PENDING"
    assert list(s.attention) == order_attention(s.attention)       # SQL order == pure order
    resumes = [(c, n) for k, c, n in got if k == "RESUME_MISSING"]
    assert resumes == sorted(resumes, key=lambda cn: (cn[0].lower(), cn[1].lower()))
    assert (code_a, "Ben") in resumes and code_b not in {c for c, _ in resumes}


def test_the_cap_shows_25_and_states_the_total(empty_db):
    w = _open(_job(empty_db))
    for i in range(30):
        _bare(empty_db, w, f"Person {i:02d}", minutes=i)
    s = _summary(empty_db, w)
    assert len(s.attention) == 25 and s.attention_total == 30
    assert [a.candidate_name for a in s.attention] == [f"Person {i:02d}" for i in range(25)]
    assert {a.kind for a in s.attention} == {"RESUME_MISSING"}


def test_attention_items_carry_the_ids_the_deep_links_need(mixed):
    for item in _summary(mixed["db"], mixed["a"]).attention:
        job = mixed["a"]["job"] if item.job_code == mixed["a"]["job"].job_code else mixed["b"]["job"]
        assert item.job_id == job.id and isinstance(item.application_id, uuid.UUID)
        assert item.job_title == "Backend Engineer"


def test_attention_from_another_job_is_attributed_to_that_job(mixed):
    code_b = mixed["b"]["job"].job_code
    eve = [a for a in _summary(mixed["db"], mixed["a"]).attention if a.candidate_name == "Eve"]
    assert [(a.job_code, a.kind) for a in eve] == [(code_b, "DECISION_PENDING")]


def test_a_decision_clears_the_pending_item(mixed):
    before = _attention(_summary(mixed["db"], mixed["a"]))
    _decide(mixed["db"], mixed["eve"], mixed["mgr"], "HOLD")
    after = _attention(_summary(mixed["db"], mixed["a"]))
    assert len(after) == len(before) - 1
    assert all(n != "Eve" for _k, _c, n in after)


def test_the_summary_is_frozen_and_carries_no_free_text_field():
    names = {f.name for f in dataclasses.fields(O.DashboardSummary)}
    assert names == {"jobs", "open_jobs", "applications", "interviewed", "decided",
                     "attention", "attention_total", "pipelines"}
    item_fields = {f.name for f in dataclasses.fields(O.AttentionItem)}
    assert item_fields == {"kind", "job_id", "job_code", "job_title", "application_id",
                           "candidate_name"}


def test_the_dashboard_costs_a_fixed_few_statements_however_many_rows(empty_db):
    w = _open(_job(empty_db))
    for i in range(2):
        _bare(empty_db, w, f"Small {i}", minutes=i)
    uid = w["hr"].id
    empty_db.expire_all()
    small = _count_statements(empty_db, lambda: get_dashboard_summary(empty_db, acting_user_id=uid))
    for i in range(12):
        _bare(empty_db, w, f"Large {i}", minutes=10 + i)
    for _ in range(3):
        _open(_job(empty_db))
    empty_db.expire_all()
    large = _count_statements(empty_db, lambda: get_dashboard_summary(empty_db, acting_user_id=uid))
    assert len(small) == len(large) <= 4, ([s[:60] for s in large])
    assert all(s.lstrip().upper().startswith("SELECT") for s in large)


# ================================================================= jobs table ================


def _jobs(db, w, **kw):
    return list_jobs_overview(db, acting_user_id=w["hr"].id, **kw)


def test_jobs_rows_carry_counts_status_and_the_current_stage(mixed):
    data = _jobs(mixed["db"], mixed["a"])
    assert data.total == 2
    by_code = {r.job_code: r for r in data.rows}
    a, b = by_code[mixed["a"]["job"].job_code], by_code[mixed["b"]["job"].job_code]
    assert (a.title, a.status, a.applicants, a.interviewed, a.decided) == (
        "Backend Engineer", "OPEN", 4, 2, 1)
    assert (b.status, b.applicants, b.interviewed, b.decided) == ("CLOSED", 1, 1, 0)
    assert a.stage_key in ("applicants", "shortlist", "interviews", "final_ranking", "setup")


def test_the_stage_matches_the_workspace_for_jobs_in_every_state(empty_db):
    db = empty_db
    hr = _hr(db)
    mgr = _manager(db)
    jobs = []
    # 1. brand new job: no rubric
    jobs.append(create_job(db, title="Fresh", department="Eng",
                           jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
                           created_by_user_id=hr.id))
    # 2. approved rubric, no applicants
    jobs.append(_job(db)["job"])
    # 3. screened candidate with a ranking, nobody shortlisted
    w = _job(db)
    _candidate(db, w, rounds=None)
    jobs.append(w["job"])
    # 4. shortlisted, nobody interviewed
    w = _job(db)
    _candidate(db, w, rounds={})
    jobs.append(w["job"])
    # 5. an interview round without ratings
    w = _job(db)
    _candidate(db, w, rounds={1: []})
    jobs.append(w["job"])
    # 6. interviewed and rated, nobody decided
    w = _job(db)
    _candidate(db, w, rounds={1: [4]})
    jobs.append(w["job"])
    # 7. everyone interviewed is decided
    w = _job(db)
    done = _candidate(db, w, rounds={1: [4]})
    _decide(db, done, mgr, "PROCEED")
    jobs.append(w["job"])
    db.flush()

    got = {r.job_id: r for r in list_jobs_overview(db, acting_user_id=hr.id, limit=100).rows}
    assert len(got) == 7
    expected_keys = set()
    for job in jobs:
        summary = get_job_stage_summary(db, job.id, acting_user_id=hr.id)
        expected = default_stage(summary)
        expected_keys.add(expected)
        assert got[job.id].stage_key == expected, (job.title, expected)
        assert got[job.id].applicants == summary.applicants.count
        assert got[job.id].interviewed == summary.interviewed_count
        assert got[job.id].decided == summary.final_ranking.count
    assert {"setup", "applicants", "shortlist", "interviews", "final_ranking"} <= expected_keys


def test_jobs_are_listed_newest_first(empty_db):
    hr = _hr(empty_db)
    first = create_job(empty_db, title="Older", department="X",
                       jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
                       created_by_user_id=hr.id)
    from datetime import datetime, timezone

    first.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    second = create_job(empty_db, title="Newer", department="X",
                        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
                        created_by_user_id=hr.id)
    empty_db.flush()
    rows = list_jobs_overview(empty_db, acting_user_id=hr.id).rows
    assert [r.title for r in rows] == ["Newer", "Older"]
    assert second.id == rows[0].job_id


@pytest.fixture
def titled(empty_db):
    hr = _hr(empty_db)
    out = {}
    for title, dept, status in (
        ("Senior Backend Engineer", "Platform Team", JobStatus.OPEN),
        ("Data Analyst", "Insights", JobStatus.OPEN),
        ("50% Discount Coordinator", "Sales", JobStatus.DRAFT),
        ("Closed Role", "Ops", JobStatus.CLOSED),
        ("Old Archive", "Ops", JobStatus.ARCHIVED),
    ):
        job = create_job(empty_db, title=title, department=dept,
                         jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
                         created_by_user_id=hr.id)
        job.status = status
        out[title] = job
    empty_db.flush()
    return dict(db=empty_db, hr=hr, jobs=out)


def _titles(data):
    return sorted(r.title for r in data.rows)


@pytest.mark.parametrize("term", ["backend", "BACKEND", "BaCk", "end eng"])
def test_job_search_is_case_insensitive_and_partial_on_title(titled, term):
    data = list_jobs_overview(titled["db"], acting_user_id=titled["hr"].id, search=term)
    assert _titles(data) == ["Senior Backend Engineer"] and data.total == 1


def test_job_search_matches_code_and_department(titled):
    code = titled["jobs"]["Data Analyst"].job_code
    d = list_jobs_overview(titled["db"], acting_user_id=titled["hr"].id, search=code.lower())
    assert "Data Analyst" in _titles(d)
    d = list_jobs_overview(titled["db"], acting_user_id=titled["hr"].id, search="platform")
    assert _titles(d) == ["Senior Backend Engineer"]


def test_job_search_wildcards_are_literal(titled):
    db, uid = titled["db"], titled["hr"].id
    assert _titles(list_jobs_overview(db, acting_user_id=uid, search="50%")) == ["50% Discount Coordinator"]
    assert list_jobs_overview(db, acting_user_id=uid, search="%").total == 1       # only the % title
    # every job code is like V_1234, so a LITERAL underscore matches all five ...
    assert list_jobs_overview(db, acting_user_id=uid, search="_").total == 5
    # ... while two in a row (a wildcard would match any two characters) match none
    assert list_jobs_overview(db, acting_user_id=uid, search="__").total == 0
    assert list_jobs_overview(db, acting_user_id=uid, search="a_a").total == 0     # not "a?a"
    assert list_jobs_overview(db, acting_user_id=uid, search="'; DROP TABLE jobs;--").total == 0
    assert list_jobs_overview(db, acting_user_id=uid).total == 5                  # nothing dropped


def test_job_status_groups_partition_the_total(titled):
    from app.pages.jobs import _TABS

    db, uid = titled["db"], titled["hr"].id
    per_group = [list_jobs_overview(db, acting_user_id=uid, statuses=s).total for _k, _l, s in _TABS]
    assert per_group == [2, 1, 2]                  # open | draft group | closed + archived
    assert sum(per_group) == list_jobs_overview(db, acting_user_id=uid).total == 5


def test_an_empty_status_set_matches_nothing_and_blank_search_is_ignored(titled):
    db, uid = titled["db"], titled["hr"].id
    assert list_jobs_overview(db, acting_user_id=uid, statuses=[]).total == 0
    assert list_jobs_overview(db, acting_user_id=uid, search="   ").total == 5


def test_job_paging_totals_and_slices(empty_db):
    hr = _hr(empty_db)
    for i in range(7):
        job = create_job(empty_db, title=f"Job {i}", department="X",
                         jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
                         created_by_user_id=hr.id)
        from datetime import datetime, timedelta, timezone

        job.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
    empty_db.flush()
    pages = [list_jobs_overview(empty_db, acting_user_id=hr.id, limit=3, offset=o) for o in (0, 3, 6)]
    assert [p.total for p in pages] == [7, 7, 7]
    assert [len(p.rows) for p in pages] == [3, 3, 1]
    seen = [r.title for p in pages for r in p.rows]
    assert seen == [f"Job {i}" for i in range(6, -1, -1)] and len(set(seen)) == 7
    assert list_jobs_overview(empty_db, acting_user_id=hr.id, limit=3, offset=30).rows == ()


def test_page_arguments_are_clamped(empty_db):
    hr = _hr(empty_db)
    data = list_jobs_overview(empty_db, acting_user_id=hr.id, limit=10_000, offset=-5)
    assert data.total == 0 and data.rows == ()
    assert O.MAX_PAGE_SIZE == 100


def test_the_jobs_table_costs_the_same_statements_for_2_rows_and_12(empty_db):
    hr = _hr(empty_db)
    uid = hr.id

    def make(n):
        for i in range(n):
            create_job(empty_db, title=f"J{n}-{i}", department="X",
                       jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
                       created_by_user_id=uid)
        empty_db.flush()
        empty_db.expire_all()

    make(2)
    small = _count_statements(empty_db, lambda: list_jobs_overview(empty_db, acting_user_id=uid))
    make(12)
    large = _count_statements(empty_db, lambda: list_jobs_overview(empty_db, acting_user_id=uid))
    assert len(small) == len(large) <= 4


# ============================================================ candidates list ================


@pytest.fixture
def stages(empty_db):
    """One candidate in each stage, on one job, plus the facts needed to prove the
    SQL stage rule equals ``candidate_progress.stage_name``."""
    db = empty_db
    w = _open(_job(db))
    mgr = _manager(db)
    c = {}
    c["applied"] = _bare(db, w, "Alma Applied", minutes=0)
    screened = _candidate(db, w, name="Sam Screened", rounds=None, minutes=1)
    shortlisted = _candidate(db, w, name="Shay Shortlisted", rounds={}, minutes=2)
    notes_only = _candidate(db, w, name="Nora Notes", rounds={1: []}, minutes=3, with_analysis=False)
    interviewed = _candidate(db, w, name="Ivy Interviewed", rounds={1: [4]}, minutes=4)
    ranked = _candidate(db, w, name="Rhea Ranked", rounds={1: [5]}, minutes=5)
    decided = _candidate(db, w, name="Dee Decided", rounds={1: [3, 4]}, minutes=6)
    inelig = _candidate(db, w, name="Ian Ineligible", rounds={1: [3]}, minutes=7, eligible=False)
    _generate(db, w)                                     # Rhea / Dee / Ivy / Ian get entries
    _decide(db, decided, mgr, "HOLD")
    db.flush()
    return dict(db=db, w=w, mgr=mgr, apps={
        "Alma Applied": c["applied"], "Sam Screened": screened["app"],
        "Shay Shortlisted": shortlisted["app"], "Nora Notes": notes_only["app"],
        "Ivy Interviewed": interviewed["app"], "Rhea Ranked": ranked["app"],
        "Dee Decided": decided["app"], "Ian Ineligible": inelig["app"],
    })


def _cands(db, w, **kw):
    kw.setdefault("limit", 100)
    return list_candidates_overview(db, acting_user_id=w["hr"].id, **kw)


def test_the_sql_stage_equals_stage_name_for_every_candidate(stages):
    db, w = stages["db"], stages["w"]
    rows = {r.application_id: r for r in _cands(db, w).rows}
    assert len(rows) == 8
    seen = set()
    for name, app in stages["apps"].items():
        header = get_candidate_header(db, w["job"].id, app.id, acting_user_id=w["hr"].id)
        assert rows[app.id].stage == stage_name(header), name
        seen.add(rows[app.id].stage)
    assert {"Applied", "Screened", "Shortlisted", "Interviewed", "Decided"} <= seen


def test_a_ranked_but_undecided_candidate_is_ranked_and_a_notes_only_one_is_not_interviewed(stages):
    db, w = stages["db"], stages["w"]
    by_name = {r.candidate_name: r for r in _cands(db, w).rows}
    assert by_name["Nora Notes"].stage == "Shortlisted"        # a round, but no ratings
    assert by_name["Dee Decided"].stage == "Decided" and by_name["Dee Decided"].decision == "HOLD"
    assert by_name["Ian Ineligible"].stage != "Ranked"          # an entry without a rank
    assert by_name["Alma Applied"].stage == "Applied" and by_name["Alma Applied"].decision is None


@pytest.mark.parametrize("stage", list(STAGE_LABELS.values()))
def test_the_stage_filter_returns_exactly_that_stage(stages, stage):
    db, w = stages["db"], stages["w"]
    data = _cands(db, w, stage=stage)
    assert all(r.stage == stage for r in data.rows)
    assert data.total == len(data.rows)
    everyone = _cands(db, w)
    assert data.total == sum(1 for r in everyone.rows if r.stage == stage)


def test_stages_partition_the_candidates(stages):
    db, w = stages["db"], stages["w"]
    total = sum(_cands(db, w, stage=s).total for s in STAGE_LABELS.values())
    assert total == _cands(db, w).total == 8


def test_an_unknown_stage_is_ignored(stages):
    assert _cands(stages["db"], stages["w"], stage="Nonsense").total == 8


def test_the_decision_is_the_current_one_only(stages):
    db, w = stages["db"], stages["w"]
    _decide(db, {"app": stages["apps"]["Dee Decided"]}, stages["mgr"], "REJECT")
    row = next(r for r in _cands(db, w).rows if r.candidate_name == "Dee Decided")
    assert row.decision == "REJECT"
    assert db.execute(select(func.count()).select_from(FinalDecision).where(
        FinalDecision.application_id == row.application_id)).scalar_one() == 2


def test_candidate_text_filter_matches_name_or_email_case_insensitively(empty_db):
    w = _open(_job(empty_db))
    _bare(empty_db, w, "Ada Lovelace", email="ada@analytical.test")
    _bare(empty_db, w, "Bob Builder", email="bob@build.test")
    for term, expected in (("lovelace", ["Ada Lovelace"]), ("LOVE", ["Ada Lovelace"]),
                           ("@build", ["Bob Builder"]), ("  ada  ", ["Ada Lovelace"]),
                           ("zzz", []), ("", ["Ada Lovelace", "Bob Builder"])):
        got = sorted(r.candidate_name for r in _cands(empty_db, w, search=term).rows)
        assert got == expected, term


def test_candidate_filter_wildcards_are_literal(empty_db):
    w = _open(_job(empty_db))
    for name in ("Ann 50% Off", "Bob 500 Off", "Cy_Cl", "CyXCl", "Back\\slash"):
        _bare(empty_db, w, name)
    def names(term):
        return sorted(r.candidate_name for r in _cands(empty_db, w, search=term).rows)

    assert names("50%") == ["Ann 50% Off"]               # "50%" is not "50 then anything"
    assert names("y_c") == ["Cy_Cl"]                     # "_" is not "any one character"
    assert names("k\\s") == ["Back\\slash"]
    assert names("%") == ["Ann 50% Off"]
    assert names("'; DROP TABLE applications;--") == []
    assert _cands(empty_db, w).total == 5


def test_the_job_filter_isolates_one_job(stages):
    db, w = stages["db"], stages["w"]
    other = _open(_job(db))
    _bare(db, other, "Zed Elsewhere")
    mine = _cands(db, w, job_id=w["job"].id)
    assert mine.total == 8 and all(r.job_id == w["job"].id for r in mine.rows)
    theirs = _cands(db, w, job_id=str(other["job"].id))
    assert [r.candidate_name for r in theirs.rows] == ["Zed Elsewhere"]
    assert _cands(db, w).total == 9


@pytest.mark.parametrize("bad", ["not-a-uuid", "", "123", uuid.uuid4()])
def test_a_malformed_or_unknown_job_matches_nothing(stages, bad):
    data = _cands(stages["db"], stages["w"], job_id=bad)
    if bad == "":
        assert data.total in (0, 8)               # blank is "no such job id", never a crash
    else:
        assert data.total == 0 and data.rows == ()


def test_filters_combine(stages):
    db, w = stages["db"], stages["w"]
    data = _cands(db, w, search="i", stage="Interviewed", job_id=w["job"].id)
    assert all(r.stage == "Interviewed" and "i" in r.candidate_name.lower() for r in data.rows)


def test_candidate_rows_carry_job_code_and_title_and_email(stages):
    row = _cands(stages["db"], stages["w"]).rows[0]
    assert row.job_code == stages["w"]["job"].job_code and row.job_title == "Backend Engineer"
    assert "@" in row.candidate_email
    assert {f.name for f in dataclasses.fields(O.CandidateOverviewRow)} == {
        "application_id", "job_id", "candidate_name", "candidate_email", "job_code",
        "job_title", "stage", "decision"}


def test_candidates_are_newest_first_with_a_stable_order(stages):
    names = [r.candidate_name for r in _cands(stages["db"], stages["w"]).rows]
    assert names == ["Ian Ineligible", "Dee Decided", "Rhea Ranked", "Ivy Interviewed",
                     "Nora Notes", "Shay Shortlisted", "Sam Screened", "Alma Applied"]


def test_candidate_paging_covers_every_row_once_and_reports_the_total(empty_db):
    w = _open(_job(empty_db))
    for i in range(11):
        _bare(empty_db, w, f"Person {i:02d}", minutes=i)
    pages = [_cands(empty_db, w, limit=4, offset=o) for o in (0, 4, 8)]
    assert [p.total for p in pages] == [11, 11, 11]
    assert [len(p.rows) for p in pages] == [4, 4, 3]
    ids = [r.application_id for p in pages for r in p.rows]
    assert len(ids) == len(set(ids)) == 11
    assert _cands(empty_db, w, limit=4, offset=40).rows == ()


def test_the_total_follows_the_filters_not_the_page(empty_db):
    w = _open(_job(empty_db))
    for i in range(30):
        _bare(empty_db, w, f"Match {i:02d}")
    _bare(empty_db, w, "Other Person")
    data = _cands(empty_db, w, search="match", limit=25)
    assert data.total == 30 and len(data.rows) == 25


def test_the_candidates_list_costs_the_same_statements_for_2_rows_and_12(empty_db):
    w = _open(_job(empty_db))
    uid = w["hr"].id
    for i in range(2):
        _bare(empty_db, w, f"Small {i}")
    empty_db.expire_all()
    small = _count_statements(empty_db, lambda: list_candidates_overview(empty_db, acting_user_id=uid))
    for i in range(12):
        _bare(empty_db, w, f"Large {i}")
    empty_db.expire_all()
    large = _count_statements(empty_db, lambda: list_candidates_overview(
        empty_db, acting_user_id=uid, stage="Applied", search="a"))
    assert len(small) == len(large) <= 4


def test_job_options_are_code_and_title_newest_first(empty_db):
    hr = _hr(empty_db)
    job = create_job(empty_db, title="Only One", department="X",
                     jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
                     created_by_user_id=hr.id)
    opts = list_job_options(empty_db, acting_user_id=hr.id)
    assert [(o.job_id, o.job_code, o.title) for o in opts] == [(job.id, job.job_code, "Only One")]


# =============================================================== quick find ================


@pytest.fixture
def findable(empty_db):
    w = _open(_job(empty_db))
    w["job"].title = "Quantum Backend Role"
    _bare(empty_db, w, "Ada Lovelace", email="ada.l@analytical.test")
    _bare(empty_db, w, "Adam Smith", email="adam@wealth.test")
    _bare(empty_db, w, "Grace Hopper", email="grace@navy.test")
    empty_db.flush()
    return dict(db=empty_db, w=w)


def _find(f, q, **kw):
    return quick_find(f["db"], q, acting_user_id=f["w"]["hr"].id, **kw)


def test_quick_find_matches_candidates_by_name_and_email(findable):
    assert sorted(m.label for m in _find(findable, "ada")) == ["Ada Lovelace", "Adam Smith"]
    assert [m.label for m in _find(findable, "navy")] == ["Grace Hopper"]
    assert [m.label for m in _find(findable, "GRACE@")] == ["Grace Hopper"]
    assert all(m.kind == "candidate" for m in _find(findable, "ada"))


def test_quick_find_matches_jobs_by_code_and_title(findable):
    code = findable["w"]["job"].job_code
    by_code = _find(findable, code.lower())
    assert [m.kind for m in by_code] == ["job"] and by_code[0].label == "Quantum Backend Role"
    assert by_code[0].detail == code and by_code[0].application_id is None
    assert [m.kind for m in _find(findable, "quantum")] == ["job"]


def test_quick_find_results_carry_the_ids_needed_to_open_them(findable):
    (m,) = _find(findable, "grace")
    assert m.job_id == findable["w"]["job"].id and isinstance(m.application_id, uuid.UUID)
    assert m.detail == f"grace@navy.test · {findable['w']['job'].job_code}"


@pytest.mark.parametrize("short", ["", " ", "a", " a ", None, "\t"])
def test_a_query_shorter_than_two_characters_finds_nothing(findable, short):
    assert _find(findable, short) == []


def test_two_characters_is_enough(findable):
    assert [m.label for m in _find(findable, "gr")] == ["Grace Hopper"]


def test_quick_find_wildcards_are_literal(empty_db):
    w = _open(_job(empty_db))
    for name in ("Ann 50% Off", "Bob 500 Off", "Cy_Cl", "CyXCl"):
        _bare(empty_db, w, name)
    f = dict(db=empty_db, w=w)
    assert [m.label for m in _find(f, "50%")] == ["Ann 50% Off"]
    assert [m.label for m in _find(f, "y_c")] == ["Cy_Cl"]
    assert _find(f, "%%") == [] and _find(f, "__") == []


def test_quick_find_treats_the_query_as_plain_text(findable):
    for hostile in ("'; DROP TABLE jobs;--", "x' OR '1'='1", "\\", "%'; --", "a\u0000b"):
        try:
            assert _find(findable, hostile) == []
        except Exception as exc:                      # a NUL byte may be refused by the driver
            assert "\x00" in hostile, exc
            findable["db"].rollback()
            return
    assert len(_find(findable, "ada")) == 2           # nothing was dropped


def test_quick_find_returns_at_most_eight_matches(empty_db):
    w = _open(_job(empty_db))
    for i in range(20):
        _bare(empty_db, w, f"Match {i:02d}")
    f = dict(db=empty_db, w=w)
    assert len(_find(f, "match")) == QUICK_FIND_LIMIT == 8
    assert len(_find(f, "match", limit=3)) == 3
    assert len(_find(f, "match", limit=500)) == 8


def test_quick_find_lists_jobs_before_candidates_within_the_cap(empty_db):
    w = _open(_job(empty_db))
    w["job"].title = "Match Maker"
    for i in range(10):
        _bare(empty_db, w, f"Match {i}")
    kinds = [m.kind for m in _find(dict(db=empty_db, w=w), "match")]
    assert kinds[0] == "job" and kinds.count("candidate") == 7


def test_quick_find_costs_the_same_for_few_and_many_matches(empty_db):
    w = _open(_job(empty_db))
    uid = w["hr"].id
    _bare(empty_db, w, "Match 00")
    empty_db.expire_all()
    few = _count_statements(empty_db, lambda: quick_find(empty_db, "match", acting_user_id=uid))
    for i in range(1, 15):
        _bare(empty_db, w, f"Match {i:02d}")
    empty_db.expire_all()
    many = _count_statements(empty_db, lambda: quick_find(empty_db, "match", acting_user_id=uid))
    assert len(few) == len(many) <= 4


# ================================================= guards, read-only, structure ================

_READERS = {
    "dashboard": lambda db, who: get_dashboard_summary(db, acting_user_id=who),
    "jobs": lambda db, who: list_jobs_overview(db, acting_user_id=who),
    "candidates": lambda db, who: list_candidates_overview(db, acting_user_id=who),
    "options": lambda db, who: list_job_options(db, acting_user_id=who),
    "find": lambda db, who: quick_find(db, "ada", acting_user_id=who),
}


@pytest.mark.parametrize("reader", sorted(_READERS))
@pytest.mark.parametrize("who", [None, "not-a-uuid", "unknown"])
def test_a_missing_malformed_or_unknown_user_is_refused(db, reader, who):
    acting = uuid.uuid4() if who == "unknown" else who
    with pytest.raises(UnauthorizedError):
        _READERS[reader](db, acting)


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_an_inactive_user_is_refused(db, reader):
    hr = _hr(db)
    hr.is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _READERS[reader](db, hr.id)


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_every_internal_role_may_read(db, reader):
    for user in (_hr(db), _manager(db)):
        _READERS[reader](db, user.id)


def test_the_guard_runs_before_anything_else(db):
    with pytest.raises(UnauthorizedError):
        list_candidates_overview(db, acting_user_id=uuid.uuid4(), job_id="not-a-uuid")
    with pytest.raises(UnauthorizedError):
        quick_find(db, "", acting_user_id=None)


def test_no_reader_writes_a_row_or_an_audit_event(mixed):
    db, w = mixed["db"], mixed["a"]
    from app.database.models.application import Application
    from app.database.models.job import Job

    def counts():
        return {m.__name__: db.execute(select(func.count()).select_from(m)).scalar_one()
                for m in (Job, Application, AuditEvent, FinalDecision)}

    before = counts()
    for name in sorted(_READERS):
        _READERS[name](db, w["hr"].id)
    assert counts() == before


def test_the_module_has_no_write_call_no_ai_and_no_streamlit():
    tree = ast.parse(_SERVICE.read_text(encoding="utf-8"))
    forbidden = {"add", "add_all", "delete", "merge", "commit", "flush", "rollback",
                 "record_event", "insert", "update", "execute_update"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in forbidden, name
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] + [a.name for a in node.names])
            for n in names:
                low = n.lower()
                assert not low.startswith(("app.ai", "streamlit")), n
                assert "claude" not in low and "anthropic" not in low and "audit_service" not in low, n
                assert n not in {"insert", "update", "delete"}, n


def test_no_view_can_carry_a_hash_a_note_a_rationale_or_transcript_text():
    banned = ("password", "hash", "note", "rationale", "transcript", "text", "answer", "reason")
    for cls in (O.AttentionItem, O.JobPipeline, O.DashboardSummary, O.JobOverviewRow,
                O.JobsOverview, O.CandidateOverviewRow, O.CandidatesOverview, O.FindMatch,
                O.JobOption):
        for f in dataclasses.fields(cls):
            assert not any(b in f.name.lower() for b in banned), (cls.__name__, f.name)
            assert cls.__dataclass_params__.frozen


def test_no_query_selects_a_free_text_or_hash_column():
    source = _SERVICE.read_text(encoding="utf-8")
    for column in ("hashed_password", "FinalDecision.rationale", "InterviewFeedback.notes",
                   "answer_text", "extracted_data", "event_metadata", "previous_state",
                   "new_state", "shortlist_reason", "Candidate.phone"):
        assert column not in source, column
