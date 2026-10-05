"""Document storage service — the single boundary between this app and the
document-storage provider (Google Drive today; S3 or similar tomorrow).

CLAUDE.md §17 (hard requirement)
-------------------------------
**This is the ONLY module that may import the Google Drive SDK.** No page and no
other service imports ``googleapiclient`` / ``google.oauth2`` directly. That is
what keeps a future swap to another provider a change to *this* file only.

The public interface is deliberately provider-agnostic in naming
(``upload_document`` / ``get_document`` / ``get_document_download_bytes``, not
``upload_to_drive``) so callers are not semantically coupled to "Drive".

Configuration (environment only, never hard-coded) — loaded lazily on first use,
mirroring ``claude_client._get_client`` (an unconfigured import must not fail;
the first real call fails fast and clearly).

Auth is **OAuth 2.0 as the human user who owns the Drive folder**, NOT a service
account. A personal Gmail account has no service-account storage quota — Google
returns a hard ``403 storageQuotaExceeded`` for a service-account upload, and
the documented alternatives (shared drives / domain-wide delegation) need
Workspace. Authorizing as the folder's own owner means uploads count against
that human's real quota. The one-time ``scripts/authorize_drive_oauth.py`` mints
the refresh token; this module then refreshes access tokens automatically.

Env vars (all required at the first Drive call):

* ``GOOGLE_OAUTH_CLIENT_ID`` / ``GOOGLE_OAUTH_CLIENT_SECRET`` — the "Desktop app"
  OAuth client from the Google Cloud console.
* ``GOOGLE_OAUTH_REFRESH_TOKEN`` — long-lived; produced once by
  ``scripts/authorize_drive_oauth.py``. Credential-equivalent: env only, never
  logged, never echoed in an error.
* ``GOOGLE_DRIVE_ROOT_FOLDER_ID`` — the root folder (owned by the same account).
  One subfolder per job is created lazily beneath it, named
  ``"<job_code> - <title>"``. Jobs uploaded to before job codes existed keep
  their UUID-named folder: it is found by the ``drive_folder_id`` stored on their
  documents, never by name, and is never renamed.

Missing config raises :class:`StorageConfigError`; a revoked/invalid refresh
token raises :class:`DriveAuthError` — never a silent no-op, never a raw SDK
exception from deep in the stack.

Transaction model (matches ``application_service``): :func:`upload_document` is a
top-level business action — the caller passes a ``Session``; the Drive upload
happens first, and only if it succeeds is the ``documents`` row created + the
``RESUME_UPLOADED`` audit event written + a single ``commit`` issued (CLAUDE.md
§27: a Drive failure must never leave a "successfully stored" DB row).

Auth boundary
-------------
:func:`get_document_download_bytes` fetches raw file bytes and is **HR/internal
only** — it must never be reachable from the public candidate app
(``app/public_main.py`` / ``candidate_portal_service``). As of the
authorization-hardening step this is **enforced in code**, not just convention:
the function requires an ``acting_user_id`` and calls
:func:`app.utils.authorization.require_internal_user` before any other work.
``upload_document`` and the metadata reads (``get_document`` /
``list_documents_for_application``) are NOT gated — ``upload_document`` is on the
public application path (a candidate uploads their own résumé) and the metadata
reads are used by both apps.
"""

from __future__ import annotations

import io
import logging
import os
import unicodedata
import uuid
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.document import Document
from app.database.models.job import Job
from app.services.audit_service import record_event
from app.utils.authorization import require_internal_user
from app.utils.validation import sanitize_filename, validate_uploaded_file

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

# Full drive scope is required (not the narrower drive.file):
#   * GOOGLE_DRIVE_ROOT_FOLDER_ID points at a folder created in the Drive UI,
#     NOT by this app.
#   * drive.file only grants access to files the app created, or files the user
#     hands it via the Google Picker. This is a headless CLI OAuth flow with no
#     Picker step, so drive.file cannot see (list under / create children in)
#     the pre-existing root folder — files.create with parents=[root] would 404.
# Trade-off accepted: the token can read/write the owner's whole Drive; it is
# the owner's own account authorizing their own app, the refresh token is a
# top-tier secret, and this code only ever touches paths under the root folder.
_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Google's OAuth 2.0 token endpoint — where google-auth exchanges the refresh
# token for short-lived access tokens.
_TOKEN_URI = "https://oauth2.googleapis.com/token"

