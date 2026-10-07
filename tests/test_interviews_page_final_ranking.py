"""AppTest page tests for Step 10b — the per-job final-ranking section on the
Interviews page, and the scorecard's final-score block.

The loader and the generate action are stubbed (no database, no AI). Scripts
rebind attributes on the real page module, so each is snapshotted at IMPORT time
and restored before and after every test (see the ``apptest-module-mutation-leak``
project note).
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.interviews as I

_TIMEOUT = 60

_PATCHED_ATTRS = (
    "_load_final_ranking_view", "_run_generate_final_ranking",
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


_PRELUDE = '''
import uuid
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS

import streamlit as st

import app.pages.interviews as I

WHEN = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)
st.session_state.setdefault("GEN_CALLS", 0)


def entry(name, *, rank=None, tied=False, screening="7.25", interview="8.33",
          final="7.90", status="RANKED", reason="", confidence="HIGH",
          screening_conf="HIGH", mandatory_unknown=False, eligible=True,
          means=((1, "4.00"), (2, "4.33")), analysis_conf="HIGH",
          analysis_rec="PROCEED", screening_rank=3):
    d = lambda v: None if v is None else D(v)
    return NS(
        entry_id=uuid.uuid4(), application_id=uuid.uuid4(),
        candidate_name=name, candidate_email=name.lower().replace(" ", ".") + "@x.test",
        rank=rank, tied=tied, screening_score=d(screening),
        interview_score=d(interview), final_score=d(final), eligible=eligible,
        mandatory_unknown=mandatory_unknown, entry_status=status,
        status_reason=reason, final_confidence=confidence,
        screening_confidence=screening_conf, screening_rank=screening_rank,
        screening_generated_at=WHEN, rounds_used=tuple(r for r, _ in means),
        round_means=tuple((r, D(m)) for r, m in means),
        analysis_id=uuid.uuid4() if analysis_rec else None,
        analysis_confidence=analysis_conf, analysis_recommendation=analysis_rec,
    )


def run(entries, *, version=1, status="CURRENT", superseded=None):
    return NS(
        final_ranking_id=uuid.uuid4(), job_id=uuid.uuid4(),
        rubric_version_id=uuid.uuid4(), rubric_version_number=version,
        status=status, screening_weight=D("0.4000"), interview_weight=D("0.6000"),
        created_at=WHEN, superseded_at=superseded, requested_by_name="Dana HR",
        entries=tuple(entries),
    )


def stale(*reasons):
    return NS(is_stale=bool(reasons), reasons=tuple(reasons))


__BODY__

def _loader(job_id, acting_user_id):
    if globals().get("BOOM") is not None:
        raise BOOM
    return VIEW


I._load_final_ranking_view = _loader


def fake_generate(job_id, acting_user_id):
    st.session_state["GEN_CALLS"] += 1


I._run_generate_final_ranking = fake_generate
I._render_final_ranking_section("job-1", "u")
'''


def _run(body: str) -> AppTest:
    at = AppTest.from_string(_PRELUDE.replace("__BODY__", body), default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [w.value for w in at.warning]
    parts += [i.value for i in at.info]
    parts += [e.value for e in at.error]
    return "\n".join(str(p) for p in parts)


_EMPTY = 'VIEW = {"current": [], "history": [], "staleness": stale()}'

_FULL = '''
ranked1 = entry("Ananya Rao", rank=1)
ranked2 = entry("Bala Iyer", rank=2, screening="6.00", interview="7.00",
                final="6.60", confidence="MEDIUM", means=((1, "3.50"),),
                analysis_rec=None, analysis_conf=None)
inel = entry("Chitra Nair", status="NOT_RANKED_INELIGIBLE", eligible=False,
             final="9.10", reason="A mandatory requirement was assessed as not met.")
inc1 = entry("Dev Patel", status="INCOMPLETE_SCREENING", screening=None, final=None,
             interview="7.00", confidence=None, means=((1, "3.50"),),
             reason="Generate the screening ranking first - on the Candidates page.")
inc2 = entry("Esha Rao", status="INCOMPLETE_INTERVIEW", interview=None, final=None,
             confidence=None, means=(), reason="No interview round has competency ratings.")
cur = run([ranked1, ranked2, inel, inc1, inc2])
VIEW = {"current": [cur], "history": [cur], "staleness": stale()}
'''


# --- laziness and the trigger ---------------------------------------------------------------


def test_the_section_never_generates_on_page_load():
    at = _run(_FULL)
    assert at.session_state["GEN_CALLS"] == 0


def test_generate_button_when_none_exists_and_regenerate_when_one_does():
    none = _run(_EMPTY)
    assert [b.label for b in none.button] == ["Generate final ranking"]
    assert "No final ranking has been generated for this job yet." in _text(none)

    full = _run(_FULL)
    assert [b.label for b in full.button] == ["Regenerate final ranking"]


def test_clicking_the_button_triggers_exactly_one_generation():
    at = _run(_EMPTY)
    at.button[0].click().run()
    assert at.session_state["GEN_CALLS"] == 1


def test_the_formula_and_the_ai_boundaries_are_stated_up_front():
    body = _text(_run(_EMPTY))
    assert "40% screening score + 60% interview score" in body
    assert "every round" in body and "counting equally" in body
    assert "the AI never assigns a score" in body
    assert "transcripts do not change the number" in body
    assert "does not reject or approve anyone" in body


# --- the ranked table --------------------------------------------------------------------------


def test_ranked_rows_show_rank_name_score_and_breakdown_arithmetic():
    body = _text(_run(_FULL))
    assert "#1. Ananya Rao" in body
    assert "7.90 / 10" in body
    # the numbers HR can reproduce with a calculator
    assert "Breakdown: 40% × screening 7.25 + 60% × interview 8.33 = 7.90" in body
    assert "each rounded to 2 decimals" in body or "rounded to 2 decimals" in body
    assert "#2. Bala Iyer" in body and "6.60 / 10" in body


def test_rounds_used_and_their_means_are_listed_with_equal_weight_caveat():
    body = _text(_run(_FULL))
    assert "round 1: mean 4.00/5, round 2: mean 4.33/5" in body
    assert "Every round counts equally" in body
    assert "rounds without ratings are not scored" in body


def test_confidence_is_shown_with_text_and_its_rule_not_colour_alone():
    body = _text(_run(_FULL))
    assert "Confidence: High" in body and "Confidence: Medium" in body
    assert "lowest of the screening confidence" in body
    assert "capped at Medium" in body


def test_the_analysis_is_context_only_and_labelled_as_ai_generated():
    body = _text(_run(_FULL))
    assert "AI-generated post-interview analysis (context only" in body
    assert "NOT part of the score" in body
    assert "recommends Proceed" in body
    # a candidate whose analysis did not count says so instead of inventing one
    assert "No post-interview analysis counted for this candidate" in body


def test_mandatory_unknown_is_flagged_prominently_and_does_not_exclude():
    at = _run('''
e = entry("Fay Roy", rank=1, mandatory_unknown=True)
VIEW = {"current": [run([e])], "history": [], "staleness": stale()}
''')
    assert any("no evidence either way" in w.value for w in at.warning)
    assert "#1. Fay Roy" in _text(at)


def test_every_displayed_final_score_is_followed_by_its_breakdown():
    at = _run(_FULL)
    body = _text(at)
    # ranked x2 + ineligible x1 each show a score; each has a breakdown line
    assert body.count("Breakdown:") >= 3


# --- ties ----------------------------------------------------------------------------------------


def test_tied_candidates_share_a_rank_and_the_skip_and_caption_are_shown():
    at = _run('''
a = entry("Ann One", rank=1, tied=True, final="8.00")
b = entry("Bob Two", rank=1, tied=True, final="8.00")
c = entry("Cy Three", rank=3, final="7.00")
VIEW = {"current": [run([a, b, c])], "history": [], "staleness": stale()}
''')
    body = _text(at)
    assert body.count("#1. ") == 2 and "#2. " not in body and "#3. Cy Three" in body
    assert body.count("Tied — shares this rank") == 2
    assert (
        "Tied candidates share a rank. The on-screen order within a tie carries "
        "no meaning." in body
    )


def test_no_tie_caption_when_nobody_is_tied():
    assert "Tied candidates share a rank" not in _text(_run(_FULL))


# --- ineligible and incomplete -----------------------------------------------------------------


def test_ineligible_section_is_separate_after_ranked_and_not_a_rejection():
    body = _text(_run(_FULL))
    assert "Not ranked — ineligible (1)" in body
    assert body.index("#2. Bala Iyer") < body.index("Not ranked — ineligible (1)")
    assert body.index("Not ranked — ineligible (1)") < body.index("Chitra Nair")
    assert "This is not a rejection; that remains a human decision." in body
    assert "Not ranked — mandatory requirement not met" in body
    assert "final (for context) **9.10 / 10**" in body    # a score for display only
    # the ineligible candidate carries NO rank number
    assert "#" not in body.split("Chitra Nair")[1].split("Incomplete")[0].replace(
        "Pre-interview screening rank: #3.", ""
    ).replace("##### ", "")


def test_incomplete_section_lists_each_candidate_with_a_plain_reason():
    body = _text(_run(_FULL))
    assert "Incomplete (2)" in body
    assert "Generate the screening ranking first" in body
    assert "No interview round has competency ratings." in body
    assert "Nothing is scored as zero and the weights are never redistributed." in body
    assert "Incomplete — no final score" in body
    # no invented number for the incomplete
    after = body.split("Incomplete (2)")[1]
    assert "Esha Rao" in after and "final (for context)" not in after


def test_a_group_with_nobody_ranked_says_so():
    at = _run('''
inc = entry("Dev Patel", status="INCOMPLETE_SCREENING", screening=None, final=None,
            confidence=None, reason="Generate the screening ranking first.")
VIEW = {"current": [run([inc])], "history": [], "staleness": stale()}
''')
    assert "No candidate in this group could be ranked." in _text(at)


# --- stale banner and history -------------------------------------------------------------------


def test_the_stale_banner_uses_the_rank_drift_language_and_lists_reasons():
    at = _run(_FULL.replace(
        'stale()}', 'stale("1 newly interviewed candidate(s)", '
        '"2 candidate(s) with new interview feedback")}'
    ))
    body = _text(at)
    assert (
        "Final ranking may be out of date — 1 newly interviewed candidate(s); "
        "2 candidate(s) with new interview feedback" in body
    )
    assert "Regenerate before relying on the order." in body


def test_no_banner_when_the_ranking_is_current():
    body = _text(_run(_FULL))
    assert "may be out of date" not in body


def test_earlier_runs_are_collapsed_and_listed():
    at = _run('''
cur = run([entry("Ananya Rao", rank=1)])
old = run([entry("Ananya Rao", rank=1, final="6.50")], status="SUPERSEDED",
          superseded=WHEN)
VIEW = {"current": [cur], "history": [cur, old], "staleness": stale()}
''')
    labels = [e.label for e in at.expander]
    assert "Earlier runs (1)" in labels
    body = _text(at)
    assert "superseded 2026-10-07" in body and "top: Ananya Rao (6.50)" in body


def test_no_history_expander_when_there_is_only_the_current_run():
    at = _run(_FULL)
    assert not any(str(e.label).startswith("Earlier runs") for e in at.expander)


# --- load failure ------------------------------------------------------------------------------------


def test_a_load_failure_is_a_calm_message_not_a_traceback():
    at = _run('''
from sqlalchemy.exc import SQLAlchemyError
BOOM = SQLAlchemyError("down")
VIEW = None
''')
    assert not at.exception
    assert "Couldn't load the final ranking right now." in _text(at)


# --- no recommendation language and no disagreement ------------------------------------------------------


def test_no_disagreement_or_automatic_decision_language():
    lowered = _text(_run(_FULL)).lower()
    for forbidden in ("disagree", "rejected", "auto-reject", "we recommend rejecting"):
        assert forbidden not in lowered


# --- the scorecard block ----------------------------------------------------------------------------------


def _scorecard(final_ranking_code: str, **fields) -> str:
    fields_code = ", ".join(f"{k}={v}" for k, v in fields.items())
    script = f'''
import dataclasses
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS

import app.pages.interviews as I
from tests.test_interviews_page_final_scorecard import _card

base = _card()
interview = NS(
    label="Interview", score=D("7.50"), coverage=None,
    evidence=("Round 1: mean 3.75/5 from 4 rating(s)",),
    provenance="System-calculated score", unavailable_reason=None,
    score_places=2,
)
view = NS(
    **{{f.name: getattr(base, f.name) for f in dataclasses.fields(base)}},
)
view.interview = interview
view.interview_rounds_used = (1, 2)
view.interview_rounds_unscored = (3,)
view.final_ranking = {final_ranking_code}
I._render_final_scorecard(view)
'''
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return _text(at)


_FR = '''NS(
    entry_status="RANKED", status_reason="", final_score=D("7.90"),
    screening_score=D("7.25"), interview_score=D("8.33"), rank=2, tied=True,
    ranked_count=5, final_confidence="MEDIUM", screening_weight=D("0.4000"),
    interview_weight=D("0.6000"),
    generated_at=datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc),
)'''


def test_scorecard_interview_score_shows_two_decimals_and_the_rounds_used():
    body = _scorecard("None")
    assert "Interview: **7.50 / 10**" in body
    assert "The Interview score uses rounds 1 and 2, every round counting equally." in body
    assert "Not scored (feedback without ratings, never counted as zero): round 3." in body


def test_scorecard_says_when_no_final_ranking_exists():
    assert "Final ranking not generated yet." in _scorecard("None")


def test_scorecard_shows_final_score_rank_confidence_breakdown_and_tie():
    body = _scorecard(_FR)
    assert "Final score:** **7.90 / 10**" in body
    assert "rank #2 of 5 (tied)" in body
    assert "40% × screening 7.25 + 60% × interview 8.33." in body
    assert "Confidence: Medium" in body
    assert "Tied candidates share a rank." in body
    assert "Not AI-produced, and not a hiring decision." in body
    assert "2026-10-07 09:30 UTC" in body


def test_scorecard_marks_an_ineligible_score_as_not_ranked():
    body = _scorecard(_FR.replace('"RANKED"', '"NOT_RANKED_INELIGIBLE"').replace(
        "rank=2, tied=True", "rank=None, tied=False").replace(
        'status_reason=""', 'status_reason="A mandatory requirement was assessed as not met."'))
    assert "Final score (not ranked):** **7.90 / 10**" in body
    assert "A mandatory requirement was assessed as not met." in body
    assert "rank #" not in body


def test_scorecard_incomplete_entry_shows_no_score_and_the_reason():
    body = _scorecard('''NS(
    entry_status="INCOMPLETE_SCREENING",
    status_reason="Generate the screening ranking first.", final_score=None,
    screening_score=None, interview_score=D("8.00"), rank=None, tied=False,
    ranked_count=0, final_confidence=None, screening_weight=D("0.4000"),
    interview_weight=D("0.6000"),
    generated_at=datetime(2026, 10, 7, tzinfo=timezone.utc),
)''')
    assert "No final score" in body
    assert "Generate the screening ranking first." in body
    assert "Final score:**" not in body
