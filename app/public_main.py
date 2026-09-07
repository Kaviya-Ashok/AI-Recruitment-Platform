"""Public candidate entrypoint — a SEPARATE, unauthenticated Streamlit app.

Run it alongside the HR app::

    streamlit run app/main.py          --server.port 8501   # HR app
    streamlit run app/public_main.py   --server.port 8502   # this (candidates)

Candidates reach it via the application links generated in the HR app
(Phase 1.4). It shares ``app/services`` / ``app/database`` with the HR app but:

* has NO login and NO access to any HR page, navigation, or mutation,
* can only ever see / touch data scoped to the single application-link token in
  its URL.

Tokens in the URL
-----------------
Streamlit 1.62 has no path parameters, so links are query strings and tokens are
read from ``st.query_params``:

* ``<APP_PUBLIC_BASE_URL>/?token=<job token>`` — the shared per-job application
  link (Phase 1.4).
* ``<APP_PUBLIC_BASE_URL>/?screening=<access token>`` — a candidate's private
  per-session link to resume a stalled automatic screening pipeline (Phase 4
  Step 2, CLAUDE.md §2A items 5, 6). Distinct param name; distinct entry point.
* ``<APP_PUBLIC_BASE_URL>/?retry=<resume retry token>`` — a candidate's private
  link back to their own application when the résumé upload failed (Phase D
  Fix 3). Minted lazily on failure only. Distinct param name; distinct entry
  point.

Why the single ``st.navigation`` call
-------------------------------------
``app/pages/jobs.py`` sits next to this entrypoint, so Streamlit's *automatic*
multipage feature would otherwise expose the HR "jobs" page in this public
app's sidebar. Calling ``st.navigation`` with one hidden page disables that
auto-scan entirely (the same mechanism ``app/main.py`` uses).

What this file contains
-----------------------
* pure, unit-tested helpers: ``is_valid_candidate_email``,
  ``validate_application_form``, and the ``st.session_state`` helpers
  (``remember_pending_application`` etc.) — all take a plain mapping / strings,
  never touch ``st.*``. Tested in ``tests/test_public_application_form_helpers.py``.
* ``_render_*`` functions — thin ``st.*`` glue, not unit-tested (matches this
  codebase's pattern: page rendering is verified manually).

The actual submission is orchestrated by
``candidate_portal_service.submit_application_via_token`` /
``retry_resume_upload`` (composition of the existing application / storage /
candidate services — no new business rules).
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``streamlit run app/public_main.py`` puts ``app/`` on sys.path, not the repo
# root, so ``import app.*`` would fail without this.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, MutableMapping

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.services.candidate_portal_service import (
    CandidateJobView,
    PortalOutcome,
    SubmissionOutcome,
    get_application_contact,
    get_resume_retry_token,
    resolve_resume_retry_token,
    retry_resume_upload,
    submission_failure_message,
    submit_application_via_token,
    view_job_via_token,
)
from app.database.models.screening_session import ScreeningSessionStatus
from app.services.screening_pipeline_service import (
    PipelineOutcome,
    PipelineStage,
    advance_screening_pipeline,
    get_candidate_screening_token,
    get_pipeline_state,
    resolve_screening_access_token,
)
from app.services.screening_evaluation_service import ensure_screening_evaluated
from app.services.screening_question_service import (
    ScreeningAnswerNotAllowedError,
    ScreeningQuestionError,
    ensure_round_2_generated,
    get_round_questions,
    submit_answer,
)
from app.utils.candidate_ui import follow_up_notice, resume_link_block
from app.utils.validation import FileValidationError, validate_uploaded_file

logger = logging.getLogger("app.public_main")

# --- candidate-facing copy -------------------------------------------

# One message for every invalid-link case (not-found / revoked / superseded /
# closed / malformed) — never distinguished to the candidate (anti-enumeration).
_INVALID_MSG = (
    "This application link is no longer active. Please contact the hiring team "
    "or check the job posting for an updated link."
)
_ERROR_MSG = "Something went wrong. Please try again in a few moments."
_DUPLICATE_MSG = "You have already applied for this job."
_RESUME_RETRY_MSG = (
    "Your application has been received, but we couldn't upload your résumé "
    "just now. Please attach it again below and we'll finish your submission."
)
_PROCESSING_INTRO = (
    "Thanks — your application is in. We're getting it ready now; this usually "
    "takes under a minute. Please keep this page open."
)
# NB: this copy must never promise an email. This MVP has no email-sending
# capability of any kind (no SMTP / SES / SendGrid client anywhere in the
# codebase or in requirements.txt), so "we'll email you a link" was a promise
# the system could not keep. The candidate's only durable way back is the
# ``?screening=`` link rendered above by ``_render_save_link``. Phrasing is
# aligned with ``_SCREENING_DONE_MSG``: a human follow-up, never a system one.
_PROCESSING_DONE = (
    "You're all set — nothing further is needed from you right now. The hiring "
    "team will review your application and be in touch."
)
# Landing-page framing shown above the application form. Every line must stay
# true of what the system ACTUALLY does: no timeline promise, and — per Fix 2 —
# no claim that anyone will email the candidate, since this MVP has no
# email-sending capability at all. The screening step really does begin
# immediately after submission (the pipeline is automatic), and the resume-later
# link really is offered, so both are safe to state.
_APPLY_INTRO_HEADING = "What to expect"
_APPLY_INTRO_POINTS = (
    "You'll need your résumé as a PDF or Word file, plus your name and email. "
    "A phone number is optional.",
    "Right after you submit, we'll process your résumé and take you straight "
    "to a short set of written questions about your experience.",
    "We'll give you a link to save. If you get interrupted, open it to pick up "
    "where you left off.",
    "Your résumé and answers stay private and are used only to consider you "
    "for this role.",
)
_ROUND_1_INTRO = (
    "A few questions about your background and this role. Answer in your own "
    "words — there are no trick questions. You can save and come back to this "
    "link while any question is still open."
)
_ROUND_2_INTRO = (
    "Thanks. Just a couple of quick follow-ups based on your answers."
)
_ANSWER_BLANK_MSG = (
    "Nothing to save for that question yet. You can leave it for now and "
    "come back to it using your saved link."
)
_ROUND_PREPARING_MSG = "Preparing your follow-up questions…"
_SCREENING_DONE_MSG = (
    "Your screening is complete. Thank you for your time — the hiring team "
    "will review your responses and be in touch."
)
_RETRY_SAVE_LINK_MSG = (
    "Keep this link. If this page closes before your résumé is attached, open "
    "it to come back and finish:"
)
# One message for every invalid résumé-retry case (missing / malformed /
# unknown / résumé already attached) — never distinguished (anti-enumeration),
# same discipline as ``_INVALID_MSG`` and ``_INVALID_SCREENING_MSG``.
_INVALID_RETRY_MSG = (
    "This link is no longer valid. Please contact the hiring team if you need "
    "a new one."
)
_SAVE_LINK_MSG = (
    "Keep this link. If you get interrupted, open it to come back and pick up "
    "your screening where you left off:"
)
# One message for every invalid screening-link case (missing / malformed /
# unknown / any future revoked state) — never distinguished (anti-enumeration),
# same discipline as the job-link ``_INVALID_MSG``.
_INVALID_SCREENING_MSG = (
    "This screening link is no longer valid. Please contact the hiring team if "
    "you need a new one."
)

# --- session-state keys (primitives only, per CLAUDE.md §16) --------

_SS_PENDING_APP = "pending_application_id"  # str UUID — résumé retry in progress
_SS_DONE_JOB = "form_done_for_job"          # str UUID — form already completed
_SS_DONE_KIND = "form_done_kind"            # "SUCCESS" | "DUPLICATE"
# Phase 4 (CLAUDE.md §2A items 1, 6): the submitted application whose automatic
# pipeline this browser session is watching, and the last stage failure (if any).
# Both are plain strings — the durable state lives in Postgres, and the pipeline
# re-derives its true position from there on every rerun. Losing these keys
# costs the candidate a progress view, never any data.
_SS_PROCESSING_APP = "processing_application_id"  # str UUID
_SS_PIPELINE_ERROR = "pipeline_error_message"     # candidate-safe str
# Phase 4 Step 2: the candidate's own screening access token, once resolved from
# ``?screening=`` — kept so subsequent reruns still resume even if the query
# param is dropped. Per-browser, server-side session only; it is the candidate's
# own credential for their own screening. Never logged.
_SS_SCREENING_TOKEN = "screening_access_token"
# Phase D Fix 3: the candidate's own résumé-retry credential, once resolved from
# ``?retry=``. Kept so subsequent reruns still resume even if the query param is
# dropped. Per-browser, server-side only; it is the candidate's own credential
# for their own application. Never logged.
_SS_RETRY_TOKEN = "resume_retry_token"
# C6: the candidate-safe failure message for the résumé upload that just failed.
# RESUME_UPLOAD_FAILED reruns before it can render anything, so the message has
# to survive one rerun to reach the retry screen. This stores the MAPPED,
# pre-written string — never ``internal_reason`` itself.
_SS_RETRY_FAILURE_MSG = "resume_retry_failure_message"


# =====================================================================
# Pure helpers (unit-tested — no st.*, no DB)
# =====================================================================

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_candidate_email(email: str | None) -> bool:
    """Simple MVP email check: one ``@``, a dot in the domain, no whitespace."""
    return bool(_EMAIL_RE.match((email or "").strip()))


@dataclass(frozen=True)
class FormErrors:
    full_name: str | None = None
    email: str | None = None
    resume: str | None = None

    @property
    def ok(self) -> bool:
        return not (self.full_name or self.email or self.resume)

    def messages(self) -> list[str]:
        return [m for m in (self.full_name, self.email, self.resume) if m]


def validate_application_form(
    *,
    full_name: str | None,
    email: str | None,
    resume_name: str | None,
    resume_bytes: bytes | None,
) -> FormErrors:
    """Client-side form validation — runs before any service call.

    Reuses ``app.utils.validation.validate_uploaded_file`` for the résumé (type
    / size / filename), so the rules stay in one place.
    """
    name_err = None if (full_name or "").strip() else "Please enter your full name."
    email_err = (
        None if is_valid_candidate_email(email)
        else "Please enter a valid email address."
    )

    resume_err: str | None = None
    if not resume_bytes:
        resume_err = "Please attach your résumé (PDF or DOCX)."
    else:
        try:
            validate_uploaded_file(resume_name or "", resume_bytes)
        except FileValidationError as exc:
            resume_err = str(exc)

    return FormErrors(name_err, email_err, resume_err)


def remember_pending_application(
    state: MutableMapping[str, Any], application_id: str
) -> None:
    state[_SS_PENDING_APP] = str(application_id)


def get_pending_application_id(
    state: MutableMapping[str, Any],
) -> str | None:
    value = state.get(_SS_PENDING_APP)
    return value if isinstance(value, str) and value else None


def clear_pending_application(state: MutableMapping[str, Any]) -> None:
    state.pop(_SS_PENDING_APP, None)


def mark_form_done(
    state: MutableMapping[str, Any], job_id: str, kind: str
) -> None:
    """Record that this browser session has finished with the form for a job
    (``kind`` is ``"SUCCESS"`` or ``"DUPLICATE"``) so a rerun shows the final
    message instead of the form again. Also clears any pending retry."""
    state[_SS_DONE_JOB] = str(job_id)
    state[_SS_DONE_KIND] = kind
    state.pop(_SS_PENDING_APP, None)


def form_done_kind(
    state: MutableMapping[str, Any], job_id: str
) -> str | None:
    """Return ``"SUCCESS"`` / ``"DUPLICATE"`` if this session already finished
    the form for ``job_id``, else ``None``."""
    if state.get(_SS_DONE_JOB) == str(job_id):
        kind = state.get(_SS_DONE_KIND)
        return kind if kind in ("SUCCESS", "DUPLICATE") else None
    return None


def remember_processing_application(
    state: MutableMapping[str, Any], application_id: str
) -> None:
    """Watch this application's automatic pipeline on subsequent reruns."""
    state[_SS_PROCESSING_APP] = str(application_id)


