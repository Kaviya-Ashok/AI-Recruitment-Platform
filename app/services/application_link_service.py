"""Application-link service — generate / revoke / regenerate a job's unique
candidate application link, close a job, and resolve a token.

Phase 1.4 scope: link lifecycle + a token-resolution contract for Phase 1.5's
public candidate entrypoint (nothing calls :func:`resolve_link` yet).

Transaction model (matches ``rubric_service``): the caller passes a ``Session``;
this service does the work, writes audit event(s) via
``audit_service.record_event`` (flush-not-commit), and issues a single
``commit`` per business action. The Streamlit UI wraps each call in
``session_scope()``.

SECURITY
--------
The ``token`` is credential-equivalent:
* generated with :mod:`secrets` (never ``random`` / anything seeded),
* never written to audit metadata, log lines, or error messages,
* protected by a UNIQUE DB index as a backstop to service-layer randomness.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models.application_link import (
    ApplicationLink,
    ApplicationLinkStatus,
)
from app.database.models.audit_event import AuditEventType
from app.database.models.job import Job, JobStatus
from app.services.audit_service import record_event

logger = logging.getLogger(__name__)

# Statuses from which a *first* link may be generated / a link may be regenerated.
_FIRST_GENERATION_STATUS = JobStatus.RUBRIC_APPROVED
_REGENERATION_STATUS = JobStatus.OPEN

_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> 43 URL-safe chars, 256 bits


class ApplicationLinkError(Exception):
    """Base class for application-link service failures."""


class LinkJobNotFoundError(ApplicationLinkError):
    """The target job does not exist."""


class ApplicationLinkNotFoundError(ApplicationLinkError):
    """The target link does not exist."""


class LinkStateError(ApplicationLinkError):
    """Operation not allowed in the current job/link state."""


class LinkGenerationError(ApplicationLinkError):
    """A unique token could not be generated (retry exhausted)."""


class LinkResolutionOutcome:
    """The four distinguishable results of :func:`resolve_link`."""

    VALID = "VALID"           # token active, job open -> candidate may apply
    NOT_FOUND = "NOT_FOUND"   # no such token
    INACTIVE = "INACTIVE"     # token exists but REVOKED / SUPERSEDED
    JOB_CLOSED = "JOB_CLOSED" # token active, but job is no longer OPEN

    ALL: frozenset[str] = frozenset({VALID, NOT_FOUND, INACTIVE, JOB_CLOSED})


@dataclass(frozen=True)
class LinkResolution:
    """Result of resolving a candidate application token.

    ``outcome`` is one of :class:`LinkResolutionOutcome`. ``link`` and ``job``
    are populated whenever they are known (``link`` is ``None`` only for
    ``NOT_FOUND``; ``job`` is ``None`` only for ``NOT_FOUND`` or the defensive
    orphan-link case).

    Caller guidance: a candidate-facing message must be **identical** for
    ``NOT_FOUND`` and ``INACTIVE`` ("this link is no longer valid") to avoid
    leaking whether a token ever existed. ``JOB_CLOSED`` may show a distinct
    "no longer accepting applications" message (the holder had a real link).
    """

    outcome: str
    link: ApplicationLink | None = None
    job: Job | None = None

    @property
    def is_valid(self) -> bool:
        return self.outcome == LinkResolutionOutcome.VALID


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _token_in_use(db: Session, token: str) -> bool:
    return db.execute(
        select(ApplicationLink.id).where(ApplicationLink.token == token)
    ).first() is not None


# --- reads ---------------------------------------------------------------


def get_active_link(
    db: Session, job_id: uuid.UUID | str
) -> ApplicationLink | None:
    """Return the job's single ACTIVE link, or ``None``."""
    return db.execute(
        select(ApplicationLink).where(
            ApplicationLink.job_id == job_id,
            ApplicationLink.status == ApplicationLinkStatus.ACTIVE,
        )
    ).scalar_one_or_none()


