"""Routing guarantees for the candidate app's page dispatch (Phase D Tier 1).

Two behaviours are pinned here because getting them wrong is silent and
candidate-visible:

* a candidate who FINISHES screening in the browser session they started it in
  must see the completion message. Before this, ``_page`` resolved the cached
  ``?screening=`` token first; a completed screening resolves as the generic
  ``INVALID`` (deliberately — see ``ScreeningAccessOutcome``), so the last rerun
  of a *successful* screening showed "this screening link is no longer valid"
  and ``_SCREENING_DONE_MSG`` was unreachable;
* a RETURNING visitor opening a saved link in a fresh session, after their
  screening is already complete, must STILL get that generic message. The
  anti-enumeration collapse is intentional and this fix must not have widened
  it — so the returning-visitor path is asserted explicitly, not assumed.

Style follows ``tests/test_candidates_page_structure.py``: Streamlit's own
``AppTest`` harness with the service layer stubbed inside the script, so no
database and no Streamlit server is involved. The DB-reading helper
``_same_session_screening_is_complete`` is covered separately with the pipeline
accessor patched at the module boundary (the same seam the service tests use).
"""

from __future__ import annotations

import pytest

from streamlit.testing.v1 import AppTest

import app.public_main as P
from app.database.models.screening_session import ScreeningSessionStatus
from app.utils.candidate_ui import FOLLOW_UP_MAY_FOLLOW

_TIMEOUT = 60


# =====================================================================
# Fix 1 — same-session completion routing
# =====================================================================


#: Module attributes the AppTest scripts below reassign. ``AppTest.from_string``
#: executes in THIS process, so those assignments mutate the real module and
#: would leak into every later test — the fixture restores them.
_PATCHED_ATTRS = (
    "_same_session_screening_is_complete",
    "_render_processing",
    "_render_screening_resume",
    "_token_from_url",
    "_screening_token_from_url",
    "_render_invalid",
    "_render_error",
)


