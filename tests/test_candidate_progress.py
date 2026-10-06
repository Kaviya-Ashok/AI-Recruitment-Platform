"""Pure helpers for the HR candidate page (HR UI, Increment 3): Progress, Next step,
tab and activity labels, number/rank wording, the candidate/tab query-parameter
helpers and ``deep_link_target`` with a candidate. No database, no Streamlit app.
"""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import app.pages.candidate_page as CP
import app.pages.job_workspace as W
import app.utils.workspace_nav as nav
from app.services.job_workspace_service import CandidateHeader, JobCandidate
from app.utils.candidate_progress import (
    ACTION_RECORD_DECISION,
    ACTIVITY_LABELS,
    NOT_AVAILABLE,
    TAB_LABELS,
    activity_label,
    has_ratings,
    next_step,
    progress_button_label,
    progress_items,
    rank_text,
    score_text,
    stage_name,
    tab_label,
    tab_labels,
)

APP = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
JOB = uuid.UUID("6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b")


def make_header(**overrides) -> CandidateHeader:
    """A candidate header with nothing done yet; override what a test needs."""
    base = dict(
        application_id=APP, job_id=JOB, candidate_name="Ada Lovelace",
        candidate_email="ada@example.test", job_code="V_001",
        job_title="Backend Engineer", application_status="SCREENING_EVALUATED",
        screened=True, is_shortlisted=True,
        screening_score=Decimal("8.00"), screening_rank=1,
        interview_score=None, final_score=None, final_rank=None,
        final_ranked_count=None, entry_status=None, entry_reason=None,
        has_current_final_ranking=False, has_analysis=False, rounds_count=0,
        has_unrated_round=False, transcripts_count=0, decision=None,
        decided_by_name=None, decided_at=None,
    )
    base.update(overrides)
    return CandidateHeader(**base)


def _complete(**kw) -> CandidateHeader:
    return make_header(
        interview_score=Decimal("9.00"), final_score=Decimal("8.60"), final_rank=1,
        final_ranked_count=3, entry_status="RANKED", has_current_final_ranking=True,
        has_analysis=True, rounds_count=2, decision="PROCEED",
        decided_by_name="Mia", decided_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
        **kw,
    )


# --- tabs ---------------------------------------------------------------------------


def test_tabs_are_in_the_specified_order_and_labelled_in_plain_text():
    assert list(nav.CANDIDATE_TABS) == [
        "overview", "screening", "interview", "analysis", "scorecard", "decision",
    ]
    assert [tab_label(k) for k in nav.CANDIDATE_TABS] == [
        "Overview", "Screening", "Interview", "AI analysis", "Scorecard", "Decision",
    ]
    assert list(tab_labels()) == list(nav.CANDIDATE_TABS)
    assert set(TAB_LABELS) == set(nav.CANDIDATE_TABS)


def test_an_unknown_tab_key_is_labelled_not_raised():
    assert tab_label("some_new_tab") == "Some new tab"


def test_tab_labels_carry_no_markup_or_emoji():
    for label in tab_labels().values():
        assert label.isascii() and "<" not in label and ":" not in label


# --- number wording --------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    (None, NOT_AVAILABLE), (Decimal("7.5"), "7.50 / 10"), (8, "8.00 / 10"),
    (0, "0.00 / 10"), (Decimal("9.999"), "10.00 / 10"),
])
def test_score_text(value, expected):
    assert score_text(value) == expected


@pytest.mark.parametrize("rank, count, status, expected", [
    (1, 3, "RANKED", "#1 of 3 ranked"),
    (2, None, "RANKED", "#2"),
    (None, None, "NOT_RANKED_INELIGIBLE", "Not ranked"),
    (None, None, "INCOMPLETE_INTERVIEW", "Incomplete"),
    (None, None, "INCOMPLETE_SCREENING", "Incomplete"),
    (None, None, None, "Not ranked yet"),
])
def test_rank_text(rank, count, status, expected):
    assert rank_text(rank, count, status) == expected


# --- has_ratings -------------------------------------------------------------------------


@pytest.mark.parametrize("rounds, unrated, expected", [
    (0, False, False), (1, False, True), (2, False, True), (1, True, False), (2, True, False),
])
def test_has_ratings(rounds, unrated, expected):
    assert has_ratings(make_header(rounds_count=rounds, has_unrated_round=unrated)) is expected


# --- progress -----------------------------------------------------------------------------


def _by_key(header):
    return {i.key: i for i in progress_items(header)}