def get_job_for_valid_token(db: Session, token: str) -> Job | None:
    """Return the :class:`Job` for a token that resolves as VALID, else ``None``.

    Thin composition over :func:`resolve_link` for callers (Phase 1.5's public
    portal) that only need "the renderable job or nothing" — it does not
    reimplement any validation.
    """
    resolution = resolve_link(db, token)
    return resolution.job if resolution.is_valid else None


def resolve_link(db: Session, token: str) -> LinkResolution:
    """Resolve a candidate application token to a :class:`LinkResolution`.

    Read-only: no commit, no audit event. This is the contract Phase 1.5's
    public entrypoint will call.
    """
    if not token:
        return LinkResolution(LinkResolutionOutcome.NOT_FOUND)

    link = db.execute(
        select(ApplicationLink).where(ApplicationLink.token == token)
    ).scalar_one_or_none()

    if link is None:
        return LinkResolution(LinkResolutionOutcome.NOT_FOUND)

    job = db.get(Job, link.job_id)

    if link.status != ApplicationLinkStatus.ACTIVE:
        return LinkResolution(LinkResolutionOutcome.INACTIVE, link=link, job=job)

    if job is None:  # defensive: CASCADE should prevent an orphan link
        return LinkResolution(LinkResolutionOutcome.NOT_FOUND, link=link)

    if job.status != JobStatus.OPEN:
        return LinkResolution(
            LinkResolutionOutcome.JOB_CLOSED, link=link, job=job
        )

    return LinkResolution(LinkResolutionOutcome.VALID, link=link, job=job)


# --- generation --------------------------------------------------------


def generate_link(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
) -> ApplicationLink:
    """Generate a fresh ACTIVE application link for a job.

    Allowed when ``job.status`` is ``RUBRIC_APPROVED`` (first link) or ``OPEN``
    (regeneration). Any existing ACTIVE link is marked ``SUPERSEDED`` (kept for
    history). On first generation the job moves ``RUBRIC_APPROVED -> OPEN`` and
    a separate ``JOB_OPENED`` audit event is written.

    Raises
    ------
    LinkJobNotFoundError
        No job with ``job_id``.
    LinkStateError
        Rubric not approved yet, or the job is CLOSED.
    LinkGenerationError
        Could not produce a unique token after one retry (astronomically
        unlikely).
    """
    job = db.get(Job, job_id)
    if job is None:
        raise LinkJobNotFoundError(f"No job with id {job_id!r}.")

    if job.status == JobStatus.CLOSED:
        raise LinkStateError(
            "This job is closed. Reopening a closed job is not supported."
        )
    if job.status not in (_FIRST_GENERATION_STATUS, _REGENERATION_STATUS):
        raise LinkStateError(
            "Approve the evaluation rubric before generating an application link."
        )

    # Unique token: pre-check twice, then give up (UNIQUE index is the backstop).
    for _ in range(2):
        token = _generate_token()
        if not _token_in_use(db, token):
            break
    else:
        raise LinkGenerationError(
            "Could not generate a unique application link. Please try again."
        )

    superseded_prior_sequence: int | None = None
    existing_active = get_active_link(db, job.id)
    if existing_active is not None:
        existing_active.status = ApplicationLinkStatus.SUPERSEDED
        superseded_prior_sequence = existing_active.sequence_number

    prev_max_seq = db.execute(
        select(func.max(ApplicationLink.sequence_number)).where(
            ApplicationLink.job_id == job.id
        )
    ).scalar()
    is_first_link = prev_max_seq is None
    next_sequence = (prev_max_seq or 0) + 1

    link = ApplicationLink(
        job_id=job.id,
        token=token,
        sequence_number=next_sequence,
        status=ApplicationLinkStatus.ACTIVE,
        created_by=requested_by_user_id,
    )
    db.add(link)

    previous_job_status = job.status
    job_opened = job.status == JobStatus.RUBRIC_APPROVED
    if job_opened:
        job.status = JobStatus.OPEN
    db.flush()

    # NOTE: token value is deliberately absent from all audit metadata below.
    record_event(
        db,
        event_type=AuditEventType.APPLICATION_LINK_GENERATED,
        action=(
            f"Application link #{next_sequence} generated for '{job.title}'"
            + (
                f" (replaces #{superseded_prior_sequence})"
                if superseded_prior_sequence is not None
                else ""
            )
            + "."
        ),
        entity_type="application_link",
        entity_id=link.id,
        user_id=requested_by_user_id,
        previous_state={
            "job_status": previous_job_status,
            "superseded_prior_link_sequence": superseded_prior_sequence,
        },
        new_state={
            "sequence_number": next_sequence,
            "status": ApplicationLinkStatus.ACTIVE,
            "token_present": True,  # existence only, never the value
            "is_first_link": is_first_link,
            "job_status": job.status,
        },
    )

    if job_opened:
        # Distinct fact: the job is now accepting applications.
        record_event(
            db,
            event_type=AuditEventType.JOB_OPENED,
            action=f"Job '{job.title}' opened for applications.",
            entity_type="job",
            entity_id=job.id,
            user_id=requested_by_user_id,
            previous_state={"status": previous_job_status},
            new_state={"status": JobStatus.OPEN},
        )

    db.commit()
    db.refresh(link)
    logger.info(
        "application_link job=%s outcome=generated sequence=%d first=%s",
        job_id, next_sequence, is_first_link,
    )
    return link


