"""Structural guarantees for the Final Scorecard block on the Interviews page
(Phase 4 Step 10).

Two things are worth pinning here because getting them wrong is silent:

* the scorecard is assembled **on demand**, not on every rerun — the assembly
  issues roughly eight guarded reads per candidate and this page renders every
  shortlisted candidate at once, so an eager version would look identical on
  screen while multiplying the query cost (the same reasoning behind the
  Candidates page's lazy application cards);
* the page renders the two §10 fields this MVP does not compute as fixed
  strings, and never states or implies a verdict about the two recommendations.

Streamlit's own ``AppTest`` harness with the service layer stubbed — no
database, no Streamlit server, no AI.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from streamlit.testing.v1 import AppTest

import app.pages.interviews as I
from app.services.final_scorecard_service import (
    DISAGREEMENT_NOT_ASSESSED,
    FINAL_DECISION_NOT_DECIDED,
    MULTIPLE_RUBRIC_VERSIONS_WARNING,
    Provenance,
)

_TIMEOUT = 60

#: Attributes the scripts below reassign, plus ones an earlier test module may
#: have left stubbed. See tests/test_interviews_page_analysis_section.py for why
#: the pristine snapshot is taken at import time rather than in the fixture.
_PATCHED_ATTRS = (
    "get_final_scorecard",
)

_PRISTINE = {name: getattr(I, name) for name in _PATCHED_ATTRS}


@pytest.fixture(autouse=True)
def _restore_interviews_module():
    for name, value in _PRISTINE.items():
        setattr(I, name, value)
    try:
        yield
    finally:
        for name, value in _PRISTINE.items():
            setattr(I, name, value)


_APP_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


@dataclass(frozen=True)
class _Row:
    application_id: uuid.UUID = _APP_ID
    candidate_name: str = "Casey Candidate"
    candidate_email: str = "casey@x.test"
    shortlist_reason: str | None = None
    rank_position_at_decision: int | None = 1
    current_rank_available: bool = True
    current_rank_position: int | None = 1
    rubric_version_number: int | None = 1
    rubric_version_status: str | None = "APPROVED"
    guide_exists: bool = True


@dataclass(frozen=True)
class _Bucket:
    label: str
    score: int | None
    coverage: float | None
    evidence: tuple
    provenance: str
    unavailable_reason: str | None


@dataclass(frozen=True)
class _Crit:
    criterion_text: str
    requirement_type: str
    result: str
    evidence_summary: str
    confidence: str
    provenance: str


@dataclass(frozen=True)
class _VerRef:
    source_label: str
    rubric_version_id: uuid.UUID
    version_number: int | None
    status: str | None


@dataclass(frozen=True)
class _Rating:
    competency_label: str
    rating: int
    rating_max: int
    comment: str | None


@dataclass(frozen=True)
class _TranscriptEntry:
    round: int
    category: str
    question_text: str
    answer_text: str | None
    answered: bool


@dataclass(frozen=True)
class _Ranking:
    rank_position: int | None = 2
    overall_score: float | None = 7.25
    eligible: bool | None = True
    mandatory_unknown_flag: bool | None = False
    rubric_version_id: uuid.UUID | None = None
    generated_at: datetime | None = None
    is_shortlisted: bool | None = True
    rank_position_at_decision: int | None = 2
    shortlist_reason: str | None = "Strong Spark evidence"


@dataclass(frozen=True)
class _Card:
    application_id: uuid.UUID = _APP_ID
    candidate_name: str = "Casey Candidate"
    candidate_email: str = "casey@x.test"
    job_id: uuid.UUID = _APP_ID
    job_title: str = "Backend Engineer"
    application_status: str = "SCREENING_EVALUATED"
    rubric_versions: tuple = ()
    has_multiple_rubric_versions: bool = False
    overall_score: float | None = 7.25
    overall_score_provenance: str = Provenance.RANKING
    overall_score_unavailable_reason: str | None = None
    screening_confidence: str | None = "HIGH"
    post_interview_confidence: str | None = "MEDIUM"
    mandatory_criteria: tuple = ()
    preferred_criteria: tuple = ()
    other_criteria: tuple = ()
    requirements: _Bucket = None            # type: ignore[assignment]
    experience: _Bucket = None              # type: ignore[assignment]
    behavioral: _Bucket = None              # type: ignore[assignment]
    interview: _Bucket = None               # type: ignore[assignment]
    screening_strengths: tuple = ("Strong Python",)
    screening_gaps: tuple = ()
    screening_unknowns: tuple = ()
    post_interview_strengths: tuple = ("AI strength.",)
    post_interview_gaps: tuple = ()
    post_interview_unknowns: tuple = ()
    post_interview_summary: str | None = "AI consolidated summary."
    post_interview_evidence_consistency: str | None = "Sources line up."
    resume_evidence: dict = None            # type: ignore[assignment]
    has_screening_transcript: bool = True
    screening_question_count: int = 2
    screening_transcript: tuple = ()
    interview_guide_question_count: int | None = 8
    interview_round: int | None = 1
    interview_notes: str | None = "Interviewer's own words."
    interview_ratings: tuple = ()
    interviewer_name: str | None = "HR Person"
    interview_recorded_at: datetime | None = datetime(2026, 9, 7, tzinfo=timezone.utc)
    screening_ai_recommendation: str | None = "PROCEED"
    post_interview_ai_recommendation: str | None = "PROCEED"
    human_recommendation: str | None = "REJECT"
    disagreement_status: str = DISAGREEMENT_NOT_ASSESSED
    final_decision_status: str = FINAL_DECISION_NOT_DECIDED
    ranking: _Ranking = None                # type: ignore[assignment]
    missing_sources: tuple = ()


def _card(**over):
    kw = dict(
        rubric_versions=(
            _VerRef("Screening evaluation", uuid.uuid4(), 1, "APPROVED"),
        ),
        requirements=_Bucket(
            "Requirements", 8, 1.0, ("Python: solid evidence",),
            Provenance.SYSTEM_SCORE, None,
        ),
        experience=_Bucket(
            "Experience", 7, 1.0, ("Fintech: three years",),
            Provenance.SYSTEM_SCORE, None,
        ),
        behavioral=_Bucket(
            "Behavioral", None, None, (), Provenance.SYSTEM_SCORE,
            "Not assessed for this role",
        ),
        interview=_Bucket(
            "Interview", 7, None, ("System design: 4/5",),
            Provenance.SYSTEM_SCORE, None,
        ),
        resume_evidence={"skills": ["Python", "Spark"]},
        interview_ratings=(_Rating("System design", 4, 5, "Clear."),),
        screening_transcript=(
            _TranscriptEntry(1, "JD", "How long have you used Python?",
                              "About five years.", True),
            _TranscriptEntry(1, "GAP", "Describe your Kafka experience.",
                              None, False),
        ),
        ranking=_Ranking(),
    )
    kw.update(over)
    return _Card(**kw)


def _script(*, open_panel: bool, card_kwargs: str = "") -> str:
    return f'''
import uuid

import streamlit as st

import app.pages.interviews as I
from app.utils.session import SESSION_USER_KEY
from tests.test_interviews_page_final_scorecard import _Row, _card

APP_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CALLS = []


def fake_get(db, application_id, *, acting_user_id):
    CALLS.append(application_id)
    return _card({card_kwargs})


I.get_final_scorecard = fake_get

if {open_panel}:
    st.session_state[I._scorecard_open_key(APP_ID)] = True

st.session_state[SESSION_USER_KEY] = {{
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test",
    "full_name": "P",
    "role": "HR",
}}
# Increment 5: the old Interviews page is gone; the candidate page's Scorecard tab
# calls this same section directly, so the tests call it too.
I._render_final_scorecard_section(APP_ID, "11111111-1111-1111-1111-111111111111")
st.session_state["_calls"] = len(CALLS)
'''


def _run(**kw) -> AppTest:
    at = AppTest.from_string(_script(**kw), default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at) -> str:
    # st.caption lands in at.caption, NOT at.markdown — several §10 labels and
    # every provenance line are captions, so both must be collected.
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption] if hasattr(at, "caption") else []
    parts += [i.value for i in at.info]
    parts += [w.value for w in at.warning]
    return "\n".join(str(p) for p in parts)


# --- laziness --------------------------------------------------------


def test_scorecard_is_not_assembled_until_opened():
    at = _run(open_panel=False)
    assert at.session_state["_calls"] == 0
    keys = [b.key for b in at.button]
    assert f"fsc_btn_{_APP_ID}" in keys


def test_opening_the_panel_assembles_it_once():
    at = _run(open_panel=True)
    assert at.session_state["_calls"] == 1


def test_button_label_reflects_state():
    closed = _run(open_panel=False)
    assert [b.label for b in closed.button if b.key == f"fsc_btn_{_APP_ID}"] == [
        "Show final scorecard"
    ]
    opened = _run(open_panel=True)
    assert [b.label for b in opened.button if b.key == f"fsc_btn_{_APP_ID}"] == [
        "Hide final scorecard"
    ]


# --- the two uncomputed §10 fields ----------------------------------


def test_disagreement_field_renders_the_fixed_placeholder():
    at = _run(open_panel=True)
    body = _text(at)
    assert DISAGREEMENT_NOT_ASSESSED in body
    assert "AI-Human disagreement" in body


def test_page_never_states_a_disagreement_verdict():
    """Human REJECT vs AI PROCEED is the maximal divergence — the page must
    still not say they disagree."""
    at = _run(open_panel=True)
    lowered = _text(at).lower()
    for forbidden in (
        "disagreement: yes", "disagreement: no", "they disagree",
        "they agree", "conflict detected", "mismatch",
    ):
        assert forbidden not in lowered


def test_final_decision_renders_not_decided():
    at = _run(open_panel=True)
    body = _text(at)
    assert FINAL_DECISION_NOT_DECIDED in body
    assert "Final human decision" in body


# --- §10 content -----------------------------------------------------


def test_all_three_recommendations_are_shown_with_provenance():
    at = _run(open_panel=True)
    body = _text(at)
    assert "Screening AI recommendation" in body
    assert "Post-interview AI recommendation" in body
    assert "Human recommendation" in body


def test_scores_are_shown_with_their_evidence():
    """CLAUDE.md line 1074 — never a score without supporting context."""
    at = _run(open_panel=True)
    body = _text(at)
    assert "Requirements" in body and "Python: solid evidence" in body
    assert "Experience" in body and "Fintech: three years" in body
    assert "Interview" in body and "System design: 4/5" in body


def test_an_unavailable_score_shows_its_reason_not_a_zero():
    at = _run(open_panel=True)
    body = _text(at)
    assert "Behavioral" in body
    assert "Not assessed for this role" in body
    assert "Behavioral: 0" not in body


def test_ranking_is_labeled_pre_interview():
    at = _run(open_panel=True)
    body = _text(at)
    assert "Pre-Interview Candidate Ranking" in body


def test_sections_are_ordered_evidence_to_final_decision():
    at = _run(open_panel=True)
    body = _text(at)
    order = [
        "### Evidence",
        "### AI assessment",
        "### Human interview feedback",
        "### System-calculated scores",
        "### Recommendations",
        "### Final human decision",
    ]
    positions = [body.index(h) for h in order]
    assert positions == sorted(positions), body


# --- rubric divergence ----------------------------------------------


def test_multi_version_warning_is_shown_when_sources_diverge():
    at = _run(open_panel=True, card_kwargs="has_multiple_rubric_versions=True")
    warnings = [str(w.value) for w in at.warning]
    assert any(MULTIPLE_RUBRIC_VERSIONS_WARNING in w for w in warnings)


def test_no_warning_when_versions_agree():
    at = _run(open_panel=True)
    warnings = [str(w.value) for w in at.warning]
    assert not any(MULTIPLE_RUBRIC_VERSIONS_WARNING in w for w in warnings)


# --- missing sources -------------------------------------------------


def test_missing_sources_are_listed_not_hidden():
    at = _run(
        open_panel=True,
        card_kwargs='missing_sources=("Human interview feedback",)',
    )
    body = _text(at)
    assert "Not yet available" in body
    assert "Human interview feedback" in body


# --- screening transcript (wired to the real accessor) -----------


def test_transcript_preview_renders_real_qa_content():
    """Real question/answer text from the fixture's transcript entries, not
    fabricated or placeholder text — the default fixture already seeds two
    real entries (one answered, one not)."""
    at = _run(open_panel=True)
    body = _text(at)
    assert "How long have you used Python?" in body
    assert "About five years." in body
    assert "Describe your Kafka experience." in body
    assert "(not answered)" in body


def test_transcript_count_appears_in_the_expander_label():
    at = _run(open_panel=True)
    labels = [e.label for e in at.expander]
    assert any("Screening transcript (2 question" in label for label in labels)


def test_no_transcript_shows_an_honest_unavailable_message():
    at = _run(
        open_panel=True,
        card_kwargs=(
            "has_screening_transcript=False, screening_question_count=0, "
            "screening_transcript=()"
        ),
    )
    body = _text(at)
    assert "No screening transcript is available for this candidate yet." in body
    labels = [e.label for e in at.expander]
    assert not any("Screening transcript" in label for label in labels)


# --- no raw HTML / CSS -----------------------------------------------


def test_page_uses_no_unsafe_html_or_custom_css():
    # Still true after the HR design foundation: the page file itself has no raw
    # HTML. The app's ONE unsafe_allow_html call lives in app/ui/theme.py, and
    # tests/test_ui_theme.py enforces that it is the only one under app/.
    import pathlib
    src = pathlib.Path("app/pages/interviews.py").read_text(encoding="utf-8")
    for forbidden in ("unsafe_allow_html", "<style", "<div", "<span", "<script"):
        assert forbidden not in src
