"""Structural guarantees for the post-interview analysis block on the
Interviews page (Phase 4 Step 9).

The behaviour worth pinning here is the expensive-and-silent one: rendering
this page must NEVER trigger the analysis. CLAUDE.md §§9 and 29 make the AI
call a deliberate, explicitly-requested action; a refactor that moved it into a
loader would look identical on screen and quietly bill a Claude call for every
shortlisted candidate on every rerun.

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

_TIMEOUT = 60

#: Module attributes the scripts below reassign. ``AppTest.from_string``
#: executes in THIS process, so those assignments mutate the real module and
#: would leak into every later test — the fixture restores them.
#:
#: ``_render_shortlisted_section`` is in this list even though no script here
#: assigns it: ``tests/test_candidates_page_structure.py`` replaces it on this
#: same module and does not put it back, so by the time these tests run it can
#: already be a stub that renders nothing. This file is the first to depend on
#: it actually running, which is why the leak only surfaces here.
_PATCHED_ATTRS = (
    "_load_view",
    "load_job_options",
    "create_post_interview_analysis",
    "_render_feedback_section",
    "_render_shortlisted_section",
)

#: Captured at IMPORT time, not fixture-setup time. pytest finishes collecting
#: (and therefore importing) every test module before it runs the first test,
#: so these are the genuine functions — snapshotting in the fixture instead
#: would faithfully preserve another module's leaked stub.
_PRISTINE = {name: getattr(I, name) for name in _PATCHED_ATTRS}


@pytest.fixture(autouse=True)
def _restore_interviews_module():
    # Restore BEFORE the test too, to undo anything an earlier module leaked.
    for name, value in _PRISTINE.items():
        setattr(I, name, value)
    try:
        yield
    finally:
        for name, value in _PRISTINE.items():
            setattr(I, name, value)


_APP_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


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
class _AnalysisView:
    analysis_id: uuid.UUID
    summary: str = "AI consolidated summary."
    strengths: tuple = ("A strength.",)
    gaps: tuple = ()
    unknowns: tuple = ()
    evidence_consistency_notes: str = "Sources line up."
    confidence: str = "MEDIUM"
    ai_recommendation: str = "PROCEED"
    human_recommendation_snapshot: str = "REJECT"
    analyzed_only_latest_feedback: bool = True
    ai_model: str = "claude-sonnet-5"
    status: str = "CURRENT"
    superseded_at: datetime | None = None
    requested_by_name: str = "HR Person"
    created_at: datetime = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _script(
    *, has_feedback: bool, has_analysis: bool, superseded: int = 0,
    latest_only: bool = True,
) -> str:
    """A page script with every loader stubbed, recording AI-call attempts."""
    return f'''
import uuid
from datetime import datetime, timezone

import streamlit as st

import app.pages.interviews as I
from app.utils.session import SESSION_USER_KEY
from tests.test_interviews_page_analysis_section import _AnalysisView, _Row

APP_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
AI_CALLS = []

current = (
    _AnalysisView(
        analysis_id=uuid.uuid4(),
        analyzed_only_latest_feedback={latest_only},
    )
    if {has_analysis}
    else None
)
history = ([current] if current is not None else []) + [
    _AnalysisView(
        analysis_id=uuid.uuid4(),
        summary=f"Older summary {{i}}.",
        status="SUPERSEDED",
        superseded_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    for i in range({superseded})
]

I._load_view = lambda job_id, acting_user_id: {{
    "shortlisted": [_Row()],
    "guides_by_application": {{}},
    "feedback_by_application": {{
        APP_ID: {{"context": None, "history": ([object()] if {has_feedback} else [])}}
    }},
    "analysis_by_application": {{
        APP_ID: {{"current": current, "history": history}}
    }},
}}
I.load_job_options = lambda: [
    {{"id": "job-1", "title": "Backend Engineer", "status": "OPEN"}}
]
# Step 8's block is not under test here; stubbing it keeps the feedback
# fixtures to the one thing the analysis section actually reads (whether any
# feedback exists at all).
I._render_feedback_section = lambda *a, **k: None


def fake_create(*a, **k):
    AI_CALLS.append((a, k))


I.create_post_interview_analysis = fake_create

st.session_state[SESSION_USER_KEY] = {{
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test",
    "full_name": "P",
    "role": "HR",
}}
I.render_interviews_page()
st.session_state["_ai_calls"] = len(AI_CALLS)
'''


def _run(**kw) -> AppTest:
    at = AppTest.from_string(_script(**kw), default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption] if hasattr(at, "caption") else []
    parts += [i.value for i in at.info]
    return "\n".join(str(p) for p in parts)


# --- the guarantee that matters --------------------------------------


def test_rendering_the_page_never_triggers_the_analysis():
    """CLAUDE.md §§9, 29 — the AI call is click-gated, never a page-load
    side-effect."""
    at = _run(has_feedback=True, has_analysis=False)
    assert at.session_state["_ai_calls"] == 0


def test_rendering_an_existing_analysis_never_regenerates_it():
    at = _run(has_feedback=True, has_analysis=True)
    assert at.session_state["_ai_calls"] == 0


# --- the section itself ----------------------------------------------


def test_analysis_section_is_rendered_for_a_candidate():
    at = _run(has_feedback=True, has_analysis=True)
    labels = [e.label for e in at.expander]
    assert "Post-interview AI analysis" in labels


def test_run_button_offered_once_feedback_exists():
    at = _run(has_feedback=True, has_analysis=False)
    keys = [b.key for b in at.button]
    assert f"pia_{_APP_ID}" in keys
    labels = [b.label for b in at.button if b.key == f"pia_{_APP_ID}"]
    assert labels == ["Run post-interview analysis"]


def test_button_says_regenerate_once_an_analysis_exists():
    at = _run(has_feedback=True, has_analysis=True)
    labels = [b.label for b in at.button if b.key == f"pia_{_APP_ID}"]
    assert labels == ["Regenerate post-interview analysis"]


def test_no_run_button_without_interview_feedback():
    """The analysis reads the feedback, so it is not offered before there is
    any — the page explains that instead of failing in the service."""
    at = _run(has_feedback=False, has_analysis=False)
    keys = [b.key for b in at.button]
    assert f"pia_{_APP_ID}" not in keys
    assert any(
        "Record the human interview feedback first" in str(i.value)
        for i in at.info
    )


def test_analysis_prose_is_rendered():
    at = _run(has_feedback=True, has_analysis=True)
    body = _text(at)
    assert "AI consolidated summary." in body
    assert "A strength." in body
    assert "Sources line up." in body


def test_both_recommendations_are_shown_without_claiming_agreement():
    """§8 is a later step: the page shows the two recommendations side by side
    and must NOT assert that they agree or disagree."""
    at = _run(has_feedback=True, has_analysis=True)
    body = _text(at)
    assert "AI recommends" in body
    assert "Interviewer recommended" in body
    lowered = body.lower()
    for forbidden in (
        "disagreement", "disagree", "they agree", "agreement detected",
        "conflict detected",
    ):
        assert forbidden not in lowered


def test_superseded_analyses_are_offered_as_history():
    at = _run(has_feedback=True, has_analysis=True, superseded=2)
    labels = [e.label for e in at.expander]
    assert "Earlier analyses (2)" in labels


def test_no_history_expander_when_nothing_was_superseded():
    at = _run(has_feedback=True, has_analysis=True, superseded=0)
    labels = [e.label for e in at.expander]
    assert not any(str(x).startswith("Earlier analyses") for x in labels)


def test_page_states_the_analysis_does_not_decide():
    at = _run(has_feedback=True, has_analysis=True)
    body = _text(at).lower()
    assert "it does not decide" in body or "not a hiring decision" in body


# --- scope disclosure (Step 9 requirement #5) ------------------------


def test_page_discloses_that_only_the_latest_feedback_was_read():
    """The analysis reads exactly ONE feedback record, and the page has to say
    so — otherwise a reader of a round-2 interview could reasonably assume the
    analysis covered both rounds.

    The disclosure is rendered by the page from the stored
    ``analyzed_only_latest_feedback`` field. It is deliberately NOT expected to
    come from the AI's prose: the prompt never asks for it, so relying on the
    model to volunteer it would make the disclosure conditional on model
    behaviour.
    """
    at = _run(has_feedback=True, has_analysis=True)
    body = _text(at)
    assert "Based on the most recent interview-feedback record only." in body
    assert "regenerate to include it" in body


def test_the_disclosure_is_driven_by_the_stored_field():
    """Guards against the caption being hardcoded outside the conditional,
    which would make the test above pass while the field meant nothing."""
    at = _run(has_feedback=True, has_analysis=True, latest_only=False)
    body = _text(at)
    # The analysis itself still renders — so the missing caption is a real
    # consequence of the field, not a page that failed to draw anything.
    assert "AI consolidated summary." in body
    assert "most recent interview-feedback record only" not in body