_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

# Resume-appropriate allowlist. Kept in lockstep with what app/utils/parsing.py
# can actually extract later (PDF, DOCX) and with validation.ALLOWED_EXTENSIONS
# — no point accepting bytes a later step cannot read.
ALLOWED_MIME_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


# --- exceptions --------------------------------------------------------


class StorageError(Exception):
    """Base class for document-storage failures."""


class StorageConfigError(StorageError):
    """A required storage env var is missing."""


class DriveAuthError(StorageError):
    """The OAuth refresh token is invalid or has been revoked.

    A human must re-run ``scripts/authorize_drive_oauth.py`` and update
    ``GOOGLE_OAUTH_REFRESH_TOKEN``.
    """


class DocumentValidationError(StorageError):
    """The file failed a storage-layer check (e.g. disallowed MIME type)."""


class DocumentTargetNotFoundError(StorageError):
    """The referenced application or document does not exist / does not match."""


class DriveUploadError(StorageError):
    """A Drive call failed while resolving the folder or uploading the file.

    When raised from :func:`upload_document`, no ``documents`` row was created.
    """


class DriveDownloadError(StorageError):
    """A Drive call failed while fetching file content."""


# --- lazy client ------------------------------------------------------

# Cached ``(drive_service, root_folder_id)`` — built once, on first use.
_drive_cache: tuple[object, str] | None = None


def _build_user_credentials(
    client_id: str, client_secret: str, refresh_token: str
) -> object:
    """Build an auto-refreshing OAuth user Credentials object.

    ``token=None`` + a valid ``refresh_token`` + ``token_uri`` means google-auth
    transparently mints (and re-mints on expiry) short-lived access tokens — the
    whole point of holding a refresh token. Nothing here is logged.
    """
    from google.oauth2.credentials import Credentials  # noqa: PLC0415 - SDK isolated

    return Credentials(
        None,  # no access token yet; refreshed on demand from the refresh token
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=_SCOPES,
    )


def _verify_credentials(credentials: object) -> None:
    """Force one token refresh now, so a revoked/invalid refresh token fails
    *here* (at client init) with an actionable message, instead of surfacing
    confusingly deep inside a later Drive call."""
    from google.auth.exceptions import RefreshError  # noqa: PLC0415 - SDK isolated
    from google.auth.transport.requests import Request  # noqa: PLC0415

    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        raise DriveAuthError(
            "Google Drive authorization failed: the OAuth refresh token is "
            "invalid or has been revoked. Re-run "
            "scripts/authorize_drive_oauth.py and update "
            "GOOGLE_OAUTH_REFRESH_TOKEN in your .env."
        ) from exc


def _build_drive_service(credentials) -> object:
    from googleapiclient.discovery import build  # noqa: PLC0415 - SDK isolated here

    return build(
        "drive", "v3", credentials=credentials, cache_discovery=False
    )


def _get_drive() -> tuple[object, str]:
    """Return ``(drive_service, root_folder_id)``, building them once.

    Raises
    ------
    StorageConfigError
        Any of the four required env vars is unset.
    DriveAuthError
        The refresh token is present but invalid / revoked.
    """
    global _drive_cache
    if _drive_cache is not None:
        return _drive_cache

    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")
    root_folder_id = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID")

    missing = [
        name
        for name, val in (
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
            ("GOOGLE_DRIVE_ROOT_FOLDER_ID", root_folder_id),
        )
        if not val
    ]
    if missing:
        hint = (
            " Run scripts/authorize_drive_oauth.py once to obtain "
            "GOOGLE_OAUTH_REFRESH_TOKEN."
            if "GOOGLE_OAUTH_REFRESH_TOKEN" in missing
            else ""
        )
        raise StorageConfigError(
            f"Missing required storage config: {', '.join(missing)}.{hint} "
            "See .env.example."
        )

    credentials = _build_user_credentials(client_id, client_secret, refresh_token)
    _verify_credentials(credentials)
    service = _build_drive_service(credentials)
    _drive_cache = (service, root_folder_id)
    logger.info(
        "storage: Drive client initialised (OAuth user credentials, "
        "root folder configured)"
    )
    return _drive_cache


