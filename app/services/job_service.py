"""Job service — create a job, analyse its JD, and read jobs back.

Phase 1.1: create + list + get.
Phase 1.2: ``analyze_jd`` (explicit, button-triggered AI requirement extraction)
and ``get_current_requirements``.

Transaction model (matches ``auth_service``): the caller passes a ``Session``;
this service does the inserts, writes the audit event, and ``commit``s so the
business rows and their audit row land together or not at all. The Streamlit UI
wraps each call in ``session_scope()``.
"""

from __future__ import annotations

import logging
import os
import uuid

from collections.abc import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai.claude_client import AIError, AIOutputError, get_structured_response
from app.ai.prompts.jd_analysis import build_jd_analysis_prompt
from app.ai.schemas.jd_analysis import JdAnalysisResult
from app.database.models.audit_event import AuditEventType
from app.database.models.job import JdInputMethod, Job, JobStatus
from app.database.models.job_requirement import JobRequirement
from app.services.audit_service import record_event
from app.utils.parsing import (
    DocumentParsingError,
    extract_text_from_docx,
    extract_text_from_pdf,
)
from app.utils.validation import sanitize_filename, validate_uploaded_file

logger = logging.getLogger(__name__)


class JobSort:
    """Sort orders accepted by :func:`list_jobs`.

    A read/display concern only — none of these affects scoring, ranking, or any
    business rule. ``NEWEST`` is the historic default and the value assumed by
    every caller that omits the argument.
    """

    NEWEST = "NEWEST"                      # created_at DESC
    OLDEST = "OLDEST"                      # created_at ASC
    RECENTLY_UPDATED = "RECENTLY_UPDATED"  # updated_at DESC

    ALL: frozenset[str] = frozenset({NEWEST, OLDEST, RECENTLY_UPDATED})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class JobValidationError(Exception):
    """Raised for invalid job input (missing title, empty JD, bad method)."""


class JobNotFoundError(Exception):
    """Raised when an operation targets a job id that does not exist."""


class JdAnalysisError(Exception):
    """Raised when AI JD analysis fails.

    When this is raised the job's ``status`` and ``job_requirements`` are
    guaranteed unchanged — the caller can safely offer a retry.
    """


def _coerce_input_method(value: JdInputMethod | str) -> JdInputMethod:
    if isinstance(value, JdInputMethod):
        return value
    try:
        return JdInputMethod(value)
    except ValueError as exc:
        raise JobValidationError(
            f"jd_input_method must be one of "
            f"{[m.value for m in JdInputMethod]}, got {value!r}"
        ) from exc


def _extract_jd_from_upload(file_bytes: bytes, safe_name: str) -> str:
    ext = os.path.splitext(safe_name)[1].lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_bytes)
    if ext == ".docx":
        return extract_text_from_docx(file_bytes)
    # validate_uploaded_file already guards this; defensive only.
    raise DocumentParsingError(f"No parser for extension {ext!r}.")


def create_job(
    db: Session,
    *,
    title: str,
    department: str | None,
    jd_input_method: JdInputMethod | str,
    jd_source_text: str | None = None,
    uploaded_file_bytes: bytes | None = None,
    uploaded_filename: str | None = None,
    created_by_user_id: uuid.UUID | None,
) -> Job:
    """Create a job (status ``DRAFT``) with its JD text stored, and audit it.

    Raises
    ------
    JobValidationError
        Missing title, unknown input method, missing payload for the chosen
        method, or empty/whitespace JD text (after extraction or paste).
    app.utils.validation.FileValidationError
        Upload fails extension / size / filename checks (FILE_UPLOAD only).
    app.utils.parsing.DocumentParsingError
        Uploaded PDF/DOCX cannot be read (FILE_UPLOAD only).
    """
    if not title or not title.strip():
        raise JobValidationError("Job title is required.")
    method = _coerce_input_method(jd_input_method)
    department_clean = department.strip() if department and department.strip() else None

    original_filename: str | None = None

    if method is JdInputMethod.FILE_UPLOAD:
        if not uploaded_file_bytes:
            raise JobValidationError("No file was uploaded.")
        if not uploaded_filename:
            raise JobValidationError("Uploaded file has no name.")
        # Raises FileValidationError on any violation.
        validate_uploaded_file(uploaded_filename, uploaded_file_bytes)
        original_filename = sanitize_filename(uploaded_filename)
        # Raises DocumentParsingError on unreadable content.
        jd_text = _extract_jd_from_upload(uploaded_file_bytes, original_filename)
    else:  # TEXT_PASTE
        if jd_source_text is None:
            raise JobValidationError("No JD text was provided.")
        jd_text = jd_source_text

    jd_text = (jd_text or "").strip()
    if not jd_text:
        raise JobValidationError(
            "The Job Description is empty. Paste the text or upload a readable file."
        )

    job = Job(
        title=title.strip(),
        department=department_clean,
        status=JobStatus.DRAFT,
        jd_source_text=jd_text,
        jd_input_method=method,
        jd_original_filename=original_filename,
        created_by=created_by_user_id,
    )
    db.add(job)
    db.flush()  # assign job.id

    method_desc = (
        f"pasted text ({len(jd_text)} chars)"
        if method is JdInputMethod.TEXT_PASTE
        else f"file upload ({original_filename})"
    )
    record_event(
        db,
        event_type=AuditEventType.JOB_CREATED,
        action=f"Job '{job.title}' created via {method_desc}.",
        entity_type="job",
        entity_id=job.id,
        user_id=created_by_user_id,
        new_state={
            "title": job.title,
            "department": job.department,
            "status": job.status,
            "jd_input_method": method.value,
        },
    )

    db.commit()
    db.refresh(job)
    return job


