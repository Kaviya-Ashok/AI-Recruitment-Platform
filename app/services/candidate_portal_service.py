"""Candidate portal service — the token-scoped composition layer behind
``app/public_main.py`` (the public, unauthenticated candidate app).

It:

* validates the token format defensively before any DB access,
* delegates ALL access decisions to :func:`application_link_service.resolve_link`
  (never reimplements them),
* builds a **candidate-appropriate** rendering of the job from the *approved*
  rubric (not the raw ``job_requirements`` AI extraction) — Phase 1.5,
* orchestrates a candidate's application submission — Phase 2: re-resolve link ->
  :func:`application_service.create_application` -> :func:`storage_service.upload_document`.

It owns **no business rules** of its own — dedup, field validation, the
"validate -> Drive -> only then commit" ordering, and every audit event all live
in the downstream services. This module only sequences those calls and returns
primitives (dataclasses of strings) for the Streamlit page to render. It exposes
NO mutation of jobs / rubrics / links, and it never reads ``st.session_state``.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.candidate import Candidate
from app.database.models.job import Job
from app.services.application_link_service import (
    LinkResolutionOutcome,
    resolve_link,
)
from app.services.application_service import (
    ApplicationTargetNotFoundError,
    DuplicateApplicationError,
    create_application,
    get_application,
)
from app.services.audit_service import record_event
from app.services.candidate_service import CandidateValidationError
from app.services.rubric_service import get_approved_rubric, list_criteria
from app.services.storage_service import (
    DriveAuthError,
    DriveUploadError,
    StorageConfigError,
    list_documents_for_application,
    upload_document,
)
from app.utils.validation import FileValidationError

logger = logging.getLogger(__name__)

# secrets.token_urlsafe(32) -> 43 chars of [A-Za-z0-9_-]; allow a generous
# range so a future entropy change doesn't break this, but bound it hard.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

#: Entropy for the résumé-retry credential. Deliberately the SAME constant used
#: by ``application_link_service`` and ``screening_pipeline_service``:
#: ``secrets.token_urlsafe(32)`` -> 43 URL-safe chars, 256 bits.
_TOKEN_BYTES = 32

# Internal requirement type -> candidate-facing section heading. The candidate
# never sees "MANDATORY" / "Technical Skill" style internal labels.
_SECTION_HEADINGS: dict[str, str] = {
    "MANDATORY": "What you'll need",
    "PREFERRED": "Nice to have",
    "EXPERIENCE": "Experience we're looking for",
    "BEHAVIORAL": "How you work",
    "OTHER": "Also good to know",
}
_SECTION_ORDER = ["MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"]


class PortalOutcome:
    """Result of :func:`view_job_via_token` — what the public page should show."""

    OK = "OK"                       # render the job view
    INVALID_LINK = "INVALID_LINK"   # render the uniform "not active" message
    MALFORMED_TOKEN = "MALFORMED_TOKEN"  # same message; never hit the DB

    ALL: frozenset[str] = frozenset({OK, INVALID_LINK, MALFORMED_TOKEN})


@dataclass(frozen=True)
class JobSection:
    heading: str
    items: list[str]


@dataclass(frozen=True)
class CandidateJobView:
    """Everything the public page needs to render a job — primitives only."""

    job_title: str
    department: str | None
    # The job's UUID as a string. Not sensitive (it is in HR-side URLs); the
    # page needs it to key per-job submission state in st.session_state.
    job_id: str = ""
    sections: list[JobSection] = field(default_factory=list)
    # False when the job somehow has no approved rubric (defensive; not
    # reachable via a real link) -> show a generic "details unavailable" state.
    details_available: bool = True


@dataclass(frozen=True)
class PortalResult:
    outcome: str
    view: CandidateJobView | None = None
    # For logs / tests ONLY — never shown to the candidate. Distinguishes
    # resolve_link's NOT_FOUND / INACTIVE / JOB_CLOSED, or MALFORMED_TOKEN.
    internal_reason: str = ""


def looks_like_token(raw: str | None) -> bool:
    """Cheap defensive format check before the token touches a DB query."""
    return isinstance(raw, str) and _TOKEN_RE.match(raw) is not None


def _build_sections(criteria) -> list[JobSection]:
    grouped: dict[str, list[str]] = {}
    for c in criteria:
        grouped.setdefault(c.requirement_type, []).append(c.criterion_text)
    sections: list[JobSection] = []
    for rtype in _SECTION_ORDER:
        items = grouped.get(rtype)
        if items:
            sections.append(
                JobSection(heading=_SECTION_HEADINGS[rtype], items=items)
            )
    # Any unknown/future type falls under "Also good to know".
    leftovers: list[str] = []
    for rtype, items in grouped.items():
        if rtype not in _SECTION_HEADINGS:
            leftovers.extend(items)
    if leftovers:
        sections.append(
            JobSection(heading=_SECTION_HEADINGS["OTHER"], items=leftovers)
        )
    return sections


def build_candidate_job_view(db: Session, job: Job) -> CandidateJobView:
    """Build the candidate-facing view from the job's APPROVED rubric.

    Uses the HR-reviewed approved rubric as the source, never the raw
    ``job_requirements``. If there is no approved rubric (shouldn't happen —
    link generation requires an approved rubric — but defend anyway) the view
    has ``details_available=False``.
    """
    approved = get_approved_rubric(db, job.id)
    if approved is None:
        logger.warning(
            "candidate portal: job=%s is viewable but has no approved rubric",
            job.id,
        )
        return CandidateJobView(
            job_title=job.title,
            department=job.department,
            job_id=str(job.id),
            sections=[],
            details_available=False,
        )

    criteria = list_criteria(db, approved.id)
    return CandidateJobView(
        job_title=job.title,
        department=job.department,
        job_id=str(job.id),
        sections=_build_sections(criteria),
        details_available=True,
    )


def _record_link_view(
    db: Session, *, link_id: uuid.UUID, job_id: uuid.UUID
) -> None:
    """Audit a successful candidate view. No token value in metadata; no
    authenticated user in this context."""
    record_event(
        db,
        event_type=AuditEventType.APPLICATION_LINK_VIEWED,
        action="Application link viewed by a candidate.",
        entity_type="application_link",
        entity_id=link_id,
        user_id=None,
        metadata={"job_id": str(job_id)},
    )


def view_job_via_token(db: Session, token: str) -> PortalResult:
    """Resolve a token and, if valid, build the candidate view + audit it.

    Commits on the successful path (the audit write). Invalid/malformed tokens
    make no DB writes and emit no audit event (abuse-monitoring on invalid
    attempts is a later concern — see the Phase 1.5 TODOs).
    """
    if not looks_like_token(token):
        logger.info("candidate portal: malformed token rejected pre-DB")
        return PortalResult(
            PortalOutcome.MALFORMED_TOKEN, internal_reason="MALFORMED_TOKEN"
        )

    resolution = resolve_link(db, token)

    if not resolution.is_valid:
        logger.info(
            "candidate portal: token rejected outcome=%s", resolution.outcome
        )
        return PortalResult(
            PortalOutcome.INVALID_LINK, internal_reason=resolution.outcome
        )

    view = build_candidate_job_view(db, resolution.job)
    _record_link_view(
        db, link_id=resolution.link.id, job_id=resolution.job.id
    )
    db.commit()
    logger.info(
        "candidate portal: served job=%s link_seq=%s details=%s",
        resolution.job.id,
        resolution.link.sequence_number,
        view.details_available,
    )
    return PortalResult(
        PortalOutcome.OK, view=view, internal_reason=LinkResolutionOutcome.VALID
    )


# =====================================================================
# Phase 2 — candidate application submission (still pure composition)
# =====================================================================

# Resume extension -> MIME type. Kept as the inverse of
# storage_service.ALLOWED_MIME_TYPES; a drift-guard test asserts they agree.
_MIME_FOR_EXT: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
}


def mime_type_for_filename(filename: str | None) -> str | None:
    """Map a résumé filename's extension to its MIME type, or ``None``."""
    ext = os.path.splitext((filename or "").lower())[1]
    return _MIME_FOR_EXT.get(ext)


