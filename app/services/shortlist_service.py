"""Shortlist service — HR "selected to proceed toward human interview"
(CLAUDE.md §§5, 6, 11, 12, 24; Phase 4 Step 6).

Shortlisting is a **reversible HR marker**, not a status transition and not the
final hiring decision. **No function here ever writes ``Application.status``**,
and there are **zero AI calls** anywhere in this module.

WHAT PYTHON DOES
----------------
* ``shortlist_candidate`` / ``unshortlist_candidate``: flip the one
  ``candidate_shortlist_entries`` row for ``(job_id, application_id)`` in place,
  capturing the decision's rubric-version partition, the candidate's
  rank_position at that moment, the HR actor and the timestamp. Emit exactly one
  audit event **per real state transition** — a no-op click writes nothing and
  emits nothing.
* ``get_shortlist_status_for_job``: current state for every application that has
  ever had a shortlist row on this job (shortlisted OR previously-shortlisted).
* ``get_ranking_staleness_for_partition``: how many candidates reached
  ``SCREENING_EVALUATED`` in this rubric-version partition *after* the partition's
  most recent ranking was generated — a non-blocking "your ranking may be out of
  date" signal, computed purely from existing timestamp columns.

AUTH — HR/INTERNAL ONLY
----------------------
Every function calls :func:`~app.utils.authorization.require_internal_user`
first. Attribution is always the **real HR user** id passed in — this module
never imports or uses the SYSTEM actor.

IDEMPOTENCY (CLAUDE.md §12 — one event per fact)
----------------------------------------------
``shortlist_candidate`` on an already-shortlisted candidate returns the existing
row unchanged: no row update, no ``decided_at`` rewrite, no second audit event.
``unshortlist_candidate`` on a missing / already-unshortlisted row is the same.

PRIVACY (CLAUDE.md §§12, 24)
--------------------------
``reason`` is optional free text stored ONLY on the row. It is NEVER placed in
an audit event's ``action`` / ``previous_state`` / ``new_state`` /
``event_metadata`` — those carry ids / enums / the boolean only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.screening_evaluation import ScreeningEvaluation
from app.database.models.screening_session import ScreeningSession
from app.services.audit_service import record_event
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

_NOT_EVALUATED = (
    "This candidate's screening has not been evaluated yet, so they cannot be "
    "shortlisted."
)


class ShortlistError(Exception):
    """Shortlist action could not be completed. User-safe message; never wraps
    the ``reason`` free text or any candidate content."""


class ShortlistTargetNotFoundError(ShortlistError):
    """No such application, or the application does not belong to the job."""


class ShortlistPreconditionError(ShortlistError):
    """The application is not in ``ApplicationStatus.SCREENING_EVALUATED``."""


# --- row shapes -------------------------------------------------------


@dataclass(frozen=True)
class ShortlistStatusRow:
    """Current shortlist state for one application on one job."""

    application_id: uuid.UUID
    is_shortlisted: bool
    reason: str | None
    rubric_version_id: uuid.UUID
    rank_position_at_decision: int | None
    decided_by_user_id: uuid.UUID
    decided_at: datetime


@dataclass(frozen=True)
class StalenessInfo:
    """How out-of-date a partition's ranking is relative to its evaluations."""

    stale_count: int
    latest_ranking_generated_at: datetime | None


# --- helpers ---------------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    cleaned = reason.strip()
    return cleaned or None


def _load_application_in_job(
    db: Session, job_id: uuid.UUID, application_id: uuid.UUID
) -> Application:
    application = db.get(Application, application_id)
    if application is None or application.job_id != job_id:
        raise ShortlistTargetNotFoundError(
            "No such application for this job."
        )
    return application


def _existing_entry(
    db: Session, job_id: uuid.UUID, application_id: uuid.UUID
) -> CandidateShortlistEntry | None:
    return db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.job_id == job_id,
            CandidateShortlistEntry.application_id == application_id,
        )
    ).scalar_one_or_none()


def _rank_position_now(
    db: Session,
    *,
    job_id: uuid.UUID,
    rubric_version_id: uuid.UUID,
    application_id: uuid.UUID,
) -> int | None:
    """The candidate's ``candidate_rankings.rank_position`` for this partition,
    or NULL if there is no ranking row (never ranked) or the row's position is
    itself NULL (ineligible)."""
    return db.execute(
        select(CandidateRanking.rank_position).where(
            CandidateRanking.job_id == job_id,
            CandidateRanking.rubric_version_id == rubric_version_id,
            CandidateRanking.application_id == application_id,
        )
    ).scalar_one_or_none()


