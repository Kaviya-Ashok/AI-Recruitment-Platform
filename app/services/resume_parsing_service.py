"""Resume-parsing service — run the ``resume_parsing`` AI task against one
stored resume document and persist the structured evidence inventory
(CLAUDE.md §§3, 18, 23, 27, 29).

Transaction model (matches ``job_service`` / ``rubric_service``): the caller
passes a ``Session``; :func:`parse_resume` is the **top-level business action**.
It reads the document, downloads the bytes, extracts text, calls Claude,
validates, then — on success only — inserts the ``resume_extractions`` row,
moves the application status, writes one ``RESUME_PROCESSED`` audit event, and
issues a **single** ``db.commit()`` so the extraction row and its audit row land
together or not at all. The Streamlit UI wraps the call in ``session_scope()``.

Manual-trigger only — the "Parse Resume" button. Never auto-triggered on upload
or on page load (CLAUDE.md §§9, 29; same rule as ``analyze_jd`` /
``generate_rubric``).

AUTH BOUNDARY (CLAUDE.md §23) — ENFORCED IN CODE
-----------------------------------------------
:func:`parse_resume` and :func:`get_extraction_for_document` are **HR/INTERNAL
ONLY** (they read candidate resume bytes + extracted content). This is now
**enforced in code**: both call
:func:`app.utils.authorization.require_internal_user` before any other work, and
:func:`parse_resume` threads its validated ``requested_by_user_id`` into the
downstream ``get_document_download_bytes`` / ``get_extraction_for_document``
calls (each re-checks — defense in depth). A caller from ``app/public_main.py``
has no user in session and gets ``UnauthorizedError`` immediately.

Scope of that guard: it stops *accidental* exposure (a wrong import, a future
mistake). It is not a defense against a malicious authenticated internal user —
that is a different, out-of-scope threat model.

VALIDATION SUFFICIENCY (CLAUDE.md §18)
-------------------------------------
Schema validation (``ResumeExtractionResult``) is the *only* validation gate
here, and that is deliberate. Unlike ``rubric_generation`` (which enforces
"at least one MANDATORY criterion" because a rubric with none is structurally
unusable), an evidence inventory has **no** analogous invariant: a sparse
resume can legitimately yield mostly-empty lists, and there is no downstream
contract at this stage that a non-empty extraction must satisfy — judging the
evidence is prequalification's job (the next step). So there is no extra
business-rule validation layered on top of the schema.

PRIVACY / LOGGING (CLAUDE.md §§19, 23, 24)
-----------------------------------------
Raw resume text and the AI's extracted content are NEVER logged or put in
exceptions/audit metadata. Log lines carry task/model/outcome/counts only;
audit metadata references entities by id.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.claude_client import (
    AIError,
    AIOutputError,
    _resolve_model,
    get_structured_response,
)
from app.ai.prompts.resume_parsing import build_resume_parsing_prompt
from app.ai.schemas.resume_parsing import ResumeExtractionResult
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.document import Document
from app.database.models.resume_extraction import ResumeExtraction
from app.services.audit_service import record_event
from app.services.storage_service import (
    ALLOWED_MIME_TYPES,
    StorageError,
    get_document_download_bytes,
)
from app.utils.authorization import require_internal_user
from app.utils.parsing import (
    DocumentParsingError,
    extract_text_from_docx,
    extract_text_from_pdf,
)

logger = logging.getLogger(__name__)

_TASK_NAME = "resume_parsing"

_PARSE_UNUSABLE = (
    "The AI could not produce a usable evidence inventory from this resume. "
    "Please try again; if it keeps failing, re-upload the resume."
)
_PARSE_FAILED = "Resume parsing failed — please try again."
_TEXT_UNREADABLE = (
    "Couldn't read any text from this resume file. It may be a scanned image "
    "or corrupted — ask the candidate to re-upload a text-based PDF or DOCX."
)


class ResumeParsingError(Exception):
    """Resume parsing failed (bad document, unreadable file, or AI failure).

    Carries a user-safe message; never wraps raw resume text or AI content.
    """


class ResumeDocumentNotFoundError(ResumeParsingError):
    """No ``documents`` row with the given id."""


class ResumeAlreadyExtractedError(ResumeParsingError):
    """An extraction already exists for this document and ``force`` was not set.

    Callers that genuinely want to re-run must pass ``force=True`` (which
    deletes the existing extraction and replaces it).
    """


# --- reads --------------------------------------------------------------


def get_extraction_for_document(
    db: Session, document_id: uuid.UUID | str, *, acting_user_id: uuid.UUID | str
) -> ResumeExtraction | None:
    """Return the resume extraction for a document, or ``None``.

    HR/INTERNAL ONLY — returns extracted résumé evidence.
    :func:`~app.utils.authorization.require_internal_user` is checked first;
    ``acting_user_id`` must resolve to an active internal user (raises
    :class:`~app.utils.authorization.UnauthorizedError` otherwise).
    """
    require_internal_user(db, acting_user_id)
    return db.execute(
        select(ResumeExtraction).where(
            ResumeExtraction.document_id == document_id
        )
    ).scalar_one_or_none()


# --- helpers -----------------------------------------------------------


def _extract_resume_text(document: Document, file_bytes: bytes) -> str:
    """Dispatch on the document's stored mime type. Raises DocumentParsingError."""
    ext = ALLOWED_MIME_TYPES.get(document.mime_type)
    if ext == ".pdf":
        return extract_text_from_pdf(file_bytes)
    if ext == ".docx":
        return extract_text_from_docx(file_bytes)
    # storage_service validated the mime type at upload time; defensive only.
    raise DocumentParsingError(
        f"No text extractor for mime type {document.mime_type!r}."
    )