class SubmissionOutcome:
    """Result of :func:`submit_application_via_token` / :func:`retry_resume_upload`
    — what the public page should show. Never distinguishes *why* a link is
    invalid (anti-enumeration)."""

    SUCCESS = "SUCCESS"
    DUPLICATE = "DUPLICATE"                    # already applied to this job
    LINK_INVALID = "LINK_INVALID"              # link revoked / job closed / gone
    RESUME_UPLOAD_FAILED = "RESUME_UPLOAD_FAILED"  # application kept; retry the file
    ERROR = "ERROR"                            # unexpected; generic message + log

    ALL: frozenset[str] = frozenset(
        {SUCCESS, DUPLICATE, LINK_INVALID, RESUME_UPLOAD_FAILED, ERROR}
    )


@dataclass(frozen=True)
class SubmissionResult:
    outcome: str
    # Set on SUCCESS and RESUME_UPLOAD_FAILED (the application row exists).
    # A UUID string — the page stashes it in st.session_state for the résumé
    # retry path. Not sensitive.
    application_id: str | None = None
    # Logs / tests only — never shown to the candidate.
    internal_reason: str = ""


class ResumeRetryOutcome:
    """The two distinguishable results of :func:`resolve_resume_retry_token`.

    ``INVALID`` covers *every* non-resumable case — missing token, malformed
    token, unknown token, and a token whose application already has its résumé
    — with an identical response, so a probe cannot learn whether a token ever
    existed or what became of it. Same anti-enumeration discipline as
    ``application_link_service.resolve_link`` and
    ``screening_pipeline_service.ScreeningAccessOutcome``.
    """

    VALID = "VALID"
    INVALID = "INVALID"

    ALL: frozenset[str] = frozenset({VALID, INVALID})