# --- revocation --------------------------------------------------------


def revoke_link(
    db: Session,
    *,
    link_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
) -> ApplicationLink:
    """Revoke a job's ACTIVE link. Does NOT change ``job.status`` — the job
    stays OPEN with no active link until HR regenerates one.

    Raises
    ------
    ApplicationLinkNotFoundError
        No link with ``link_id``.
    LinkStateError
        The link is not ACTIVE (already revoked or superseded).
    """
    link = db.get(ApplicationLink, link_id)
    if link is None:
        raise ApplicationLinkNotFoundError(f"No link with id {link_id!r}.")
    if link.status != ApplicationLinkStatus.ACTIVE:
        raise LinkStateError(
            f"This link is already {link.status.lower()}; nothing to revoke."
        )

    link.status = ApplicationLinkStatus.REVOKED
    link.revoked_by = requested_by_user_id
    link.revoked_at = _now()
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.APPLICATION_LINK_REVOKED,
        action=f"Application link #{link.sequence_number} revoked.",
        entity_type="application_link",
        entity_id=link.id,
        user_id=requested_by_user_id,
        previous_state={"status": ApplicationLinkStatus.ACTIVE},
        new_state={
            "status": ApplicationLinkStatus.REVOKED,
            "sequence_number": link.sequence_number,
        },
    )
    db.commit()
    db.refresh(link)
    logger.info(
        "application_link link=%s outcome=revoked sequence=%d",
        link_id, link.sequence_number,
    )
    return link


# --- close job --------------------------------------------------------


def close_job(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
) -> Job:
    """Close an OPEN job. Stops link resolution via the job-status check in
    :func:`resolve_link` — **no write to application_links**.

    Terminal for the MVP: there is no "reopen" path this step.

    Raises
    ------
    LinkJobNotFoundError
        No job with ``job_id``.
    LinkStateError
        The job is not OPEN.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise LinkJobNotFoundError(f"No job with id {job_id!r}.")
    if job.status != JobStatus.OPEN:
        raise LinkStateError(
            f"Only an open job can be closed; this job is {job.status}."
        )

    job.status = JobStatus.CLOSED
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.JOB_CLOSED,
        action=f"Job '{job.title}' closed to applications.",
        entity_type="job",
        entity_id=job.id,
        user_id=requested_by_user_id,
        previous_state={"status": JobStatus.OPEN},
        new_state={"status": JobStatus.CLOSED},
    )
    db.commit()
    db.refresh(job)
    logger.info("application_link job=%s outcome=job_closed", job_id)
    return job