# --- main action -----------------------------------------------------


def parse_resume(
    db: Session,
    *,
    document_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
    force: bool = False,
) -> ResumeExtraction:
    """Run the resume-parsing AI task on one document and persist the result.

    Manual-trigger only. HR/INTERNAL ONLY —
    :func:`~app.utils.authorization.require_internal_user` is checked as the
    first line, before any DB read of business data, any Drive call, or any AI
    call. ``requested_by_user_id`` is required (non-``None``) here and doubles as
    the authorization subject and the audit actor.

    Re-run behaviour (CLAUDE.md §29): if an extraction already exists for
    ``document_id`` and ``force`` is False, raises
    :class:`ResumeAlreadyExtractedError` **without** an AI call or any DB write.
    With ``force=True`` the existing row is deleted and replaced in the same
    transaction (``resume_extractions`` has no child rows and is not versioned
    in this phase).

    On success: inserts one ``resume_extractions`` row, moves the owning
    application to ``RESUME_PROCESSED``, writes one ``RESUME_PROCESSED`` audit
    event, and commits once.

    Raises
    ------
    UnauthorizedError
        ``requested_by_user_id`` is missing / malformed / unknown / inactive.
        Raised before any DB read, Drive call or AI call.
    ResumeDocumentNotFoundError
        No document with ``document_id``.
    ResumeAlreadyExtractedError
        An extraction exists and ``force`` is False.
    ResumeParsingError
        The file could not be downloaded, produced no text, or the AI call /
        its output failed. No DB changes were made.
    """
    require_internal_user(db, requested_by_user_id)

    document = db.get(Document, document_id)
    if document is None:
        raise ResumeDocumentNotFoundError(f"No document with id {document_id!r}.")

    existing = get_extraction_for_document(
        db, document.id, acting_user_id=requested_by_user_id
    )
    if existing is not None and not force:
        raise ResumeAlreadyExtractedError(
            "This resume has already been parsed. Re-parsing must be explicitly "
            "requested."
        )

    # --- download + text extraction (all before any DB mutation) ----------
    try:
        file_bytes = get_document_download_bytes(
            document.id, db, acting_user_id=requested_by_user_id
        )
    except StorageError as exc:
        logger.warning(
            "resume_parsing document=%s failure=download kind=%s",
            document_id, type(exc).__name__,
        )
        raise ResumeParsingError(_PARSE_FAILED) from exc

    try:
        resume_text = _extract_resume_text(document, file_bytes)
    except DocumentParsingError as exc:
        logger.warning(
            "resume_parsing document=%s failure=text_extraction", document_id
        )
        raise ResumeParsingError(_TEXT_UNREADABLE) from exc

    if not resume_text.strip():
        logger.warning(
            "resume_parsing document=%s failure=empty_text", document_id
        )
        raise ResumeParsingError(_TEXT_UNREADABLE)

    # --- AI call (still before any DB mutation) --------------------------
    prompt = build_resume_parsing_prompt(resume_text)
    try:
        result: ResumeExtractionResult = get_structured_response(
            prompt, ResumeExtractionResult, task_name=_TASK_NAME
        )
    except AIOutputError as exc:
        logger.warning(
            "resume_parsing document=%s failure=validation "
            "(invalid AI output after retries)",
            document_id,
        )
        raise ResumeParsingError(_PARSE_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "resume_parsing document=%s failure=request kind=%s",
            document_id, type(exc).__name__,
        )
        raise ResumeParsingError(_PARSE_FAILED) from exc

    resolved_model = _resolve_model(_TASK_NAME, None)

    # --- AI succeeded: now (and only now) mutate the DB, one transaction ---
    replaced_extraction_id: uuid.UUID | None = None
    if existing is not None:
        replaced_extraction_id = existing.id
        db.delete(existing)
        db.flush()

    extraction = ResumeExtraction(
        document_id=document.id,
        extracted_data=result.model_dump(mode="json"),
        ai_model=resolved_model,
    )
    db.add(extraction)
    db.flush()  # assign extraction.id

    application = db.get(Application, document.application_id)
    previous_status = application.status if application is not None else None
    if application is not None:
        application.status = ApplicationStatus.RESUME_PROCESSED

    counts = {
        "skills": len(result.skills),
        "technologies": len(result.technologies),
        "experience": len(result.experience),
        "projects": len(result.projects),
        "certifications": len(result.certifications),
        "education": len(result.education),
        "other_relevant_claims": len(result.other_relevant_claims),
    }

    record_event(
        db,
        event_type=AuditEventType.RESUME_PROCESSED,
        action=(
            f"Resume parsed for application {document.application_id}: "
            f"evidence inventory extracted"
            f"{' (re-parsed)' if existing is not None else ''}."
        ),
        entity_type="document",
        entity_id=document.id,
        user_id=requested_by_user_id,
        previous_state=(
            {"application_status": previous_status}
            if previous_status is not None
            else None
        ),
        new_state={
            "extraction_id": str(extraction.id),
            "ai_model": resolved_model,
            "forced": force,
            "replaced_extraction_id": (
                str(replaced_extraction_id) if replaced_extraction_id else None
            ),
            "evidence_counts": counts,
            "application_status": (
                ApplicationStatus.RESUME_PROCESSED
                if application is not None
                else None
            ),
        },
    )

    db.commit()
    db.refresh(extraction)
    logger.info(
        "resume_parsing document=%s outcome=ok model=%s forced=%s counts=%s",
        document_id, resolved_model, force, counts,
    )
    return extraction