def get_processing_application_id(
    state: MutableMapping[str, Any],
) -> str | None:
    value = state.get(_SS_PROCESSING_APP)
    return value if isinstance(value, str) and value else None


def set_pipeline_error(
    state: MutableMapping[str, Any], message: str
) -> None:
    """Park a candidate-safe stage-failure message so the next rerun renders the
    error + Retry instead of immediately re-running the failed stage (which
    would spin)."""
    state[_SS_PIPELINE_ERROR] = message


def get_pipeline_error(state: MutableMapping[str, Any]) -> str | None:
    value = state.get(_SS_PIPELINE_ERROR)
    return value if isinstance(value, str) and value else None


def clear_pipeline_error(state: MutableMapping[str, Any]) -> None:
    state.pop(_SS_PIPELINE_ERROR, None)


def remember_screening_token(
    state: MutableMapping[str, Any], token: str
) -> None:
    state[_SS_SCREENING_TOKEN] = str(token)


def get_screening_token(state: MutableMapping[str, Any]) -> str | None:
    value = state.get(_SS_SCREENING_TOKEN)
    return value if isinstance(value, str) and value else None


def clear_screening_token(state: MutableMapping[str, Any]) -> None:
    state.pop(_SS_SCREENING_TOKEN, None)


def remember_retry_token(state: MutableMapping[str, Any], token: str) -> None:
    state[_SS_RETRY_TOKEN] = str(token)


