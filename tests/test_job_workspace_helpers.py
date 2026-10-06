"""Pure helpers of the job workspace (HR UI, Increment 2): ``default_stage``,
``stage_label``, the page's label/neighbour helpers and the query-parameter
functions. No database, no Streamlit server."""

from __future__ import annotations

import uuid

import pytest

import app.utils.workspace_nav as nav
from app.pages.job_workspace import neighbours, stage_count_text, stage_labels
from app.services.job_workspace_service import (
    STAGE_KEYS,
    JobHeaderFacts,
    JobStageSummary,
    StageSummary,
    default_stage,
)
from app.utils.ui import stage_label

_NONE = StageSummary(0, False, 0)


def _summary(**over) -> JobStageSummary:
    stages = {key: _NONE for key in STAGE_KEYS}
    interviewed = over.pop("interviewed_count", 0)
    stages.update(over)
    return JobStageSummary(
        header=JobHeaderFacts(uuid.uuid4(), "V_001", "Role", "OPEN", None),
        interviewed_count=interviewed,
        **stages,
    )


# --- default_stage ----------------------------------------------------------------


def test_default_stage_is_setup_when_nothing_is_done():
    assert default_stage(_summary()) == "setup"


@pytest.mark.parametrize("done, expected", [
    (("setup",), "applicants"),
    (("setup", "applicants"), "shortlist"),
    (("setup", "applicants", "shortlist"), "interviews"),
    (("setup", "applicants", "shortlist", "interviews"), "final_ranking"),
])
def test_default_stage_is_the_first_incomplete_one(done, expected):
    s = _summary(**{k: StageSummary(1, True, 0) for k in done})
    assert default_stage(s) == expected


def test_default_stage_skips_over_a_gap_to_the_first_incomplete():
    s = _summary(setup=StageSummary(1, True, 0), shortlist=StageSummary(1, True, 0))
    assert default_stage(s) == "applicants"


def test_default_stage_is_the_last_when_everything_is_complete():
    s = _summary(**{k: StageSummary(1, True, 0) for k in STAGE_KEYS})
    assert default_stage(s) == "final_ranking"


def test_stage_summary_lookup_rejects_an_unknown_key():
    with pytest.raises(KeyError):
        _summary().stage("nope")


# --- stage_label ---------------------------------------------------------------------


def test_label_plain():
    assert stage_label(5, "Final ranking") == "5. Final ranking"


def test_label_with_count():
    assert stage_label(3, "Shortlist", count_text="2") == "3. Shortlist · 2"


def test_label_complete_gets_a_tick_prefix():
    assert stage_label(1, "Setup", count_text="v1", complete=True) == "✓ 1. Setup · v1"


def test_label_attention_adds_a_to_review_part():
    assert (
        stage_label(2, "Applicants", count_text="4", attention=2)
        == "2. Applicants · 4 · 2 to review"
    )


def test_label_all_parts():
    assert (
        stage_label(2, "Applicants", count_text="5", complete=True, attention=1)
        == "✓ 2. Applicants · 5 · 1 to review"
    )


@pytest.mark.parametrize("attention", [0, -1])
def test_label_ignores_zero_or_negative_attention(attention):
    assert stage_label(4, "Interviews", attention=attention) == "4. Interviews"


def test_label_is_plain_text_with_no_markup_or_emoji():
    label = stage_label(2, "Applicants", count_text="5", complete=True, attention=1)
    assert not any(ch in label for ch in "<>*_`[]:")
    assert all(ord(ch) < 0x2700 or ch == "✓" or ch == "·" for ch in label)


# --- the page's label helpers -------------------------------------------------------------


def test_count_text_per_stage():
    s = _summary(
        setup=StageSummary(2, True, 0), applicants=StageSummary(5, True, 1),
        shortlist=StageSummary(3, True, 0), interviews=StageSummary(0, False, 0),
        final_ranking=StageSummary(1, False, 2), interviewed_count=3,
    )
    assert stage_count_text("setup", s) == "v2"
    assert stage_count_text("applicants", s) == "5"
    assert stage_count_text("shortlist", s) == "3"
    assert stage_count_text("interviews", s) == "0"
    assert stage_count_text("final_ranking", s) == "1 of 3 decided"


def test_count_text_hides_empty_setup_and_final_ranking():
    s = _summary()
    assert stage_count_text("setup", s) is None
    assert stage_count_text("final_ranking", s) is None