@dataclass(frozen=True)
class ResumeRetryResolution:
    """Result of resolving a ``resume_retry_token``.

    On ``VALID`` carries only ``application_id`` — never the application row,
    never the token, never the candidate's name or email. On ``INVALID``
    carries nothing.
    """

    outcome: str
    application_id: uuid.UUID | None = None

    @property
    def is_valid(self) -> bool:
        return self.outcome == ResumeRetryOutcome.VALID


def _ensure_resume_retry_token(db: Session, application) -> None:
    """Mint this application's résumé-retry credential, once.

    Lazy and idempotent: called ONLY from the ``RESUME_UPLOAD_FAILED`` path, and
    a second failure for the same application reuses the token minted by the
    first — never replaces it, so a link the candidate already saved keeps
    working.

    Same mechanism as ``application_link_service._new_token`` /
    ``screening_pipeline_service``'s access token: ``secrets.token_urlsafe(32)``
    (256 bits, 43 URL-safe chars). Flushes only — the caller owns the
    transaction, matching ``record_event``'s contract.

    Writes NOTHING to the log and emits NO audit event: the value is
    credential-equivalent, and a résumé failure is not an auditable domain fact
    in this codebase today (``RESUME_UPLOADED`` fires only on success, inside
    ``storage_service.upload_document``). Minting must not invent one.
    """
    if application.resume_retry_token:
        return
    application.resume_retry_token = secrets.token_urlsafe(_TOKEN_BYTES)
    db.flush()


def get_resume_retry_token(
    db: Session, *, application_id: uuid.UUID | str
) -> str | None:
    """This application's résumé-retry token, or ``None`` if it never failed.

    Candidate-facing (no auth guard), exactly like
    ``screening_pipeline_service.get_candidate_screening_token``: the caller is
    already mid-flow with this ``application_id`` — it just submitted the
    application, or resolved a valid retry token for it. Used only so the retry
    page can render the "save this link" URL.

    The return value is a credential — the caller MUST NOT log it or put it in
    an error message.
    """
    return db.execute(
        select(Application.resume_retry_token).where(
            Application.id == application_id
        )
    ).scalar_one_or_none()