def get_retry_token(state: MutableMapping[str, Any]) -> str | None:
    value = state.get(_SS_RETRY_TOKEN)
    return value if isinstance(value, str) and value else None


def clear_retry_token(state: MutableMapping[str, Any]) -> None:
    state.pop(_SS_RETRY_TOKEN, None)


def answer_widget_key(question_id: str, answer_text: str | None) -> str:
    """Widget key for one answer box, revisioned by the answer actually stored.

    WHY THE KEY CARRIES A REVISION
    ------------------------------
    Streamlit's ``key=`` silently wins over ``value=`` once that key exists in
    session state: after a box has been touched in this session, a *different*
    value arriving from the database is ignored, with no warning anywhere (no
    DOM alert, no console output, no exception — verified live). That produced a
    real inconsistency in the batched form: the progress caption could count an
    answer while its own box rendered empty.

    Folding a digest of the stored answer into the key removes the failure mode
    by construction rather than by remembering to patch session state. Same
    stored answer -> same key -> the widget (and any unsaved draft in it)
    survives reruns. Different stored answer, arriving by ANY path -> different
    key -> Streamlit builds a fresh widget and ``value=`` applies. There is no
    state in which a box can disagree with what was persisted for it.

    The digest is truncated only for readability in test failures; collisions
    would merely re-use a widget for identical text, which is a no-op.
    """
    digest = hashlib.sha256((answer_text or "").encode("utf-8")).hexdigest()[:12]
    return f"ans_{question_id}_{digest}"


def remember_retry_failure_message(
    state: MutableMapping[str, Any], message: str
) -> None:
    """Park the candidate-safe failure copy for the next rerun (C6)."""
    state[_SS_RETRY_FAILURE_MSG] = str(message)


def take_retry_failure_message(state: MutableMapping[str, Any]) -> str | None:
    """Read-and-clear: the message is shown once, on the screen it explains."""
    value = state.pop(_SS_RETRY_FAILURE_MSG, None)
    return value if isinstance(value, str) and value else None


def resolve_active_retry_token(
    *, url_token: str | None, session_token: str | None
) -> str | None:
    """Which résumé-retry token to act on this rerun. Same precedence rule as
    :func:`resolve_active_screening_token`: the URL param wins, session state is
    only a fallback for a rerun that dropped it."""
    for candidate in (url_token, session_token):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def resume_retry_url(base_url: str, token: str) -> str:
    """The ``?retry=<token>`` link a candidate saves to finish a failed
    résumé upload later."""
    return f"{base_url.rstrip('/')}/?retry={token}"


def resolve_active_screening_token(
    *, url_token: str | None, session_token: str | None
) -> str | None:
    """Which screening token to act on this rerun.

    The ``?screening=`` URL param wins; the session-state copy is only a
    fallback for a rerun where the param is absent. Returns a trimmed non-empty
    string, or ``None`` when neither source has one.
    """
    for candidate in (url_token, session_token):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def public_base_url() -> str:
    """Base URL of this public app — mirrors ``app/pages/jobs.py``."""
    return os.getenv("APP_PUBLIC_BASE_URL", "http://localhost:8502").rstrip("/")


