"""Page tests for the interview-transcript UI (Increment B).

AppTest harness, same style as ``test_interviews_page_analysis_section``: the
real ``_render_feedback_section`` / ``_render_final_scorecard`` run against
stubbed loaders and stubbed service calls (no database, no Drive, no AI).

Scripts rebind attributes on the real page module, so every one is snapshotted
at IMPORT time and restored before and after each test (see the
``apptest-module-mutation-leak`` note in the project memory).
"""

from __future__ import annotations

import uuid

import pytest
from streamlit.testing.v1 import AppTest

import app.pages.interviews as I

_TIMEOUT = 60

_PATCHED_ATTRS = (
    "_load_view",
    "load_job_options",
    "session_scope",
    "create_interview_feedback",
    "attach_interview_transcript",
    "get_transcript_download_bytes",
    "_render_shortlisted_section",
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


_FEEDBACK_SCRIPT = '''
import contextlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import streamlit as st

import app.pages.interviews as I
from app.services.interview_transcript_service import (
    InterviewTranscriptStorageError,
)
from app.utils.session import SESSION_USER_KEY

APP_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
FB_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
GUIDE_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
WHEN = datetime(2026, 10, 6, tzinfo=timezone.utc)

st.session_state.setdefault("LOG", [])
LOG = st.session_state["LOG"]


def version(n, status, *, name, extractable=True, superseded=False):
    return SimpleNamespace(
        transcript_id=uuid.uuid4(), interview_feedback_id=FB_ID,
        application_id=APP_ID, interview_round=1, version_number=n,
        file_name=name, mime_type="application/pdf", file_size_bytes=4096,
        text_extractable=extractable, status=status,
        uploaded_by_user_id=uuid.uuid4(), uploaded_by_name="Uma Uploader",
        created_at=WHEN, superseded_at=WHEN if superseded else None,
    )


SPEC = "__SPEC__"
if SPEC == "none":
    versions = []
elif SPEC == "current":
    versions = [version(1, "CURRENT", name="Ananya_Rao_Transcript_R1.pdf")]
elif SPEC == "scan":
    versions = [version(1, "CURRENT", name="Ananya_Rao_Transcript_R1.pdf",
                        extractable=False)]
elif SPEC == "two":
    versions = [
        version(2, "CURRENT", name="Ananya_Rao_Transcript_R1_v2.pdf"),
        version(1, "SUPERSEDED", name="Ananya_Rao_Transcript_R1.pdf",
                superseded=True),
    ]

history = [SimpleNamespace(
    feedback_id=FB_ID, application_id=APP_ID, interview_guide_id=GUIDE_ID,
    interview_round=1, recommendation="PROCEED", notes="Went well.",
    submitted_by_user_id=uuid.uuid4(), submitted_by_name="Dana Interviewer",
    created_at=WHEN, ratings=[],
)] if __HAS_HISTORY__ else []

context = SimpleNamespace(
    application_id=APP_ID, candidate_name="Ananya Rao",
    candidate_email="a@x.test", interview_guide_id=GUIDE_ID,
    suggested_round=2 if history else 1, feedback_count=len(history),
)
state = {APP_ID: {
    "context": context, "history": history,
    "transcripts": {FB_ID: versions} __TRANSCRIPTS_KEY__,
}}


@contextlib.contextmanager
def fake_scope():
    yield object()


I.session_scope = fake_scope


def fake_create(db, **kw):
    LOG.append(("create", kw["interview_round"], kw["application_id"]))
    return SimpleNamespace(id=FB_ID)


def fake_attach(db, *, interview_feedback_id, file_bytes, original_filename,
                acting_user_id):
    LOG.append(("attach", interview_feedback_id, original_filename,
                len(file_bytes)))
    if __FAIL_ATTACH__:
        raise InterviewTranscriptStorageError("Drive is unavailable.")


I.create_interview_feedback = fake_create
I.attach_interview_transcript = fake_attach

st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111", "email": "p@x.test",
    "full_name": "Dana Interviewer", "role": "HR",
}
I._render_feedback_section(APP_ID, state, "11111111-1111-1111-1111-111111111111")
'''


def _feedback_script(
    spec="none", *, history=True, fail_attach=False, transcripts_key=True
) -> str:
    return (
        _FEEDBACK_SCRIPT
        .replace("__SPEC__", spec)
        .replace("__HAS_HISTORY__", str(history))
        .replace("__FAIL_ATTACH__", str(fail_attach))
        .replace(
            "__TRANSCRIPTS_KEY__",
            "" if transcripts_key else "if False else {}",
        )
    )


def _run(script: str) -> AppTest:
    at = AppTest.from_string(script, default_timeout=_TIMEOUT).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def _text(at) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [w.value for w in at.warning]
    parts += [e.value for e in at.error]
    return "\n".join(str(p) for p in parts)


def _uploader(at, label_part):
    matches = [u for u in at.get("file_uploader") if label_part in u.label]
    assert matches, [u.label for u in at.get("file_uploader")]
    return matches[0]


def _button(at, label):
    return next(b for b in at.button if b.label == label)


# --- history: the current transcript ---------------------------------------


def test_history_shows_an_attached_transcript_with_its_name_and_a_download():
    at = _run(_feedback_script("current"))
    text = _text(at)
    assert "Ananya_Rao_Transcript_R1.pdf" in text
    assert "Uma Uploader" in text and "2026-10-06" in text
    assert [b.label for b in at.get("download_button")] == ["Download transcript"]


def test_history_says_so_when_no_transcript_is_attached():
    at = _run(_feedback_script("none"))
    assert "No transcript attached." in _text(at)
    assert at.get("download_button") == []


def test_no_extractable_warning_for_a_normal_file():
    at = _run(_feedback_script("current"))
    assert "no readable text" not in _text(at)


def test_extractable_warning_appears_for_a_scan():
    at = _run(_feedback_script("scan"))
    warnings = " ".join(w.value for w in at.warning)
    assert "no readable text" in warnings
    assert "probably a scan" in warnings
    assert "AI analysis will not be able to use it" in warnings


def test_superseded_versions_are_listed_collapsed_and_current_is_primary():
    at = _run(_feedback_script("two"))
    text = _text(at)
    assert "Ananya_Rao_Transcript_R1_v2.pdf" in text
    assert "Ananya_Rao_Transcript_R1.pdf" in text            # the old one
    popovers = at.get("popover")
    assert len(popovers) == 1
    assert "Earlier versions (1)" in popovers[0].proto.popover.label


def test_no_earlier_versions_control_when_there_is_only_one_version():
    at = _run(_feedback_script("current"))
    assert at.get("popover") == []


def test_no_raw_ids_are_shown():
    at = _run(_feedback_script("two"))
    text = _text(at)
    assert "55555555" not in text and "44444444" not in text


def test_the_pre_increment_call_shape_draws_no_transcript_block():
    """``_render_feedback_history(history)`` with no transcripts is unchanged."""
    at = _run(_feedback_script("current", transcripts_key=False))
    assert "No transcript attached." in _text(at)    # {} -> honest empty line


# --- history: attach / replace ----------------------------------------------


def test_history_offers_attach_when_none_and_replace_when_one_exists():
    none = _run(_feedback_script("none"))
    assert _uploader(none, "Attach transcript") is not None
    assert _button(none, "Attach transcript").disabled is True

    cur = _run(_feedback_script("current"))
    assert _uploader(cur, "Replace transcript") is not None
    assert _button(cur, "Replace transcript").disabled is True


def test_attach_button_calls_the_service_with_the_rounds_feedback_id():
    at = _run(_feedback_script("none"))
    _uploader(at, "Attach transcript").set_value(
        ("raw name.pdf", b"%PDF-1.4 bytes", "application/pdf")
    ).run()
    assert not at.exception
    _button(at, "Attach transcript").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    log = at.session_state["LOG"]
    assert log[-1] == (
        "attach", uuid.UUID("55555555-5555-5555-5555-555555555555"),
        "raw name.pdf", len(b"%PDF-1.4 bytes"),
    )


def test_a_failed_attach_from_history_shows_a_clear_error():
    at = _run(_feedback_script("none", fail_attach=True))
    _uploader(at, "Attach transcript").set_value(
        ("t.pdf", b"%PDF-1.4 x", "application/pdf")
    ).run()
    _button(at, "Attach transcript").click().run()
    assert any("Drive is unavailable." in e.value for e in at.error)


# --- the feedback form's optional uploader ------------------------------------


def test_the_feedback_form_has_the_optional_transcript_uploader():
    at = _run(_feedback_script("none", history=False))
    up = _uploader(at, "Interview transcript (PDF or DOCX, up to 10 MB)")
    assert up.label == "Interview transcript (PDF or DOCX, up to 10 MB)"
    assert up.key.startswith("fb_transcript_")


def test_saving_feedback_without_a_file_never_calls_attach():
    at = _run(_feedback_script("none", history=False))
    _button(at, "Save interview feedback").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    kinds = [entry[0] for entry in at.session_state["LOG"]]
    assert kinds == ["create"]


def test_saving_feedback_with_a_file_creates_feedback_first_then_attaches():
    at = _run(_feedback_script("none", history=False))
    _uploader(at, "Interview transcript").set_value(
        ("notes.pdf", b"%PDF-1.4 abc", "application/pdf")
    ).run()
    _button(at, "Save interview feedback").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    log = at.session_state["LOG"]
    assert [entry[0] for entry in log] == ["create", "attach"]
    assert log[1][1] == uuid.UUID("55555555-5555-5555-5555-555555555555")
    assert log[1][2] == "notes.pdf"


def test_a_failed_attach_keeps_the_feedback_and_explains_calmly():
    at = _run(_feedback_script("none", history=False, fail_attach=True))
    _uploader(at, "Interview transcript").set_value(
        ("notes.pdf", b"%PDF-1.4 abc", "application/pdf")
    ).run()
    _button(at, "Save interview feedback").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    log = at.session_state["LOG"]
    assert [entry[0] for entry in log] == ["create", "attach"]   # feedback saved

    warnings = " ".join(w.value for w in at.warning)
    assert "feedback was saved" in warnings
    assert "transcript could not be attached" in warnings
    assert "Drive is unavailable." in warnings
    assert "history below" in warnings
    assert not at.error                                  # not presented as a failure


# --- final scorecard line --------------------------------------------------------


def _scorecard_script(file_name) -> str:
    return f'''
import dataclasses
from types import SimpleNamespace

import streamlit as st

import app.pages.interviews as I
from tests.test_interviews_page_final_scorecard import _card

base = _card()
view = SimpleNamespace(
    **{{f.name: getattr(base, f.name) for f in dataclasses.fields(base)}},
    interview_transcript_file_name={file_name!r},
)
I._render_final_scorecard(view)
'''


def test_scorecard_says_a_transcript_is_attached_with_its_name():
    at = _run(_scorecard_script("Ananya_Rao_Transcript_R1.pdf"))
    assert (
        "Interview transcript attached: Ananya_Rao_Transcript_R1.pdf" in _text(at)
    )
    assert at.get("download_button") == []             # no download on this page


def test_scorecard_says_none_is_attached_otherwise():
    at = _run(_scorecard_script(None))
    assert "No interview transcript attached to this round." in _text(at)


def test_scorecard_is_silent_about_transcripts_when_no_feedback_exists():
    script = _scorecard_script("x.pdf").replace(
        "I._render_final_scorecard(view)",
        "view.interview_round = None\nI._render_final_scorecard(view)",
    )
    text = _text(_run(script))
    assert "transcript attached" not in text.lower() or "No interview feedback" in text
    assert "x.pdf" not in text