def resolve_resume_retry_token(
    db: Session, token: str | None
) -> ResumeRetryResolution:
    """Resolve a résumé-retry token to the ONE application it belongs to.

    Read-only, unauthenticated — the token IS the credential, exactly like
    ``application_link_service.resolve_link``. The lookup is a single equality
    match on the UNIQUE ``applications.resume_retry_token`` column: there is no
    job-wide search, no email or name fallback, no ``applications.id`` fallback,
    and no prefix/partial matching. A token therefore reaches exactly one
    application or none.

    Returns ``VALID`` only for a known token whose application still has **no
    document**. Once the résumé lands, the retry is finished and the same token
    resolves ``INVALID`` — so a saved link cannot be replayed later to attach a
    second file. Everything else — no token, wrong shape, unknown token, an
    already-completed upload — returns the single ``INVALID`` outcome, so a
    probe cannot tell any of those apart. The token value is never logged.
    """
    if not looks_like_token(token):
        logger.info("resume retry: token rejected pre-DB")
        return ResumeRetryResolution(ResumeRetryOutcome.INVALID)

    application_id = db.execute(
        select(Application.id).where(Application.resume_retry_token == token)
    ).scalar_one_or_none()

    if application_id is None:
        logger.info("resume retry: token not found")
        return ResumeRetryResolution(ResumeRetryOutcome.INVALID)

    if list_documents_for_application(db, application_id):
        logger.info("resume retry: résumé already uploaded (generic INVALID)")
        return ResumeRetryResolution(ResumeRetryOutcome.INVALID)

    return ResumeRetryResolution(
        ResumeRetryOutcome.VALID, application_id=application_id
    )


@dataclass(frozen=True)
class CandidateContact:
    """The name and email a candidate already gave, for read-back only."""

    full_name: str
    email: str


def get_application_contact(
    db: Session, *, application_id: uuid.UUID | str
) -> CandidateContact | None:
    """The contact details already captured for ONE application, or ``None``.

    Candidate-facing (no auth guard), on exactly the same footing as
    ``get_resume_retry_token``: the caller is already mid-flow with this
    ``application_id`` — it resolved a valid retry token for it, or just
    submitted it. This grants no access the retry flow did not already have; it
    only reads back what the candidate themselves typed, so the retry screen can
    confirm their details were kept rather than making them wonder.

    Scoped to a single known ``application_id`` by construction. There is
    deliberately **no** lookup by name, by email, or by any other criterion —
    this must never become a way to discover who applied for what.
    """
    row = db.execute(
        select(Candidate.full_name, Candidate.email)
        .join(Application, Application.candidate_id == Candidate.id)
        .where(Application.id == application_id)
    ).one_or_none()

    if row is None:
        return None
    return CandidateContact(full_name=row[0], email=row[1])


# --- candidate-safe submission failure copy ---------------------------
#
# ``SubmissionResult.internal_reason`` is documented "logs / tests only — never
# shown to the candidate", and that stays true: it is a KEY here, never a value
# that reaches a screen. The shape mirrors
# ``screening_pipeline_service._STAGE_FAILED`` — a module-level map in the
# service, read through a ``.get(..., fallback)`` — so both halves of the app
# describe failures to candidates the same way.

#: The three storage exceptions differ only in cause, never in what the
#: candidate can do about them, so they deliberately share one message.
_STORAGE_UNAVAILABLE = (
    "We couldn't save your résumé just now. Your application is safe — please "
    "try attaching it again in a moment."
)

#: Anything not explicitly mapped, including the two "should never happen"
#: reasons (``ApplicationTargetNotFoundError`` / ``CandidateValidationError``)
#: that indicate a real bug rather than anything the candidate did.
SUBMISSION_FAILURE_FALLBACK = (
    "Something went wrong and we couldn't finish that just now. Your "
    "application is safe — please try again."
)