def test_progress_items_are_ordered_and_target_the_right_tabs():
    items = progress_items(make_header())
    assert [i.key for i in items] == [
        "applied", "screened", "shortlisted", "interviewed", "analysis", "ranked",
        "decided",
    ]
    assert [i.label for i in items] == [
        "Applied", "Screened", "Shortlisted", "Interviewed", "AI analysis (optional)",
        "Ranked", "Decided",
    ]
    assert [i.tab for i in items] == [
        "overview", "screening", "overview", "interview", "analysis", "scorecard",
        "decision",
    ]
    assert all(i.tab in nav.CANDIDATE_TABS for i in items)


def test_a_complete_candidate_has_everything_done():
    items = _by_key(_complete())
    assert all(i.done for i in items.values())
    assert items["screened"].status == "8.00"
    assert items["interviewed"].status == "2 rounds"
    assert items["ranked"].status == "#1"
    assert items["decided"].status == "Proceed"
    assert not any(i.warn for i in items.values())


def test_a_new_applicant_has_only_applied_and_screened_done():
    items = _by_key(make_header(is_shortlisted=False))
    assert [k for k, i in items.items() if i.done] == ["applied", "screened"]
    assert items["shortlisted"].status == "not shortlisted"
    assert items["interviewed"].status == "not recorded"
    assert items["analysis"].status == "not generated"
    assert items["ranked"].status == "not ranked yet"
    assert items["decided"].status == ""


def test_a_round_without_ratings_is_a_warning_not_done():
    item = _by_key(make_header(rounds_count=1, has_unrated_round=True))["interviewed"]
    assert item.done is False and item.warn is True
    assert item.status == "ratings missing"


def test_one_round_is_singular():
    assert _by_key(make_header(rounds_count=1))["interviewed"].status == "1 round"


@pytest.mark.parametrize("entry_status, expected", [
    ("NOT_RANKED_INELIGIBLE", "not ranked"),
    ("INCOMPLETE_INTERVIEW", "incomplete"),
    ("INCOMPLETE_SCREENING", "incomplete"),
])
def test_ranked_explains_why_it_is_not_done(entry_status, expected):
    item = _by_key(make_header(entry_status=entry_status, has_current_final_ranking=True))["ranked"]
    assert item.done is False and item.status == expected


def test_screened_without_a_score_still_reads_as_done_when_an_evaluation_exists():
    item = _by_key(make_header(screened=True, screening_score=None))["screened"]
    assert item.done is True and item.status == ""


def test_not_screened_says_so():
    item = _by_key(make_header(screened=False, screening_score=None))["screened"]
    assert item.done is False and item.status == "not screened yet"


def test_progress_button_label_states_status_in_words():
    items = _by_key(_complete())
    assert progress_button_label(items["interviewed"]) == "Interviewed — 2 rounds"
    assert progress_button_label(items["applied"]) == "Applied"


def test_every_progress_item_is_distinguishable_without_colour():
    """Done, warn and todo differ in WORDS (status text) as well as icon."""
    items = _by_key(make_header(is_shortlisted=False, rounds_count=1, has_unrated_round=True))
    assert progress_button_label(items["interviewed"]) != progress_button_label(
        _by_key(_complete())["interviewed"]
    )
    assert "not shortlisted" in progress_button_label(items["shortlisted"])


# --- stage name ---------------------------------------------------------------------------


@pytest.mark.parametrize("header, expected", [
    (make_header(screened=False, screening_score=None, is_shortlisted=False), "Applied"),
    (make_header(is_shortlisted=False), "Screened"),
    (make_header(), "Shortlisted"),
    (make_header(rounds_count=1), "Interviewed"),
    (make_header(rounds_count=1, final_rank=2), "Ranked"),
    (_complete(), "Decided"),
])
def test_stage_name_is_the_furthest_stage_reached(header, expected):
    assert stage_name(header) == expected


# --- next step: each branch ----------------------------------------------------------------


def test_no_rounds_goes_to_the_interview_tab():
    message, label, action = next_step(make_header())
    assert action == "interview" and label == "Go to Interview"
    assert "Record the interview feedback" in message


def test_a_round_without_ratings_goes_to_the_interview_tab():
    message, label, action = next_step(make_header(rounds_count=1, has_unrated_round=True))
    assert (label, action) == ("Go to Interview", "interview")
    assert "Add competency ratings" in message


def test_rated_and_undecided_opens_the_decision_dialog():
    message, label, action = next_step(make_header(rounds_count=1))
    assert (label, action) == ("Record decision", ACTION_RECORD_DECISION)
    assert "optional" in message                       # no analysis yet
    message2, _, _ = next_step(make_header(rounds_count=1, has_analysis=True))
    assert "optional" not in message2