def test_stage_labels_cover_every_stage_in_order_with_numbers():
    s = _summary(
        setup=StageSummary(1, True, 0), applicants=StageSummary(4, True, 2),
        shortlist=StageSummary(2, True, 0),
    )
    labels = stage_labels(s)
    assert list(labels) == list(STAGE_KEYS)
    assert labels["setup"] == "✓ 1. Setup · v1"
    assert labels["applicants"] == "✓ 2. Applicants · 4 · 2 to review"
    assert labels["shortlist"] == "✓ 3. Shortlist · 2"
    assert labels["interviews"] == "4. Interviews · 0"
    assert labels["final_ranking"] == "5. Final ranking"


@pytest.mark.parametrize("stage, expected", [
    ("setup", (None, "applicants")),
    ("applicants", ("setup", "shortlist")),
    ("shortlist", ("applicants", "interviews")),
    ("interviews", ("shortlist", "final_ranking")),
    ("final_ranking", ("interviews", None)),
])
def test_neighbours(stage, expected):
    assert neighbours(stage) == expected


# --- query-parameter state (the documented behaviour) --------------------------------------


class _FakeSt:
    """Stands in for ``streamlit`` inside workspace_nav: a plain dict as the query
    string, and ``rerun`` that must never be called."""

    def __init__(self, params=None):
        self.query_params = dict(params or {})
        self.reruns = 0

    def rerun(self):                                   # pragma: no cover - must not run
        self.reruns += 1
        raise AssertionError("workspace navigation must not call st.rerun()")


@pytest.fixture
def fake_st(monkeypatch):
    fake = _FakeSt()
    monkeypatch.setattr(nav, "st", fake)
    return fake


def test_open_workspace_replaces_the_query_string(fake_st):
    fake_st.query_params.update({"other": "x", "stage": "old"})
    nav.open_workspace("job-1")
    assert fake_st.query_params == {"job": "job-1"}


def test_open_workspace_can_name_a_stage(fake_st):
    nav.open_workspace("job-1", "shortlist")
    assert fake_st.query_params == {"job": "job-1", "stage": "shortlist"}


def test_set_stage_changes_only_the_stage_and_keeps_the_job(fake_st):
    nav.open_workspace("job-1", "setup")
    nav.set_stage("interviews")
    assert fake_st.query_params == {"job": "job-1", "stage": "interviews"}


def test_close_workspace_drops_both_parameters(fake_st):
    nav.open_workspace("job-1", "setup")
    nav.close_workspace()
    assert fake_st.query_params == {}


def test_requested_values_are_stripped_and_blank_is_none(monkeypatch):
    monkeypatch.setattr(nav, "st", _FakeSt({"job": "  abc ", "stage": "   "}))
    assert nav.requested_job() == "abc"
    assert nav.requested_stage() is None
    monkeypatch.setattr(nav, "st", _FakeSt({}))
    assert nav.requested_job() is None and nav.requested_stage() is None


def test_navigation_never_reruns_so_it_cannot_loop(fake_st):
    """Callbacks only write the URL; Streamlit reruns by itself afterwards."""
    nav.open_workspace("job-1", "setup")
    nav.set_stage("applicants")
    nav.close_workspace()
    assert fake_st.reruns == 0


def test_url_is_the_only_state_so_back_forward_shows_stale_content_until_next_click(
    monkeypatch,
):
    """PINS THE DOCUMENTED LIMITATION (see app/utils/workspace_nav.py): Back/Forward
    changes the query string without a script rerun. The state functions therefore
    read the URL afresh every time they are called — the next interaction picks up
    whatever the URL says — and nothing is cached in between."""
    fake = _FakeSt({"job": "job-1", "stage": "shortlist"})
    monkeypatch.setattr(nav, "st", fake)
    assert nav.requested_stage() == "shortlist"
    fake.query_params["stage"] = "setup"          # what the browser's Back button does
    assert nav.requested_stage() == "setup"       # re-read, never remembered
    assert fake.reruns == 0                       # and the helpers did not force a rerun


# --- deep_link_target ---------------------------------------------------------------------


_JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"


@pytest.mark.parametrize("params, expected", [
    ({"job": _JOB, "stage": "shortlist"}, {"job": _JOB, "stage": "shortlist"}),
    ({"job": _JOB}, {"job": _JOB}),
    ({"job": f"  {_JOB}  ", "stage": " setup "}, {"job": _JOB, "stage": "setup"}),
    ({"job": _JOB, "stage": ""}, {"job": _JOB}),
    ({"job": _JOB, "stage": "not-a-stage"}, {"job": _JOB, "stage": "not-a-stage"}),
    ({"job": "123"}, None),
    ({"job": "not-a-uuid", "stage": "setup"}, None),
    ({"job": ""}, None),
    ({"stage": "setup"}, None),
    ({}, None),
])
def test_deep_link_target(monkeypatch, params, expected):
    monkeypatch.setattr(nav, "st", _FakeSt(params))
    assert nav.deep_link_target() == expected
