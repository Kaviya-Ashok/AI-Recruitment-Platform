"""Interview-transcript service — store, version and fetch ONE transcript file
per interview round (CLAUDE.md §§7, 12, 17, 23, 24; Increment B).

Increment B stored and displayed transcripts. Increment C adds ONE read path for
the AI: :func:`get_transcript_texts_for_analysis`, called only by
``post_interview_service``. This module itself still contains no Claude call, and
nothing under ``app/ai/`` imports it (a structural test pins both facts). The
text is fetched from Drive at that moment, held in memory for the prompt, and
never stored or logged.

WHAT PYTHON DOES
----------------
* validates the actor (internal user, and explicitly **not** the SYSTEM actor —
  the same :func:`_reject_system_actor` mechanism as ``interview_feedback_service``),
* validates the feedback row exists, the file (``validate_uploaded_file``) and a
  light magic-byte check (PDF starts with ``%PDF``, DOCX with ``PK``) — done HERE
  only; ``validate_uploaded_file`` is untouched,
* names the file ``<CandidateName>_Transcript_R<round>[_v<N>].<ext>``; the round
  comes from the feedback row, never from user input,
* uploads to ``<root>/<job folder>/Transcripts/`` in Google Drive, reusing
  ``storage_service._resolve_job_folder`` and the existing folder seams,
* records ``text_extractable`` (does the file hold readable text, or is it a
  scan?) without storing or logging the text,
* inserts the row + exactly one ``INTERVIEW_TRANSCRIPT_UPLOADED`` audit event in
  one transaction; a replacement marks the old row SUPERSEDED in that same
  transaction.

The public ``storage_service.upload_document`` has NO auth guard (it serves the
candidate app) and is deliberately not reused.

ORDER OF OPERATIONS AND FAILURE MODES (CLAUDE.md §27)
-----------------------------------------------------
actor -> feedback exists -> file validation -> magic bytes -> Drive upload ->
DB write. Anything before the Drive upload fails with nothing persisted anywhere.
A Drive failure raises :class:`InterviewTranscriptStorageError` and creates NO
row; the feedback row is never touched either way.

KNOWN LIMITATION — ORPHANED DRIVE FILE. If the DB write fails AFTER the Drive
upload succeeded, the uploaded Drive file is left behind with no row pointing at
it. This matches ``storage_service.upload_document``. No deletion is attempted: a
second Drive call from a failure path could itself fail, and this increment adds
no delete/retention/purge feature. The orphan holds only what the user chose to
upload and is invisible to the application.

NEVER DELETES. A replacement keeps the old row (SUPERSEDED) and the old Drive
file. Nothing here removes either.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
The standardized file name embeds the candidate's name, and the original upload
name may too. Neither, nor the candidate's name, appears in any audit field, log
line or exception message here. Audit metadata is ids, the round, the version,
size, MIME type and the ``text_extractable`` flag.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.candidate import Candidate
from app.database.models.interview_feedback import InterviewFeedback
from app.database.models.interview_transcript import (
    InterviewTranscript,
    InterviewTranscriptStatus,
)
from app.database.models.job import Job
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
from app.services import storage_service
from app.services.audit_service import record_event
from app.services.storage_service import StorageError
from app.utils.authorization import require_internal_user
from app.utils.parsing import extract_text_from_docx, extract_text_from_pdf
from app.utils.validation import (
    FileValidationError,
    sanitize_filename,
    validate_uploaded_file,
)

logger = logging.getLogger(__name__)

#: Name of the per-job Drive subfolder holding transcripts. Exact, not configurable.
TRANSCRIPTS_FOLDER_NAME = "Transcripts"

#: A file with at most this many characters of text (after stripping) counts as
#: "no readable text" — most likely a scan or an empty export.
MIN_EXTRACTABLE_CHARS = 50

#: Cap on the sanitized candidate-name part of the file name.
_NAME_MAX = 60

_PDF_MIME = "application/pdf"
_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_EXT_TO_MIME = {".pdf": _PDF_MIME, ".docx": _DOCX_MIME}
#: First bytes each extension must start with. Light check only — a renamed text
#: file fails, a hostile but well-formed file is not this module's concern.
_MAGIC = {".pdf": b"%PDF", ".docx": b"PK"}

# --- user-safe messages (never interpolate names or file content) -----------

_SYSTEM_ACTOR = (
    "An interview transcript must be attached by a signed-in person. The "
    "automated pipeline account cannot attach one."
)
_NO_FEEDBACK = (
    "No such interview round — it may have been removed. Transcripts attach to "
    "recorded interview feedback."
)
_NO_TRANSCRIPT = "No such transcript — it may have been removed."
_TEXT_FETCH_FAILED = (
    "The interview transcript for Round {round} could not be fetched from "
    "Google Drive, so the analysis was not run. Nothing was changed. Please "
    "try again."
)
_BAD_MAGIC = (
    "That file doesn't look like a real {kind}. Check that it is the actual "
    "document and not a renamed file."
)
_DRIVE_FAILED = (
    "Couldn't store the transcript in Google Drive — nothing was saved. "
    "Please try again."
)
_DOWNLOAD_FAILED = (
    "Couldn't fetch the transcript from Google Drive. Please try again."
)
_CONCURRENT = (
    "This round's transcript was changed at the same time. Please refresh and "
    "try again."
)


class InterviewTranscriptError(Exception):
    """A transcript could not be stored or read. Carries a user-safe message;
    never a file name, candidate name or file content."""


class InterviewTranscriptTargetNotFoundError(InterviewTranscriptError):
    """No such feedback row (or transcript)."""


class InterviewTranscriptValidationError(InterviewTranscriptError):
    """The upload failed validation (type, size, emptiness, magic bytes)."""


class InterviewTranscriptActorError(InterviewTranscriptError):
    """The actor passed the internal-user guard but may not attach transcripts
    (the SYSTEM pipeline actor)."""


class InterviewTranscriptStorageError(InterviewTranscriptError):
    """Google Drive could not be reached / used. No row was created."""


# --- row shapes (safe outside a Session) -------------------------------


@dataclass(frozen=True)
class InterviewTranscriptView:
    """One transcript, with the uploader's name resolved and the round and
    version derived. Frozen primitives only (the ``InterviewFeedbackView``
    pattern), so a Streamlit page can render it after the session closes."""

    transcript_id: uuid.UUID
    interview_feedback_id: uuid.UUID
    application_id: uuid.UUID
    interview_round: int
    version_number: int
    file_name: str
    mime_type: str
    file_size_bytes: int
    text_extractable: bool
    status: str
    uploaded_by_user_id: uuid.UUID
    uploaded_by_name: str
    created_at: datetime
    superseded_at: datetime | None


@dataclass(frozen=True)
class TranscriptTextForAnalysis:
    """One round's CURRENT transcript, read for the AI. ``text`` is the empty
    string when ``readable`` is False — unreadable text is never passed on."""

    transcript_id: uuid.UUID
    interview_round: int
    text: str
    readable: bool


@dataclass(frozen=True)
class InterviewTranscriptDownload:
    content: bytes
    file_name: str
    mime_type: str


# --- helpers ------------------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _reject_system_actor(actor: User) -> None:
    """Refuse the SYSTEM pipeline actor. ``require_internal_user`` accepts it by
    design, so this is the explicit check its docstring tells callers to make
    (the same two-part role + id check as ``interview_feedback_service``)."""
    if actor.role is UserRole.SYSTEM or actor.id == SYSTEM_USER_ID:
        logger.warning(
            "interview_transcript rejected: SYSTEM actor user_id=%s", actor.id
        )
        raise InterviewTranscriptActorError(_SYSTEM_ACTOR)


def _require_human_actor(db: Session, user_id: uuid.UUID | str | None) -> User:
    """``require_internal_user`` plus the SYSTEM rejection, in that order."""
    actor = require_internal_user(db, user_id)
    _reject_system_actor(actor)
    return actor


def sanitize_candidate_name(full_name: str | None) -> str:
    """Make a candidate's name safe for a file name.

    Whitespace runs become one underscore; everything is dropped except letters,
    combining marks (so Indic and accented names keep their vowel signs), decimal
    digits, underscore and hyphen — which also removes ``/`` ``\\`` and every
    other path or control character. Repeated underscores collapse, the result is
    capped at :data:`_NAME_MAX` characters and trimmed of ``_``/``-``; if nothing
    survives, ``"Candidate"``.
    """
    text_ = unicodedata.normalize("NFC", full_name or "")
    text_ = "_".join(text_.split())
    kept: list[str] = []
    for ch in text_:
        cat = unicodedata.category(ch)
        if ch in "_-" or cat[0] in ("L", "M") or cat == "Nd":
            kept.append(ch)
    cleaned = "".join(kept)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned[:_NAME_MAX].strip("_-")
    return cleaned or "Candidate"


def build_transcript_file_name(
    full_name: str | None, interview_round: int, extension: str, version: int
) -> str:
    """``<Name>_Transcript_R<round>.<ext>``, or ``..._R<round>_v<N>.<ext>`` for
    the second and later uploads (``version >= 2``). ``extension`` includes the
    dot and is lowercased here."""
    suffix = f"_v{version}" if version >= 2 else ""
    return (
        f"{sanitize_candidate_name(full_name)}_Transcript_R{interview_round}"
        f"{suffix}{extension.lower()}"
    )


def _has_extractable_text(file_bytes: bytes, extension: str) -> bool:
    """True if the file holds more than :data:`MIN_EXTRACTABLE_CHARS` characters
    of text. Any parsing problem means False — never an exception that could
    block the upload. The text itself is neither stored nor logged."""
    try:
        if extension == ".pdf":
            extracted = extract_text_from_pdf(file_bytes)
        else:
            extracted = extract_text_from_docx(file_bytes)
    except Exception:  # noqa: BLE001 - corrupt/unusual files must not block upload
        return False
    return len(extracted.strip()) > MIN_EXTRACTABLE_CHARS


def _extract_text_or_none(file_bytes: bytes, mime_type: str) -> str | None:
    """Extracted text, or ``None`` if the file cannot be parsed. Never raises."""
    try:
        if mime_type == _PDF_MIME:
            return extract_text_from_pdf(file_bytes)
        return extract_text_from_docx(file_bytes)
    except Exception:  # noqa: BLE001 - an unreadable file is a state, not an error
        return None


def _validated_extension(original_filename: str, file_bytes: bytes) -> str:
    """Run ``validate_uploaded_file`` then the magic-byte check. Returns the
    lowercased extension (``.pdf`` / ``.docx``)."""
    try:
        validate_uploaded_file(original_filename, file_bytes)
        safe = sanitize_filename(original_filename)
    except FileValidationError as exc:
        # These messages name only the rule (size limit, extension), never the
        # file's name or content.
        raise InterviewTranscriptValidationError(str(exc)) from exc
    extension = "." + safe.rsplit(".", 1)[-1].lower()
    if not file_bytes.startswith(_MAGIC[extension]):
        raise InterviewTranscriptValidationError(
            _BAD_MAGIC.format(kind="PDF" if extension == ".pdf" else "DOCX")
        )
    return extension


def _find_or_create_transcripts_folder(service: object, job_folder_id: str) -> str:
    """The job folder's ``Transcripts`` subfolder, created on first use. Reuses
    the existing folder seams; they take any parent folder id, and the find
    seam already escapes the name for Drive's query language."""
    existing = storage_service._drive_find_folder(
        service, job_folder_id, TRANSCRIPTS_FOLDER_NAME
    )
    if existing is not None:
        return existing
    return storage_service._drive_create_folder(
        service, job_folder_id, TRANSCRIPTS_FOLDER_NAME
    )