_SUBMISSION_FAILURE_MESSAGES: dict[str, str] = {
    "UNSUPPORTED_TYPE": (
        "That file type isn't supported. Please upload your résumé as a PDF or "
        "a Word document."
    ),
    "FILE_VALIDATION": (
        "We couldn't read that file. Please check it opens correctly, then "
        "upload it again as a PDF or a Word document."
    ),
    "DriveUploadError": _STORAGE_UNAVAILABLE,
    "StorageConfigError": _STORAGE_UNAVAILABLE,
    "DriveAuthError": _STORAGE_UNAVAILABLE,
    "APPLICATION_NOT_FOUND": (
        "We couldn't find that application. Please open your saved link again, "
        "or contact the hiring team."
    ),
    "RESUME_ALREADY_UPLOADED": (
        "Your résumé is already attached to this application — there's nothing "
        "more to upload."
    ),
}


def submission_failure_message(internal_reason: str | None) -> str:
    """Candidate-safe copy for a failed submission, keyed by ``internal_reason``.

    Never returns the reason code, an exception name, or any raw exception text
    — only one of the pre-written strings above. An unknown or missing reason
    yields :data:`SUBMISSION_FAILURE_FALLBACK` rather than raising or rendering
    nothing.
    """
    return _SUBMISSION_FAILURE_MESSAGES.get(
        internal_reason or "", SUBMISSION_FAILURE_FALLBACK
    )


def _attempt_resume_upload(
    db: Session,
    *,
    application,
    job_id: uuid.UUID | str,
    file_bytes: bytes,
    original_filename: str,
) -> SubmissionResult | None:
    """Try the résumé upload for an application that already exists.

    Returns ``None`` on success, or a ``RESUME_UPLOAD_FAILED`` result on failure.
    The application row is NEVER rolled back here (CLAUDE.md §27) — the caller
    offers a retry.

    Every failure return goes through :func:`_failed_upload`, which mints the
    durable retry credential. That is the single minting point in the codebase.
    """

    def _failed_upload(reason: str) -> SubmissionResult:
        # Mint the retry credential on the way out, so the candidate has a
        # durable route back even if their browser session is lost. Idempotent.
        # ``reason`` is an exception class name / constant — never the token.
        _ensure_resume_retry_token(db, application)
        return SubmissionResult(
            SubmissionOutcome.RESUME_UPLOAD_FAILED,
            application_id=str(application.id),
            internal_reason=reason,
        )

    mime = mime_type_for_filename(original_filename)
    if mime is None:  # defensive — the uploader + page validation prevent this
        logger.warning(
            "candidate portal: unmappable résumé type for application=%s",
            application.id,
        )
        return _failed_upload("UNSUPPORTED_TYPE")

    try:
        upload_document(
            db,
            application_id=application.id,
            job_id=job_id,
            file_bytes=file_bytes,
            original_filename=original_filename,
            mime_type=mime,
        )
    except (DriveUploadError, StorageConfigError, DriveAuthError) as exc:
        logger.warning(
            "candidate portal: résumé upload failed for application=%s (%s)",
            application.id, type(exc).__name__,
        )
        return _failed_upload(type(exc).__name__)
    except FileValidationError:
        # Page pre-validates the file, so reaching here is unexpected — but the
        # application row exists and must not be rolled back. Offer the retry.
        logger.warning(
            "candidate portal: résumé re-validation failed post-application "
            "(application=%s)", application.id,
        )
        return _failed_upload("FILE_VALIDATION")
    return None


