"""Tests for the pure helpers extracted from app/public_main.py.

The Streamlit rendering (``_render_*``) is not unit-tested — it is thin ``st.*``
glue and is verified manually. These helpers take plain strings / a plain
mapping (no ``st.*``, no DB), exactly so they can be tested here.
"""

from __future__ import annotations

import pytest

from app.public_main import (
    FormErrors,
    clear_pending_application,
    clear_pipeline_error,
    clear_screening_token,
    form_done_kind,
    get_pending_application_id,
    get_pipeline_error,
    get_processing_application_id,
    get_screening_token,
    is_valid_candidate_email,
    mark_form_done,
    public_base_url,
    remember_pending_application,
    remember_processing_application,
    remember_screening_token,
    resolve_active_screening_token,
    screening_resume_url,
    set_pipeline_error,
    validate_application_form,
)
from app.services.candidate_portal_service import (
    _MIME_FOR_EXT,
    mime_type_for_filename,
)
from app.services.storage_service import ALLOWED_MIME_TYPES

_PDF = b"%PDF-1.4\n%stub\n"  # passes validate_uploaded_file (non-empty, .pdf)


# --- is_valid_candidate_email --------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "casey@example.com",
        "first.last+tag@sub.example.co.uk",
        "  trimmed@example.com  ",
        "x@y.z",
    ],
)
def test_email_valid(email):
    assert is_valid_candidate_email(email) is True


@pytest.mark.parametrize(
    "email",
    [
        None,
        "",
        "no-at-sign.com",
        "no-domain-dot@example",
        "spaces in@example.com",
        "two@@example.com",
        "@example.com",
        "casey@",
    ],
)
def test_email_invalid(email):
    assert is_valid_candidate_email(email) is False


# --- mime_type_for_filename (+ drift guard) -----------------------


def test_mime_type_for_filename():
    assert mime_type_for_filename("resume.pdf") == "application/pdf"
    assert mime_type_for_filename("RESUME.PDF") == "application/pdf"
    assert mime_type_for_filename("cv.docx").endswith("wordprocessingml.document")
    assert mime_type_for_filename("notes.txt") is None
    assert mime_type_for_filename("noext") is None
    assert mime_type_for_filename(None) is None


def test_mime_map_matches_storage_allowlist_inverse():
    """_MIME_FOR_EXT must stay the exact inverse of storage's allowlist."""
    assert _MIME_FOR_EXT == {v: k for k, v in ALLOWED_MIME_TYPES.items()}


# --- validate_application_form -----------------------------------


def test_form_all_valid():
    errors = validate_application_form(
        full_name="Casey Candidate",
        email="casey@example.com",
        resume_name="casey_resume.pdf",
        resume_bytes=_PDF,
    )
    assert errors.ok is True
    assert errors.messages() == []


def test_form_missing_name():
    errors = validate_application_form(
        full_name="   ", email="casey@example.com",
        resume_name="r.pdf", resume_bytes=_PDF,
    )
    assert errors.full_name and not errors.email and not errors.resume
    assert errors.ok is False


def test_form_bad_email():
    errors = validate_application_form(
        full_name="Casey", email="nope",
        resume_name="r.pdf", resume_bytes=_PDF,
    )
    assert errors.email and not errors.full_name


def test_form_missing_resume():
    errors = validate_application_form(
        full_name="Casey", email="casey@example.com",
        resume_name=None, resume_bytes=None,
    )
    assert errors.resume == "Please attach your résumé (PDF or DOCX)."


def test_form_bad_resume_type_uses_validation_message():
    errors = validate_application_form(
        full_name="Casey", email="casey@example.com",
        resume_name="resume.txt", resume_bytes=b"hello",
    )
    assert errors.resume and "Unsupported file type" in errors.resume


def test_form_reports_every_error_at_once():
    errors = validate_application_form(
        full_name="", email="bad", resume_name=None, resume_bytes=None,
    )
    assert len(errors.messages()) == 3


def test_form_errors_dataclass_is_frozen():
    errors = FormErrors()
    with pytest.raises(Exception):
        errors.full_name = "x"  # type: ignore[misc]


# --- session-state helpers (plain dict stands in for st.session_state) ---


def test_pending_application_roundtrip():
    state: dict = {}
    assert get_pending_application_id(state) is None

    remember_pending_application(state, "11111111-1111-1111-1111-111111111111")
    assert get_pending_application_id(state) == "11111111-1111-1111-1111-111111111111"

    clear_pending_application(state)
    assert get_pending_application_id(state) is None
    clear_pending_application(state)  # idempotent