def _safe_new_state(entry: CandidateShortlistEntry) -> dict:
    """Structured-only audit metadata — NEVER the ``reason`` free text."""
    return {
        "job_id": str(entry.job_id),
        "application_id": str(entry.application_id),
        "rubric_version_id": str(entry.rubric_version_id),
        "rank_position_at_decision": entry.rank_position_at_decision,
        "is_shortlisted": entry.is_shortlisted,
    }


# --- write actions -------------------------------------------------


def shortlist_candidate(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    application_id: uuid.UUID | str,
    rubric_version_id: uuid.UUID | str,
    reason: str | None = None,
    requested_by_user_id: uuid.UUID | str,
) -> CandidateShortlistEntry:
    """Mark a candidate as shortlisted for one job/rubric-version partition.

    HR/INTERNAL ONLY. Check-first idempotent: if the candidate is already
    shortlisted this returns the existing row unchanged with **no** row update
    and **no** audit event. Only a real transition (no row / False -> True)
    writes and emits ``CANDIDATE_SHORTLISTED`` exactly once.

    Precondition: the application must belong to the job and be in
    ``ApplicationStatus.SCREENING_EVALUATED`` (eligibility in the ranking sense
    is NOT checked — an ineligible candidate may still be an HR shortlist).

    Raises
    ------
    UnauthorizedError
    ShortlistTargetNotFoundError
    ShortlistPreconditionError
    """
    actor = require_internal_user(db, requested_by_user_id)
    job_uuid = _as_uuid(job_id)
    application_uuid = _as_uuid(application_id)
    rubric_uuid = _as_uuid(rubric_version_id)

    application = _load_application_in_job(db, job_uuid, application_uuid)
    if application.status != ApplicationStatus.SCREENING_EVALUATED:
        raise ShortlistPreconditionError(_NOT_EVALUATED)

    entry = _existing_entry(db, job_uuid, application_uuid)
    if entry is not None and entry.is_shortlisted:
        return entry  # no-op — already shortlisted

    now = datetime.now(timezone.utc)
    rank_position = _rank_position_now(
        db,
        job_id=job_uuid,
        rubric_version_id=rubric_uuid,
        application_id=application_uuid,
    )
    cleaned_reason = _clean_reason(reason)

    if entry is None:
        entry = CandidateShortlistEntry(
            job_id=job_uuid,
            application_id=application_uuid,
            rubric_version_id=rubric_uuid,
            is_shortlisted=True,
            reason=cleaned_reason,
            rank_position_at_decision=rank_position,
            decided_by_user_id=actor.id,
            decided_at=now,
        )
        db.add(entry)
    else:
        entry.is_shortlisted = True
        entry.rubric_version_id = rubric_uuid
        entry.rank_position_at_decision = rank_position
        entry.decided_by_user_id = actor.id
        entry.decided_at = now
        if cleaned_reason is not None:
            entry.reason = cleaned_reason
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.CANDIDATE_SHORTLISTED,
        action=(
            f"Candidate shortlisted for job {job_uuid} "
            f"(rubric version {rubric_uuid})."
        ),
        entity_type="application",
        entity_id=application_uuid,
        user_id=actor.id,
        new_state=_safe_new_state(entry),
    )
    db.commit()
    db.refresh(entry)
    logger.info(
        "candidate_shortlisted job=%s application=%s rubric_version=%s "
        "rank_at_decision=%s",
        job_uuid, application_uuid, rubric_uuid, rank_position,
    )
    return entry


def unshortlist_candidate(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    application_id: uuid.UUID | str,
    reason: str | None = None,
    requested_by_user_id: uuid.UUID | str,
) -> CandidateShortlistEntry | None:
    """Reverse a shortlist decision.

    HR/INTERNAL ONLY. Check-first idempotent: if there is no row, or the row is
    already ``is_shortlisted == False``, this returns the current state with no
    update and no audit event. Only a real ``True -> False`` transition writes
    and emits ``CANDIDATE_UNSHORTLISTED`` exactly once.

    ``rubric_version_id`` and ``rank_position_at_decision`` are **not** cleared —
    they stay informative as the context of the most recent decision. An
    optional ``reason`` may be recorded for the unshortlist too.

    Returns the row, or ``None`` if there was never one.
    """
    actor = require_internal_user(db, requested_by_user_id)
    job_uuid = _as_uuid(job_id)
    application_uuid = _as_uuid(application_id)

    # Confirm the target is real and belongs to the job (same discipline as the
    # shortlist path); status is not re-gated on the way out.
    _load_application_in_job(db, job_uuid, application_uuid)

    entry = _existing_entry(db, job_uuid, application_uuid)
    if entry is None or not entry.is_shortlisted:
        return entry  # no-op — nothing to reverse

    now = datetime.now(timezone.utc)
    cleaned_reason = _clean_reason(reason)

    entry.is_shortlisted = False
    entry.decided_by_user_id = actor.id
    entry.decided_at = now
    if cleaned_reason is not None:
        entry.reason = cleaned_reason
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.CANDIDATE_UNSHORTLISTED,
        action=f"Candidate unshortlisted for job {job_uuid}.",
        entity_type="application",
        entity_id=application_uuid,
        user_id=actor.id,
        new_state=_safe_new_state(entry),
    )
    db.commit()
    db.refresh(entry)
    logger.info(
        "candidate_unshortlisted job=%s application=%s", job_uuid, application_uuid
    )
    return entry


