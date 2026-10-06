"""Page tests for Increment C — what a post-interview analysis says it read.

AppTest harness (same style as ``test_interviews_page_analysis_section``): the
real ``_render_analysis_section`` / ``_render_final_scorecard`` run against fake
views. The disclosure is driven by STORED provenance, never by the AI's prose.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.interviews as I

_TIMEOUT = 60

_PATCHED_ATTRS = ("_load_view", "load_job_options", "_render_shortlisted_section")
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


_SCRIPT = '''
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import streamlit as st

import app.pages.interviews as I

APP_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
FB1, FB2, FB3 = (uuid.UUID(int=i) for i in (101, 102, 103))
TR1, TR2, TR3, TR_NEW = (uuid.UUID(int=i) for i in (201, 202, 203, 204))
WHEN = datetime(2026, 10, 6, tzinfo=timezone.utc)

def fb_ref(fid, rnd, rec):
    return SimpleNamespace(feedback_id=fid, interview_round=rnd,
                           recommendation_snapshot=rec)

def tr_ref(tid, rnd):
    return SimpleNamespace(transcript_id=tid, interview_round=rnd)

def tr_version(tid, fid, rnd, *, status="CURRENT", extractable=True):
    return SimpleNamespace(
        transcript_id=tid, interview_feedback_id=fid, interview_round=rnd,
        status=status, text_extractable=extractable, file_name="x.pdf",
        mime_type="application/pdf", file_size_bytes=2048, version_number=1,
        uploaded_by_name="U", created_at=WHEN, superseded_at=None,
    )

def analysis(**over):
    kw = dict(
        analysis_id=uuid.uuid4(), summary="AI consolidated summary.",
        strengths=("A strength.",), gaps=(), unknowns=(),
        evidence_consistency_notes="Sources line up.", confidence="MEDIUM",
        ai_recommendation="PROCEED", human_recommendation_snapshot="PROCEED",
        analyzed_only_latest_feedback=False, ai_model="claude-sonnet-5",
        status="CURRENT", superseded_at=None, requested_by_name="HR Person",
        created_at=WHEN, transcript_evidence_notes="",
        transcript_unreadable_rounds=(), feedback_records=(),
        transcript_records=(),
    )
    kw.update(over)
    return SimpleNamespace(**kw)

def feedback_item(fid, rnd):
    return SimpleNamespace(feedback_id=fid, interview_round=rnd)

__BODY__

I._render_analysis_section(APP_ID, feedback_state, analysis_state, "u")
'''

_DEFAULT_BODY = '''
current = analysis(
    feedback_records=(fb_ref(FB1, 1, "HOLD"), fb_ref(FB2, 2, "PROCEED")),
    transcript_records=(tr_ref(TR1, 1), tr_ref(TR2, 2)),
    transcript_evidence_notes="Transcripts confirmed the Spark claim.",
)
feedback_state = {APP_ID: {
    "context": None,
    "history": [feedback_item(FB2, 2), feedback_item(FB1, 1)],
    "transcripts": {FB1: [tr_version(TR1, FB1, 1)], FB2: [tr_version(TR2, FB2, 2)]},
}}
analysis_state = {APP_ID: {"current": current, "history": [current]}}
'''


def _run(body: str = _DEFAULT_BODY) -> AppTest:
    script = _SCRIPT.replace("__BODY__", body)
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [w.value for w in at.warning]
    parts += [i.value for i in at.info]
    return "\n".join(str(p) for p in parts)


_STALE = (
    "Feedback or a transcript was added or replaced after this analysis was "
    "generated. Regenerate to include it."
)


def _variant(*, feedback, transcripts, history_extra="", records_fb=None,
             records_tr=None, **analysis_over) -> str:
    """Build a body from compact specs. ``feedback`` / ``transcripts`` are the
    CURRENT state of the application; ``records_*`` default to the same sets (an
    up-to-date analysis)."""
    fb_ids = {1: "FB1", 2: "FB2", 3: "FB3"}
    tr_ids = {1: "TR1", 2: "TR2", 3: "TR3"}
    records_fb = feedback if records_fb is None else records_fb
    records_tr = transcripts if records_tr is None else records_tr
    recs = {1: "HOLD", 2: "PROCEED", 3: "REJECT"}
    extra = "".join(f"{k}={v!r}, " for k, v in analysis_over.items())
    return f'''
current = analysis(
    feedback_records=({"".join(f"fb_ref({fb_ids[n]}, {n}, {recs[n]!r})," for n in records_fb)}),
    transcript_records=({"".join(f"tr_ref({tr_ids[n]}, {n})," for n in records_tr)}),
    {extra}
)
feedback_state = {{APP_ID: {{
    "context": None,
    "history": [{", ".join(f"feedback_item({fb_ids[n]}, {n})" for n in feedback)}],
    "transcripts": {{{", ".join(f"{fb_ids[n]}: [tr_version({tr_ids[n]}, {fb_ids[n]}, {n})]" for n in transcripts)}}},
}}}}
{history_extra}
analysis_state = {{APP_ID: {{"current": current, "history": [current]}}}}
'''


# --- disclosure ----------------------------------------------------------------


def test_new_analysis_discloses_all_rounds_and_transcripts():
    body = _text(_run())
    assert (
        "Based on interviewer feedback for rounds 1 and 2 and the interview "
        "transcripts for rounds 1 and 2." in body
    )


def test_new_analysis_without_transcripts_says_none_was_available():
    body = _text(_run(_variant(feedback=[1, 2], transcripts=[])))
    assert "Based on interviewer feedback for rounds 1 and 2." in body
    assert (
        "No interview transcript was available when this analysis was "
        "generated." in body
    )
    assert "and the interview transcripts" not in body


def test_a_single_round_reads_naturally():
    body = _text(_run(_variant(feedback=[1], transcripts=[1])))
    assert (
        "Based on interviewer feedback for round 1 and the interview "
        "transcripts for round 1." in body
    )


def test_three_rounds_use_a_serial_list():
    body = _text(_run(_variant(feedback=[1, 2, 3], transcripts=[2, 3])))
    assert "interviewer feedback for rounds 1, 2 and 3" in body
    assert "transcripts for rounds 2 and 3" in body


def test_legacy_analysis_says_it_read_only_the_most_recent_record():
    body = _text(_run(_variant(
        feedback=[2], transcripts=[], analyzed_only_latest_feedback=True,
    )))
    assert (
        "This earlier analysis read only the most recent feedback record "
        "(Round 2) and no transcripts." in body
    )
    assert "Based on interviewer feedback" not in body


def test_the_disclosure_comes_from_stored_provenance_not_ai_prose():
    """Prose that merely CLAIMS a different scope changes nothing on screen."""
    body = _text(_run(_variant(
        feedback=[1, 2], transcripts=[],
        summary="I read only round 7 and three transcripts.",
    )))
    assert "Based on interviewer feedback for rounds 1 and 2." in body


# --- per-round recommendations ----------------------------------------------------


def test_per_round_recommendations_are_shown_as_plain_context():
    at = _run()
    body = _text(at)
    assert "Interviewer recommendations:" in body
    assert "Round 1 Hold" in body and "Round 2 Proceed" in body
    assert (
        "The AI analysis did not see these, and no comparison is made." in body
    )


def test_a_legacy_analysis_does_not_claim_the_ai_never_saw_the_recommendation():
    """Honesty: pre-Increment-C analyses DID receive the recommendation."""
    body = _text(_run(_variant(
        feedback=[2], transcripts=[], analyzed_only_latest_feedback=True,
    )))
    assert "Round 2 Proceed" in body
    assert "did not see these" not in body
    assert "could see the interviewer's recommendation" in body
    assert "No comparison is made." in body


def test_no_disagreement_language_anywhere():
    lowered = _text(_run()).lower()
    for forbidden in ("disagree", "agreement", "conflict detected", "mismatch"):
        assert forbidden not in lowered


# --- transcript notes and unreadable warning ---------------------------------------


def test_transcript_notes_are_shown_under_their_own_ai_heading():
    at = _run()
    body = _text(at)
    assert "From the interview transcripts (AI-generated)" in body
    assert "Transcripts confirmed the Spark claim." in body
    # ai_provenance convention: the provenance caption sits under the heading
    markdown = [m.value for m in at.markdown]
    captions = [c.value for c in at.caption]
    assert "*From the interview transcripts (AI-generated)*" in markdown
    assert any(c.startswith("AI-generated") or "AI" in c for c in captions)


def test_no_transcript_section_when_there_are_no_notes():
    body = _text(_run(_variant(feedback=[1], transcripts=[])))
    assert "From the interview transcripts" not in body


def test_unreadable_rounds_get_a_warning():
    body = _text(_run(_variant(
        feedback=[1, 2], transcripts=[2], transcript_unreadable_rounds=(1,),
        transcript_evidence_notes="Round 2 transcript confirmed X.",
    )))
    assert (
        "The transcript for Round 1 had no readable text (probably a scan) and "
        "was not used in this analysis." in body
    )


def test_no_unreadable_warning_when_all_were_read():
    assert "had no readable text" not in _text(_run())


# --- staleness ------------------------------------------------------------------------


def test_an_up_to_date_analysis_shows_no_stale_hint():
    assert _STALE not in _text(_run())


def test_a_new_feedback_round_marks_the_analysis_stale():
    body = _text(_run(_variant(
        feedback=[1, 2, 3], transcripts=[1, 2], records_fb=[1, 2],
    )))
    assert _STALE in body


def test_a_new_transcript_marks_the_analysis_stale():
    body = _text(_run(_variant(
        feedback=[1, 2], transcripts=[1, 2], records_tr=[1],
    )))
    assert _STALE in body


def test_a_replaced_transcript_marks_the_analysis_stale():
    """Same round, different transcript id: the recorded one is superseded."""
    body = _text(_run(_variant(
        feedback=[1, 2], transcripts=[1, 2],
        history_extra=(
            "feedback_state[APP_ID]['transcripts'][FB2] = "
            "[tr_version(TR_NEW, FB2, 2), "
            "tr_version(TR2, FB2, 2, status='SUPERSEDED')]"
        ),
    )))
    assert _STALE in body


def test_the_hint_disappears_after_regeneration():
    """Regenerating records the new sets, so recorded == current again."""
    before = _text(_run(_variant(
        feedback=[1, 2, 3], transcripts=[1, 2], records_fb=[1, 2],
    )))
    after = _text(_run(_variant(feedback=[1, 2, 3], transcripts=[1, 2])))
    assert _STALE in before and _STALE not in after


def test_a_legacy_analysis_is_stale_once_a_second_round_exists():
    """The backfilled legacy row recorded exactly one feedback id."""
    body = _text(_run(_variant(
        feedback=[1, 2], transcripts=[], records_fb=[2], records_tr=[],
        analyzed_only_latest_feedback=True,
    )))
    assert _STALE in body


def test_a_legacy_analysis_with_one_round_and_no_transcripts_is_not_stale():
    body = _text(_run(_variant(
        feedback=[2], transcripts=[], analyzed_only_latest_feedback=True,
    )))
    assert _STALE not in body


def test_an_unreadable_scan_does_not_make_every_analysis_look_stale():
    """A current transcript flagged text_extractable=False can never be used, so
    its absence from the recorded set is expected."""
    body = _text(_run(_variant(
        feedback=[1], transcripts=[], transcript_unreadable_rounds=(1,),
        history_extra=(
            "feedback_state[APP_ID]['transcripts'] = "
            "{FB1: [tr_version(TR1, FB1, 1, extractable=False)]}"
        ),
    )))
    assert _STALE not in body


def test_a_view_without_provenance_is_never_reported_stale():
    class _Old:
        pass

    assert I._analysis_is_stale(_Old(), {"history": [object()]}) is False


# --- pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "rounds, expected",
    [([], "no rounds"), ([1], "round 1"), ([2, 1], "rounds 1 and 2"),
     ([3, 1, 2], "rounds 1, 2 and 3"), ([1, 1], "round 1")],
)
def test_rounds_phrase(rounds, expected):
    assert I._rounds_phrase(rounds) == expected


# --- final scorecard line ----------------------------------------------------------------


def _scorecard_script(**fields) -> str:
    return f'''
import dataclasses
from types import SimpleNamespace

import app.pages.interviews as I
from tests.test_interviews_page_final_scorecard import _card

base = _card()
view = SimpleNamespace(
    **{{f.name: getattr(base, f.name) for f in dataclasses.fields(base)}},
    **{fields!r},
)
I._render_final_scorecard(view)
'''


def _card_text(**fields) -> str:
    at = AppTest.from_string(
        _scorecard_script(**fields), default_timeout=_TIMEOUT
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return _text(at)


def test_scorecard_names_the_rounds_the_analysis_read():
    body = _card_text(
        post_interview_feedback_rounds=(1, 2),
        post_interview_transcript_rounds=(1,),
        post_interview_read_latest_only=False,
    )
    assert (
        "Based on interviewer feedback for rounds 1 and 2 and the interview "
        "transcripts for round 1." in body
    )


def test_scorecard_says_when_no_transcript_was_available():
    body = _card_text(
        post_interview_feedback_rounds=(1,),
        post_interview_transcript_rounds=(),
        post_interview_read_latest_only=False,
    )
    assert "No interview transcript was available when this analysis" in body


def test_scorecard_flags_a_legacy_analysis():
    body = _card_text(
        post_interview_feedback_rounds=(2,),
        post_interview_transcript_rounds=(),
        post_interview_read_latest_only=True,
    )
    assert (
        "This earlier analysis read only the most recent feedback record "
        "(Round 2) and no transcripts." in body
    )


def test_the_scorecards_interview_score_now_uses_every_round():
    """Step 10b reversed Step 10's latest-round-only Interview score: it is the
    shared ``final_scoring`` function over ALL rounds. (This test previously
    pinned the old ``compute_interview_score(ratings)`` signature.)"""
    import inspect

    from app.services import final_scorecard_service as scorecard
    from app.services.final_scoring import compute_interview_score_all_rounds

    assert not hasattr(scorecard, "compute_interview_score")
    assert list(inspect.signature(compute_interview_score_all_rounds).parameters) == [
        "ratings_by_round"
    ]
