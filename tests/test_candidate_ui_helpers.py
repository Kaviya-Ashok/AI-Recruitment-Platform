"""Unit tests for the candidate-side presentation helpers
(``app/utils/candidate_ui.py``, Phase D Tier 2 Increment 2).

Two things are pinned here:

* the module's **defining invariant** — it is severed from the HR-side UI
  modules and from every HR service. That severance is the whole reason the
  module exists, and it is the kind of thing an autocomplete slip undoes
  silently, so it is asserted by parsing the module's imports, not by eye. This
  mirrors ``tests/test_ui_helpers.py``'s AST guard that ``ui.py`` never calls
  ``st.*`` — same rigour, different invariant;
* the pure follow-up-framing logic, tested directly with no Streamlit runtime.

The one rendering function is exercised through Streamlit's ``AppTest``.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from streamlit.testing.v1 import AppTest

from app.utils import candidate_ui

_TIMEOUT = 60


def _imported_modules() -> set[str]:
    """Every module name ``candidate_ui`` imports, by parsing its source."""
    tree = ast.parse(inspect.getsource(candidate_ui))
    plain = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    from_imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    return plain | from_imports


# --- the module's defining invariant: severance ------------------------


def test_candidate_ui_never_imports_the_hr_ui_modules():
    """``ui_widgets.py`` drags ``job_service.list_jobs`` into the import graph,
    and ``ui.py``'s ``label_for`` would render internal HR status vocabulary to
    a candidate. Neither may be reachable from the public app."""
    for forbidden in ("app.utils.ui", "app.utils.ui_widgets"):
        assert forbidden not in _imported_modules()


def test_candidate_ui_never_imports_any_service():
    """No HR service — and, being belt-and-braces, no service at all: this
    module is presentation only."""
    offenders = [m for m in _imported_modules() if m.startswith("app.services")]
    assert offenders == []


def test_candidate_ui_imports_nothing_from_the_app_beyond_itself():
    """The strongest form of the invariant: today it imports only stdlib and
    Streamlit. If a genuine future need arises this test should be widened
    deliberately, not tripped over."""
    assert _imported_modules() == {"__future__", "streamlit"}


def test_the_hr_ui_widgets_module_really_is_unsafe_to_import():
    """Pins the *reason* for severance, so it cannot quietly stop being true
    without someone noticing."""
    from app.utils import ui_widgets

    tree = ast.parse(inspect.getsource(ui_widgets))
    modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert any(m.startswith("app.services") for m in modules)


def test_the_hr_label_for_still_maps_internal_status_vocabulary():
    """The other half of the reason: ``label_for`` would happily render
    internal pipeline states as prose to a candidate."""
    from app.utils import ui

    assert ui.label_for("SCREENING_EVALUATED") != "SCREENING_EVALUATED"
    assert ui.label_for("SCREENING_INCOMPLETE") != "SCREENING_INCOMPLETE"


# --- pure helper: follow_up_notice -------------------------------------


def test_round_one_gets_the_follow_up_notice():
    assert candidate_ui.follow_up_notice(1) == candidate_ui.FOLLOW_UP_MAY_FOLLOW


def test_round_two_gets_no_notice():
    """By the time round 2 renders it exists and its own count is accurate; a
    "may come next" line would be stale."""
    assert candidate_ui.follow_up_notice(2) is None


@pytest.mark.parametrize("round_number", [0, 3, 99, -1])
def test_no_other_round_number_gets_a_notice(round_number):
    assert candidate_ui.follow_up_notice(round_number) is None


def test_the_notice_implies_no_number_of_follow_up_questions():
    """Round 2 yields 0-3 questions and may be empty, so the copy must not
    imply a count — not even a vague nonzero one."""
    text = candidate_ui.FOLLOW_UP_MAY_FOLLOW.lower()
    for forbidden in (
        "a few more", "a couple", "some more", "two", "three",
        "several", "questions remain",
    ):
        assert forbidden not in text
    # ...and it must stay conditional
    assert "may" in text


def test_the_notice_states_no_cross_round_total():
    """No "X of Y" spanning both rounds — that number does not exist."""
    import re

    assert re.search(r"\d+\s+of\s+\d+", candidate_ui.FOLLOW_UP_MAY_FOLLOW) is None


# --- widget: resume_link_block -----------------------------------------


_LINK = "http://localhost:8502/?screening=" + "T" * 43

_LINK_SCRIPT = f'''
import streamlit as st
from app.utils.candidate_ui import resume_link_block

resume_link_block({_LINK!r})
'''


def _link_app() -> AppTest:
    at = AppTest.from_string(_LINK_SCRIPT, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_the_resume_link_renders_the_exact_url():
    at = _link_app()
    assert len(at.code) == 1
    assert at.code[0].value == _LINK


def test_the_resume_link_wraps_instead_of_overflowing():
    """The C8 regression: a base URL plus a 43-char token overflows a phone-
    width container in a non-wrapping code block."""
    at = _link_app()
    assert at.code[0].wrap_lines is True


def test_the_resume_link_keeps_the_native_copy_affordance():
    """``st.code`` renders Streamlit's own copy button — a real asset on mobile,
    where selecting a long URL by hand is miserable. Using ``st.code`` (rather
    than plain markdown or a text_input) is what preserves it."""
    at = _link_app()
    assert at.code[0].type == "code"
    assert at.code[0].language == "plaintext"
