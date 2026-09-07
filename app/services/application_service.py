"""Application service — create and read a candidate's application to a job.

Phase 2 scope: the candidate x job x link row plus its ``CANDIDATE_APPLIED``
audit event. Resume upload / parsing / prequalification are later steps and are
deliberately absent here.

Transaction model (matches ``job_service`` / ``rubric_service`` /
``application_link_service``): :func:`create_application` is the **top-level
business action**. The caller passes a ``Session``; this function resolves the
candidate (same session, no commit), inserts the application row, writes the
audit event via ``audit_service.record_event`` (flush-not-commit), and issues a
**single ``commit``** so the application row and its audit row land together or
not at all (CLAUDE.md §20). The Streamlit / public-app UI wraps the call in
``session_scope()``.

This function assumes **no authenticated session** — it is reached from the
public, unauthenticated application flow, so the audit event has ``user_id=None``.

PRIVACY (CLAUDE.md §§23, 24)
---------------------------
``email``, ``phone`` and ``full_name`` are personal data. They are NEVER written
to audit metadata, log lines, or exceptions that reach a UI. Audit metadata for
``CANDIDATE_APPLIED`` references entities by id only
(``application_id`` / ``candidate_id`` / ``job_id`` / ``application_link_id``).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models.application import Application, ApplicationStatus
from app.database.models.application_link import ApplicationLink
from app.database.models.audit_event import AuditEventType
from app.database.models.job import Job
from app.services import candidate_service
from app.services.audit_service import record_event

logger = logging.getLogger(__name__)


class ApplicationError(Exception):
    """Base class for application-service failures."""


class ApplicationTargetNotFoundError(ApplicationError):
    """The target job or application link does not exist / does not match."""


class DuplicateApplicationError(ApplicationError):
    """This candidate has already applied to this job (one application per
    candidate per job — no re-application in this MVP)."""


# --- reads --------------------------------------------------------------


def get_application(
    db: Session, application_id: uuid.UUID | str
) -> Application | None:
    """Return the application by id, or ``None``."""
    return db.get(Application, application_id)


def list_applications_for_job(
    db: Session,
    job_id: uuid.UUID | str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[Application]:
    """Return applications for a job, newest-first.

    Called with no keyword arguments this behaves exactly as it always has:
    every application for the job, ``created_at`` descending. ``limit`` /
    ``offset`` are additive and read-only, for the Candidates page's
    "Show more" control.

    The ``id`` tie-break is what makes paging safe: Postgres ``now()`` is
    constant within a transaction, so applications created in one request share
    a ``created_at`` and would otherwise have no total order — a second page
    could then repeat or skip a row. It only orders rows whose relative order
    was previously undefined.
    """
    stmt = (
        select(Application)
        .where(Application.job_id == job_id)
        .order_by(Application.created_at.desc(), Application.id.desc())
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())


def count_applications_for_job(db: Session, job_id: uuid.UUID | str) -> int:
    """How many applications a job has. Read-only.

    Lets the Candidates page show a true total in its tab header without
    loading every row just to call ``len()`` on it.
    """
    return db.execute(
        select(func.count(Application.id)).where(Application.job_id == job_id)
    ).scalar_one()


def _find_existing_application(
    db: Session, *, candidate_id: uuid.UUID, job_id: uuid.UUID | str
) -> Application | None:
    return db.execute(
        select(Application).where(
            Application.candidate_id == candidate_id,
            Application.job_id == job_id,
        )
    ).scalar_one_or_none()


# --- create ------------------------------------------------------------


def create_application(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    application_link_id: uuid.UUID | str,
    email: str,
    full_name: str,
    phone: str | None,
) -> Application:
    """Create one application for a candidate (resolved/created by email) to a
    job, and record a ``CANDIDATE_APPLIED`` audit event — in one transaction.

    The candidate is resolved via
    :func:`candidate_service.get_or_create_candidate` on the **same session**;
    a brand-new candidate row and this application row commit together.

    Raises
    ------
    candidate_service.CandidateValidationError
        ``email`` or ``full_name`` is empty/whitespace.
    ApplicationTargetNotFoundError
        No job with ``job_id``, no link with ``application_link_id``, or the
        link belongs to a different job.
    DuplicateApplicationError
        ``(candidate_id, job_id)`` already has an application.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise ApplicationTargetNotFoundError(f"No job with id {job_id!r}.")

    link = db.get(ApplicationLink, application_link_id)
    if link is None:
        raise ApplicationTargetNotFoundError(
            f"No application link with id {application_link_id!r}."
        )
    if link.job_id != job.id:
        # Defensive: an application must be attributed to a link for *its* job.
        raise ApplicationTargetNotFoundError(
            "Application link does not belong to this job."
        )

    candidate = candidate_service.get_or_create_candidate(
        db, email=email, full_name=full_name, phone=phone
    )

    # Pre-check (friendly error); the unique constraint below is the backstop.
    if _find_existing_application(
        db, candidate_id=candidate.id, job_id=job.id
    ) is not None:
        raise DuplicateApplicationError(
            "An application for this job already exists for this candidate."
        )

    application = Application(
        candidate_id=candidate.id,
        job_id=job.id,
        application_link_id=link.id,
        status=ApplicationStatus.APPLIED,
    )
    db.add(application)
    try:
        db.flush()  # assign application.id; trigger the unique constraint
    except IntegrityError as exc:
        db.rollback()
        raise DuplicateApplicationError(
            "An application for this job already exists for this candidate."
        ) from exc

    record_event(
        db,
        event_type=AuditEventType.CANDIDATE_APPLIED,
        action=f"Candidate applied to '{job.title}'.",
        entity_type="application",
        entity_id=application.id,
        user_id=None,  # public, unauthenticated flow
        new_state={"status": application.status},
        metadata={
            # ids only — never email / phone / name (personal data).
            "application_id": str(application.id),
            "candidate_id": str(candidate.id),
            "job_id": str(job.id),
            "application_link_id": str(link.id),
        },
    )

    db.commit()
    db.refresh(application)
    logger.info(
        "application created id=%s job=%s candidate=%s link_seq=%s",
        application.id, job.id, candidate.id, link.sequence_number,
    )
    return application