def screening_resume_url(base_url: str, token: str) -> str:
    """The ``?screening=<token>`` link a candidate saves to resume later."""
    return f"{base_url.rstrip('/')}/?screening={token}"


# =====================================================================
# Rendering (st.* glue — not unit-tested)
# =====================================================================


def _token_from_url() -> str:
    try:
        raw = st.query_params.get("token", "")
    except Exception:  # noqa: BLE001 - never crash on a query-param quirk
        return ""
    return raw if isinstance(raw, str) else ""


def _screening_token_from_url() -> str:
    try:
        raw = st.query_params.get("screening", "")
    except Exception:  # noqa: BLE001 - never crash on a query-param quirk
        return ""
    return raw if isinstance(raw, str) else ""


def _retry_token_from_url() -> str:
    try:
        raw = st.query_params.get("retry", "")
    except Exception:  # noqa: BLE001 - never crash on a query-param quirk
        return ""
    return raw if isinstance(raw, str) else ""


def _render_invalid() -> None:
    st.title("Application link")
    st.warning(_INVALID_MSG)


def _render_error() -> None:
    st.title("Application link")
    st.error(_ERROR_MSG)


def _render_job_details(view: CandidateJobView) -> None:
    st.title(view.job_title)
    if view.department:
        st.caption(view.department)
    st.divider()

    if not view.details_available:
        st.info("This position's details aren't available right now.")
        return

    st.subheader("About this role")
    for section in view.sections:
        st.markdown(f"**{section.heading}**")
        for item in section.items:
            st.markdown(f"- {item}")
    if not view.sections:
        st.caption("No further details have been published for this role.")




def _handle_full_submit(
    *, token: str, job_id: str, full_name: str, email: str,
    phone: str, resume_name: str | None, resume_bytes: bytes | None,
) -> None:
    errors = validate_application_form(
        full_name=full_name, email=email,
        resume_name=resume_name, resume_bytes=resume_bytes,
    )
    if not errors.ok:
        for message in errors.messages():
            st.error(message)
        return

    try:
        with st.spinner("Submitting your application…"):
            with session_scope() as db:
                result = submit_application_via_token(
                    db,
                    token=token,
                    full_name=full_name.strip(),
                    email=email.strip(),
                    phone=(phone or "").strip() or None,
                    file_bytes=resume_bytes,
                    original_filename=resume_name or "",
                )
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error on submit")
        st.error(_ERROR_MSG)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback to a candidate
        logger.exception("candidate portal page: unexpected error on submit")
        st.error(_ERROR_MSG)
        return

    _apply_result(result, job_id)


def _handle_retry_submit(
    *, job_id: str, application_id: str,
    resume_name: str | None, resume_bytes: bytes | None,
) -> None:
    errors = validate_application_form(
        full_name="placeholder",  # name/email already captured on the application
        email="placeholder@x.co",
        resume_name=resume_name, resume_bytes=resume_bytes,
    )
    if errors.resume:
        st.error(errors.resume)
        return

    try:
        with st.spinner("Uploading your résumé…"):
            with session_scope() as db:
                result = retry_resume_upload(
                    db,
                    application_id=application_id,
                    file_bytes=resume_bytes,
                    original_filename=resume_name or "",
                )
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error on résumé retry")
        st.error(_ERROR_MSG)
        return
    except Exception:  # noqa: BLE001
        logger.exception("candidate portal page: unexpected error on résumé retry")
        st.error(_ERROR_MSG)
        return

    _apply_result(result, job_id)


def _apply_result(result, job_id: str) -> None:
    """Turn a SubmissionResult into the next screen. Terminal outcomes stash a
    primitive in session_state and rerun so the form disappears; transient
    outcomes (ERROR) show an inline message and leave the form for a retry."""
    if result.outcome == SubmissionOutcome.SUCCESS:
        logger.info("candidate portal page: submission complete job=%s", job_id)
        mark_form_done(st.session_state, job_id, "SUCCESS")
        # Phase 4: hand off to the automatic pipeline's progress view instead of
        # the old static confirmation (CLAUDE.md §2A items 1, 6).
        if result.application_id:
            remember_processing_application(
                st.session_state, result.application_id
            )
        st.rerun()
    elif result.outcome == SubmissionOutcome.DUPLICATE:
        mark_form_done(st.session_state, job_id, "DUPLICATE")
        st.rerun()
    elif result.outcome == SubmissionOutcome.LINK_INVALID:
        # The link went bad between page load and submit — let _page() re-render
        # the clean, uniform invalid-link screen.
        st.rerun()
    elif result.outcome == SubmissionOutcome.RESUME_UPLOAD_FAILED:
        if result.application_id:
            remember_pending_application(st.session_state, result.application_id)
        # C6: this branch reruns before it can render anything, so the mapped,
        # candidate-safe explanation is parked for the retry screen to show.
        # ``internal_reason`` is the KEY only — it never leaves this call.
        remember_retry_failure_message(
            st.session_state, submission_failure_message(result.internal_reason)
        )
        st.rerun()  # re-render as the résumé-only retry form
    else:  # ERROR — transient; keep the form so they can resubmit
        # C6: differentiated where we can be, generic fallback where we can't.
        st.error(submission_failure_message(result.internal_reason))


def _render_apply_intro() -> None:
    """Brief, honest framing of the process, shown above the form only."""
    st.markdown(f"**{_APPLY_INTRO_HEADING}**")
    st.markdown("\n".join(f"- {point}" for point in _APPLY_INTRO_POINTS))