def test_decided_views_the_decision():
    message, label, action = next_step(_complete())
    assert (label, action) == ("View decision", "decision")
    assert "decision is recorded" in message


def test_ratings_are_checked_before_the_decision():
    """Specified order: no ratings -> Interview, even if a decision exists."""
    header = make_header(rounds_count=1, has_unrated_round=True, decision="HOLD")
    assert next_step(header).action == "interview"


def test_next_step_is_a_plain_tuple_of_three():
    message, label, action = next_step(make_header())
    assert all(isinstance(x, str) for x in (message, label, action))
    assert len(next_step(make_header())) == 3


def test_action_is_a_known_tab_or_the_dialog():
    for header in (make_header(), make_header(rounds_count=1), _complete()):
        action = next_step(header).action
        assert action == ACTION_RECORD_DECISION or action in nav.CANDIDATE_TABS


# --- activity labels -----------------------------------------------------------------------


def test_activity_labels_cover_every_application_level_event():
    for event in (
        "CANDIDATE_APPLIED", "RESUME_UPLOADED", "RESUME_PROCESSED",
        "PREQUALIFICATION_COMPLETED", "AI_SCREENING_STARTED", "AI_SCREENING_COMPLETED",
        "AI_SCREENING_INCOMPLETE", "SCORE_GENERATED", "CANDIDATE_SHORTLISTED",
        "CANDIDATE_UNSHORTLISTED", "INTERVIEW_GUIDE_GENERATED",
        "HUMAN_FEEDBACK_SUBMITTED", "INTERVIEW_TRANSCRIPT_UPLOADED",
        "POST_INTERVIEW_ANALYSIS_COMPLETED", "FINAL_DECISION_SUBMITTED",
    ):
        assert event in ACTIVITY_LABELS, event


def test_activity_label_falls_back_and_never_raises():
    assert activity_label("CANDIDATE_APPLIED") == "Application submitted"
    assert activity_label("SOME_NEW_EVENT") == "Some New Event"
    assert activity_label(None) == "—"


def test_activity_labels_are_plain_text():
    for label in ACTIVITY_LABELS.values():
        assert "<" not in label and "_" not in label


# --- the module is pure -----------------------------------------------------------------------


def test_the_pure_module_imports_neither_streamlit_nor_the_database():
    path = Path(__file__).resolve().parents[1] / "app" / "utils" / "candidate_progress.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(
        m.startswith(("streamlit", "sqlalchemy", "app.database", "app.services"))
        for m in imported
    ), imported


# --- neighbours (page) ------------------------------------------------------------------------


def _order(n):
    return [JobCandidate(uuid.UUID(int=i + 1), f"C{i}", i + 1) for i in range(n)]


def test_neighbours_in_the_middle():
    order = _order(3)
    assert CP.neighbours(order, order[1].application_id) == (
        order[0].application_id, order[2].application_id, 2,
    )


def test_neighbours_at_the_ends_are_none():
    order = _order(3)
    assert CP.neighbours(order, order[0].application_id) == (None, order[1].application_id, 1)
    assert CP.neighbours(order, order[2].application_id) == (order[1].application_id, None, 3)


def test_neighbours_of_a_single_or_missing_candidate():
    order = _order(1)
    assert CP.neighbours(order, order[0].application_id) == (None, None, 1)
    assert CP.neighbours(order, uuid.uuid4()) == (None, None, None)
    assert CP.neighbours([], uuid.uuid4()) == (None, None, None)


# --- workspace helpers: table selection, feedback text -----------------------------------------


@pytest.mark.parametrize("rows, ids, expected", [
    ([], ["a", "b"], None),
    (None, ["a", "b"], None),
    ([0], ["a", "b"], "a"),
    ([1], ["a", "b"], "b"),
    ([1, 0], ["a", "b"], "b"),            # single-row mode: the first wins
    ([2], ["a", "b"], None),              # outside the table
    ([-1], ["a", "b"], None),
    (["x"], ["a", "b"], None),
])
def test_selected_application_id(rows, ids, expected):
    assert W.selected_application_id(rows, ids) == expected


def test_selected_application_id_stringifies_uuids():
    assert W.selected_application_id([0], [APP]) == str(APP)


@pytest.mark.parametrize("rounds, unrated, expected", [
    (0, False, "None recorded"), (0, True, "None recorded"),
    (1, False, "Rated"), (2, True, "Ratings missing"),
])
def test_feedback_status_text(rounds, unrated, expected):
    assert W.feedback_status_text(rounds, unrated) == expected


# --- query-parameter helpers: candidate and tab ------------------------------------------------


