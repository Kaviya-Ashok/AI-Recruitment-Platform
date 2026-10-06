"""Final human decision service — the hiring manager's PROCEED / HOLD / REJECT
(CLAUDE.md §§11, 12, 22, 23, 24; Phase 4 Step 11).

ZERO AI. This module has no Claude call, no prompt and no AI import. The decision
and its rationale are a person's words and are never sent to a model.

WHO MAY DECIDE
--------------
A real, active, internal user whose role is ``HIRING_MANAGER`` or ``ADMIN``
(:data:`DECIDER_ROLES`, :func:`role_may_decide`). The plain ``HR`` role may READ
decisions but not create one; the ``SYSTEM`` pipeline actor is rejected outright
(role AND well-known id, as in the other human-authored services). Unknown,
malformed and inactive users are rejected by ``require_internal_user``.

This is the first role-based gate in the codebase: ``require_internal_user``
deliberately accepts any active internal user (see its docstring). The role comes
from the ``User`` row it returns, so no authentication change was needed.

WHAT A DECISION IS
------------------
PROCEED, HOLD or REJECT (``ScreeningRecommendation.ALL`` — a human MAY choose
REJECT, unlike the automated paths) plus a REQUIRED rationale: trimmed, 10-2000
characters, stored exactly as written. Every submission INSERTS a new row; the
previous CURRENT row is marked SUPERSEDED in the same transaction. Nothing is ever
deleted or edited. At most one CURRENT row per application — enforced here first
and by a partial unique index; a lost race becomes a calm business error.

PRECONDITION
------------
At least one ``interview_feedback`` row. Shortlist status and final-ranking status
are NOT required (consistent with Step 8): a decision may be recorded for an
ineligible or incomplete candidate, whose status the UI shows plainly.

THE SNAPSHOT (what the manager could see)
-----------------------------------------
Copied as plain values at decision time: the CURRENT final-ranking entry (id,
score, rank, status, confidence) and its run's rubric version, the CURRENT
post-interview analysis (id, its AI recommendation) and the ids of the feedback
records that existed. All nullable. :func:`get_final_decision_staleness` compares
it with the present to report "based on earlier evidence". The AI recommendation
snapshot is display context only: nothing here compares it to the decision, and
there is no disagreement logic.

NO SIDE EFFECTS
---------------
No ``Application.status`` change, no shortlist or ranking write, no notification,
no email, no scheduling, no offer logic. REJECT is RECORDED ONLY — the UI says so.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
The rationale is stored on the row and nowhere else: never in audit metadata (only
its LENGTH), never in a log line, never in an exception message. Neither are
candidate or decider names. Names are joined at read time for display only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.final_decision import FinalDecision, FinalDecisionStatus
from app.database.models.final_ranking import (
    FinalRanking,
    FinalRankingEntry,
    FinalRankingStatus,
)
from app.database.models.interview_feedback import InterviewFeedback
from app.database.models.screening_evaluation import ScreeningRecommendation
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
from app.services.audit_service import record_event
from app.services.post_interview_service import get_current_post_interview_analysis
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

#: Roles allowed to RECORD a decision. HR may read, not decide.
DECIDER_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.HIRING_MANAGER, UserRole.ADMIN}
)

#: Rationale length limits, in characters, AFTER trimming.
RATIONALE_MIN_CHARS = 10
RATIONALE_MAX_CHARS = 2000

# --- user-safe messages (never interpolate names, the rationale or ids) -------

_SYSTEM_ACTOR = (
    "A final decision is made by a person, so the automated pipeline account "
    "cannot record one."
)
_NOT_A_DECIDER = (
    "Only a hiring manager or an admin can record the final decision. You can "
    "read decisions, but not make one."
)
_NO_APPLICATION = "No such application — it may have been removed."
_NO_FEEDBACK = (
    "Record at least one interview round's feedback before the final decision."
)
_BAD_DECISION = "Choose one decision: Proceed, Hold or Reject."
_RATIONALE_REQUIRED = (
    f"A rationale is required — at least {RATIONALE_MIN_CHARS} characters."
)
_RATIONALE_TOO_SHORT = (
    f"The rationale is too short: it needs at least {RATIONALE_MIN_CHARS} "
    "characters."
)
_RATIONALE_TOO_LONG = (
    f"The rationale is too long: it can be at most {RATIONALE_MAX_CHARS} "
    "characters."
)
_CONFLICT = (
    "Another decision was recorded just now. Please reload this candidate and "
    "review it before deciding again. Nothing was changed."
)


class FinalDecisionError(Exception):
    """A final decision could not be recorded or read. Carries a user-safe
    message; never the rationale or any name."""


class FinalDecisionActorError(FinalDecisionError):
    """The SYSTEM pipeline actor cannot decide."""


class FinalDecisionPermissionError(FinalDecisionError):
    """An internal user whose role may not record decisions (e.g. HR)."""


class FinalDecisionTargetNotFoundError(FinalDecisionError):
    """No such application."""


class FinalDecisionPreconditionError(FinalDecisionError):
    """The application has no interview feedback yet."""


class FinalDecisionValidationError(FinalDecisionError):
    """The decision value or the rationale is invalid."""


class FinalDecisionConflictError(FinalDecisionError):
    """Another decision won a race for the one CURRENT slot."""


# --- pure rules (no DB, no AI) -----------------------------------------------


def role_may_decide(role: UserRole | str | None) -> bool:
    """True for HIRING_MANAGER and ADMIN only. HR, SYSTEM and anything
    unrecognised are denied."""
    value = getattr(role, "value", role)
    return value in {r.value for r in DECIDER_ROLES}


def validate_decision(value: object) -> str:
    """The decision as one of PROCEED / HOLD / REJECT. A human may choose REJECT.

    Raises :class:`FinalDecisionValidationError` otherwise (including non-strings
    and lower-case values: the vocabulary is exact)."""
    if not isinstance(value, str) or not ScreeningRecommendation.is_valid(value):
        raise FinalDecisionValidationError(_BAD_DECISION)
    return value


def clean_rationale(value: object) -> str:
    """The rationale, trimmed and length-checked, otherwise EXACTLY as written —
    never summarised, rewritten or normalised. Raises
    :class:`FinalDecisionValidationError` if missing or outside 10-2000
    characters (counted after trimming)."""
    if not isinstance(value, str) or not value.strip():
        raise FinalDecisionValidationError(_RATIONALE_REQUIRED)
    trimmed = value.strip()
    if len(trimmed) < RATIONALE_MIN_CHARS:
        raise FinalDecisionValidationError(_RATIONALE_TOO_SHORT)
    if len(trimmed) > RATIONALE_MAX_CHARS:
        raise FinalDecisionValidationError(_RATIONALE_TOO_LONG)
    return trimmed


# --- views (frozen primitives; safe outside a Session) -----------------------


@dataclass(frozen=True)
class FinalDecisionView:
    decision_id: uuid.UUID
    application_id: uuid.UUID
    decision: str
    rationale: str
    status: str
    decided_by_user_id: uuid.UUID
    decided_by_name: str          # joined at read time; never stored on the row
    created_at: datetime
    superseded_at: datetime | None
    # snapshot
    final_ranking_entry_id: uuid.UUID | None
    post_interview_analysis_id: uuid.UUID | None
    final_score: Decimal | None
    final_rank: int | None
    entry_status: str | None
    final_confidence: str | None
    rubric_version_id: uuid.UUID | None
    ai_recommendation_snapshot: str | None
    interview_feedback_ids: tuple[str, ...]


@dataclass(frozen=True)
class DecisionStaleness:
    """Whether the evidence has moved on since a decision was recorded."""

    is_stale: bool
    ranking_changed: bool
    analysis_changed: bool
    new_feedback_count: int

    @property
    def reasons(self) -> tuple[str, ...]:
        out = []
        if self.ranking_changed:
            out.append("the final ranking was regenerated")
        if self.analysis_changed:
            out.append("the post-interview analysis changed")
        if self.new_feedback_count:
            out.append(
                f"{self.new_feedback_count} interview feedback record(s) were "
                "added"
            )
        return tuple(out)


# --- helpers -----------------------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _require_decider(db: Session, user_id: uuid.UUID | str | None) -> User:
    """``require_internal_user``, then the SYSTEM rejection, then the role gate —
    in that order, so an unknown user learns nothing about roles."""
    actor = require_internal_user(db, user_id)
    if actor.role is UserRole.SYSTEM or actor.id == SYSTEM_USER_ID:
        logger.warning("final_decision rejected: SYSTEM actor user_id=%s", actor.id)
        raise FinalDecisionActorError(_SYSTEM_ACTOR)
    if not role_may_decide(actor.role):
        logger.warning(
            "final_decision denied: role=%s user_id=%s", actor.role.value, actor.id
        )
        raise FinalDecisionPermissionError(_NOT_A_DECIDER)
    return actor


def _feedback_ids(db: Session, application_id: uuid.UUID) -> list[str]:
    return sorted(
        str(i) for i in db.execute(
            select(InterviewFeedback.id).where(
                InterviewFeedback.application_id == application_id
            )
        ).scalars().all()
    )


def _current_ranking_entry(
    db: Session, application_id: uuid.UUID
) -> tuple[FinalRankingEntry, FinalRanking] | None:
    """The application's entry in its latest CURRENT final-ranking run (read-only)."""
    row = db.execute(
        select(FinalRankingEntry, FinalRanking)
        .join(FinalRanking, FinalRanking.id == FinalRankingEntry.final_ranking_id)
        .where(
            FinalRankingEntry.application_id == application_id,
            FinalRanking.status == FinalRankingStatus.CURRENT,
        )
        .order_by(FinalRanking.created_at.desc(), FinalRanking.id.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row is not None else None


def _current_decision_row(
    db: Session, application_id: uuid.UUID, *, for_update: bool = False
) -> FinalDecision | None:
    stmt = (
        select(FinalDecision)
        .where(
            FinalDecision.application_id == application_id,
            FinalDecision.status == FinalDecisionStatus.CURRENT,
        )
        .order_by(FinalDecision.created_at.desc(), FinalDecision.id.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def _to_view(db: Session, row: FinalDecision) -> FinalDecisionView:
    author = db.get(User, row.decided_by_user_id)
    return FinalDecisionView(
        decision_id=row.id,
        application_id=row.application_id,
        decision=row.decision,
        rationale=row.rationale,
        status=row.status,
        decided_by_user_id=row.decided_by_user_id,
        decided_by_name=(author.full_name if author else "—"),
        created_at=row.created_at,
        superseded_at=row.superseded_at,
        final_ranking_entry_id=row.final_ranking_entry_id,
        post_interview_analysis_id=row.post_interview_analysis_id,
        final_score=row.final_score,
        final_rank=row.final_rank,
        entry_status=row.entry_status,
        final_confidence=row.final_confidence,
        rubric_version_id=row.rubric_version_id,
        ai_recommendation_snapshot=row.ai_recommendation_snapshot,
        interview_feedback_ids=tuple(str(i) for i in (row.interview_feedback_ids or [])),
    )


# --- write -------------------------------------------------------------------


def record_final_decision(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    decision: str,
    rationale: str,
    acting_user_id: uuid.UUID | str,
) -> FinalDecisionView:
    """Record (or revise) the final human decision for one application.

    Validation order: actor (internal, not SYSTEM, active, role allowed) ->
    application exists -> at least one feedback row -> decision valid -> rationale
    rules. Only then is the snapshot gathered, the previous CURRENT row (if any)
    marked SUPERSEDED, the new row inserted and ONE audit event recorded, in a
    single commit. Nothing is persisted on any failure.

    Side effects: none beyond those rows — no status change, no notification.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is missing / unknown / inactive.
    FinalDecisionActorError
        The actor is the SYSTEM pipeline account.
    FinalDecisionPermissionError
        The actor's role is not HIRING_MANAGER or ADMIN.
    FinalDecisionTargetNotFoundError
        No such application.
    FinalDecisionPreconditionError
        No interview feedback has been recorded.
    FinalDecisionValidationError
        Bad decision value or rationale.
    FinalDecisionConflictError
        Another decision took the CURRENT slot first; nothing was changed.
    """
    actor = _require_decider(db, acting_user_id)

    application = db.get(Application, _as_uuid(application_id))
    if application is None:
        raise FinalDecisionTargetNotFoundError(_NO_APPLICATION)

    feedback_ids = _feedback_ids(db, application.id)
    if not feedback_ids:
        raise FinalDecisionPreconditionError(_NO_FEEDBACK)

    decision_value = validate_decision(decision)
    rationale_text = clean_rationale(rationale)

    # --- snapshot: what the decider could see ---------------------------
    ranking = _current_ranking_entry(db, application.id)
    entry, run = ranking if ranking is not None else (None, None)
    analysis = get_current_post_interview_analysis(
        db, application.id, acting_user_id=actor.id
    )

    # One clock reading, kept strictly after the previous decision's so the
    # ordering never depends on a tie-break (two writes in one transaction would
    # otherwise share Postgres' transaction-start now()).
    now = datetime.now(timezone.utc)
    previous = _current_decision_row(db, application.id, for_update=True)
    if previous is not None and now <= previous.created_at:
        now = previous.created_at + timedelta(microseconds=1)

    try:
        if previous is not None:
            previous.status = FinalDecisionStatus.SUPERSEDED
            previous.superseded_at = now
            # Flush the supersede BEFORE the insert so the partial unique index
            # never sees two CURRENT rows.
            db.flush()

        row = FinalDecision(
            application_id=application.id,
            decided_by_user_id=actor.id,
            decision=decision_value,
            rationale=rationale_text,
            status=FinalDecisionStatus.CURRENT,
            final_ranking_entry_id=entry.id if entry is not None else None,
            post_interview_analysis_id=(
                analysis.analysis_id if analysis is not None else None
            ),
            final_score=entry.final_score if entry is not None else None,
            final_rank=entry.rank if entry is not None else None,
            entry_status=entry.entry_status if entry is not None else None,
            final_confidence=entry.final_confidence if entry is not None else None,
            rubric_version_id=run.rubric_version_id if run is not None else None,
            ai_recommendation_snapshot=(
                analysis.ai_recommendation if analysis is not None else None
            ),
            interview_feedback_ids=feedback_ids,
            created_at=now,
        )
        db.add(row)
        db.flush()

        record_event(
            db,
            event_type=AuditEventType.FINAL_DECISION_SUBMITTED,
            action=(
                f"Final human decision recorded for application "
                f"{application.id}: {decision_value}."
            ),
            entity_type="application",
            entity_id=application.id,
            user_id=actor.id,
            # Structural only: ids, enum values, counts. NEVER the rationale text
            # (only its LENGTH), a name, or any note.
            new_state={
                "application_id": str(application.id),
                "job_id": str(application.job_id),
                "final_decision_id": str(row.id),
                "decision": decision_value,
                "previous_decision_id": (
                    str(previous.id) if previous is not None else None
                ),
                "previous_decision": (
                    previous.decision if previous is not None else None
                ),
                "is_revision": previous is not None,
                "final_ranking_entry_id": (
                    str(entry.id) if entry is not None else None
                ),
                "post_interview_analysis_id": (
                    str(analysis.analysis_id) if analysis is not None else None
                ),
                "rationale_length": len(rationale_text),
                "feedback_count": len(feedback_ids),
            },
        )
        db.commit()
    except IntegrityError as exc:
        # The partial unique index is the backstop for a lost race: roll back the
        # whole transaction (supersede included) so no partial state remains.
        db.rollback()
        logger.warning(
            "final_decision conflict application=%s actor=%s", application.id, actor.id
        )
        raise FinalDecisionConflictError(_CONFLICT) from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(row)
    logger.info(
        "final_decision application=%s decision=%s revision=%s actor=%s",
        application.id, decision_value, previous is not None, actor.id,
    )
    return _to_view(db, row)


# --- reads (HR/INTERNAL ONLY; HR may read, not decide) ------------------------


def get_current_final_decision(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> FinalDecisionView | None:
    """The application's CURRENT decision, or ``None``."""
    require_internal_user(db, acting_user_id)
    row = _current_decision_row(db, _as_uuid(application_id))
    return _to_view(db, row) if row is not None else None


def list_final_decision_history(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[FinalDecisionView]:
    """Every decision for the application, CURRENT and SUPERSEDED, newest first
    (``created_at DESC, id DESC``)."""
    require_internal_user(db, acting_user_id)
    rows = db.execute(
        select(FinalDecision)
        .where(FinalDecision.application_id == _as_uuid(application_id))
        .order_by(FinalDecision.created_at.desc(), FinalDecision.id.desc())
    ).scalars().all()
    return [_to_view(db, r) for r in rows]


def list_current_decisions_for_job(
    db: Session,
    job_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> dict[uuid.UUID, FinalDecisionView]:
    """``{application_id: CURRENT decision}`` for every application of the job
    that has one — for the read-only line on the final-ranking rows."""
    require_internal_user(db, acting_user_id)
    rows = db.execute(
        select(FinalDecision)
        .join(Application, Application.id == FinalDecision.application_id)
        .where(
            Application.job_id == _as_uuid(job_id),
            FinalDecision.status == FinalDecisionStatus.CURRENT,
        )
    ).scalars().all()
    return {r.application_id: _to_view(db, r) for r in rows}


def get_final_decision_staleness(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> DecisionStaleness:
    """Is the CURRENT decision "based on earlier evidence"? Read-only.

    True if the application now has a different CURRENT final-ranking entry, a
    different CURRENT analysis, or feedback records the snapshot did not include.
    With no decision there is nothing to be stale relative to."""
    actor = require_internal_user(db, acting_user_id)
    app_uuid = _as_uuid(application_id)
    row = _current_decision_row(db, app_uuid)
    if row is None:
        return DecisionStaleness(False, False, False, 0)

    ranking = _current_ranking_entry(db, app_uuid)
    entry_id = ranking[0].id if ranking is not None else None
    analysis = get_current_post_interview_analysis(
        db, app_uuid, acting_user_id=actor.id
    )
    analysis_id = analysis.analysis_id if analysis is not None else None
    recorded = {str(i) for i in (row.interview_feedback_ids or [])}
    new_feedback = len(set(_feedback_ids(db, app_uuid)) - recorded)

    ranking_changed = entry_id != row.final_ranking_entry_id
    analysis_changed = analysis_id != row.post_interview_analysis_id
    return DecisionStaleness(
        is_stale=bool(ranking_changed or analysis_changed or new_feedback),
        ranking_changed=ranking_changed,
        analysis_changed=analysis_changed,
        new_feedback_count=new_feedback,
    )