def submit_application_via_token(
    db: Session,
    *,
    token: str,
    full_name: str,
    email: str,
    phone: str | None,
    file_bytes: bytes,
    original_filename: str,
) -> SubmissionResult:
    """Submit a candidate application: re-resolve link -> create application ->
    upload résumé.

    The link is re-resolved **here**, at submit time (requirement 2a) — a link
    revoked or a job closed between page load and submit is caught and yields
    ``LINK_INVALID`` (the page then shows the uniform invalid-link message).

    Caller (the page) has already client-side-validated name / email / résumé.
    """
    resolution = resolve_link(db, token)
    if not resolution.is_valid:
        logger.info(
            "candidate portal: submit rejected, link outcome=%s",
            resolution.outcome,
        )
        return SubmissionResult(
            SubmissionOutcome.LINK_INVALID, internal_reason=resolution.outcome
        )

    job_id = resolution.job.id
    link_id = resolution.link.id

    try:
        application = create_application(
            db,
            job_id=job_id,
            application_link_id=link_id,
            email=email,
            full_name=full_name,
            phone=phone,
        )
    except DuplicateApplicationError:
        logger.info(
            "candidate portal: duplicate application for job=%s", job_id
        )
        return SubmissionResult(
            SubmissionOutcome.DUPLICATE, internal_reason="DUPLICATE"
        )
    except (ApplicationTargetNotFoundError, CandidateValidationError) as exc:
        # Not normally reachable — the link and job resolved together above and
        # the page validated name/email. If it ever fires, it is a real bug.
        logger.error(
            "candidate portal: unexpected %s creating application for job=%s",
            type(exc).__name__, job_id,
        )
        return SubmissionResult(
            SubmissionOutcome.ERROR, internal_reason=type(exc).__name__
        )

    failure = _attempt_resume_upload(
        db,
        application=application,
        job_id=job_id,
        file_bytes=file_bytes,
        original_filename=original_filename,
    )
    if failure is not None:
        return failure

    logger.info(
        "candidate portal: application submitted id=%s job=%s",
        application.id, job_id,
    )
    return SubmissionResult(
        SubmissionOutcome.SUCCESS,
        application_id=str(application.id),
        internal_reason="SUCCESS",
    )


def retry_resume_upload(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    file_bytes: bytes,
    original_filename: str,
) -> SubmissionResult:
    """Re-attempt only the résumé upload for an application whose first upload
    failed. Reuses the same application row — never creates a new one.

    Refuses outright once a résumé is already stored: a retry is for a *failed*
    upload, so a second file can never replace a successful one.
    """
    application = get_application(db, application_id)
    if application is None:
        logger.warning(
            "candidate portal: résumé retry for missing application=%s",
            application_id,
        )
        return SubmissionResult(
            SubmissionOutcome.ERROR, internal_reason="APPLICATION_NOT_FOUND"
        )

    # DEFENCE IN DEPTH. ``resolve_resume_retry_token`` already stops the public
    # ``?retry=`` route from reaching here once a document exists — but that
    # protection lives at the routing boundary, so any caller that bypasses the
    # resolver would still land in this function. The same check at the service
    # boundary makes the function safe on its own terms.
    #
    # Deliberately the SAME accessor the resolver uses, not a parallel
    # reimplementation: the two can never drift into disagreeing about what
    # "already has a résumé" means. A ``documents`` row is exactly what a
    # successful ``upload_document`` creates, so its presence IS the success
    # marker (there is no flag on ``applications`` for this).
    #
    # Placed BEFORE ``_attempt_resume_upload``, so a refused retry performs no
    # Drive call, writes no row, and leaves the stored document untouched.
    if list_documents_for_application(db, application.id):
        logger.info(
            "candidate portal: résumé retry refused, one is already stored "
            "for application=%s",
            application.id,
        )
        return SubmissionResult(
            SubmissionOutcome.ERROR,
            application_id=str(application.id),
            internal_reason="RESUME_ALREADY_UPLOADED",
        )

    failure = _attempt_resume_upload(
        db,
        application=application,
        job_id=application.job_id,
        file_bytes=file_bytes,
        original_filename=original_filename,
    )
    if failure is not None:
        return failure

    logger.info(
        "candidate portal: résumé retry succeeded for application=%s",
        application.id,
    )
    return SubmissionResult(
        SubmissionOutcome.SUCCESS,
        application_id=str(application.id),
        internal_reason="SUCCESS",
    )