def test_pending_application_ignores_non_string_or_empty():
    assert get_pending_application_id({"pending_application_id": ""}) is None
    assert get_pending_application_id({"pending_application_id": 123}) is None
    assert get_pending_application_id({}) is None


def test_mark_form_done_success_sets_job_and_clears_pending():
    state = {"pending_application_id": "abc"}
    mark_form_done(state, "job-uuid-1", "SUCCESS")
    assert form_done_kind(state, "job-uuid-1") == "SUCCESS"
    assert form_done_kind(state, "other-job") is None
    assert get_pending_application_id(state) is None  # cleared on completion


def test_mark_form_done_duplicate():
    state: dict = {}
    mark_form_done(state, "job-2", "DUPLICATE")
    assert form_done_kind(state, "job-2") == "DUPLICATE"


def test_form_done_kind_ignores_unknown_kind():
    assert form_done_kind({"form_done_for_job": "j", "form_done_kind": "WAT"}, "j") is None
    assert form_done_kind({}, "j") is None


# --- Phase 4: automatic-pipeline progress state (CLAUDE.md §2A item 6) ---


def test_processing_application_roundtrip():
    state: dict = {}
    assert get_processing_application_id(state) is None

    remember_processing_application(state, "abc-123")
    assert get_processing_application_id(state) == "abc-123"


def test_remember_processing_application_stringifies():
    import uuid

    state: dict = {}
    app_id = uuid.uuid4()
    remember_processing_application(state, app_id)
    assert get_processing_application_id(state) == str(app_id)


@pytest.mark.parametrize("bad", [None, "", 123, [], {}])
def test_get_processing_application_id_rejects_non_strings(bad):
    assert get_processing_application_id({"processing_application_id": bad}) is None


def test_pipeline_error_roundtrip():
    state: dict = {}
    assert get_pipeline_error(state) is None

    set_pipeline_error(state, "We couldn't finish that just now.")
    assert get_pipeline_error(state) == "We couldn't finish that just now."

    clear_pipeline_error(state)
    assert get_pipeline_error(state) is None


def test_mark_form_done_does_not_clobber_the_processing_application():
    """The SUCCESS path calls mark_form_done AND remembers the application to
    process — mark_form_done clears only the résumé-retry key."""
    state: dict = {}
    remember_processing_application(state, "app-1")

    mark_form_done(state, "job-1", "SUCCESS")

    assert form_done_kind(state, "job-1") == "SUCCESS"
    assert get_processing_application_id(state) == "app-1"


def test_resume_retry_and_processing_keys_are_independent():
    state: dict = {}
    remember_pending_application(state, "app-retry")
    remember_processing_application(state, "app-processing")

    clear_pending_application(state)

    assert get_pending_application_id(state) is None
    assert get_processing_application_id(state) == "app-processing"


# --- Phase 4 Step 2: screening resume-link helpers -----------------


def test_screening_token_session_roundtrip():
    state: dict = {}
    assert get_screening_token(state) is None

    remember_screening_token(state, "abc-tok-123")
    assert get_screening_token(state) == "abc-tok-123"

    clear_screening_token(state)
    assert get_screening_token(state) is None


@pytest.mark.parametrize("bad", [None, "", 123, [], {}])
def test_get_screening_token_rejects_non_strings(bad):
    assert get_screening_token({"screening_access_token": bad}) is None


def test_resolve_active_screening_token_url_wins_over_session():
    assert resolve_active_screening_token(
        url_token="from-url", session_token="from-session"
    ) == "from-url"


def test_resolve_active_screening_token_falls_back_to_session():
    assert resolve_active_screening_token(
        url_token="", session_token="from-session"
    ) == "from-session"
    assert resolve_active_screening_token(
        url_token=None, session_token="from-session"
    ) == "from-session"


def test_resolve_active_screening_token_trims_and_returns_none_when_empty():
    assert resolve_active_screening_token(
        url_token="  spaced  ", session_token=None
    ) == "spaced"
    assert resolve_active_screening_token(url_token=None, session_token=None) is None
    assert resolve_active_screening_token(url_token="   ", session_token="  ") is None


def test_screening_resume_url_shape():
    url = screening_resume_url("http://localhost:8502", "TOK")
    assert url == "http://localhost:8502/?screening=TOK"
    # tolerates a trailing slash on the base
    assert screening_resume_url("http://x/", "T") == "http://x/?screening=T"


def test_public_base_url_defaults_and_strips_trailing_slash(monkeypatch):
    monkeypatch.delenv("APP_PUBLIC_BASE_URL", raising=False)
    assert public_base_url() == "http://localhost:8502"
    monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://apply.example.com/")
    assert public_base_url() == "https://apply.example.com"