# --- Drive seams (thin, individually mockable in tests) --------------
#
# Every actual Drive API call lives in one of these four functions. Tests patch
# them; nothing else in the module touches ``service.files()``.


def _drive_find_folder(
    service: object, root_folder_id: str, name: str
) -> str | None:
    """Return the id of a non-trashed subfolder named ``name`` under the root,
    or ``None``. First match wins (names are ``"<job_code> - <title>"``, and
    ``job_code`` is globally unique, so a name identifies at most one job).

    Drive's query language uses backslash as its escape character, so
    backslashes are escaped FIRST and quotes second — the other order would
    double-escape the backslash that the quote escape just introduced."""
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and '{root_folder_id}' in parents and "
        f"mimeType = '{_DRIVE_FOLDER_MIME}' and trashed = false"
    )
    try:
        resp = (
            service.files()
            .list(
                q=query,
                fields="files(id, name)",
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - normalise to our domain error
        raise DriveUploadError(
            f"Could not query Drive for the job subfolder: {type(exc).__name__}."
        ) from exc
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _drive_create_folder(
    service: object, root_folder_id: str, name: str
) -> str:
    try:
        folder = (
            service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": _DRIVE_FOLDER_MIME,
                    "parents": [root_folder_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - normalise to our domain error
        raise DriveUploadError(
            f"Could not create the job subfolder in Drive: {type(exc).__name__}."
        ) from exc
    return folder["id"]


def _drive_folder_usable(service: object, folder_id: str) -> bool:
    """True if ``folder_id`` is an existing, non-trashed Drive folder.

    Gives a DEFINITIVE answer or raises. ``404`` (deleted, or not visible to this
    account) and ``trashed`` mean "gone" -> ``False``. Any other failure (network,
    5xx, 403 quota) raises :class:`DriveUploadError` rather than returning
    ``False``: treating a transient blip as "folder missing" would send the
    caller on to create a second folder for a job that already has one.
    """
    from googleapiclient.errors import HttpError  # noqa: PLC0415 - SDK isolated

    try:
        meta = (
            service.files()
            .get(
                fileId=folder_id,
                fields="id, trashed, mimeType",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as exc:
        if getattr(getattr(exc, "resp", None), "status", None) == 404:
            return False
        raise DriveUploadError(
            f"Could not verify the job subfolder in Drive: {type(exc).__name__}."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - normalise to our domain error
        raise DriveUploadError(
            f"Could not verify the job subfolder in Drive: {type(exc).__name__}."
        ) from exc
    return (
        not meta.get("trashed", False)
        and meta.get("mimeType") == _DRIVE_FOLDER_MIME
    )


def _drive_upload_file(
    service: object,
    folder_id: str,
    name: str,
    file_bytes: bytes,
    mime_type: str,
) -> tuple[str, int]:
    """Upload bytes to ``folder_id``. Returns ``(drive_file_id, size_bytes)``."""
    from googleapiclient.http import MediaIoBaseUpload  # noqa: PLC0415

    try:
        media = MediaIoBaseUpload(
            io.BytesIO(file_bytes), mimetype=mime_type, resumable=False
        )
        created = (
            service.files()
            .create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id, size",
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - normalise to our domain error
        raise DriveUploadError(
            f"Drive upload failed: {type(exc).__name__}."
        ) from exc
    size = created.get("size")
    return created["id"], int(size) if size is not None else len(file_bytes)


def _drive_download_file(service: object, drive_file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload  # noqa: PLC0415

    try:
        request = service.files().get_media(
            fileId=drive_file_id, supportsAllDrives=True
        )
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    except Exception as exc:  # noqa: BLE001 - normalise to our domain error
        raise DriveDownloadError(
            f"Drive download failed: {type(exc).__name__}."
        ) from exc
    return buffer.getvalue()


# --- internal helpers ------------------------------------------------


#: Cap on the TITLE part of a folder name. Drive allows far longer, but a
#: 255-char job title makes an unreadable folder in the Drive UI.
_FOLDER_TITLE_MAX = 80


def _job_folder_name(job_code: str, title: str | None) -> str:
    """``"<job_code> - <sanitized title>"``, or just ``job_code`` if nothing of
    the title survives sanitising. Pure; never mutates the stored job title.

    Sanitising, in order: collapse all whitespace (so a tab or newline between
    words becomes one space rather than gluing them together) -> remove ``/`` and
    ``\\`` -> remove remaining control characters -> collapse whitespace again
    (the removals can leave doubles) -> cap at :data:`_FOLDER_TITLE_MAX` ->
    trim. Only Unicode category ``Cc`` is removed: format characters such as the
    zero-width joiner are legitimate inside Indic scripts and emoji.
    """
    text_ = " ".join((title or "").split())
    text_ = "".join(
        ch
        for ch in text_
        if ch not in "/\\" and unicodedata.category(ch) != "Cc"
    )
    text_ = " ".join(text_.split())[:_FOLDER_TITLE_MAX].rstrip()
    return f"{job_code} - {text_}" if text_ else job_code


def _resolve_job_folder(
    db: Session, service: object, root_folder_id: str, job: Job
) -> str:
    """Return the Drive folder id for a job, creating it on first use.

    Lookup order — so legacy UUID-named folders keep working and a job never
    ends up with two folders:

    a. **Stored id.** If any existing document for this job recorded a
       ``drive_folder_id``, use the first one that still exists and is not
       trashed. This is what keeps a pre-existing UUID-named folder in use: it
       is found by id, so its (old-style) name no longer matters. Existing
       folders are never renamed.
    b. **New-style name** under the root.
    c. **Create** a folder with the new-style name.

    Not pre-created at job creation; checked-then-created on first upload.
    """
    stored = db.execute(
        select(Document.drive_folder_id)
        .join(Application, Application.id == Document.application_id)
        .where(Application.job_id == job.id)
        .order_by(Document.uploaded_at, Document.id)
    ).scalars().all()
    seen: set[str] = set()
    for folder_id in stored:
        if folder_id in seen:
            continue
        seen.add(folder_id)
        if _drive_folder_usable(service, folder_id):
            return folder_id

    name = _job_folder_name(job.job_code, job.title)
    existing = _drive_find_folder(service, root_folder_id, name)
    if existing is not None:
        return existing
    folder_id = _drive_create_folder(service, root_folder_id, name)
    # Ids only — the folder name embeds the job title.
    logger.info(
        "storage: created Drive subfolder for job=%s code=%s",
        job.id, job.job_code,
    )
    return folder_id


def _validate_mime_type(mime_type: str) -> None:
    if mime_type not in ALLOWED_MIME_TYPES:
        allowed = ", ".join(sorted(ALLOWED_MIME_TYPES))
        raise DocumentValidationError(
            f"Unsupported MIME type {mime_type!r}. Allowed: {allowed}."
        )


# --- public interface ----------------------------------------------


def upload_document(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    job_id: uuid.UUID | str,
    file_bytes: bytes,
    original_filename: str,
    mime_type: str,
) -> Document:
    """Store one file for an application and record it in Postgres + the audit
    trail, in one transaction.

    Order (CLAUDE.md §27): validate -> resolve/create the job's Drive subfolder
    -> upload the bytes -> **only then** insert the ``documents`` row +
    ``RESUME_UPLOADED`` audit event -> single ``commit``. Any Drive failure
    raises before the DB row exists, so the caller can safely retry.

    Raises
    ------
    app.utils.validation.FileValidationError
        Empty / oversized file, bad extension, or unsanitisable filename.
    DocumentValidationError
        ``mime_type`` is not in :data:`ALLOWED_MIME_TYPES`.
    DocumentTargetNotFoundError
        No application with ``application_id``, or its ``job_id`` differs from
        the ``job_id`` argument.
    StorageConfigError
        Storage is not configured (missing env var / unreadable key file).
    DriveUploadError
        A Drive call failed. No ``documents`` row was created.
    """
    # 1. Cheap, side-effect-free validation FIRST — before any Drive call.
    validate_uploaded_file(original_filename, file_bytes)  # FileValidationError
    safe_name = sanitize_filename(original_filename)
    _validate_mime_type(mime_type)  # DocumentValidationError

    application = db.get(Application, application_id)
    if application is None:
        raise DocumentTargetNotFoundError(
            f"No application with id {application_id!r}."
        )
    if str(application.job_id) != str(job_id):
        raise DocumentTargetNotFoundError(
            "job_id does not match the application's job."
        )

    # 2. Drive side effects. Any failure here raises DriveUploadError and we
    #    never reach the DB write below.
    service, root_folder_id = _get_drive()
    job = db.get(Job, application.job_id)
    folder_id = _resolve_job_folder(db, service, root_folder_id, job)
    drive_file_id, size_bytes = _drive_upload_file(
        service, folder_id, safe_name, file_bytes, mime_type
    )

    # 3. Drive upload succeeded -> now (and only now) persist + audit + commit.
    document = Document(
        application_id=application.id,
        drive_file_id=drive_file_id,
        drive_folder_id=folder_id,
        original_filename=safe_name,
        mime_type=mime_type,
        file_size_bytes=size_bytes,
    )
    db.add(document)
    db.flush()  # assign document.id

    record_event(
        db,
        event_type=AuditEventType.RESUME_UPLOADED,
        action="Resume uploaded for an application.",
        entity_type="document",
        entity_id=document.id,
        user_id=None,  # reached from the public, unauthenticated flow
        new_state={"mime_type": mime_type, "file_size_bytes": size_bytes},
        metadata={
            # ids + non-PII facts only — never original_filename (possible PII).
            "document_id": str(document.id),
            "application_id": str(application.id),
            "job_id": str(job_id),
            "file_size_bytes": size_bytes,
            "mime_type": mime_type,
        },
    )

    db.commit()
    db.refresh(document)
    logger.info(
        "storage: stored document id=%s application=%s size=%d",
        document.id, application.id, size_bytes,
    )
    return document


def get_document(
    db: Session, document_id: uuid.UUID | str
) -> Document | None:
    """Return the document row by id, or ``None``."""
    return db.get(Document, document_id)


def list_documents_for_application(
    db: Session, application_id: uuid.UUID | str
) -> list[Document]:
    """Return every document for an application, oldest-first."""
    return list(
        db.execute(
            select(Document)
            .where(Document.application_id == application_id)
            .order_by(Document.uploaded_at)
        ).scalars().all()
    )


def get_document_download_bytes(
    document_id: uuid.UUID | str, db: Session, *, acting_user_id: uuid.UUID | str
) -> bytes:
    """Fetch the raw file content from the provider.

    HR/INTERNAL ONLY — returns candidate resume bytes. ``acting_user_id`` must
    resolve to an active internal :class:`~app.database.models.user.User`;
    :func:`~app.utils.authorization.require_internal_user` is checked **first**,
    before any DB read or Drive call. A caller from the public candidate app has
    no user and gets :class:`~app.utils.authorization.UnauthorizedError`.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is missing / malformed / unknown / inactive.
    DocumentTargetNotFoundError
        No document with ``document_id``.
    StorageConfigError
        Storage is not configured.
    DriveDownloadError
        The provider fetch failed.
    """
    require_internal_user(db, acting_user_id)

    document = db.get(Document, document_id)
    if document is None:
        raise DocumentTargetNotFoundError(
            f"No document with id {document_id!r}."
        )
    service, _ = _get_drive()
    return _drive_download_file(service, document.drive_file_id)