def _rows_for_feedback(
    db: Session, feedback_id: uuid.UUID
) -> list[InterviewTranscript]:
    """Every version for one feedback row, OLDEST first. ``created_at`` is set
    explicitly (not by ``now()``) when a row is inserted, so two uploads in one
    transaction still order correctly; ``id`` is only a last-resort tiebreak."""
    return list(
        db.execute(
            select(InterviewTranscript)
            .where(InterviewTranscript.interview_feedback_id == feedback_id)
            .order_by(InterviewTranscript.created_at, InterviewTranscript.id)
        ).scalars().all()
    )


def _to_views(
    db: Session, feedback: InterviewFeedback, rows: list[InterviewTranscript]
) -> list[InterviewTranscriptView]:
    """Project oldest-first ``rows`` (one feedback's versions) to views,
    returned NEWEST first. Version numbers are the 1-based age rank."""
    views: list[InterviewTranscriptView] = []
    for index, row in enumerate(rows, start=1):
        author = db.get(User, row.uploaded_by_user_id)
        views.append(
            InterviewTranscriptView(
                transcript_id=row.id,
                interview_feedback_id=row.interview_feedback_id,
                application_id=feedback.application_id,
                interview_round=feedback.interview_round,
                version_number=index,
                file_name=row.file_name,
                mime_type=row.mime_type,
                file_size_bytes=row.file_size_bytes,
                text_extractable=row.text_extractable,
                status=row.status,
                uploaded_by_user_id=row.uploaded_by_user_id,
                uploaded_by_name=author.full_name if author else "—",
                created_at=row.created_at,
                superseded_at=row.superseded_at,
            )
        )
    views.reverse()
    return views