_ANALYSIS_UNUSABLE = (
    "AI analysis returned an unusable result. Please try again."
)
_ANALYSIS_FAILED = "AI analysis failed — please try again."


def analyze_jd(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
) -> Job:
    """Run AI requirement extraction on a job's JD and persist the result.

    Only ever called from an explicit user action (the "Analyze JD" button) —
    never automatically, per CLAUDE.md §§9, 24.

    On success: supersedes any prior requirements (old rows kept with
    ``is_current=False``), inserts the new set at ``source_version = max+1``,
    moves ``job.status`` to ``JD_ANALYZED``, and writes one ``JD_ANALYZED``
    audit event — all in a single commit.

    Raises
    ------
    JobNotFoundError
        No job with ``job_id``.
    JdAnalysisError
        The JD is empty, or the AI call / its output failed. ``job.status`` and
        ``job_requirements`` are left unchanged.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(f"No job with id {job_id!r}.")

    jd_text = (job.jd_source_text or "").strip()
    if not jd_text:
        raise JdAnalysisError("This job has no Job Description text to analyse.")

    prompt = build_jd_analysis_prompt(jd_text)
    try:
        result: JdAnalysisResult = get_structured_response(
            prompt, JdAnalysisResult, task_name="jd_analysis"
        )
    except AIOutputError as exc:
        logger.warning(
            "jd_analysis job=%s failure=validation (invalid/empty AI output after retries)",
            job_id,
        )
        raise JdAnalysisError(_ANALYSIS_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "jd_analysis job=%s failure=request kind=%s", job_id, type(exc).__name__
        )
        raise JdAnalysisError(_ANALYSIS_FAILED) from exc

    # --- supersession: keep history, bump version, mark new set current -----
    prev_max_version = db.execute(
        select(func.max(JobRequirement.source_version)).where(
            JobRequirement.job_id == job.id
        )
    ).scalar()
    next_version = (prev_max_version or 0) + 1

    for row in db.execute(
        select(JobRequirement).where(
            JobRequirement.job_id == job.id, JobRequirement.is_current.is_(True)
        )
    ).scalars():
        row.is_current = False

    for extracted in result.requirements:
        db.add(
            JobRequirement(
                job_id=job.id,
                requirement_type=extracted.requirement_type,
                category=extracted.category,
                requirement_text=extracted.requirement_text,
                source_version=next_version,
                is_current=True,
            )
        )

    previous_status = job.status
    job.status = JobStatus.JD_ANALYZED
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.JD_ANALYZED,
        action=(
            f"JD analysed for '{job.title}': "
            f"{len(result.requirements)} requirements extracted (v{next_version})."
        ),
        entity_type="job",
        entity_id=job.id,
        user_id=requested_by_user_id,
        previous_state={"status": previous_status},
        new_state={
            "status": JobStatus.JD_ANALYZED,
            "requirement_count": len(result.requirements),
            "source_version": next_version,
        },
    )

    db.commit()
    db.refresh(job)
    logger.info(
        "jd_analysis job=%s outcome=ok requirements=%d version=%d",
        job_id, len(result.requirements), next_version,
    )
    return job


def get_current_requirements(
    db: Session, job_id: uuid.UUID | str
) -> list[JobRequirement]:
    """Return only the current (``is_current=True``) requirements for a job.

    Ordered by a fixed requirement-type sequence, then creation order, so the UI
    can render grouped sections directly.
    """
    type_order = {
        t: i
        for i, t in enumerate(
            ["MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"]
        )
    }
    rows = db.execute(
        select(JobRequirement)
        .where(
            JobRequirement.job_id == job_id,
            JobRequirement.is_current.is_(True),
        )
        .order_by(JobRequirement.created_at)
    ).scalars().all()
    return sorted(
        rows, key=lambda r: (type_order.get(r.requirement_type, 99), r.created_at)
    )


def get_job(db: Session, job_id: uuid.UUID | str) -> Job | None:
    """Return the job by id, or ``None`` if it does not exist."""
    return db.get(Job, job_id)


def _escape_like(term: str) -> str:
    """Neutralise LIKE wildcards a user typed into a search box."""
    return (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _order_by(sort: str):
    """Ordering clause for ``sort``. Every order carries an ``id`` tie-break so
    limit/offset paging cannot skip or duplicate a row when two jobs share a
    timestamp (Postgres ``now()`` is constant within a transaction, so this is
    common)."""
    if sort == JobSort.OLDEST:
        return (Job.created_at.asc(), Job.id.asc())
    if sort == JobSort.RECENTLY_UPDATED:
        return (Job.updated_at.desc(), Job.id.desc())
    # JobSort.NEWEST, and any unrecognised value (a read path used by the UI
    # must never raise on a bad sort key — fall back to the historic default).
    return (Job.created_at.desc(), Job.id.desc())


def list_jobs(
    db: Session,
    *,
    created_by_user_id: uuid.UUID | None = None,
    statuses: Iterable[str] | None = None,
    search: str | None = None,
    sort: str = JobSort.NEWEST,
    limit: int | None = None,
    offset: int = 0,
) -> list[Job]:
    """Return jobs newest-first.

    Called with no keyword arguments this behaves exactly as it always has:
    every job, ``created_at`` descending, unlimited. Every parameter below is
    optional and read-side only — none of them changes which *fields* are
    returned, only which jobs and in what order.

    ``created_by_user_id`` optionally filters to that creator's jobs.
    ``statuses``  restricts to those :class:`JobStatus` values. An empty
                  iterable matches nothing (an explicit "none of these").
    ``search``    case-insensitive substring match against title OR department.
                  A NULL department still matches on title. LIKE wildcards the
                  user typed are escaped, not honoured. Blank/whitespace is
                  ignored.
    ``sort``      one of :class:`JobSort`; unknown values fall back to NEWEST.
    ``limit`` / ``offset``  paging for the Jobs page's "Show more" control.

    TODO (role-based scoping): the ``User`` model has a ``role`` (HR /
    HIRING_MANAGER / ADMIN) but no ownership/team model yet. For now any
    authenticated user sees all jobs; when team/ownership arrives, non-ADMIN
    roles should be scoped to their own jobs by default.
    """
    stmt = select(Job).order_by(*_order_by(sort))

    if created_by_user_id is not None:
        stmt = stmt.where(Job.created_by == created_by_user_id)

    if statuses is not None:
        stmt = stmt.where(Job.status.in_(list(statuses)))

    if search is not None and search.strip():
        pattern = f"%{_escape_like(search.strip())}%"
        stmt = stmt.where(
            or_(
                Job.title.ilike(pattern, escape="\\"),
                Job.department.ilike(pattern, escape="\\"),
            )
        )

    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)

    return list(db.execute(stmt).scalars().all())


def count_jobs_by_status(
    db: Session,
    *,
    created_by_user_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """``{status: count}`` over all jobs, in one grouped query.

    Read-only. Used for the Jobs page's tab count badges, which are deliberately
    *not* narrowed by the search box — they are a stable overview of the whole
    pipeline. Statuses with no jobs are simply absent from the mapping.
    """
    stmt = select(Job.status, func.count(Job.id)).group_by(Job.status)
    if created_by_user_id is not None:
        stmt = stmt.where(Job.created_by == created_by_user_id)
    return {status: count for status, count in db.execute(stmt).all()}