def _render_application_form(job_id: str, token: str) -> None:
    st.subheader("Apply for this role")
    _render_apply_intro()
    with st.form("candidate_application_form"):
        # Explicit keys so typed values survive the rerun on a validation error.
        full_name = st.text_input("Full name", key="apply_full_name")
        email = st.text_input("Email", key="apply_email")
        phone = st.text_input("Phone (optional)", key="apply_phone")
        resume = st.file_uploader(
            "Résumé (PDF or DOCX, up to 10 MB)",
            type=["pdf", "docx"],
            accept_multiple_files=False,
            key="apply_resume",
        )
        submitted = st.form_submit_button("Submit application")

    if submitted:
        _handle_full_submit(
            token=token,
            job_id=job_id,
            full_name=full_name,
            email=email,
            phone=phone,
            resume_name=resume.name if resume is not None else None,
            resume_bytes=resume.getvalue() if resume is not None else None,
        )


def _render_retry_save_link(token: str) -> None:
    """Show the candidate their private finish-later link. The token is theirs;
    it is rendered here and nowhere else, and never logged.

    Uses the same ``candidate_ui.resume_link_block`` as the screening save-link,
    so this link wraps on a narrow screen instead of scrolling sideways while
    keeping the native copy button. Placement and callers are unchanged.
    """
    st.info(_RETRY_SAVE_LINK_MSG)
    resume_link_block(resume_retry_url(public_base_url(), token))


def _render_retry_contact(application_id: str) -> None:
    """C4: read back the details the candidate already gave, so the retry screen
    confirms they were kept rather than leaving the candidate guessing.

    Read-only — never an editable field, never re-typed. Scoped to the one
    application this flow already holds; the accessor cannot search by name or
    email.
    """
    try:
        with session_scope() as db:
            contact = get_application_contact(db, application_id=application_id)
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error reading contact")
        return
    except Exception:  # noqa: BLE001 - never surface a traceback to a candidate
        logger.exception("candidate portal page: unexpected error reading contact")
        return

    if contact is None:
        return
    st.markdown(
        f"- *Name:* {contact.full_name}" + chr(10) + f"- *Email:* {contact.email}"
    )


def _render_resume_retry_form(job_id: str, application_id: str) -> None:
    st.subheader("Finish your application")
    # C6: the specific reason this upload failed, if one was parked for us;
    # otherwise the original generic explanation.
    st.warning(
        take_retry_failure_message(st.session_state) or _RESUME_RETRY_MSG
    )
    _render_retry_contact(application_id)

    # The durable way back. Without this the only pointer to this application is
    # ``st.session_state``, which a refresh clears — leaving an application row
    # with no résumé and no route to attach one (the dead end this fixes).
    try:
        with session_scope() as db:
            retry_token = get_resume_retry_token(db, application_id=application_id)
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error reading retry token")
        retry_token = None
    except Exception:  # noqa: BLE001 - never surface a traceback to a candidate
        logger.exception("candidate portal page: unexpected error reading retry token")
        retry_token = None
    if retry_token:
        _render_retry_save_link(retry_token)

    with st.form("candidate_resume_retry_form"):
        resume = st.file_uploader(
            "Résumé (PDF or DOCX, up to 10 MB)",
            type=["pdf", "docx"],
            accept_multiple_files=False,
        )
        submitted = st.form_submit_button("Upload résumé")

    if submitted:
        _handle_retry_submit(
            job_id=job_id,
            application_id=application_id,
            resume_name=resume.name if resume is not None else None,
            resume_bytes=resume.getvalue() if resume is not None else None,
        )


# --- Phase 4: automatic screening pipeline progress view --------------

# Candidate-facing stage labels. Internal stage names are never shown. Only the
# two stages that actually take time (AI work) are surfaced; SCREENING_SESSION
# (create the row) and FINALIZE (flip a status) are instant plumbing and the
# candidate is not shown a line for them — the final "Preparing your screening"
# line stands in for FINALIZE.
_VISIBLE_STAGES: tuple[str, ...] = (
    PipelineStage.RESUME_PARSING,
    PipelineStage.PREQUALIFICATION,
)
_STAGE_LABELS: dict[str, str] = {
    PipelineStage.RESUME_PARSING: "Reviewing your résumé",
    PipelineStage.PREQUALIFICATION: "Checking your application against the role",
    PipelineStage.SCREENING_SESSION: "Setting up your screening",
    PipelineStage.FINALIZE: "Preparing your screening",
}


def _render_stage_checklist(state, running_stage: str | None) -> None:
    """Per-stage progress. Marker text carries the meaning, never colour alone
    (CLAUDE.md §26)."""
    for stage in _VISIBLE_STAGES:
        label = _STAGE_LABELS[stage]
        if state.is_done(stage):
            st.markdown(f"✓ &nbsp;{label} — done")
        elif stage == running_stage:
            st.markdown(f"⏳ &nbsp;{label} — in progress…")
        else:
            st.markdown(f"· &nbsp;:gray[{label} — waiting]")

    final_label = _STAGE_LABELS[PipelineStage.FINALIZE]
    if state.is_done(PipelineStage.FINALIZE):
        st.markdown(f"✓ &nbsp;{final_label} — done")
    elif running_stage in (PipelineStage.SCREENING_SESSION, PipelineStage.FINALIZE):
        st.markdown(f"⏳ &nbsp;{final_label} — in progress…")
    else:
        st.markdown(f"· &nbsp;:gray[{final_label} — waiting]")