# --- reads ---------------------------------------------------------


def get_shortlist_entry_for_application(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    application_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> CandidateShortlistEntry | None:
    """The one shortlist-state row for ``(job_id, application_id)``, or ``None``.

    HR/INTERNAL ONLY. Returns the row regardless of ``is_shortlisted`` — callers
    that need "currently shortlisted" must check ``.is_shortlisted``. The row's
    ``rubric_version_id`` is the partition the decision was made against,
    captured at decision time (never re-derived).
    """
    require_internal_user(db, acting_user_id)
    return _existing_entry(db, _as_uuid(job_id), _as_uuid(application_id))


def get_shortlist_status_for_job(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> dict[uuid.UUID, ShortlistStatusRow]:
    """Current shortlist state for every application that has ANY shortlist row
    on this job — both currently shortlisted and previously-shortlisted-now-not
    (HR wants the full picture). Keyed by ``application_id`` for O(1) lookup
    while rendering a ranked list. HR/INTERNAL ONLY.
    """
    require_internal_user(db, acting_user_id)
    rows = db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.job_id == _as_uuid(job_id)
        )
    ).scalars().all()
    return {
        r.application_id: ShortlistStatusRow(
            application_id=r.application_id,
            is_shortlisted=r.is_shortlisted,
            reason=r.reason,
            rubric_version_id=r.rubric_version_id,
            rank_position_at_decision=r.rank_position_at_decision,
            decided_by_user_id=r.decided_by_user_id,
            decided_at=r.decided_at,
        )
        for r in rows
    }


def get_ranking_staleness_for_partition(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    rubric_version_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> StalenessInfo:
    """How many candidates in this rubric-version partition were evaluated
    *after* the partition's most recent ranking was generated. HR/INTERNAL ONLY.

    Non-blocking signal only — nothing here regenerates a ranking or gates a
    shortlist.

    Uses only existing timestamp columns: ``ScreeningEvaluation.created_at`` vs
    ``max(candidate_rankings.generated_at)`` for the partition. The
    ``Application -> ScreeningSession -> ScreeningEvaluation`` join shape mirrors
    ``ranking_service.list_evaluated_applications_for_job`` (that function
    returns heavier rows and lacks the partition / created_at filters, so this
    is a focused re-implementation of the same join, not a reuse).

    If no ranking has ever been generated for the partition,
    ``latest_ranking_generated_at`` is ``None`` and ``stale_count`` is ``0`` —
    "stale relative to a ranking that does not exist" is not meaningful; the UI
    prompts to generate a ranking first in that case.
    """
    require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)
    rubric_uuid = _as_uuid(rubric_version_id)

    latest = db.execute(
        select(func.max(CandidateRanking.generated_at)).where(
            CandidateRanking.job_id == job_uuid,
            CandidateRanking.rubric_version_id == rubric_uuid,
        )
    ).scalar()

    if latest is None:
        return StalenessInfo(stale_count=0, latest_ranking_generated_at=None)

    stale_count = db.execute(
        select(func.count(Application.id))
        .join(
            ScreeningSession,
            ScreeningSession.application_id == Application.id,
        )
        .join(
            ScreeningEvaluation,
            ScreeningEvaluation.screening_session_id == ScreeningSession.id,
        )
        .where(
            Application.job_id == job_uuid,
            Application.status == ApplicationStatus.SCREENING_EVALUATED,
            ScreeningEvaluation.rubric_version_id == rubric_uuid,
            ScreeningEvaluation.created_at > latest,
        )
    ).scalar_one()

    return StalenessInfo(
        stale_count=stale_count, latest_ranking_generated_at=latest
    )
