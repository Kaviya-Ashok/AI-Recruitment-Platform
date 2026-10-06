"""The candidate page's REAL tab bodies and its decision-dialog body (HR UI,
Increment 3), through AppTest.

``tests/test_candidate_page.py`` spies on whole tab bodies to prove routing and
laziness. This file runs each real body with its loaders stubbed and the reused
renderers replaced by recorders, to prove each tab calls the right EXISTING
renderer with the right arguments — nothing is re-implemented in the page.

The decision DIALOG itself cannot be driven by AppTest: a click inside ``st.dialog``
is a fragment rerun in a browser but a full-script rerun in AppTest, so the dialog is
closed again before its validation message could show. The dialog's content is a
plain function (``_decision_dialog_body``), which is driven here directly through the
SAME form and the SAME service call; opening, validation-in-place and closing after
save are verified in the browser (see the report).
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.candidate_page as CP
import app.pages.candidates as C
import app.pages.interviews as I

_TIMEOUT = 60

_CP_ATTRS = (
    "session_scope", "get_interview_guide_for_application", "list_candidate_activity",
    "get_screening_transcript",
)
_C_ATTRS = ("_load_application_summary", "_load_application_detail", "_render_application_body")
_I_ATTRS = (
    "_load_feedback_state", "_load_analysis_state", "_load_decision_state",
    "_render_guide_controls", "_render_guide", "_render_feedback_section",
    "_render_analysis_section", "_render_final_scorecard_section",
    "_render_screening_transcript", "session_scope", "record_final_decision",
    "_run_record_final_decision",
)
_PRISTINE = {
    CP: {n: getattr(CP, n) for n in _CP_ATTRS},
    C: {n: getattr(C, n) for n in _C_ATTRS},
    I: {n: getattr(I, n) for n in _I_ATTRS},
}


def _restore():
    for module, attrs in _PRISTINE.items():
        for name, value in attrs.items():
            setattr(module, name, value)


@pytest.fixture(autouse=True)
def _restore_modules():
    _restore()
    try:
        yield
    finally:
        _restore()


_PRELUDE = '''
import contextlib
import datetime
import uuid
from types import SimpleNamespace as NS

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

import app.pages.candidate_page as CP
import app.pages.candidates as C
import app.pages.interviews as I
from app.services.final_decision_service import FinalDecisionValidationError
from app.utils.authorization import UnauthorizedError
from app.utils.session import SESSION_USER_KEY
from tests.test_candidate_progress import make_header

st.session_state.setdefault("CALLS", [])
CALLS = st.session_state["CALLS"]
UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
APP = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
st.session_state[SESSION_USER_KEY] = {
    "id": str(UID), "email": "m@x.test", "full_name": "M", "role": "__ROLE__",
}


@contextlib.contextmanager
def _scope():
    yield "DB"


CP.session_scope = _scope
I.session_scope = _scope


def rec(name, ret=None):
    def fn(*args, **kwargs):
        CALLS.append((name, args, kwargs))
        return ret
    return fn


__BODY__
'''


def _run(body: str, role: str = "HIRING_MANAGER") -> AppTest:
    at = AppTest.from_string(
        _PRELUDE.replace("__BODY__", body).replace("__ROLE__", role),
        default_timeout=_TIMEOUT,
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _calls(at):
    return at.session_state["CALLS"]


def _text(at) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [w.value for w in at.warning] + [e.value for e in at.error]
    parts += [i.value for i in at.info]
    return "\n".join(str(p) for p in parts)


def _ctx(header="make_header()", can_decide="True"):
    return f"ctx = CP._Ctx({header}, UID, {can_decide})\n"


# =========================================================== Overview ================


def test_overview_alert_for_an_ineligible_candidate_uses_the_stored_reason():
    at = _run(
        "CP.list_candidate_activity = lambda *a, **k: []\n"
        + _ctx('make_header(entry_status="NOT_RANKED_INELIGIBLE", '
               'entry_reason="A mandatory requirement was not met.", '
               'has_current_final_ranking=True)')
        + "CP._tab_overview(ctx)\n"
    )
    assert any("A mandatory requirement was not met." in e.value for e in at.error)


@pytest.mark.parametrize("status", ["INCOMPLETE_INTERVIEW", "INCOMPLETE_SCREENING"])
def test_overview_warns_for_an_incomplete_record(status):
    at = _run(
        "CP.list_candidate_activity = lambda *a, **k: []\n"
        + _ctx(f'make_header(entry_status="{status}", entry_reason="Ratings are missing.", '
               'has_current_final_ranking=True)')
        + "CP._tab_overview(ctx)\n"
    )
    assert any("Ratings are missing." in w.value for w in at.warning)


def test_overview_says_screening_incomplete_is_not_a_rejection():
    at = _run(
        "CP.list_candidate_activity = lambda *a, **k: []\n"
        + _ctx('make_header(application_status="SCREENING_INCOMPLETE")')
        + "CP._tab_overview(ctx)\n"
    )
    assert any("not a rejection" in w.value for w in at.warning)


def test_overview_has_no_alert_for_a_healthy_candidate():
    at = _run(
        "CP.list_candidate_activity = lambda *a, **k: []\n"
        + _ctx() + "CP._tab_overview(ctx)\n"
    )
    assert not at.warning and not at.error


def test_overview_summary_is_plain_key_values():
    at = _run(
        "CP.list_candidate_activity = lambda *a, **k: []\n"
        + _ctx('make_header(rounds_count=2, transcripts_count=1, has_analysis=True, '
               'final_rank=1, final_ranked_count=3, entry_status="RANKED", '
               'final_score="8.6", interview_score="9", has_current_final_ranking=True)')
        + "CP._tab_overview(ctx)\n"
    )
    body = _text(at)
    for expected in (
        "Application status:* Evaluation complete",
        "Screening:* 8.00 / 10 · screening rank #1",
        "Interview:* 9.00 / 10 · 2 rounds",
        "Final:* 8.60 / 10 · #1 of 3 ranked",
        "Shortlisted:* Yes", "Interview transcripts:* 1", "AI analysis:* Generated",
    ):
        assert expected in body, expected


def test_overview_activity_lists_label_time_and_actor_only():
    at = _run(
        "when = datetime.datetime(2026, 10, 6, 9, 30, tzinfo=datetime.timezone.utc)\n"
        "CP.list_candidate_activity = lambda db, app, *, acting_user_id: [\n"
        "    NS(event_type='FINAL_DECISION_SUBMITTED', timestamp=when, actor_name='Mia'),\n"
        "    NS(event_type='CANDIDATE_APPLIED', timestamp=when, actor_name=None)]\n"
        + _ctx() + "CP._tab_overview(ctx)\n"
    )
    rows = at.table[0].value
    assert list(rows.columns) == ["Event", "When", "By"]
    assert list(rows["Event"]) == ["Final decision recorded", "Application submitted"]
    assert list(rows["When"]) == ["2026-10-06 09:30 UTC"] * 2
    assert list(rows["By"]) == ["Mia", "—"]


def test_overview_activity_is_asked_for_this_application_as_this_user():
    at = _run(
        "CP.list_candidate_activity = lambda db, app, *, acting_user_id: "
        "CALLS.append(('activity', app, acting_user_id)) or []\n"
        + _ctx() + "CP._tab_overview(ctx)\n"
    )
    assert _calls(at) == [(
        "activity",
        __import__("uuid").UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        __import__("uuid").UUID("11111111-1111-1111-1111-111111111111"),
    )]
    assert "No activity recorded yet." in _text(at)


@pytest.mark.parametrize("exc, expected", [
    ("UnauthorizedError('no')", "no longer active"),
    ("SQLAlchemyError('boom')", "Couldn't load this candidate's activity"),
])
def test_overview_activity_failures_are_calm(exc, expected):
    at = _run(
        f"def boom(*a, **k):\n    raise {exc}\nCP.list_candidate_activity = boom\n"
        + _ctx() + "CP._tab_overview(ctx)\n"
    )
    assert any(expected in e.value for e in at.error)


# =========================================================== Screening ================

_SCREENING_STUBS = '''
C._load_application_summary = lambda app_id, uid: {
    "application_id": app_id, "status": "SCREENING_EVALUATED",
    "candidate_name": "Ada", "candidate_email": "a@x.test",
    "screening_stall_kind": None}
C._load_application_detail = lambda app_id, uid: {
    "screening_session_id": __SESSION__, "document_id": None}
C._render_application_body = rec("body")
I._render_screening_transcript = rec("transcript")
'''


def test_screening_tab_reuses_the_application_body_with_summary_and_detail_merged():
    at = _run(
        _SCREENING_STUBS.replace("__SESSION__", "'sess-1'")
        + "CP.get_screening_transcript = lambda db, *, screening_session_id, acting_user_id: [\n"
        "    NS(round='ROUND_1', category='REQUIREMENTS', question_text='q', answered=True, answer_text='a')]\n"
        + _ctx() + "CP._tab_screening(ctx)\n"
    )
    (name, args, _kw), (tname, targs, _tkw) = _calls(at)
    assert name == "body"
    view, uid = args
    assert view["application_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert view["screening_session_id"] == "sess-1" and view["candidate_name"] == "Ada"
    assert tname == "transcript" and targs[1] == 1            # one entry -> "1 question"


def test_screening_tab_without_a_session_says_there_is_no_transcript():
    at = _run(
        _SCREENING_STUBS.replace("__SESSION__", "None") + _ctx()
        + "CP._tab_screening(ctx)\n"
    )
    assert [c[0] for c in _calls(at)] == ["body"]
    assert "No screening transcript is available for this candidate yet." in _text(at)


def test_screening_tab_with_an_empty_transcript_says_so():
    at = _run(
        _SCREENING_STUBS.replace("__SESSION__", "'sess-1'")
        + "CP.get_screening_transcript = lambda *a, **k: []\n"
        + _ctx() + "CP._tab_screening(ctx)\n"
    )
    assert [c[0] for c in _calls(at)] == ["body"]
    assert "No screening transcript is available" in _text(at)


def test_screening_tab_for_a_missing_application_is_a_calm_error():
    at = _run(
        "C._load_application_summary = lambda *a: None\n"
        "C._load_application_detail = lambda *a: {}\n"
        "C._render_application_body = rec('body')\n"
        + _ctx() + "CP._tab_screening(ctx)\n"
    )
    assert any("Couldn't find this application." in e.value for e in at.error)
    assert _calls(at) == []


@pytest.mark.parametrize("exc, expected", [
    ("UnauthorizedError('no')", "no longer active"),
    ("SQLAlchemyError('boom')", "Couldn't load this application right now."),
])
def test_screening_tab_load_failures_are_calm(exc, expected):
    at = _run(
        f"def boom(*a, **k):\n    raise {exc}\n"
        "C._load_application_summary = boom\nC._load_application_detail = boom\n"
        + _ctx() + "CP._tab_screening(ctx)\n"
    )
    assert any(expected in e.value for e in at.error)


# =========================================================== Interview ================

_INTERVIEW_STUBS = '''
I._load_feedback_state = lambda db, app_id, uid: {"history": ["h"], "app": app_id}
I._render_guide_controls = rec("controls")
I._render_guide = rec("guide")
I._render_feedback_section = rec("feedback")
CP.get_interview_guide_for_application = lambda db, app, *, acting_user_id: __GUIDE__
'''


def test_interview_tab_for_a_shortlisted_candidate_uses_the_guide_controls():
    at = _run(
        _INTERVIEW_STUBS.replace("__GUIDE__", "'GUIDE'")
        + _ctx("make_header(is_shortlisted=True)") + "CP._tab_interview(ctx)\n"
    )
    names = [c[0] for c in _calls(at)]
    assert names == ["controls", "feedback"]
    (_, cargs, _), (_, fargs, _) = _calls(at)
    import uuid
    app = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert cargs[:3] == (app, "GUIDE", True)                  # guide_exists from the guide
    assert fargs[0] == app and fargs[1] == {app: {"history": ["h"], "app": app}}


def test_interview_tab_without_a_guide_passes_guide_exists_false():
    at = _run(
        _INTERVIEW_STUBS.replace("__GUIDE__", "None")
        + _ctx("make_header(is_shortlisted=True)") + "CP._tab_interview(ctx)\n"
    )
    assert _calls(at)[0][1][1:3] == (None, False)


def test_interview_tab_for_an_unshortlisted_candidate_blocks_generation_but_keeps_the_guide():
    at = _run(
        _INTERVIEW_STUBS.replace("__GUIDE__", "'GUIDE'")
        + _ctx("make_header(is_shortlisted=False)") + "CP._tab_interview(ctx)\n"
    )
    assert [c[0] for c in _calls(at)] == ["guide", "feedback"]     # no generate controls
    assert any("generation is blocked" in i.value for i in at.info)
    assert "Interview guide" in [e.label for e in at.expander]


def test_interview_tab_for_an_unshortlisted_candidate_without_a_guide():
    at = _run(
        _INTERVIEW_STUBS.replace("__GUIDE__", "None")
        + _ctx("make_header(is_shortlisted=False)") + "CP._tab_interview(ctx)\n"
    )
    assert [c[0] for c in _calls(at)] == ["feedback"]
    assert not at.expander


@pytest.mark.parametrize("exc, expected", [
    ("UnauthorizedError('no')", "no longer active"),
    ("SQLAlchemyError('boom')", "Couldn't load this candidate's interview details"),
])
def test_interview_tab_load_failures_are_calm_and_render_nothing_else(exc, expected):
    at = _run(
        _INTERVIEW_STUBS.replace("__GUIDE__", "None")
        + f"def boom(*a, **k):\n    raise {exc}\nCP.get_interview_guide_for_application = boom\n"
        + _ctx() + "CP._tab_interview(ctx)\n"
    )
    assert any(expected in e.value for e in at.error)
    assert _calls(at) == []


# =========================================================== AI analysis ================


def test_analysis_tab_reuses_the_analysis_section_with_both_states():
    at = _run(
        'I._load_feedback_state = lambda db, a, u: {"f": 1}\n'
        'I._load_analysis_state = lambda db, a, u: {"a": 2}\n'
        "I._render_analysis_section = rec('analysis')\n"
        + _ctx() + "CP._tab_analysis(ctx)\n"
    )
    import uuid
    app = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    ((name, args, _kw),) = _calls(at)
    assert name == "analysis"
    assert args[0] == app and args[1] == {app: {"f": 1}} and args[2] == {app: {"a": 2}}
    assert args[3] == uuid.UUID("11111111-1111-1111-1111-111111111111")


def test_analysis_tab_failure_is_calm():
    at = _run(
        "def boom(*a, **k):\n    raise SQLAlchemyError('boom')\n"
        "I._load_feedback_state = boom\nI._load_analysis_state = boom\n"
        "I._render_analysis_section = rec('analysis')\n"
        + _ctx() + "CP._tab_analysis(ctx)\n"
    )
    assert any("Couldn't load this candidate's analysis" in e.value for e in at.error)
    assert _calls(at) == []


# =========================================================== Scorecard ================


def test_scorecard_tab_reuses_the_on_demand_scorecard_section():
    at = _run(
        "I._render_final_scorecard_section = rec('scorecard')\n"
        + _ctx() + "CP._tab_scorecard(ctx)\n"
    )
    import uuid
    ((name, args, _kw),) = _calls(at)
    assert name == "scorecard"
    assert args == (
        uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
    )


# =========================================================== Decision ================

_DECISION_STATE = '''
cur = NS(decision_id=uuid.uuid4(), application_id=APP, decision="PROCEED",
         rationale="Strong fit.", status="CURRENT", decided_by_name="Mia",
         created_at=datetime.datetime(2026, 10, 6, 9, 30, tzinfo=datetime.timezone.utc),
         superseded_at=None)
I._load_decision_state = lambda db, a, u: {
    "current": __CUR__, "history": [__CUR__] if __CUR__ else [],
    "staleness": NS(is_stale=False, reasons=())}
'''


def _decision_run(*, cur="cur", can_decide=True, role="HIRING_MANAGER"):
    return _run(
        _DECISION_STATE.replace("__CUR__", cur)
        + _ctx(f'make_header(decision={"None" if cur == "None" else repr("PROCEED")})',
               str(can_decide))
        + "CP._tab_decision(ctx)\n",
        role=role,
    )


def test_decision_tab_shows_the_record_and_a_button_for_deciders():
    at = _decision_run()
    body = _text(at)
    assert "Final decision: Proceed" in body and "Strong fit." in body
    assert next(b for b in at.button if b.key == "cp_decide_tab").label == "Change decision"
    assert list(at.radio) == []                       # the form is in the dialog, not inline


def test_decision_tab_with_no_decision_offers_record():
    at = _decision_run(cur="None")
    assert "No final decision has been recorded yet." in _text(at)
    assert next(b for b in at.button if b.key == "cp_decide_tab").label == "Record decision"


def test_decision_tab_for_a_non_decider_is_read_only_with_the_existing_note():
    at = _decision_run(can_decide=False, role="HR")
    assert "Only a hiring manager or an admin can record the final decision." in _text(at)
    assert not [b for b in at.button if b.key == "cp_decide_tab"]
    assert "Final decision: Proceed" in _text(at)          # still readable


def test_decision_tab_clicking_the_button_opens_the_dialog_with_the_form():
    at = _decision_run()
    next(b for b in at.button if b.key == "cp_decide_tab").click().run()
    assert not at.exception
    assert [r.label for r in at.radio] == ["Your decision"]
    assert any(b.label == "Change final decision" for b in at.button)


def test_decision_tab_with_no_state_says_so_and_offers_no_button():
    at = _run(
        "I._load_decision_state = lambda db, a, u: {}\n"
        + _ctx() + "CP._tab_decision(ctx)\n"
    )
    assert "Decision details are not available for this candidate." in _text(at)
    assert not [b for b in at.button if b.key == "cp_decide_tab"]


def test_decision_tab_failure_is_calm():
    at = _run(
        "def boom(*a, **k):\n    raise SQLAlchemyError('boom')\n"
        "I._load_decision_state = boom\n" + _ctx() + "CP._tab_decision(ctx)\n"
    )
    assert any("Couldn't load this candidate's decision" in e.value for e in at.error)


# ========================================================= the dialog body ================

_DIALOG = '''
def fake_record(db, **kw):
    CALLS.append(("record", kw))
    if __RAISE__:
        raise FinalDecisionValidationError("The rationale is too short: it needs at least 10 characters.")
I.record_final_decision = fake_record
header = make_header(decision=__DECISION__, candidate_name="Ada Lovelace")
CP._decision_dialog_body(header, UID)
'''


def _dialog(decision="None", raise_="False", role="HIRING_MANAGER"):
    return _run(
        _DIALOG.replace("__DECISION__", decision).replace("__RAISE__", raise_), role=role
    )


def _submit(at):
    return next(b for b in at.button if b.label.endswith("final decision"))


def _fill(at, *, choice="Hold", why="Ratings are missing; holding for now.", confirm=True):
    if choice is not None:
        at.radio[0].set_value(choice)
    at.text_area[0].set_value(why)
    for c in at.checkbox:
        if "reviewed the scorecard" in c.label:
            c.set_value(confirm)
    return at


def test_the_dialog_body_names_the_candidate_and_shows_the_existing_form():
    at = _dialog()
    assert "Ada Lovelace · V_001 Backend Engineer" in _text(at)
    assert [r.label for r in at.radio] == ["Your decision"]
    assert list(at.radio[0].options) == ["Proceed", "Hold", "Reject"]
    assert at.radio[0].value is None                      # nothing preselected
    assert [t.label for t in at.text_area] == ["Rationale (required)"]
    assert _submit(at).label == "Record final decision"
    assert "Recorded only. This does not notify the candidate or change any status." in _text(at)
    assert "10–2000 characters" in _text(at)


def test_the_dialog_body_says_change_for_an_existing_decision():
    at = _dialog(decision="'PROCEED'")
    assert _submit(at).label == "Change final decision"
    assert "Change the decision" in _text(at)


def test_nothing_is_recorded_on_load_or_on_widget_changes():
    at = _dialog()
    _fill(at).run()
    assert _calls(at) == []


def test_submitting_without_choosing_is_blocked_with_the_existing_message():
    at = _dialog()
    _fill(at, choice=None).run()
    _submit(at).click().run()
    assert any("Choose Proceed, Hold or Reject." in e.value for e in at.error)
    assert _calls(at) == []


def test_submitting_without_confirming_is_blocked_with_the_existing_message():
    at = _dialog()
    _fill(at, confirm=False).run()
    _submit(at).click().run()
    assert any("Please confirm that you have reviewed" in e.value for e in at.error)
    assert _calls(at) == []


def test_a_confirmed_submission_calls_the_service_once_with_exactly_what_was_typed():
    import uuid

    at = _dialog()
    _fill(at, choice="Reject", why="  Not a fit for the senior scope.  ").run()
    _submit(at).click().run()
    assert not at.exception
    records = [c for c in _calls(at) if c[0] == "record"]
    assert len(records) == 1
    kw = records[0][1]
    assert kw == {
        "application_id": uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "decision": "REJECT",
        "rationale": "  Not a fit for the senior scope.  ",   # trimming is the SERVICE's job
        "acting_user_id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
    }


def test_a_successful_save_empties_the_form_and_reruns_which_closes_the_dialog():
    """After a save the form key version advances (a fresh, empty form on the next
    run) and ``st.rerun()`` runs — in the browser that full-app rerun is what closes
    the dialog, because the dialog function is only called on a button click."""
    at = _dialog()
    assert at.session_state.filtered_state.get(
        "fd_ver_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 0) == 0
    _fill(at).run()
    _submit(at).click().run()
    assert at.session_state["fd_ver_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"] == 1
    assert at.text_area[0].value == ""                    # the next form starts empty


def test_a_service_error_is_shown_in_place_and_nothing_resets():
    at = _dialog(raise_="True")
    _fill(at, why="short").run()
    _submit(at).click().run()
    assert any("The rationale is too short" in e.value for e in at.error)
    assert at.session_state.filtered_state.get(
        "fd_ver_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 0) == 0   # no reset on failure
    assert not at.exception


def test_the_dialog_form_is_the_very_same_function_the_interviews_page_uses():
    import inspect

    assert "_render_decision_form" in inspect.getsource(CP._decision_dialog_body)
    assert "_render_decision_form" in inspect.getsource(I._render_final_decision_section)