def _render_save_link(token: str) -> None:
    """Show the candidate their private resume-later link. The token is theirs;
    it is rendered here and nowhere else, and never logged.

    Rendering moved to ``candidate_ui.resume_link_block`` so the link wraps on a
    narrow screen instead of scrolling sideways, while keeping the native copy
    button. Placement and callers are unchanged.
    """
    st.info(_SAVE_LINK_MSG)
    resume_link_block(screening_resume_url(public_base_url(), token))


def _render_processing(application_id: str) -> None:
    """The candidate's screening view: the automatic-pipeline progress stepper
    while the pre-screening pipeline runs, then the round-1 / round-2 question
    forms, then a completion message.

    Every stage and every write is idempotent server-side, so a refresh, a
    rerun, or a resumed ``?screening=`` link can never duplicate an AI call, a
    row, or an audit event.
    """
    st.subheader("Your screening")

    try:
        with session_scope() as db:
            state = get_pipeline_state(db, application_id=application_id)
            resume_token = get_candidate_screening_token(
                db, application_id=application_id
            )
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error reading screening state")
        st.error(_ERROR_MSG)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback to a candidate
        logger.exception("candidate portal page: unexpected error reading screening state")
        st.error(_ERROR_MSG)
        return

    # As soon as the session row exists, give the candidate their resume-later
    # link and remember it for subsequent reruns.
    if resume_token:
        remember_screening_token(st.session_state, resume_token)
        _render_save_link(resume_token)

    # --- still running the automatic pre-screening pipeline ---------------
    if not state.is_complete:
        _render_pipeline_stepper(application_id, state)
        return

    # --- pipeline done: dispatch on the round lifecycle ------------------
    status = state.screening_session_status
    session_id = state.screening_session_id

    if status == ScreeningSessionStatus.SCREENING_COMPLETE:
        # Best-effort: retry the HR-facing evaluation if its auto-trigger on the
        # completion path failed. Idempotent, SYSTEM-attributed; the candidate
        # is shown nothing about it (the scorecard is HR-only).
        try:
            with session_scope() as db:
                ensure_screening_evaluated(db, application_id=application_id)
        except Exception:  # noqa: BLE001 - never surface to a candidate
            logger.warning("candidate portal page: evaluation retry failed")
        st.success(_SCREENING_DONE_MSG)
        return

    if status == ScreeningSessionStatus.ROUND_1_COMPLETE:
        # Round 1 answered; round-2 generation failed on the submit path and
        # needs a retry. Idempotent. Show an explicit retry rather than looping
        # on a persistent failure.
        parked = get_pipeline_error(st.session_state)
        if parked is not None:
            st.error(parked)
            if st.button("Try again", key="round2_retry"):
                clear_pipeline_error(st.session_state)
                st.rerun()
            return
        st.info(_ROUND_PREPARING_MSG)
        try:
            with session_scope() as db:
                ensure_round_2_generated(db, screening_session_id=session_id)
        except (SQLAlchemyError, ScreeningQuestionError):
            logger.warning("candidate portal page: round-2 retry failed")
            set_pipeline_error(st.session_state, _ERROR_MSG)
        except Exception:  # noqa: BLE001
            logger.exception("candidate portal page: round-2 retry unexpected error")
            set_pipeline_error(st.session_state, _ERROR_MSG)
        st.rerun()
        return

    if status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS:
        _render_round_form(session_id, round_=1, intro=_ROUND_1_INTRO)
        return
    if status == ScreeningSessionStatus.ROUND_2_IN_PROGRESS:
        _render_round_form(session_id, round_=2, intro=_ROUND_2_INTRO)
        return

    # Any unexpected state — never crash the candidate's page.
    logger.warning("candidate portal page: unexpected screening status %r", status)
    st.info(_PROCESSING_DONE)


def _render_pipeline_stepper(application_id: str, state) -> None:
    """One automatic-pipeline stage per rerun, with per-stage progress."""
    parked_error = get_pipeline_error(st.session_state)
    if parked_error is not None:
        _render_stage_checklist(state, running_stage=None)
        st.error(parked_error)
        if st.button("Try again", key="pipeline_retry"):
            clear_pipeline_error(st.session_state)
            st.rerun()
        return

    st.caption(_PROCESSING_INTRO)
    _render_stage_checklist(state, running_stage=state.next_stage)

    label = _STAGE_LABELS.get(state.next_stage, "Working")
    try:
        with st.spinner(f"{label}…"):
            with session_scope() as db:
                result = advance_screening_pipeline(
                    db, application_id=application_id
                )
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error advancing pipeline")
        set_pipeline_error(st.session_state, _ERROR_MSG)
        st.rerun()
        return
    except Exception:  # noqa: BLE001
        logger.exception("candidate portal page: unexpected error advancing pipeline")
        set_pipeline_error(st.session_state, _ERROR_MSG)
        st.rerun()
        return

    if result.outcome == PipelineOutcome.FAILED:
        set_pipeline_error(st.session_state, result.message or _ERROR_MSG)
    st.rerun()


