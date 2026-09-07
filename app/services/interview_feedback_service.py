"""Interview-feedback service — capture and retrieval of what a *human*
interviewer recorded (CLAUDE.md §§7, 12, 22, 23, 24; Phase 4 Step 8).

ZERO AI. This module contains no Claude call, no prompt, no schema validation of
model output, and no computed score. It is pure data capture: a person types
what they observed, Python validates it, Postgres stores it verbatim. CLAUDE.md
§7: *"Do not rewrite human feedback as if it were AI-generated evidence."*

WHAT PYTHON DOES
----------------
* validates the actor (internal user, and explicitly **not** the SYSTEM
  pipeline actor),
* validates that the application and the interview guide exist and that the
  guide actually belongs to that application,
* validates the round, the recommendation, and every competency rating,
* checks ``(application_id, interview_round)`` uniqueness *before* insert so the
  caller gets a readable business error rather than an ``IntegrityError``,
* writes the feedback row + its rating rows + exactly one
  ``HUMAN_FEEDBACK_SUBMITTED`` audit event in a single transaction.

Everything is validated **before the first ``db.add``**, so a rejected
submission leaves no partial state to roll back — no orphan parent, no audit
event without its row.

AUTH — HR/INTERNAL ONLY, AND NEVER THE SYSTEM ACTOR
---------------------------------------------------
:func:`~app.utils.authorization.require_internal_user` is the first line of
every write and of every read that returns interviewer free text. But that guard
deliberately accepts *any* active internal user, **including the SYSTEM
pipeline actor** — ``app/utils/authorization.py`` says so explicitly and tells
callers that "anything that must distinguish 'a human decided this' from 'the
pipeline did this' must check the role or the actor id explicitly".

This module is exactly such a case, so it adds :func:`_reject_system_actor`.
An in-person interview has no automated equivalent; there is no code path in
this system that should ever attribute a human write-up to the pipeline, and
without this check a future automated caller could silently manufacture human
testimony. Attribution is always the **real** interviewer's user id.

SHORTLIST STATE IS DELIBERATELY NOT CHECKED
-------------------------------------------
This service never queries ``candidate_shortlist_entries``. Feedback requires a
guide, nothing more. An interview that actually happened must remain recordable
even if HR later removed the candidate from the shortlist — refusing the write-up
would destroy real evidence to satisfy a bookkeeping flag. This mirrors Step 7's
"a generated guide stays readable after an unshortlist".

WHAT THIS STEP DOES *NOT* DO
----------------------------
No AI/human comparison, no disagreement flag, no post-interview analysis, no
final scorecard, no final decision, and no ``Application.status`` write (this
module never touches that column, matching ``shortlist_service``). Those are
later steps. Only ``HUMAN_FEEDBACK_SUBMITTED`` is emitted here;
``HUMAN_INTERVIEW_COMPLETED`` and ``HUMAN_RECOMMENDATION_SUBMITTED`` remain
declared-but-unemitted.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
``notes``, ``competency_label`` and ``comment`` are an interviewer's free text
about a real person. They are stored on the row and **nowhere else**: never in
audit ``action`` / ``previous_state`` / ``new_state`` / ``event_metadata``,
never in a log line, never in an exception message. Audit metadata carries ids,
the round, the recommendation enum, and a rating count.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.candidate import Candidate
from app.database.models.interview_feedback import (
    MIN_INTERVIEW_ROUND,
    RATING_MAX,
    RATING_MIN,
    InterviewFeedback,
    InterviewFeedbackRating,
)
from app.database.models.interview_guide import InterviewGuide
from app.database.models.screening_evaluation import ScreeningRecommendation
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
from app.services.audit_service import record_event
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

# --- user-safe messages (never interpolate candidate / interviewer text) ----

_SYSTEM_ACTOR = (
    "Interview feedback records what a person observed, so it must be "
    "submitted by a signed-in interviewer. The automated pipeline account "
    "cannot submit it."
)
_NO_APPLICATION = "No such application — it may have been removed."
_NO_GUIDE = (
    "No interview guide was found. Generate the interview guide for this "
    "candidate before recording feedback."
)
_GUIDE_MISMATCH = (
    "That interview guide belongs to a different candidate's application. "
    "Feedback can only be recorded against this candidate's own guide."
)
_BAD_ROUND = (
    f"Interview round must be a whole number of {MIN_INTERVIEW_ROUND} or more."
)
_DUPLICATE_ROUND = (
    "This interview round has already been recorded for this application. "
    "Use the next round number, or review the existing entry below."
)
_BAD_RECOMMENDATION = (
    "Recommendation must be one of PROCEED, HOLD or REJECT."
)
_BAD_RATING_SHAPE = (
    "Each competency rating needs a label and a whole-number score."
)
_BAD_LABEL = "Every competency rating needs a competency name."
_BAD_RATING_VALUE = (
    f"Competency ratings must be a whole number from {RATING_MIN} to "
    f"{RATING_MAX}."
)


class InterviewFeedbackError(Exception):
    """Interview feedback could not be recorded or read. Carries a user-safe
    message; never wraps interviewer notes, competency labels, comments, or any
    other free text."""


class InterviewFeedbackTargetNotFoundError(InterviewFeedbackError):
    """No such application, or no such interview guide."""


class InterviewFeedbackPreconditionError(InterviewFeedbackError):
    """The supplied guide does not belong to the supplied application."""


class InterviewFeedbackDuplicateRoundError(InterviewFeedbackError):
    """This ``(application_id, interview_round)`` pair already exists."""


class InterviewFeedbackValidationError(InterviewFeedbackError):
    """The round, recommendation, or a competency rating is invalid."""


class InterviewFeedbackActorError(InterviewFeedbackError):
    """The actor passed the internal-user guard but may not author human
    feedback (the SYSTEM pipeline actor)."""


# --- row shapes (safe outside a Session) ------------------------------


@dataclass(frozen=True)
class InterviewFeedbackRatingView:
    competency_label: str
    rating: int
    comment: str | None


@dataclass(frozen=True)
class InterviewFeedbackView:
    """One feedback write-up, with display names already resolved.

    Frozen primitives only, so a Streamlit page can build this inside a
    ``session_scope()`` and render it after the session closes (the same
    pattern as ``InterviewGuideWithQuestions``).
    """

    feedback_id: uuid.UUID
    application_id: uuid.UUID
    interview_guide_id: uuid.UUID
    interview_round: int
    recommendation: str
    notes: str | None
    submitted_by_user_id: uuid.UUID
    submitted_by_name: str
    created_at: datetime
    ratings: list[InterviewFeedbackRatingView]


@dataclass(frozen=True)
class InterviewFeedbackContext:
    """Everything the feedback form needs to render itself read-only.

    ``candidate_name`` and ``interviewer`` are resolved here, never typed by
    the user: CLAUDE.md §7's record must say who was actually interviewed and
    who actually ran it.
    """

    application_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    interview_guide_id: uuid.UUID | None
    suggested_round: int
    feedback_count: int


# --- helpers ----------------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _reject_system_actor(actor: User) -> None:
    """Refuse the SYSTEM pipeline actor.

    ``require_internal_user`` accepts it by design (it is an active internal
    ``User``), so this is the explicit check its docstring instructs callers to
    make. Both the role and the well-known id are checked: the role is the
    semantic rule, and the id constant is a belt-and-braces guard in case a row
    is ever mis-seeded with the wrong role.
    """
    if actor.role is UserRole.SYSTEM or actor.id == SYSTEM_USER_ID:
        logger.warning(
            "interview_feedback rejected: SYSTEM actor user_id=%s", actor.id
        )
        raise InterviewFeedbackActorError(_SYSTEM_ACTOR)


def _require_human_actor(db: Session, user_id: uuid.UUID | str | None) -> User:
    """``require_internal_user`` plus the SYSTEM rejection, in that order."""
    actor = require_internal_user(db, user_id)
    _reject_system_actor(actor)
    return actor


def _is_int(value: Any) -> bool:
    """True for a real integer. ``bool`` is excluded on purpose: it is an
    ``int`` subclass in Python, and ``True`` must not silently become rating 1.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_optional_text(value: str | None) -> str | None:
    """Strip a free-text field; empty becomes ``None`` (never ``""``)."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _validated_ratings(
    ratings: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[str, int, str | None]]:
    """Validate every rating up front and return cleaned tuples.

    Raises :class:`InterviewFeedbackValidationError` on the first problem, before
    anything is written — so a bad rating in the middle of a batch persists
    nothing at all.

    An empty list is allowed: notes-only feedback is legitimate (CLAUDE.md §7
    lists notes, ratings, comments and a recommendation without requiring all
    four).
    """
    out: list[tuple[str, int, str | None]] = []
    for entry in ratings or []:
        if not isinstance(entry, Mapping):
            raise InterviewFeedbackValidationError(_BAD_RATING_SHAPE)

        label = entry.get("competency_label")
        if not isinstance(label, str) or not label.strip():
            raise InterviewFeedbackValidationError(_BAD_LABEL)

        value = entry.get("rating")
        if not _is_int(value):
            raise InterviewFeedbackValidationError(_BAD_RATING_VALUE)
        if not (RATING_MIN <= value <= RATING_MAX):
            raise InterviewFeedbackValidationError(_BAD_RATING_VALUE)

        comment = entry.get("comment")
        if comment is not None and not isinstance(comment, str):
            raise InterviewFeedbackValidationError(_BAD_RATING_SHAPE)

        out.append((label.strip(), value, _clean_optional_text(comment)))
    return out


def _ordered_feedback_rows(
    db: Session, application_id: uuid.UUID
) -> list[InterviewFeedback]:
    """All feedback for one application, newest first.

    ``created_at DESC, id DESC`` — the id is the deterministic tie-breaker for
    two rows written inside the same transaction, which share a ``now()``
    timestamp (Postgres ``now()`` is transaction-start time, so this is not a
    theoretical case).
    """
    return list(
        db.execute(
            select(InterviewFeedback)
            .where(InterviewFeedback.application_id == application_id)
            .order_by(
                InterviewFeedback.created_at.desc(),
                InterviewFeedback.id.desc(),
            )
        ).scalars().all()
    )


# --- advisory read ----------------------------------------------------


def get_suggested_next_interview_round(
    db: Session, application_id: uuid.UUID | str
) -> int:
    """``MAX(interview_round) + 1`` for the application, or 1 if none exist.

    **Advisory only.** This is a convenience default for the form; it is never
    consulted inside :func:`create_interview_feedback` and can never override or
    replace a submitted round. The interviewer decides which round they are
    writing up.

    Unguarded on purpose (the one read here that is): it returns a bare integer
    derived from a row count and discloses no candidate information, and the
    spec fixes this signature. Every read that returns interviewer free text is
    guarded.
    """
    highest = db.execute(
        select(func.max(InterviewFeedback.interview_round)).where(
            InterviewFeedback.application_id == _as_uuid(application_id)
        )
    ).scalar()
    return MIN_INTERVIEW_ROUND if highest is None else int(highest) + 1


# --- write ------------------------------------------------------------


def create_interview_feedback(
    db: Session,
    *,
    user_id: uuid.UUID | str,
    application_id: uuid.UUID | str,
    interview_guide_id: uuid.UUID | str,
    interview_round: int,
    recommendation: str,
    notes: str | None = None,
    ratings: Sequence[Mapping[str, Any]] | None = None,
) -> InterviewFeedback:
    """Record one human interviewer's write-up of one interview round.

    HR/INTERNAL ONLY, and never the SYSTEM actor. ``submitted_by_user_id`` is
    always the real signed-in interviewer.

    ``interview_round`` is stored exactly as supplied — never replaced by
    :func:`get_suggested_next_interview_round`, never derived from
    ``created_at`` or insertion order.

    Every check runs before the first write, so a rejected submission persists
    nothing: no feedback row, no rating rows, no audit event.

    Shortlist status is deliberately not consulted — an interview that happened
    stays recordable after an unshortlist.

    Parameters
    ----------
    ratings
        Mappings of ``competency_label`` (non-blank str), ``rating``
        (int, 1-5 inclusive) and optional ``comment``. May be empty.

    Raises
    ------
    UnauthorizedError
        ``user_id`` is not an active internal user.
    InterviewFeedbackActorError
        The actor is the SYSTEM pipeline account.
    InterviewFeedbackTargetNotFoundError
        No such application, or no such interview guide.
    InterviewFeedbackPreconditionError
        The guide belongs to a different application.
    InterviewFeedbackDuplicateRoundError
        This round is already recorded for this application.
    InterviewFeedbackValidationError
        Bad round, recommendation, or competency rating.
    """
    # 1. actor — internal user, and a human one.
    actor = _require_human_actor(db, user_id)

    application_uuid = _as_uuid(application_id)
    guide_uuid = _as_uuid(interview_guide_id)

    # 2. application exists.
    application = db.get(Application, application_uuid)
    if application is None:
        raise InterviewFeedbackTargetNotFoundError(_NO_APPLICATION)

    # 3. guide exists.
    guide = db.get(InterviewGuide, guide_uuid)
    if guide is None:
        raise InterviewFeedbackTargetNotFoundError(_NO_GUIDE)

    # 4. guide belongs to this application (never a raw FK/constraint error).
    if guide.application_id != application_uuid:
        logger.warning(
            "interview_feedback rejected: guide/application mismatch "
            "application=%s guide=%s",
            application_uuid, guide_uuid,
        )
        raise InterviewFeedbackPreconditionError(_GUIDE_MISMATCH)

    # 5. round is a whole number >= 1.
    if not _is_int(interview_round) or interview_round < MIN_INTERVIEW_ROUND:
        raise InterviewFeedbackValidationError(_BAD_ROUND)

    # 6. round not already recorded (proactive; the UNIQUE constraint remains
    #    the backstop, matching the applications-per-candidate precedent).
    already = db.execute(
        select(InterviewFeedback.id).where(
            InterviewFeedback.application_id == application_uuid,
            InterviewFeedback.interview_round == interview_round,
        )
    ).scalar_one_or_none()
    if already is not None:
        raise InterviewFeedbackDuplicateRoundError(_DUPLICATE_ROUND)

    # 7. recommendation is PROCEED / HOLD / REJECT. Validated against ALL, not
    #    AUTOMATED: a human may recommend REJECT (CLAUDE.md §7).
    if not isinstance(recommendation, str) or not ScreeningRecommendation.is_valid(
        recommendation
    ):
        raise InterviewFeedbackValidationError(_BAD_RECOMMENDATION)

    # 8. every rating, up front — nothing has been written yet.
    cleaned_ratings = _validated_ratings(ratings)

    # --- persist (one transaction) -------------------------------------
    # 9. the feedback row.
    feedback = InterviewFeedback(
        application_id=application_uuid,
        interview_guide_id=guide_uuid,
        submitted_by_user_id=actor.id,
        interview_round=interview_round,
        notes=_clean_optional_text(notes),
        recommendation=recommendation,
    )
    db.add(feedback)
    db.flush()

    # 10. the rating children.
    for label, value, comment in cleaned_ratings:
        db.add(
            InterviewFeedbackRating(
                interview_feedback_id=feedback.id,
                competency_label=label,
                rating=value,
                comment=comment,
            )
        )
    db.flush()

    # 11. exactly one audit event — structural metadata only. No notes, no
    #     competency label, no comment, no candidate name.
    record_event(
        db,
        event_type=AuditEventType.HUMAN_FEEDBACK_SUBMITTED,
        action=(
            f"Human interview feedback recorded for application "
            f"{application_uuid} (round {interview_round})."
        ),
        entity_type="application",
        entity_id=application_uuid,
        user_id=actor.id,
        new_state={
            "application_id": str(application_uuid),
            "interview_feedback_id": str(feedback.id),
            "interview_guide_id": str(guide_uuid),
            "interview_round": interview_round,
            "recommendation": recommendation,
            "rating_count": len(cleaned_ratings),
        },
    )

    # 12. one transaction for the row, its children and the audit event.
    db.commit()
    db.refresh(feedback)
    logger.info(
        "interview_feedback application=%s round=%s recommendation=%s "
        "ratings=%d actor=%s",
        application_uuid, interview_round, recommendation,
        len(cleaned_ratings), actor.id,
    )
    return feedback


# --- reads ------------------------------------------------------------


def get_interview_feedback_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[InterviewFeedback]:
    """Every feedback write-up for the application, newest first
    (``created_at DESC, id DESC``). HR/INTERNAL ONLY.

    Returns the full history: nothing is ever superseded or hidden, because a
    later round accompanies an earlier one rather than replacing it.
    """
    require_internal_user(db, acting_user_id)
    return _ordered_feedback_rows(db, _as_uuid(application_id))


def get_latest_interview_feedback_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> InterviewFeedback | None:
    """The single most recent write-up, or ``None``. HR/INTERNAL ONLY.

    "Most recent" is by the same deterministic ``created_at DESC, id DESC``
    ordering as :func:`get_interview_feedback_for_application` — it is **not**
    "the highest round number", because rounds can legitimately be written up
    out of order.
    """
    require_internal_user(db, acting_user_id)
    return db.execute(
        select(InterviewFeedback)
        .where(InterviewFeedback.application_id == _as_uuid(application_id))
        .order_by(
            InterviewFeedback.created_at.desc(), InterviewFeedback.id.desc()
        )
        .limit(1)
    ).scalar_one_or_none()


def list_feedback_views(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[InterviewFeedbackView]:
    """The history as frozen display projections, newest first.
    HR/INTERNAL ONLY.

    The UI-facing sibling of :func:`get_interview_feedback_for_application`:
    interviewer names are resolved here and the result contains only primitives,
    so a Streamlit page can build it inside ``session_scope()`` and render it
    after the session closes.
    """
    require_internal_user(db, acting_user_id)
    application_uuid = _as_uuid(application_id)
    rows = _ordered_feedback_rows(db, application_uuid)
    if not rows:
        return []

    ratings_by_feedback: dict[uuid.UUID, list[InterviewFeedbackRatingView]] = {}
    for rating in db.execute(
        select(InterviewFeedbackRating)
        .where(
            InterviewFeedbackRating.interview_feedback_id.in_(
                [r.id for r in rows]
            )
        )
        .order_by(InterviewFeedbackRating.competency_label)
    ).scalars().all():
        ratings_by_feedback.setdefault(rating.interview_feedback_id, []).append(
            InterviewFeedbackRatingView(
                competency_label=rating.competency_label,
                rating=rating.rating,
                comment=rating.comment,
            )
        )

    out: list[InterviewFeedbackView] = []
    for row in rows:
        author = db.get(User, row.submitted_by_user_id)
        out.append(
            InterviewFeedbackView(
                feedback_id=row.id,
                application_id=row.application_id,
                interview_guide_id=row.interview_guide_id,
                interview_round=row.interview_round,
                recommendation=row.recommendation,
                notes=row.notes,
                submitted_by_user_id=row.submitted_by_user_id,
                submitted_by_name=author.full_name if author else "—",
                created_at=row.created_at,
                ratings=ratings_by_feedback.get(row.id, []),
            )
        )
    return out


def get_feedback_context(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> InterviewFeedbackContext | None:
    """Read-only context for the feedback form, or ``None`` if no such
    application. HR/INTERNAL ONLY.

    Resolves the candidate from the application and the guide from the
    application, so the form can display both without a text input and without
    exposing a raw id. ``interview_guide_id`` is ``None`` when no guide has been
    generated yet — the page uses that to explain why the form is unavailable
    instead of failing on submit.
    """
    require_internal_user(db, acting_user_id)
    application_uuid = _as_uuid(application_id)

    application = db.get(Application, application_uuid)
    if application is None:
        return None

    candidate = db.get(Candidate, application.candidate_id)
    guide_id = db.execute(
        select(InterviewGuide.id).where(
            InterviewGuide.application_id == application_uuid
        )
    ).scalar_one_or_none()
    feedback_count = db.execute(
        select(func.count(InterviewFeedback.id)).where(
            InterviewFeedback.application_id == application_uuid
        )
    ).scalar_one()

    return InterviewFeedbackContext(
        application_id=application_uuid,
        candidate_name=candidate.full_name if candidate else "—",
        candidate_email=candidate.email if candidate else "—",
        interview_guide_id=guide_id,
        suggested_round=get_suggested_next_interview_round(db, application_uuid),
        feedback_count=int(feedback_count),
    )