class _FakeSt:
    def __init__(self, params=None):
        self.query_params = dict(params or {})
        self.reruns = 0

    def rerun(self):                                   # pragma: no cover - must not run
        self.reruns += 1
        raise AssertionError("candidate navigation must not call st.rerun()")


@pytest.fixture
def fake_st(monkeypatch):
    fake = _FakeSt()
    monkeypatch.setattr(nav, "st", fake)
    return fake


def test_open_candidate_keeps_job_stage_and_tab(fake_st):
    fake_st.query_params.update({"job": "j", "stage": "shortlist", "tab": "scorecard"})
    nav.open_candidate("app-1")
    assert fake_st.query_params == {
        "job": "j", "stage": "shortlist", "tab": "scorecard", "candidate": "app-1",
    }


def test_open_candidate_can_name_a_tab(fake_st):
    fake_st.query_params.update({"job": "j", "stage": "applicants"})
    nav.open_candidate("app-1", "decision")
    assert fake_st.query_params == {
        "job": "j", "stage": "applicants", "candidate": "app-1", "tab": "decision",
    }


def test_set_tab_changes_only_the_tab(fake_st):
    fake_st.query_params.update({"job": "j", "candidate": "c", "tab": "overview"})
    nav.set_tab("interview")
    assert fake_st.query_params == {"job": "j", "candidate": "c", "tab": "interview"}


def test_close_candidate_drops_candidate_and_tab_but_keeps_job_and_stage(fake_st):
    fake_st.query_params.update(
        {"job": "j", "stage": "interviews", "candidate": "c", "tab": "decision"}
    )
    nav.close_candidate()
    assert fake_st.query_params == {"job": "j", "stage": "interviews"}


def test_close_candidate_is_safe_when_nothing_is_open(fake_st):
    fake_st.query_params.update({"job": "j"})
    nav.close_candidate()
    assert fake_st.query_params == {"job": "j"}


def test_candidate_navigation_never_reruns(fake_st):
    nav.open_candidate("a", "overview")
    nav.set_tab("screening")
    nav.close_candidate()
    assert fake_st.reruns == 0


def test_requested_candidate_and_tab_are_stripped_and_blank_is_none(monkeypatch):
    monkeypatch.setattr(nav, "st", _FakeSt({"candidate": "  abc ", "tab": "  "}))
    assert nav.requested_candidate() == "abc"
    assert nav.requested_tab() is None
    monkeypatch.setattr(nav, "st", _FakeSt({}))
    assert nav.requested_candidate() is None and nav.requested_tab() is None


@pytest.mark.parametrize("value, expected", [
    ("overview", "overview"), ("decision", "decision"), ("bogus", "overview"),
    ("", "overview"), (None, "overview"), ("OVERVIEW", "overview"),
])
def test_resolve_tab_falls_back_to_overview(value, expected):
    assert nav.resolve_tab(value) == expected


# --- deep_link_target with a candidate ------------------------------------------------------------

_CAND = "11111111-2222-4333-8444-555555555555"
_J = str(JOB)


@pytest.mark.parametrize("params, expected", [
    ({"job": _J, "candidate": _CAND, "tab": "decision"},
     {"job": _J, "candidate": _CAND, "tab": "decision"}),
    ({"job": _J, "stage": "interviews", "candidate": _CAND, "tab": "scorecard"},
     {"job": _J, "stage": "interviews", "candidate": _CAND, "tab": "scorecard"}),
    ({"job": _J, "candidate": _CAND}, {"job": _J, "candidate": _CAND}),
    ({"job": _J, "candidate": f"  {_CAND}  ", "tab": " screening "},
     {"job": _J, "candidate": _CAND, "tab": "screening"}),
    # an unknown tab is dropped (the page reads that as Overview)
    ({"job": _J, "candidate": _CAND, "tab": "bogus"}, {"job": _J, "candidate": _CAND}),
    # a candidate that is not a UUID is dropped, and so is its tab
    ({"job": _J, "candidate": "not-a-uuid", "tab": "decision"}, {"job": _J}),
    ({"job": _J, "candidate": "123"}, {"job": _J}),
    ({"job": _J, "candidate": ""}, {"job": _J}),
    # a tab alone is never carried
    ({"job": _J, "tab": "decision"}, {"job": _J}),
    # no valid job: nothing, whatever else is present
    ({"candidate": _CAND, "tab": "decision"}, None),
    ({"job": "nope", "candidate": _CAND}, None),
])
def test_deep_link_target_with_a_candidate(monkeypatch, params, expected):
    monkeypatch.setattr(nav, "st", _FakeSt(params))
    assert nav.deep_link_target() == expected