def _render_round_form(session_id: str, *, round_: int, intro: str) -> None:
    """Render one screening round's questions, each saved on its own.

    Every question owns its input and its own save control (see
    ``_render_question``), so an answer is durable the moment it is given rather
    than waiting behind every other question in the round. Nothing here requires
    the round to be complete: a candidate can answer what they can now and
    return through their saved link for the rest.

    The round still finishes automatically, server-side, once every question has
    an answer — that check lives in ``submit_answer`` and is unchanged; the next
    rerun then shows the follow-up round or the completion message.
    """
    try:
        with session_scope() as db:
            questions = get_round_questions(
                db, screening_session_id=session_id, round=round_
            )
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error loading round %s", round_)
        st.error(_ERROR_MSG)
        return

    if not questions:  # defensive — status said in-progress
        st.info(_ROUND_PREPARING_MSG)
        return

    answered = sum(1 for q in questions if q.answered)
    st.caption(intro)
    # Per-round and truthful: round 1's count is fixed once generated, and
    # round 2's is known by the time its form renders. Deliberately NOT a
    # cross-round total — round 2 may not exist at all, so "question 3 of 9"
    # would be a number the system cannot stand behind.
    st.caption(f"{answered} of {len(questions)} answered")
    # ...and, during round 1 only, say that a follow-up round may follow, so
    # the count above does not read as the whole screening.
    notice = follow_up_notice(round_)
    if notice is not None:
        st.caption(notice)

    for q in questions:
        _render_question(q)


def _render_question(q) -> None:
    """One question, saved on its own.

    Each question owns a single-question ``st.form``. That keeps typing free of
    reruns (a form does not rerun on change) while putting the save control
    immediately under the box it belongs to, instead of behind every other
    question in the round.
    """
    # The question is the most important thing on the screen, so it is rendered
    # as its own heading-weight line — the same ``**bold**`` idiom this file
    # already uses for section headings and that the HR-side guide view uses for
    # its questions. Passing it as the text_area's ``label`` styled the question
    # like a form-field caption instead. (C1 — unchanged by per-question save.)
    st.markdown(f"**{q.question_text}**")
    with st.form(f"screening_answer_form_{q.question_id}", clear_on_submit=False):
        box = st.text_area(
            # Generic label, visually collapsed: the heading above already
            # states the question, so a screen reader gets the question and
            # then an unambiguous "Your answer" rather than hearing the
            # question twice.
            "Your answer",
            value=q.answer_text or "",
            # Revisioned by the stored answer — see ``answer_widget_key``. This
            # is what makes the box always agree with what was persisted.
            key=answer_widget_key(q.question_id, q.answer_text),
            label_visibility="collapsed",
        )
        saved = st.form_submit_button("Save answer")

    if saved:
        _handle_single_answer(q.question_id, box)


def _handle_single_answer(question_id: str, text: str) -> None:
    """Persist ONE answer. Every other question in the round is untouched.

    A blank box is not an error: a candidate may answer what they can now and
    come back to the rest through their saved link, so a blank save is a no-op
    with a nudge rather than a failure.

    Round completion is unchanged and still lives in ``submit_answer`` — it is
    the check-first, idempotent block that runs once the last question in the
    round has an answer. Saving the final answer individually reaches it by
    exactly the same call the batched form used to make.

    No success banner: the rerun below would wipe it, and the saved text
    reappearing in its own box (plus the "X of Y answered" caption ticking up)
    is the confirmation.
    """
    if not (text or "").strip():
        st.info(_ANSWER_BLANK_MSG)
        return

    try:
        with session_scope() as db:
            submit_answer(
                db,
                screening_question_id=question_id,
                answer_text=text,
            )
    except ScreeningAnswerNotAllowedError:
        # The round closed between load and save (e.g. another tab finished it).
        st.rerun()
        return
    except (SQLAlchemyError, ScreeningQuestionError):
        logger.warning("candidate portal page: error saving a screening answer")
        st.error(_ERROR_MSG)
        return
    except Exception:  # noqa: BLE001
        logger.exception("candidate portal page: unexpected error saving an answer")
        st.error(_ERROR_MSG)
        return

    st.rerun()


def _render_invalid_screening() -> None:
    st.title("Screening link")
    st.warning(_INVALID_SCREENING_MSG)


def _render_invalid_retry() -> None:
    st.title("Finish your application")
    st.warning(_INVALID_RETRY_MSG)


def _render_retry_resume(token: str) -> None:
    """Entry point for ``?retry=<token>`` — resolve the token to the ONE
    application it belongs to and re-enter the résumé-retry form for it.

    The resolver does a single equality match on a UNIQUE column: no job-wide
    search, no email/name fallback, no application-id fallback. Any resolution
    failure — unknown token, wrong shape, or a résumé that has since been
    attached — shows one generic message, revealing nothing about whether any
    other application exists.
    """
    try:
        with session_scope() as db:
            resolution = resolve_resume_retry_token(db, token)
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error resolving retry token")
        _render_error()
        return
    except Exception:  # noqa: BLE001 - never surface a traceback publicly
        logger.exception("candidate portal page: unexpected error resolving retry token")
        _render_error()
        return

    if not resolution.is_valid:
        clear_retry_token(st.session_state)
        _render_invalid_retry()
        return

    application_id = str(resolution.application_id)
    remember_retry_token(st.session_state, token)
    remember_pending_application(st.session_state, application_id)
    st.title("Finish your application")
    _render_resume_retry_form("", application_id)


def _render_screening_resume(token: str) -> None:
    """Entry point for ``?screening=<token>`` — resolve the token to an
    application and re-enter the interstitial pipeline view for it.

    Resume logic itself is unchanged: this only feeds ``_render_processing``,
    which calls ``advance_screening_pipeline`` exactly as the post-submission
    path does. Any resolution failure shows one generic message.
    """
    try:
        with session_scope() as db:
            resolution = resolve_screening_access_token(db, token)
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error resolving screening token")
        _render_error()
        return
    except Exception:  # noqa: BLE001 - never surface a traceback publicly
        logger.exception("candidate portal page: unexpected error resolving screening token")
        _render_error()
        return

    if not resolution.is_valid:
        clear_screening_token(st.session_state)
        _render_invalid_screening()
        return

    application_id = str(resolution.application_id)
    remember_screening_token(st.session_state, token)
    remember_processing_application(st.session_state, application_id)

    _render_processing(application_id)