# --- write ---------------------------------------------------------------


def attach_interview_transcript(
    db: Session,
    *,
    interview_feedback_id: uuid.UUID | str,
    file_bytes: bytes,
    original_filename: str,
    acting_user_id: uuid.UUID | str,
) -> InterviewTranscriptView:
    """Attach a transcript to one interview round, or replace the current one.

    HR/INTERNAL ONLY, and never the SYSTEM actor. The first upload for a round
    is named ``<Name>_Transcript_R<round>.<ext>``; each later upload for the same
    round becomes ``..._v2``, ``..._v3`` (existing rows for the feedback + 1) and
    the previously CURRENT row becomes SUPERSEDED in the same transaction.
    Nothing is deleted — not the old row, not the old Drive file.

    Drive layout: ``<root>/<job folder>/Transcripts/<file name>``.

    See the module docstring for the order of operations, the Drive-failure
    behaviour and the known orphaned-Drive-file limitation.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is not an active internal user.
    InterviewTranscriptActorError
        The actor is the SYSTEM pipeline account.
    InterviewTranscriptTargetNotFoundError
        No such feedback row.
    InterviewTranscriptValidationError
        Empty / oversized file, unsupported type, or magic-byte mismatch.
    InterviewTranscriptStorageError
        Drive failed; no row was created.
    """
    # 1. actor — internal user, and a human one.
    actor = _require_human_actor(db, acting_user_id)

    # 2. feedback exists; the round, application and candidate come from it.
    feedback = db.get(InterviewFeedback, _as_uuid(interview_feedback_id))
    if feedback is None:
        raise InterviewTranscriptTargetNotFoundError(_NO_FEEDBACK)
    application = db.get(Application, feedback.application_id)
    candidate = (
        db.get(Candidate, application.candidate_id) if application else None
    )
    job = db.get(Job, application.job_id) if application else None
    if application is None or job is None:
        raise InterviewTranscriptTargetNotFoundError(_NO_FEEDBACK)

    # 3-4. file validation, then the magic-byte check.
    extension = _validated_extension(original_filename, file_bytes)
    mime_type = _EXT_TO_MIME[extension]

    # 5. name + version. The round is the FEEDBACK's, never caller-supplied.
    existing = _rows_for_feedback(db, feedback.id)
    version_number = len(existing) + 1
    file_name = build_transcript_file_name(
        candidate.full_name if candidate else None,
        feedback.interview_round,
        extension,
        version_number,
    )
    text_extractable = _has_extractable_text(file_bytes, extension)

    # 6. Drive. Any failure -> no row, feedback untouched.
    try:
        service, root_folder_id = storage_service._get_drive()
        job_folder_id = storage_service._resolve_job_folder(
            db, service, root_folder_id, job
        )
        transcripts_folder_id = _find_or_create_transcripts_folder(
            service, job_folder_id
        )
        drive_file_id, size_bytes = storage_service._drive_upload_file(
            service, transcripts_folder_id, file_name, file_bytes, mime_type
        )
    except StorageError as exc:
        # Type name only — never the message, which could carry Drive detail.
        logger.warning(
            "interview_transcript Drive failure feedback=%s: %s",
            feedback.id, type(exc).__name__,
        )
        raise InterviewTranscriptStorageError(_DRIVE_FAILED) from exc

    # 7. DB write: supersede the current row (if any) and insert the new CURRENT
    #    one, then audit — one transaction.
    try:
        previous = db.execute(
            select(InterviewTranscript)
            .where(
                InterviewTranscript.interview_feedback_id == feedback.id,
                InterviewTranscript.status == InterviewTranscriptStatus.CURRENT,
            )
            .with_for_update()
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if previous is not None:
            previous.status = InterviewTranscriptStatus.SUPERSEDED
            previous.superseded_at = now
            # Flush the supersede BEFORE the insert so the partial unique index
            # never sees two CURRENT rows.
            db.flush()

        transcript = InterviewTranscript(
            interview_feedback_id=feedback.id,
            uploaded_by_user_id=actor.id,
            drive_file_id=drive_file_id,
            drive_folder_id=transcripts_folder_id,
            file_name=file_name,
            mime_type=mime_type,
            file_size_bytes=size_bytes,
            text_extractable=text_extractable,
            status=InterviewTranscriptStatus.CURRENT,
            created_at=now,
        )
        db.add(transcript)
        db.flush()

        record_event(
            db,
            event_type=AuditEventType.INTERVIEW_TRANSCRIPT_UPLOADED,
            action="Interview transcript uploaded for an interview round.",
            entity_type="interview_transcript",
            entity_id=transcript.id,
            user_id=actor.id,
            metadata={
                # ids + structural facts only — NEVER the file name, the
                # original file name, or the candidate's name.
                "application_id": str(application.id),
                "interview_feedback_id": str(feedback.id),
                "interview_round": feedback.interview_round,
                "transcript_id": str(transcript.id),
                "file_size_bytes": size_bytes,
                "mime_type": mime_type,
                "version_number": version_number,
                "superseded_transcript_id": (
                    str(previous.id) if previous is not None else None
                ),
                "text_extractable": text_extractable,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning(
            "interview_transcript concurrent write feedback=%s "
            "(Drive file %s is orphaned)", feedback.id, drive_file_id,
        )
        raise InterviewTranscriptError(_CONCURRENT) from exc
    except Exception:
        db.rollback()
        logger.error(
            "interview_transcript DB write failed feedback=%s "
            "(Drive file %s is orphaned)", feedback.id, drive_file_id,
        )
        raise

    db.refresh(transcript)
    logger.info(
        "interview_transcript stored feedback=%s round=%s version=%d "
        "size=%d text_extractable=%s actor=%s",
        feedback.id, feedback.interview_round, version_number, size_bytes,
        text_extractable, actor.id,
    )
    return _to_views(db, feedback, _rows_for_feedback(db, feedback.id))[0]


# --- reads (HR/INTERNAL ONLY) ----------------------------------------------


def list_transcripts_for_feedback(
    db: Session,
    interview_feedback_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[InterviewTranscriptView]:
    """Every version of one round's transcript, newest first (CURRENT first,
    then SUPERSEDED). Empty if the round has none or does not exist."""
    require_internal_user(db, acting_user_id)
    feedback = db.get(InterviewFeedback, _as_uuid(interview_feedback_id))
    if feedback is None:
        return []
    return _to_views(db, feedback, _rows_for_feedback(db, feedback.id))


def get_current_transcript_for_feedback(
    db: Session,
    interview_feedback_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> InterviewTranscriptView | None:
    """The round's CURRENT transcript, or ``None``."""
    for view in list_transcripts_for_feedback(
        db, interview_feedback_id, acting_user_id=acting_user_id
    ):
        if view.status == InterviewTranscriptStatus.CURRENT:
            return view
    return None


def list_current_transcripts_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> dict[int, InterviewTranscriptView]:
    """``{interview_round: CURRENT transcript}`` for one application. Rounds
    with no transcript are simply absent."""
    require_internal_user(db, acting_user_id)
    feedback_rows = db.execute(
        select(InterviewFeedback).where(
            InterviewFeedback.application_id == _as_uuid(application_id)
        )
    ).scalars().all()
    out: dict[int, InterviewTranscriptView] = {}
    for feedback in feedback_rows:
        for view in _to_views(db, feedback, _rows_for_feedback(db, feedback.id)):
            if view.status == InterviewTranscriptStatus.CURRENT:
                out[feedback.interview_round] = view
                break
    return out


def get_transcript_download_bytes(
    db: Session,
    transcript_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> InterviewTranscriptDownload:
    """Fetch one transcript's bytes from Drive. HR/INTERNAL ONLY — the guard
    runs before any DB read or Drive call.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is not an active internal user.
    InterviewTranscriptTargetNotFoundError
        No such transcript.
    InterviewTranscriptStorageError
        Drive could not be reached or the download failed.
    """
    require_internal_user(db, acting_user_id)
    row = db.get(InterviewTranscript, _as_uuid(transcript_id))
    if row is None:
        raise InterviewTranscriptTargetNotFoundError(_NO_TRANSCRIPT)
    try:
        service, _ = storage_service._get_drive()
        content = storage_service._drive_download_file(
            service, row.drive_file_id
        )
    except StorageError as exc:
        logger.warning(
            "interview_transcript download failure transcript=%s: %s",
            row.id, type(exc).__name__,
        )
        raise InterviewTranscriptStorageError(_DOWNLOAD_FAILED) from exc
    return InterviewTranscriptDownload(
        content=content, file_name=row.file_name, mime_type=row.mime_type
    )


def get_transcript_texts_for_analysis(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[TranscriptTextForAnalysis]:
    """The text of every round's CURRENT transcript, for the post-interview
    analysis. Rounds ascending; SUPERSEDED versions are never read.

    HR/INTERNAL ONLY and, because this feeds an AI call that synthesises a human's
    testimony, never the SYSTEM actor (:func:`_require_human_actor`).

    ``readable`` is False — and ``text`` is ``''`` — when the extracted text does
    not exceed :data:`MIN_EXTRACTABLE_CHARS` or the file cannot be parsed. That is
    a state, not an error: the caller reports the round and carries on without it.

    A Drive failure RAISES :class:`InterviewTranscriptStorageError`. The analysis
    must never silently proceed as if a transcript that exists did not. The text
    is never logged or stored; messages carry only the round number.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is not an active internal user.
    InterviewTranscriptActorError
        The actor is the SYSTEM pipeline account.
    InterviewTranscriptStorageError
        Drive could not be reached, or a download failed.
    """
    _require_human_actor(db, acting_user_id)
    rows = db.execute(
        select(InterviewTranscript, InterviewFeedback.interview_round)
        .join(
            InterviewFeedback,
            InterviewFeedback.id == InterviewTranscript.interview_feedback_id,
        )
        .where(
            InterviewFeedback.application_id == _as_uuid(application_id),
            InterviewTranscript.status == InterviewTranscriptStatus.CURRENT,
        )
        .order_by(InterviewFeedback.interview_round, InterviewTranscript.id)
    ).all()
    if not rows:
        return []

    out: list[TranscriptTextForAnalysis] = []
    for transcript, interview_round in rows:
        try:
            service, _ = storage_service._get_drive()
            content = storage_service._drive_download_file(
                service, transcript.drive_file_id
            )
        except StorageError as exc:
            logger.warning(
                "interview_transcript text fetch failure transcript=%s: %s",
                transcript.id, type(exc).__name__,
            )
            raise InterviewTranscriptStorageError(
                _TEXT_FETCH_FAILED.format(round=interview_round)
            ) from exc
        extracted = _extract_text_or_none(content, transcript.mime_type)
        readable = (
            extracted is not None
            and len(extracted.strip()) > MIN_EXTRACTABLE_CHARS
        )
        out.append(
            TranscriptTextForAnalysis(
                transcript_id=transcript.id,
                interview_round=interview_round,
                text=extracted.strip() if readable else "",
                readable=readable,
            )
        )
    return out
