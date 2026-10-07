"""AppTest page tests for Step 11 — the per-candidate "Final human decision"
section, the ranking-row line and the scorecard block.

Loaders and the service call are stubbed (no database, no AI). Scripts rebind
attributes on the real page module, so each is snapshotted at IMPORT time and
restored before and after every test (see the ``apptest-module-mutation-leak``
project note).
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.interviews as I

_TIMEOUT = 60

_PATCHED_ATTRS = (
    "_run_record_final_decision", "record_final_decision", "session_scope",
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
import contextlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS

import streamlit as st

import app.pages.interviews as I
from app.services.final_decision_service import (
    FinalDecisionConflictError,
    FinalDecisionPermissionError,
    FinalDecisionValidationError,
)
from app.utils.session import SESSION_USER_KEY

APP_ID = uuid.UUID("88888888-8888-8888-8888-888888888888")
WHEN = datetime(2026, 10, 7, 9, 30, tzinfo=timezone.utc)
OLDER = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
st.session_state.setdefault("CALLS", [])
CALLS = st.session_state["CALLS"]
ROLE = "__ROLE__"
st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111", "email": "m@x.test",
    "full_name": "Mia Manager", "role": ROLE,
}


def decision(value, *, who="Mia Manager", why="A considered rationale.", status="CURRENT",
             created=WHEN, superseded=None):
    return NS(
        decision_id=uuid.uuid4(), application_id=APP_ID, decision=value,
        rationale=why, status=status, decided_by_name=who, created_at=created,
        superseded_at=superseded,
    )


def stale(*reasons):
    return NS(is_stale=bool(reasons), reasons=tuple(reasons))


__BODY__

def fake_run(application_id, choice, rationale, acting_user_id):
    CALLS.append((application_id, choice, rationale, acting_user_id))


I._run_record_final_decision = fake_run
I._render_final_decision_section(APP_ID, STATE, "11111111-1111-1111-1111-111111111111")
'''

_NONE = 'STATE = {APP_ID: {"current": None, "history": [], "staleness": stale()}}'
_DECIDED = '''
cur = decision("PROCEED", why="Strong fit; clear evidence in both rounds.")
STATE = {APP_ID: {"current": cur, "history": [cur], "staleness": stale()}}
'''


def _run(body: str = _NONE, role: str = "HIRING_MANAGER") -> AppTest:
    script = _PRELUDE.replace("__BODY__", body).replace("__ROLE__", role)
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [w.value for w in at.warning]
    parts += [i.value for i in at.info]
    parts += [e.value for e in at.error]
    return "\n".join(str(p) for p in parts)


def _submit_button(at):
    return next(b for b in at.button if b.label.endswith("final decision"))


# --- the current decision ------------------------------------------------------


def test_no_decision_yet_is_stated_plainly():
    body = _text(_run())
    assert "Final human decision" in body
    assert "No final decision has been recorded yet." in body


def test_the_current_decision_is_a_labelled_badge_with_who_when_and_why():
    body = _text(_run(_DECIDED))
    assert "Final decision: Proceed" in body                 # text, not only colour
    assert "Decided by Mia Manager on 2026-10-07 09:30 UTC" in body
    assert "Strong fit; clear evidence in both rounds." in body
    assert "*Rationale*" in body


@pytest.mark.parametrize("value, label", [
    ("PROCEED", "Proceed"), ("HOLD", "Hold"), ("REJECT", "Reject")])
def test_every_decision_value_has_its_own_text_label(value, label):
    body = _text(_run(
        f'cur = decision("{value}")\n'
        'STATE = {APP_ID: {"current": cur, "history": [cur], "staleness": stale()}}'))
    assert f"Final decision: {label}" in body


def test_the_ai_does_not_decide_caption_is_present():
    assert "The AI does not decide" in _text(_run())


# --- staleness -------------------------------------------------------------------


def test_the_stale_caption_lists_why_the_evidence_moved_on():
    body = _text(_run('''
cur = decision("HOLD")
STATE = {APP_ID: {"current": cur, "history": [cur],
    "staleness": stale("the final ranking was regenerated",
                       "1 interview feedback record(s) were added")}}
'''))
    assert "Based on earlier evidence — the final ranking was regenerated; " \
           "1 interview feedback record(s) were added." in body


def test_no_stale_caption_for_current_evidence():
    assert "Based on earlier evidence" not in _text(_run(_DECIDED))


# --- history ------------------------------------------------------------------------


def test_earlier_decisions_are_collapsed_with_their_rationale():
    at = _run('''
cur = decision("REJECT", why="Final: not a fit after the second round.")
old = decision("PROCEED", why="First impression was strong.", status="SUPERSEDED",
               created=OLDER, superseded=WHEN, who="Ada Admin")
STATE = {APP_ID: {"current": cur, "history": [cur, old], "staleness": stale()}}
''')
    assert "Earlier decisions (1)" in [e.label for e in at.expander]
    body = _text(at)
    assert "First impression was strong." in body
    assert "Decided by Ada Admin on 2026-10-05" in body and "replaced 2026-10-07" in body


def test_no_history_expander_when_there_is_only_the_current_decision():
    assert not any(str(e.label).startswith("Earlier decisions")
                   for e in _run(_DECIDED).expander)


# --- the form: who sees it -------------------------------------------------------------


@pytest.mark.parametrize("role", ["HIRING_MANAGER", "ADMIN"])
def test_deciders_get_the_form(role):
    at = _run(role=role)
    assert [r.label for r in at.radio] == ["Your decision"]
    assert [t.label for t in at.text_area] == ["Rationale (required)"]
    assert any("I have reviewed the scorecard, interview feedback and analysis"
               in c.label for c in at.checkbox)
    assert _submit_button(at).label == "Record final decision"


@pytest.mark.parametrize("role", ["HR", "SYSTEM", "", "SOMETHING"])
def test_everyone_else_sees_it_read_only_with_a_note(role):
    at = _run(_DECIDED, role=role)
    assert list(at.radio) == [] and list(at.text_area) == []
    assert not any(b.label.endswith("final decision") for b in at.button)
    body = _text(at)
    assert "Only a hiring manager or an admin can record the final decision." in body
    assert "Final decision: Proceed" in body                  # still readable


def test_the_form_states_the_rules_and_the_no_side_effects_promise():
    body = _text(_run())
    assert "10–2000 characters" in body
    assert "Base the decision on job-relevant evidence only." in body
    assert "Recorded only. This does not notify the candidate or change any status." in body


def test_the_three_options_are_offered_and_none_is_preselected():
    at = _run()
    radio = at.radio[0]
    assert list(radio.options) == ["Proceed", "Hold", "Reject"]
    assert radio.value is None


def test_with_a_current_decision_the_form_says_change_not_record():
    at = _run(_DECIDED)
    assert _submit_button(at).label == "Change final decision"
    assert "Change the decision" in _text(at)


# --- the confirmation gate and validation -------------------------------------------------


def _fill(at, *, choice="Reject", why="Not a fit for the senior scope.", confirm=True):
    if choice is not None:
        at.radio[0].set_value(choice)
    at.text_area[0].set_value(why)
    for c in at.checkbox:
        if "reviewed the scorecard" in c.label:
            c.set_value(confirm)
    return at


def test_submitting_without_confirming_is_blocked_with_a_clear_message():
    at = _run()
    _fill(at, confirm=False).run()
    _submit_button(at).click().run()
    assert any("Please confirm that you have reviewed" in e.value for e in at.error)
    assert at.session_state["CALLS"] == []


def test_submitting_without_choosing_is_blocked():
    at = _run()
    _fill(at, choice=None).run()
    _submit_button(at).click().run()
    assert any("Choose Proceed, Hold or Reject." in e.value for e in at.error)
    assert at.session_state["CALLS"] == []


def test_a_confirmed_submission_calls_the_service_once_with_exactly_what_was_typed():
    at = _run()
    _fill(at, choice="Reject", why="  Not a fit for the senior scope.  ").run()
    _submit_button(at).click().run()
    assert not at.error
    assert at.session_state["CALLS"] == [(
        "88888888-8888-8888-8888-888888888888", "REJECT",
        "  Not a fit for the senior scope.  ",       # trimming is the SERVICE's job
        "11111111-1111-1111-1111-111111111111",
    )]


def test_nothing_is_submitted_on_page_load_or_on_widget_changes():
    at = _run()
    _fill(at).run()
    assert at.session_state["CALLS"] == []


def test_a_revision_goes_through_the_same_gate_and_call():
    at = _run(_DECIDED)
    _fill(at, choice="Hold", why="Reconsidering after the new feedback.").run()
    _submit_button(at).click().run()
    assert [c[1] for c in at.session_state["CALLS"]] == ["HOLD"]


# --- business errors are friendly, never raw ---------------------------------------------------


_REAL_RUN = '''
I._run_record_final_decision = I._run_record_final_decision   # the REAL one
@contextlib.contextmanager
def fake_scope():
    yield object()
I.session_scope = fake_scope
def boom(db, **kw):
    raise __EXC__
I.record_final_decision = boom
'''


def _run_real(exc: str, role: str = "HIRING_MANAGER") -> AppTest:
    script = (
        _PRELUDE.replace("__BODY__", _NONE + _REAL_RUN.replace("__EXC__", exc))
        .replace("__ROLE__", role)
        .replace("I._run_record_final_decision = fake_run\n", "")
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_a_validation_error_from_the_service_is_shown_as_its_own_message():
    at = _run_real('FinalDecisionValidationError("The rationale is too short: it needs '
                   'at least 10 characters.")')
    _fill(at, why="short").run()
    _submit_button(at).click().run()
    assert any("The rationale is too short" in e.value for e in at.error)
    assert not at.exception


def test_a_permission_error_is_calm_and_specific():
    at = _run_real('FinalDecisionPermissionError("Only a hiring manager or an admin '
                   'can record the final decision. You can read decisions, but not make one.")')
    _fill(at).run()
    _submit_button(at).click().run()
    assert any("Only a hiring manager or an admin" in e.value for e in at.error)


def test_a_conflict_is_calm():
    at = _run_real('FinalDecisionConflictError("Another decision was recorded just now. '
                   'Please reload this candidate and review it before deciding again. '
                   'Nothing was changed.")')
    _fill(at).run()
    _submit_button(at).click().run()
    assert any("Another decision was recorded just now" in e.value for e in at.error)


def test_an_unexpected_error_never_surfaces_a_traceback_or_raw_ids():
    at = _run_real('RuntimeError("boom 88888888-8888-8888-8888-888888888888")')
    _fill(at).run()
    _submit_button(at).click().run()
    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "Something went wrong recording the final decision" in errors
    assert "88888888" not in errors and "boom" not in errors


def test_no_raw_ids_are_shown_anywhere_in_the_section():
    body = _text(_run(_DECIDED))
    assert "88888888" not in body


# --- missing state ------------------------------------------------------------------------------------


def test_missing_state_is_a_calm_line_not_a_crash():
    at = _run("STATE = {}")
    assert "Decision details are not available for this candidate." in _text(at)
    assert list(at.radio) == []


# --- the ranking-row line ------------------------------------------------------------------------------


def _ranking_script(decisions_code: str) -> str:
    from tests.test_interviews_page_final_ranking import _PRELUDE as RANK_PRELUDE

    body = f'''
e1 = entry("Ananya Rao", rank=1)
e2 = entry("Bala Iyer", rank=2, screening="6.00", interview="7.00", final="6.60")
inel = entry("Chitra Nair", status="NOT_RANKED_INELIGIBLE", eligible=False, final="9.10",
             reason="A mandatory requirement was assessed as not met.")
inc = entry("Dev Patel", status="INCOMPLETE_SCREENING", screening=None, final=None,
            confidence=None, reason="Generate the screening ranking first.")
cur = run([e1, e2, inel, inc])
DEC = NS(decision="PROCEED", decided_by_name="Mia Manager",
         created_at=datetime(2026, 10, 7, tzinfo=timezone.utc))
DEC_REJ = NS(decision="REJECT", decided_by_name="Ada Admin",
             created_at=datetime(2026, 10, 6, tzinfo=timezone.utc))
VIEW = {{"current": [cur], "history": [cur], "staleness": stale(),
        "decisions": {decisions_code}}}
'''
    return RANK_PRELUDE.replace("__BODY__", body)


def _ranking(decisions_code: str) -> str:
    at = AppTest.from_string(_ranking_script(decisions_code), default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return _text(at)


def test_ranking_rows_show_the_decision_or_that_there_is_none():
    body = _ranking("{e1.application_id: DEC, inel.application_id: DEC_REJ}")
    assert "Decision: Proceed (Mia Manager, 2026-10-07)" in body
    assert "Decision: Reject (Ada Admin, 2026-10-06)" in body       # ineligible row too
    assert body.count("Decision: No final decision yet") == 2        # e2 and the incomplete


def test_all_rows_say_no_decision_when_there_are_none():
    assert _ranking("{}").count("Decision: No final decision yet") == 4


def test_the_ranking_rows_never_show_a_rationale_or_a_form():
    at = AppTest.from_string(
        _ranking_script("{e1.application_id: DEC}"), default_timeout=_TIMEOUT).run()
    assert list(at.radio) == [] and list(at.text_area) == []


def test_decision_line_is_pure_and_read_only():
    from datetime import datetime, timezone

    d = type("D", (), {"decision": "HOLD", "decided_by_name": "Mia Manager",
                       "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)})()
    assert I._decision_line(d) == "Decision: Hold (Mia Manager, 2026-01-02)"
    assert I._decision_line(None) == "Decision: No final decision yet"


# --- the scorecard block -------------------------------------------------------------------------------------


def _scorecard(final_decision_code: str, status: str = "Not decided yet") -> str:
    script = f'''
import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import app.pages.interviews as I
from tests.test_interviews_page_final_scorecard import _card

base = _card()
view = NS(**{{f.name: getattr(base, f.name) for f in dataclasses.fields(base)}})
view.final_decision_status = {status!r}
view.final_decision = {final_decision_code}
I._render_final_scorecard(view)
'''
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return _text(at)


def test_scorecard_shows_the_recorded_decision_who_and_when_but_not_the_rationale():
    body = _scorecard(
        'NS(decision="REJECT", decided_by_name="Mia Manager", '
        'decided_at=datetime(2026, 10, 7, tzinfo=timezone.utc))', status="REJECT")
    assert "Final decision: Reject" in body
    assert "Recorded by Mia Manager on 2026-10-07" in body
    assert "Not decided yet" not in body
    assert "rationale" in body.lower()            # a POINTER to the section only
    assert "Recorded only — nothing else was changed." in body


def test_scorecard_keeps_the_placeholder_when_no_decision_exists():
    body = _scorecard("None")
    assert "Not decided yet" in body
    assert "No final decision has been recorded." in body
    assert "Final decision:" not in body


def test_scorecard_disagreement_placeholder_is_untouched():
    from app.services.final_scorecard_service import DISAGREEMENT_NOT_ASSESSED

    body = _scorecard('NS(decision="PROCEED", decided_by_name="Mia Manager", '
                      'decided_at=datetime(2026, 10, 7, tzinfo=timezone.utc))', "PROCEED")
    assert DISAGREEMENT_NOT_ASSESSED in body             # the fixed placeholder, as before
    lowered = body.lower()
    for forbidden in ("disagreement detected", "no disagreement", "agrees with"):
        assert forbidden not in lowered