def _same_session_screening_is_complete(application_id: str) -> bool:
    """True when THIS browser session's own screening has just finished.

    Why this exists
    ---------------
    ``_page`` resolves the ``?screening=`` token before anything else, and
    ``resolve_screening_access_token`` deliberately reports a completed
    screening as the same generic ``INVALID`` as an unknown or malformed one
    (anti-enumeration — ``ScreeningAccessOutcome``'s docstring). The token is
    also cached in session state by ``_render_processing``. Together those made
    the *last* rerun of a successful screening route to the "this screening link
    is no longer valid" warning, so ``_SCREENING_DONE_MSG`` was never reached.

    This restores the completion screen for the **same-session** case only, and
    does so without touching the resolver: the candidate is identified by
    ``_SS_PROCESSING_APP``, which only ever gets set by this app's own
    submission / resume flow for the application it is already mid-flow with.
    It is server-side session state — not a URL value, not attacker-supplied —
    so this adds no lookup or enumeration surface. A returning visitor opening a
    saved link in a fresh session has no such state and is unaffected: they
    still get the generic invalid-link message, by design.

    Any read failure returns ``False``, so an error can only fall back to the
    existing behaviour, never invent a completion screen.
    """
    try:
        with session_scope() as db:
            state = get_pipeline_state(db, application_id=application_id)
    except SQLAlchemyError:
        logger.warning(
            "candidate portal page: database error checking same-session completion"
        )
        return False
    except Exception:  # noqa: BLE001 - never crash the candidate's page
        logger.exception(
            "candidate portal page: unexpected error checking same-session completion"
        )
        return False
    return (
        state.screening_session_status == ScreeningSessionStatus.SCREENING_COMPLETE
    )


def _render_apply_section(view: CandidateJobView, token: str) -> None:
    st.divider()
    job_id = view.job_id

    done = form_done_kind(st.session_state, job_id)
    if done == "SUCCESS":
        # ``_apply_result`` only ever records "SUCCESS" together with the
        # processing application id (both producers of a SUCCESS outcome always
        # populate ``application_id``), so ``processing_id`` is never None here.
        # This used to carry a static confirmation message for the impossible
        # case; it was dead from the day the automatic pipeline landed.
        processing_id = get_processing_application_id(st.session_state)
        if processing_id is not None:
            _render_processing(processing_id)
        return
    if done == "DUPLICATE":
        st.info(_DUPLICATE_MSG)
        return

    pending = get_pending_application_id(st.session_state)
    if pending is not None:
        _render_resume_retry_form(job_id, pending)
        return

    _render_application_form(job_id, token)


def _page() -> None:
    # This session's own screening finishing is checked FIRST. Once a screening
    # reaches SCREENING_COMPLETE its access token stops resolving (by design —
    # see ``_same_session_screening_is_complete``), so without this the final
    # rerun of a successful screening fell through to the invalid-link warning
    # instead of the completion message. Same-session only: it requires
    # ``_SS_PROCESSING_APP``, which a returning visitor never has.
    processing_id = get_processing_application_id(st.session_state)
    if processing_id is not None and _same_session_screening_is_complete(
        processing_id
    ):
        _render_processing(processing_id)
        return

    # A ``?retry=`` link (candidate finishing a failed résumé upload) is a
    # separate entry point, checked before the job-link flow. It can only be
    # reached by a candidate holding their own application's retry credential.
    retry_token = resolve_active_retry_token(
        url_token=_retry_token_from_url(),
        session_token=get_retry_token(st.session_state),
    )
    if retry_token:
        # Once the upload succeeds, ``_apply_result`` sets _SS_PROCESSING_APP
        # and the retry credential has done its job (it stops resolving as soon
        # as the document exists). Hand straight over to the pipeline view —
        # the same place the normal submission flow lands. Without this the
        # candidate would fall through to the job-link flow, which has no
        # ``?token=`` to work with on a ``?retry=`` URL, and be shown the
        # generic invalid-link message right after succeeding.
        if processing_id is not None:
            clear_retry_token(st.session_state)
            _render_processing(processing_id)
            return
        _render_retry_resume(retry_token)
        return

    # A ``?screening=`` link (candidate resuming a stalled pipeline) is a
    # separate entry point — check it before the job-link flow. URL param wins;
    # session-state is only a fallback for a rerun that dropped the param.
    screening_token = resolve_active_screening_token(
        url_token=_screening_token_from_url(),
        session_token=get_screening_token(st.session_state),
    )
    if screening_token:
        _render_screening_resume(screening_token)
        return

    token = _token_from_url()
    try:
        with session_scope() as db:
            result = view_job_via_token(db, token)
    except SQLAlchemyError:
        logger.warning("candidate portal page: database error")
        _render_error()
        return
    except Exception:  # noqa: BLE001 - never surface a traceback publicly
        logger.exception("candidate portal page: unexpected error")
        _render_error()
        return

    if result.outcome == PortalOutcome.OK and result.view is not None:
        _render_job_details(result.view)
        _render_apply_section(result.view, token)
    else:
        _render_invalid()


def main() -> None:
    st.set_page_config(page_title="Apply", page_icon="📄", layout="centered")
    # One hidden page => Streamlit ignores the app/pages/ directory, so the HR
    # "jobs" page can never appear in this public app.
    st.navigation([st.Page(_page, title="Apply")], position="hidden").run()


if __name__ == "__main__":
    main()