@pytest.fixture(autouse=True)
def _restore_public_main():
    saved = {name: getattr(P, name) for name in _PATCHED_ATTRS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(P, name, value)


def _routing_script(*, processing_app: str | None, screening_token: str | None,
                    complete: bool) -> str:
    """A ``_page()`` script with every branch stubbed to record which one ran.

    Nothing here touches a database: the completion probe and all three render
    paths are replaced, so the test observes routing order and nothing else.
    """
    lines = []
    if processing_app is not None:
        lines.append(
            f"P.remember_processing_application(st.session_state, "
            f"{processing_app!r})"
        )
    if screening_token is not None:
        lines.append(
            f"P.remember_screening_token(st.session_state, {screening_token!r})"
        )
    seed = "\n".join(lines)

    return f'''
import streamlit as st
import app.public_main as P

st.session_state.setdefault("branch", None)

P._same_session_screening_is_complete = lambda app_id: {complete!r}
P._render_processing = lambda app_id: st.session_state.__setitem__(
    "branch", f"processing:{{app_id}}"
)
P._render_screening_resume = lambda token: st.session_state.__setitem__(
    "branch", f"resume:{{token}}"
)
P._token_from_url = lambda: ""
P._screening_token_from_url = lambda: ""
P._render_invalid = lambda: st.session_state.__setitem__("branch", "invalid")
P._render_error = lambda: st.session_state.__setitem__("branch", "error")

{seed}
P._page()
'''


def _run(**kwargs) -> AppTest:
    at = AppTest.from_string(
        _routing_script(**kwargs), default_timeout=_TIMEOUT
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_same_session_completion_routes_to_the_completion_view():
    """The regression this fix exists for: screening finished in THIS session,
    the cached token would now resolve INVALID — the completion view must win."""
    at = _run(
        processing_app="app-1", screening_token="tok-1", complete=True
    )
    assert at.session_state["branch"] == "processing:app-1"


def test_returning_visitor_still_gets_the_generic_invalid_path():
    """No same-session state (a fresh browser opening a saved link): routing
    must be exactly as before — straight to token resolution, which collapses a
    completed screening into the generic invalid message. Proves the fix did
    NOT widen the anti-enumeration behaviour."""
    at = _run(processing_app=None, screening_token="tok-1", complete=True)
    assert at.session_state["branch"] == "resume:tok-1"


def test_an_unfinished_same_session_screening_still_resolves_its_token():
    """Mid-screening is unchanged: the completion probe says no, so the normal
    ``?screening=`` resume path runs exactly as it did before."""
    at = _run(processing_app="app-1", screening_token="tok-1", complete=False)
    assert at.session_state["branch"] == "resume:tok-1"


def test_completion_check_is_skipped_without_a_processing_application():
    """The probe must never run for a visitor with no same-session state — that
    is what keeps this fix free of any new lookup surface."""
    at = _run(processing_app=None, screening_token=None, complete=True)
    # no processing app and no token -> falls through to the job-link flow
    assert at.session_state["branch"] == "invalid"


# =====================================================================
# Fix 1 — the DB-reading completion probe
# =====================================================================


class _StubState:
    def __init__(self, status):
        self.screening_session_status = status


@pytest.mark.parametrize(
    "status, expected",
    [
        (ScreeningSessionStatus.SCREENING_COMPLETE, True),
        (ScreeningSessionStatus.ROUND_1_IN_PROGRESS, False),
        (ScreeningSessionStatus.ROUND_1_COMPLETE, False),
        (ScreeningSessionStatus.ROUND_2_IN_PROGRESS, False),
        (ScreeningSessionStatus.READY_FOR_ROUND_1, False),
        (ScreeningSessionStatus.PENDING, False),
        (None, False),
    ],
)
def test_completion_probe_is_true_only_for_screening_complete(
    mocker, status, expected
):
    mocker.patch.object(
        P, "get_pipeline_state", return_value=_StubState(status)
    )
    assert P._same_session_screening_is_complete("app-1") is expected


def test_completion_probe_returns_false_when_the_read_fails(mocker):
    """A failed read must fall back to the pre-existing routing, never invent a
    completion screen."""
    from sqlalchemy.exc import SQLAlchemyError

    mocker.patch.object(
        P, "get_pipeline_state", side_effect=SQLAlchemyError("boom")
    )
    assert P._same_session_screening_is_complete("app-1") is False

    mocker.patch.object(
        P, "get_pipeline_state", side_effect=RuntimeError("boom")
    )
    assert P._same_session_screening_is_complete("app-1") is False


# =====================================================================
# Fix 2 — no email promise in candidate-facing copy
# =====================================================================


def test_processing_done_copy_promises_no_email():
    assert "email" not in P._PROCESSING_DONE.lower()
    assert "e-mail" not in P._PROCESSING_DONE.lower()


def test_no_candidate_facing_message_promises_an_email():
    """Every candidate-facing message constant in the module, not just the one
    that regressed — this codebase has no email capability at all, so none of
    them may imply one."""
    offenders = [
        name for name in dir(P)
        if name.isupper() and name.endswith(("_MSG", "_INTRO", "_DONE"))
        and isinstance(getattr(P, name), str)
        and "email" in getattr(P, name).lower()
    ]
    assert offenders == []


# =====================================================================
# Tier 2 Increment 1 — C1 (question as heading) and C5 (landing framing)
# =====================================================================


_RENDER_PATCHED_ATTRS = (
    "get_round_questions",
    "session_scope",
    "_handle_single_answer",
)


@pytest.fixture(autouse=True)
def _restore_public_main_render():
    saved = {name: getattr(P, name) for name in _RENDER_PATCHED_ATTRS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(P, name, value)


_QUESTION_A = "Tell us about a data pipeline you designed end to end."
_QUESTION_B = "Where have you used PySpark in production?"

_ROUND_FORM_SCRIPT = f'''
import contextlib
import streamlit as st
import app.public_main as P


class _Q:
    def __init__(self, qid, text, answer):
        self.question_id = qid
        self.question_text = text
        self.answer_text = answer
        self.answered = answer is not None


@contextlib.contextmanager
def _fake_scope():
    yield None


P.session_scope = _fake_scope
P.get_round_questions = lambda db, *, screening_session_id, round: [
    _Q("q-1", {_QUESTION_A!r}, None),
    _Q("q-2", {_QUESTION_B!r}, "an earlier answer"),
]
P._handle_single_answer = lambda qid, text: st.session_state.__setitem__(
    "saved", (qid, text)
)

P._render_round_form("sess-1", round_=1, intro="intro copy")
'''


def _round_form_app() -> AppTest:
    at = AppTest.from_string(_ROUND_FORM_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


# --- C1, test item 1 -----------------------------------------------------


def test_each_screening_question_renders_as_a_heading_not_a_field_label():
    at = _round_form_app()

    headings = [m.value for m in at.markdown]
    assert f"**{_QUESTION_A}**" in headings
    assert f"**{_QUESTION_B}**" in headings


def test_the_question_text_is_no_longer_the_text_areas_label():
    """The regression this fixes: the question used to BE the widget label, so
    the most important content on the screen was styled as a form caption."""
    at = _round_form_app()

    labels = [ta.label for ta in at.text_area]
    assert _QUESTION_A not in labels
    assert _QUESTION_B not in labels
    # every box now carries the same minimal, generic label instead
    assert labels == ["Your answer", "Your answer"]


# --- C1, test item 2: submission wiring unchanged ------------------------


def test_answer_boxes_keep_their_per_question_keys_and_prefilled_values():
    """One box per question, keyed by question id, pre-filled with any existing
    answer. The key now also carries a revision of the stored answer — see
    ``answer_widget_key`` — which is what keeps box and database in step."""
    at = _round_form_app()

    assert len(at.text_area) == 2
    assert at.text_area(key=P.answer_widget_key("q-1", None)).value == ""
    assert at.text_area(
        key=P.answer_widget_key("q-2", "an earlier answer")
    ).value == "an earlier answer"


def test_each_question_submits_only_itself():
    """Per-question save: clicking question 1's own button hands exactly that
    one question id and its own text to the save path — question 2 is not
    carried along, and is not required to be filled."""
    at = _round_form_app()
    at.text_area(key=P.answer_widget_key("q-1", None)).set_value("my new answer")
    at.button[0].click().run()

    assert at.session_state["saved"] == ("q-1", "my new answer")


def test_there_is_one_save_control_per_question_not_one_per_round():
    at = _round_form_app()
    assert len(at.button) == 2
    assert [b.label for b in at.button] == ["Save answer", "Save answer"]


# --- C5, test item 3 -----------------------------------------------------


_APPLY_INTRO_SCRIPT = '''
import streamlit as st
import app.public_main as P

P._render_apply_intro()
'''


def test_the_landing_page_renders_the_what_to_expect_framing():
    at = AppTest.from_string(_APPLY_INTRO_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    body = " ".join(m.value for m in at.markdown)
    assert P._APPLY_INTRO_HEADING in body
    for point in P._APPLY_INTRO_POINTS:
        assert point in body


def test_the_new_framing_makes_no_email_or_timeline_promise():
    """Fix 2 removed a false "we'll email you" claim; C5's new copy must not
    reintroduce one, nor promise a turnaround this system cannot guarantee.

    Note this checks contact *promises*, not the bare word "email" — unlike the
    ``_MSG``-constant guard above. This copy legitimately tells the candidate an
    email address is one of the things they need to supply, which is a factual
    field requirement, not a claim that anyone will write to them.
    """
    blob = (P._APPLY_INTRO_HEADING + " " + " ".join(P._APPLY_INTRO_POINTS)).lower()
    for forbidden in (
        # contact promises this system cannot keep (no email capability at all)
        "we'll email", "we will email", "email you", "emailed", "by email",
        "e-mail you", "we'll contact", "we will contact", "get in touch",
        # turnaround promises nothing in the system guarantees
        "within", "hours", "days", "weeks", "guarantee", "shortly",
    ):
        assert forbidden not in blob


def test_the_framing_uses_no_internal_terminology():
    """No status enum names or internal field names in candidate-facing copy."""
    blob = (P._APPLY_INTRO_HEADING + " " + " ".join(P._APPLY_INTRO_POINTS)).lower()
    for jargon in (
        "screening_", "application_", "prequalification", "rubric",
        "applicationstatus", "token", "pipeline",
    ):
        assert jargon not in blob


# --- C7, test item 4 -----------------------------------------------------


def test_the_dead_success_message_constant_is_gone():
    """``_apply_result`` always records SUCCESS together with the processing
    application id, so the fallback that rendered this could never run."""
    assert not hasattr(P, "_SUCCESS_MSG")


# =====================================================================
# Tier 2 Increment 2 — C2 (round-1 follow-up framing) and C8 (save link)
# =====================================================================


def _round_form_script(round_: int, *, answered: int = 0) -> str:
    """The round form for a given round, with the DB read stubbed out."""
    return f'''
import contextlib
import streamlit as st
import app.public_main as P


class _Q:
    def __init__(self, qid, text, answer):
        self.question_id = qid
        self.question_text = text
        self.answer_text = answer
        self.answered = answer is not None


@contextlib.contextmanager
def _fake_scope():
    yield None


_ANSWERS = ["done"] * {answered} + [None] * (3 - {answered})

P.session_scope = _fake_scope
P.get_round_questions = lambda db, *, screening_session_id, round: [
    _Q(f"q-{{i}}", f"Question {{i}}?", _ANSWERS[i]) for i in range(3)
]
P._handle_single_answer = lambda qid, text: None

P._render_round_form("sess-1", round_={round_}, intro="intro copy")
'''


def _captions_for_round(round_: int, *, answered: int = 0) -> list[str]:
    at = AppTest.from_string(
        _round_form_script(round_, answered=answered), default_timeout=_TIMEOUT
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return [c.value for c in at.caption]


# --- C2, test item 5 -----------------------------------------------------


def test_round_one_says_a_follow_up_round_may_come_next():
    captions = _captions_for_round(1)
    assert FOLLOW_UP_MAY_FOLLOW in captions


# --- C2, test item 6 -----------------------------------------------------


def test_round_two_does_not_repeat_the_may_come_next_framing():
    """By round 2 the follow-up exists and its own count is accurate — the
    "may come next" line would be stale and confusing."""
    captions = _captions_for_round(2)
    assert FOLLOW_UP_MAY_FOLLOW not in captions


# --- C2, test item 7 -----------------------------------------------------


def test_a_completed_screening_never_reaches_the_round_form_at_all():
    """The framing cannot leak into the completed state, because
    ``_render_processing`` dispatches SCREENING_COMPLETE to its completion
    message and only calls the round form for the two in-progress states."""
    import inspect

    source = inspect.getsource(P._render_processing)
    complete_branch = source.split("SCREENING_COMPLETE", 1)[1].split("if status ==", 1)[0]
    assert "_render_round_form" not in complete_branch
    assert "_SCREENING_DONE_MSG" in complete_branch


# --- C2, test items 8 and 9 ---------------------------------------------


def test_the_per_round_answered_caption_is_unchanged_for_both_rounds():
    """Regression guard: C2 added a line, it did not touch the existing count."""
    assert "0 of 3 answered" in _captions_for_round(1, answered=0)
    assert "2 of 3 answered" in _captions_for_round(1, answered=2)
    assert "0 of 3 answered" in _captions_for_round(2, answered=0)
    assert "3 of 3 answered" in _captions_for_round(2, answered=3)


def test_no_caption_ever_states_a_cross_round_total():
    """Any "X of Y" on screen must be the CURRENT round's own count. A total
    spanning both rounds is unknowable (round 2 may yield zero questions), so
    it must never be fabricated."""
    import re

    for round_ in (1, 2):
        for answered in (0, 3):
            for caption in _captions_for_round(round_, answered=answered):
                for found in re.findall(r"(\d+)\s+of\s+(\d+)", caption):
                    # the only permitted pair is this round's own answered/total
                    assert found == (str(answered), "3"), caption


# --- C8, test item 3 (integration through the real call site) -----------


_SAVE_LINK_SCRIPT = '''
import streamlit as st
import app.public_main as P

P._render_save_link("S" * 43)
'''


def test_the_save_link_call_site_now_renders_a_wrapping_copyable_block():
    at = AppTest.from_string(_SAVE_LINK_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert len(at.code) == 1
    block = at.code[0]
    assert block.wrap_lines is True          # no horizontal overflow
    assert block.type == "code"              # native copy button preserved
    assert block.value.endswith("?screening=" + "S" * 43)
    # the explanatory info message above it is unchanged
    assert any(P._SAVE_LINK_MSG in i.value for i in at.info)


# --- C8, test item 4: the token must not leak --------------------------


def test_rendering_the_save_link_never_logs_the_token(caplog):
    """Sentinel-style, matching the leak tests used for the résumé-retry token:
    the token IS the sentinel and must appear nowhere but the rendered block."""
    import logging

    caplog.set_level(logging.DEBUG)
    sentinel = "S" * 43
    at = AppTest.from_string(_SAVE_LINK_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    assert sentinel not in caplog.text
    # ...and nowhere in the page except the one code block that renders it
    non_code = " ".join(
        [m.value for m in at.markdown]
        + [c.value for c in at.caption]
        + [i.value for i in at.info]
    )
    assert sentinel not in non_code


def test_the_candidate_ui_renderer_makes_no_logging_call():
    """Structural: the new rendering helper has no logging seam at all, so the
    token cannot reach a log through it."""
    import ast
    import inspect

    from app.utils import candidate_ui

    tree = ast.parse(inspect.getsource(candidate_ui))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not {"info", "debug", "warning", "error", "exception"} & (
        called - {"code"}
    )


# =====================================================================
# Follow-up — the résumé-retry link gets the same wrapping fix as C8
# =====================================================================
#
# ``_render_retry_save_link`` (Fix 3) had the identical narrow-viewport overflow
# that C8 fixed on the screening save-link: a base URL plus a 43-character token
# in a non-wrapping code block scrolls sideways on a phone. It now delegates to
# the same ``candidate_ui.resume_link_block``, so the two links behave alike.


_RETRY_LINK_SCRIPT = '''
import streamlit as st
import app.public_main as P

P._render_retry_save_link("R" * 43)
'''


def _retry_link_app() -> AppTest:
    at = AppTest.from_string(_RETRY_LINK_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_the_retry_link_wraps_instead_of_overflowing():
    """The defect this fixes — asserted exactly as the screening save-link's
    equivalent test does."""
    at = _retry_link_app()
    assert len(at.code) == 1
    assert at.code[0].wrap_lines is True


def test_the_retry_link_keeps_the_native_copy_affordance():
    at = _retry_link_app()
    assert at.code[0].type == "code"
    assert at.code[0].language == "plaintext"


def test_the_retry_link_renders_the_exact_retry_url():
    """The rendering method changed; the value must not have."""
    at = _retry_link_app()
    assert at.code[0].value == P.resume_retry_url(
        P.public_base_url(), "R" * 43
    )
    assert at.code[0].value.endswith("?retry=" + "R" * 43)
    # the explanatory message above it is unchanged
    assert any(P._RETRY_SAVE_LINK_MSG in i.value for i in at.info)


def test_rendering_the_retry_link_never_logs_the_token(caplog):
    """Sentinel + caplog, mirroring the screening save-link's leak test: the
    token IS the sentinel and must appear nowhere but the rendered block."""
    import logging

    caplog.set_level(logging.DEBUG)
    sentinel = "R" * 43
    at = _retry_link_app()

    assert sentinel not in caplog.text
    non_code = " ".join(
        [m.value for m in at.markdown]
        + [c.value for c in at.caption]
        + [i.value for i in at.info]
        + [w.value for w in at.warning]
    )
    assert sentinel not in non_code


def test_both_candidate_links_now_render_identically():
    """Regression guard against the two drifting apart again: the screening
    save-link and the résumé-retry link must share wrapping and copy behaviour."""
    save = AppTest.from_string(_SAVE_LINK_SCRIPT, default_timeout=_TIMEOUT).run()
    retry = _retry_link_app()
    assert not save.exception and not retry.exception

    assert save.code[0].wrap_lines == retry.code[0].wrap_lines is True
    assert save.code[0].language == retry.code[0].language


def test_no_raw_unwrapped_code_block_remains_in_the_candidate_app():
    """Structural sweep: every ``st.code`` call in the candidate app must go
    through the shared helper, so a future link cannot reintroduce the defect."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(P))
    direct_st_code = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "code"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
    ]
    assert direct_st_code == []


# =====================================================================
# Increment 3 — C4 (name/email read-back) and C6 (differentiated failures)
# =====================================================================


_C4_PATCHED_ATTRS = ("get_application_contact", "get_resume_retry_token",
                     "session_scope")


@pytest.fixture(autouse=True)
def _restore_public_main_increment3():
    saved = {name: getattr(P, name) for name in _C4_PATCHED_ATTRS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(P, name, value)


_RETRY_SCREEN_SCRIPT = '''
import contextlib
import streamlit as st
import app.public_main as P
from app.services.candidate_portal_service import CandidateContact


@contextlib.contextmanager
def _fake_scope():
    yield None


P.session_scope = _fake_scope
P.get_application_contact = lambda db, *, application_id: CandidateContact(
    full_name="Casey Candidate", email="casey@example.com"
)
P.get_resume_retry_token = lambda db, *, application_id: None

P._render_resume_retry_form("job-1", "app-1")
'''


def _retry_screen() -> AppTest:
    at = AppTest.from_string(_RETRY_SCREEN_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


# --- C4, test item 3 -----------------------------------------------------


def test_the_retry_screen_shows_the_real_captured_name_and_email():
    at = _retry_screen()
    body = " ".join(m.value for m in at.markdown)
    assert "Casey Candidate" in body
    assert "casey@example.com" in body


def test_the_retry_screen_shows_no_placeholder_text():
    """``_handle_retry_submit`` passes literal placeholders into
    ``validate_application_form`` to reuse its résumé branch — those values must
    never reach the screen."""
    at = _retry_screen()
    body = " ".join(
        [m.value for m in at.markdown]
        + [w.value for w in at.warning]
        + [c.value for c in at.caption]
    )
    assert "placeholder" not in body.lower()


def test_the_name_and_email_are_read_only_not_editable_fields():
    """Read-back context, not a form the candidate re-types."""
    at = _retry_screen()
    labels = [ti.label for ti in at.text_input]
    assert not any("name" in (l or "").lower() for l in labels)
    assert not any("email" in (l or "").lower() for l in labels)


# --- C6, test items 5 and 7 through the real retry screen ---------------


def _retry_screen_with_parked_message(message: str | None) -> AppTest:
    seed = (
        f"P.remember_retry_failure_message(st.session_state, {message!r})"
        if message is not None else ""
    )
    script = _RETRY_SCREEN_SCRIPT.replace(
        'P._render_resume_retry_form("job-1", "app-1")',
        seed + "\nP._render_resume_retry_form(\"job-1\", \"app-1\")",
    )
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_the_retry_screen_shows_the_specific_failure_message_when_parked():
    from app.services.candidate_portal_service import submission_failure_message

    specific = submission_failure_message("UNSUPPORTED_TYPE")
    at = _retry_screen_with_parked_message(specific)
    assert any(specific in w.value for w in at.warning)


def test_the_retry_screen_falls_back_to_the_generic_message():
    at = _retry_screen_with_parked_message(None)
    assert any(P._RESUME_RETRY_MSG in w.value for w in at.warning)


def test_the_parked_failure_message_is_shown_once_then_cleared():
    """Read-and-clear, so a stale explanation cannot follow the candidate."""
    state: dict = {}
    P.remember_retry_failure_message(state, "a specific explanation")
    assert P.take_retry_failure_message(state) == "a specific explanation"
    assert P.take_retry_failure_message(state) is None


# --- C6, test item 6: the reason code never reaches the screen ----------


def test_no_internal_reason_code_appears_on_the_retry_screen():
    from app.services.candidate_portal_service import submission_failure_message

    for reason in (
        "UNSUPPORTED_TYPE", "FILE_VALIDATION", "DriveUploadError",
        "StorageConfigError", "DriveAuthError", "APPLICATION_NOT_FOUND",
        "RESUME_ALREADY_UPLOADED",
    ):
        at = _retry_screen_with_parked_message(
            submission_failure_message(reason)
        )
        body = " ".join(
            [m.value for m in at.markdown]
            + [w.value for w in at.warning]
            + [c.value for c in at.caption]
        )
        assert reason not in body


# --- C6, test item 8: unrelated outcomes untouched ----------------------


def test_apply_result_still_handles_the_non_failure_outcomes_unchanged():
    """SUCCESS / DUPLICATE / LINK_INVALID must not have acquired a message."""
    import ast
    import inspect

    source = inspect.getsource(P._apply_result)
    tree = ast.parse(source.strip())
    # ``st.*`` calls only — ``logger.info`` in the SUCCESS branch is not a
    # rendering call and must not be counted as one.
    rendered = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
    }
    # the only thing this function renders is the ERROR branch's message; the
    # SUCCESS / DUPLICATE / LINK_INVALID branches still just rerun
    assert rendered == {"error", "rerun"}
    # and the message it renders is the mapped one, never a raw reason
    assert "submission_failure_message(result.internal_reason)" in source


# =====================================================================
# D4 — per-question submission (first direct coverage of the save path)
# =====================================================================
#
# Until now ``_handle_round_answers`` was stubbed out in every UI test, so the
# real submission mechanism had no coverage at all. These exercise the REAL
# ``_render_question`` and ``_handle_single_answer``; only the service boundary
# (``submit_answer``) and the DB session are stubbed, so what is under test is
# the thing that previously had none.


_D4_PATCHED = ("get_round_questions", "session_scope", "submit_answer")


@pytest.fixture(autouse=True)
def _restore_public_main_d4():
    saved = {name: getattr(P, name) for name in _D4_PATCHED}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(P, name, value)


def _per_question_script(answers: list[str | None]) -> str:
    """The real round form over ``answers``; records every submit_answer call."""
    return f'''
import contextlib
import streamlit as st
import app.public_main as P


class _Q:
    def __init__(self, qid, text, answer):
        self.question_id = qid
        self.question_text = text
        self.answer_text = answer
        self.answered = answer is not None


@contextlib.contextmanager
def _fake_scope():
    yield None


st.session_state.setdefault("calls", [])

def _record(db, *, screening_question_id, answer_text):
    st.session_state["calls"].append((screening_question_id, answer_text))

P.session_scope = _fake_scope
P.submit_answer = _record
P.get_round_questions = lambda db, *, screening_session_id, round: [
    _Q(f"q-{{i+1}}", f"Question {{i+1}}?", a)
    for i, a in enumerate({answers!r})
]

P._render_round_form("sess-1", round_=1, intro="intro")
'''


def _per_question_app(answers) -> AppTest:
    at = AppTest.from_string(
        _per_question_script(answers), default_timeout=_TIMEOUT
    ).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


# --- item 1 & 2: one answer saves, alone ---------------------------------


def test_saving_one_answer_calls_the_service_for_that_question_only():
    at = _per_question_app([None, None, None])
    at.text_area(key=P.answer_widget_key("q-2", None)).set_value("only this one")
    at.button[1].click().run()

    assert at.session_state["calls"] == [("q-2", "only this one")]


def test_saving_one_answer_does_not_require_the_others_to_be_filled():
    """The whole point of the change: no other box needs content, and none is
    written."""
    at = _per_question_app([None, None, None])
    at.text_area(key=P.answer_widget_key("q-3", None)).set_value("third only")
    at.button[2].click().run()

    calls = at.session_state["calls"]
    assert len(calls) == 1 and calls[0][0] == "q-3"


def test_already_answered_questions_are_not_rewritten_when_another_is_saved():
    """The batched form re-submitted every non-blank box on every save; a
    per-question save must touch exactly one row."""
    at = _per_question_app(["already saved", None])
    at.text_area(key=P.answer_widget_key("q-2", None)).set_value("new one")
    at.button[1].click().run()

    assert at.session_state["calls"] == [("q-2", "new one")]


# --- item 5: blanks stay blank, and block nothing ------------------------


def test_saving_a_blank_answer_writes_nothing_and_explains():
    at = _per_question_app([None, None])
    at.button[0].click().run()

    assert at.session_state["calls"] == []
    assert any(P._ANSWER_BLANK_MSG in i.value for i in at.info)


def test_a_blank_question_does_not_block_saving_a_different_one():
    at = _per_question_app([None, None])
    at.text_area(key=P.answer_widget_key("q-2", None)).set_value("answered")
    at.button[1].click().run()

    assert at.session_state["calls"] == [("q-2", "answered")]


# --- item 3: the Q5 inconsistency, structurally impossible now -----------


def test_a_box_always_shows_what_was_persisted_for_it():
    """The Q5 discriminator: an answer that reached the database by a path other
    than typing into that box. Under the old single-key widget the box rendered
    empty while the caption counted it; the revisioned key makes the two agree
    by construction."""
    at = _per_question_app([None, "DB-ONLY-VALUE-NEVER-TYPED"])

    caption = [c.value for c in at.caption if "answered" in c.value][0]
    assert caption == "1 of 2 answered"
    assert at.text_area(
        key=P.answer_widget_key("q-2", "DB-ONLY-VALUE-NEVER-TYPED")
    ).value == "DB-ONLY-VALUE-NEVER-TYPED"
    # ...and no box is left disagreeing with the count
    assert sum(1 for t in at.text_area if t.value) == 1


def test_the_widget_key_changes_only_when_the_stored_answer_changes():
    """Same stored answer keeps the widget (so an unsaved draft survives an
    unrelated rerun); a different stored answer forces a fresh widget so
    ``value=`` is applied again."""
    k = P.answer_widget_key
    assert k("q-1", None) == k("q-1", None)
    assert k("q-1", "abc") == k("q-1", "abc")
    assert k("q-1", None) != k("q-1", "abc")
    assert k("q-1", "abc") != k("q-2", "abc")
    assert k("q-1", "") == k("q-1", None)


# --- item 6: the caption tracks individual saves -------------------------


@pytest.mark.parametrize(
    "answers, expected",
    [
        ([None, None, None], "0 of 3 answered"),
        (["a", None, None], "1 of 3 answered"),
        (["a", "b", None], "2 of 3 answered"),
        (["a", "b", "c"], "3 of 3 answered"),
    ],
)
def test_the_progress_caption_reflects_each_individual_save(answers, expected):
    at = _per_question_app(answers)
    assert any(c.value == expected for c in at.caption)


# --- item 7: C1 preserved -------------------------------------------------


def test_each_question_is_still_a_heading_not_a_field_label():
    at = _per_question_app([None, None])
    headings = [m.value for m in at.markdown]
    assert "**Question 1?**" in headings
    assert "**Question 2?**" in headings
    assert [t.label for t in at.text_area] == ["Your answer", "Your answer"]


# --- item 8: refresh recovery --------------------------------------------


def test_previously_saved_answers_repopulate_on_a_fresh_render():
    """What a candidate returning through their ``?screening=`` link sees: a
    fresh script run with answers already in the database."""
    at = _per_question_app(["saved one", None, "saved three"])

    assert at.text_area(key=P.answer_widget_key("q-1", "saved one")).value == "saved one"
    assert at.text_area(key=P.answer_widget_key("q-2", None)).value == ""
    assert at.text_area(
        key=P.answer_widget_key("q-3", "saved three")
    ).value == "saved three"
    assert any(c.value == "2 of 3 answered" for c in at.caption)


# --- item 4: the last answer's individual save reaches the completion path ---
#
# Round completion is not UI logic — it lives inside ``submit_answer`` as a
# check-first, idempotent block that runs once every question in the round has
# an answer (screening_question_service.py). The batched form reached it by
# calling ``submit_answer`` once per non-blank box; a per-question save reaches
# it by exactly the same call. What is left to prove at THIS layer is that the
# last question's own button does make that call. The transitions it then
# triggers are covered against a real database in
# tests/test_screening_question_service.py, which already drives them one
# ``submit_answer`` call at a time:
#   * test_last_round_1_answer_completes_round_and_triggers_round_2
#   * test_round_2_zero_questions_goes_straight_to_complete
#   * test_last_round_2_answer_completes_screening_once
#   * test_double_submit_of_last_answer_does_not_double_transition_or_emit


def test_saving_the_final_unanswered_question_reaches_the_service():
    """With every other question already answered, the last box's own save is
    what carries the round into completion."""
    at = _per_question_app(["a1", "a2", None])
    at.text_area(key=P.answer_widget_key("q-3", None)).set_value("the last one")
    at.button[2].click().run()

    assert at.session_state["calls"] == [("q-3", "the last one")]


def test_completion_is_not_reimplemented_in_the_page():
    """Guard: the page must not grow its own copy of the round-completion rules
    — they stay in the service, which is what keeps them idempotent."""
    import inspect

    source = inspect.getsource(P._handle_single_answer)
    for leaked in (
        "ROUND_1_COMPLETE", "SCREENING_COMPLETE", "generate_round_questions",
        "all_answered", "_trigger_round_2",
    ):
        assert leaked not in source
