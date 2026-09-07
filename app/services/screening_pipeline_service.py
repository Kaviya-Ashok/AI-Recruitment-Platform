"""Automatic screening pipeline — create session -> resume parsing ->
prequalification -> mark ready, run without any HR click (CLAUDE.md §2A items
1, 6, 7).

WHY A NEW MODULE (and not ``candidate_portal_service``)
-------------------------------------------------------
``candidate_portal_service`` is documented as owning *no* business rules — pure
composition returning primitives for the public page. This module does own
rules: stage sequencing, the ``SCREENING_IN_PROGRESS`` status transition, token
minting, and the ``AI_SCREENING_STARTED`` audit event. It also authenticates as
the SYSTEM actor, which the portal service deliberately never does. Mixing the
two would erode the portal's "no business rules" contract, so this is a
separate service. The portal/page calls in; nothing here reads
``st.session_state``.

STAGE ORDER — SESSION FIRST (Step 2), ROUND-1 QUESTIONS LAST (Step 3)
-------------------------------------------------------------------
1. ``SCREENING_SESSION``  — ensure the ``screening_sessions`` row exists
   (status ``PENDING``). This is FIRST so the ``access_token`` exists from the
   very first pipeline invocation: a candidate who closes the tab, or an HR
   user recovering a stall, always has a durable row + credential to resume
   with. ``AI_SCREENING_STARTED`` is emitted here.
2. ``RESUME_PARSING``     — reuse ``resume_parsing_service.parse_resume``.
3. ``PREQUALIFICATION``   — reuse ``prequalification_service.prequalify_application``.
4. ``FINALIZE``           — once 2 and 3 are both done, flip the session to
   ``READY_FOR_ROUND_1`` and the application to ``SCREENING_IN_PROGRESS``.
5. ``ROUND_1_QUESTIONS``  — generate + persist round-1 screening questions
   (``screening_question_service.generate_round_questions``); the session moves
   to ``ROUND_1_IN_PROGRESS``. This is where the *automatic* pipeline ends —
   the candidate then answers the questions, and round 2 is triggered by that
   (candidate action), NOT by this page-load stepper.

``AI_SCREENING_COMPLETED`` is RESERVED for real screening completion and is
never emitted by this module (it is emitted by ``screening_question_service``
when the last round-2 answer lands, or round 2 yields zero follow-ups). The
FINALIZE transition has no dedicated audit event; ``ROUND_1_QUESTIONS`` emits
``SCREENING_QUESTIONS_GENERATED`` from the question service.

Stage "done" checks use ``ScreeningSessionStatus.at_least(...)`` — a session
only moves forward through ``ScreeningSessionStatus.ORDER``, so "is the session
at or past state X?" is the robust way to know a stage completed even after
later stages have advanced the status further.

IDEMPOTENCY IS THE POINT
------------------------
Streamlit re-runs the whole script on every interaction, the candidate can
refresh mid-processing, the retry button re-enters here, and an HR user may
manually kick a stalled pipeline. So every stage is **check-first**:

* session      — skipped if a ``screening_sessions`` row exists. Never
  delete-and-recreate: a session must not be replaced once the candidate may
  already hold its ``access_token``. The ``UNIQUE(application_id)`` constraint
  is the real guarantee — two overlapping calls can both pass the check, and
  the loser catches the ``IntegrityError`` for that specific constraint, rolls
  back its insert, re-fetches the winner's row, and returns the correct current
  state (never a spurious FAILED).
* resume parsing / prequalification — skipped if the row already exists (no AI
  call). ``parse_resume`` / ``prequalify_application`` also carry their own
  ``force=False`` "already exists" guards as a second layer.
* finalize     — a no-op if the session is already ``READY_FOR_ROUND_1``.

ONE STAGE PER CALL
------------------
:func:`advance_screening_pipeline` runs **at most one** pending stage and
returns, so the public page can show honest per-stage progress via
``st.rerun()``. :func:`run_screening_pipeline` is the run-to-completion wrapper
(HR manual recovery, tests).

PREQUALIFICATION DOES NOT GATE SCREENING
----------------------------------------
CLAUDE.md §2A item 7: PASS, FAIL and UNKNOWN all proceed identically to
``READY_FOR_ROUND_1``. This module never reads the *contents* of the
prequalification result — only whether the stage has run. There is deliberately
no branch on its outcome anywhere here.

AUTHORIZATION / AUDIT
---------------------
Every guarded downstream call receives ``get_system_user_id(db)``. No event
written here ever uses ``user_id=None``. HR manual recovery
(:func:`resume_stalled_pipeline`) authorizes the *click* against the HR user
but still runs the pipeline as the SYSTEM actor — a human kicking it does not
make it a human action.

PRIVACY / LOGGING (CLAUDE.md §§19, 23, 24)
------------------------------------------
``access_token`` is credential-equivalent: never logged, never in audit
metadata, never in a returned message or resolver outcome. Candidate names,
emails, resume text and per-criterion judgments are likewise never logged here
— ids, stage names and counts only.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.services.audit_service import record_event
from app.services.prequalification_service import (
    PrequalificationError,
    get_prequalification_for_application,
    prequalify_application,
)
from app.services.resume_parsing_service import (
    ResumeParsingError,
    get_extraction_for_document,
    parse_resume,
)
from app.services.screening_question_service import (
    ScreeningQuestionError,
    generate_round_questions,
)
from app.services.storage_service import list_documents_for_application
from app.services.system_user_service import (
    SystemUserMissingError,
    get_system_user_id,
)
from app.utils.authorization import UnauthorizedError, require_internal_user

logger = logging.getLogger(__name__)

_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> 43 URL-safe chars, 256 bits

#: Same shape as ``candidate_portal_service._TOKEN_RE`` — a generous but bounded
#: URL-safe range so an entropy change does not break resolution, checked before
#: any DB access.
_ACCESS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

#: A ``screening_sessions`` row that has not been touched for longer than this,
#: while still in a non-terminal, pre-completion state, is treated as a stalled
#: pipeline / abandoned screening for the HR recovery UI. Named constant, not a
#: magic number — tune here. (Step 2: pre-round-1 only. Step 3: also mid-round —
#: ``submit_answer`` bumps ``screening_sessions.updated_at`` so the clock stays
#: honest while a candidate is actively answering.)
STALLED_PIPELINE_THRESHOLD = timedelta(minutes=15)

#: Session states that count as "stuck" once past the threshold. Terminal
#: (``SCREENING_COMPLETE``) is never stalled.
_STALLABLE_STATUSES: tuple[str, ...] = (
    ScreeningSessionStatus.PENDING,
    ScreeningSessionStatus.ROUND_1_IN_PROGRESS,
    ScreeningSessionStatus.ROUND_1_COMPLETE,
    ScreeningSessionStatus.ROUND_2_IN_PROGRESS,
)

#: Session states from which a ``?screening=`` link is still resumable. Anything
#: else (only ``SCREENING_COMPLETE``) resolves as INVALID — indistinguishable
#: from a nonexistent token.
_RESUMABLE_STATUSES: frozenset[str] = frozenset(
    {
        ScreeningSessionStatus.PENDING,
        ScreeningSessionStatus.READY_FOR_ROUND_1,
        ScreeningSessionStatus.ROUND_1_IN_PROGRESS,
        ScreeningSessionStatus.ROUND_1_COMPLETE,
        ScreeningSessionStatus.ROUND_2_IN_PROGRESS,
    }
)

_UNIQUE_APPLICATION_CONSTRAINT = "uq_screening_sessions_application"


class PipelineStage:
    """The automatic-pipeline stages, in execution order (session FIRST — Step 2;
    round-1 questions LAST — Step 3). See the module docstring."""

    SCREENING_SESSION = "SCREENING_SESSION"
    RESUME_PARSING = "RESUME_PARSING"
    PREQUALIFICATION = "PREQUALIFICATION"
    FINALIZE = "FINALIZE"
    ROUND_1_QUESTIONS = "ROUND_1_QUESTIONS"

    ORDER: tuple[str, ...] = (
        SCREENING_SESSION,
        RESUME_PARSING,
        PREQUALIFICATION,
        FINALIZE,
        ROUND_1_QUESTIONS,
    )


class PipelineOutcome:
    """Result of one :func:`advance_screening_pipeline` call."""

    COMPLETED = "COMPLETED"      # automatic pipeline done (round-1 questions ready)
    IN_PROGRESS = "IN_PROGRESS"  # a stage ran (or was skipped); more remain
    FAILED = "FAILED"            # the pending stage failed; safe to retry

    ALL: frozenset[str] = frozenset({COMPLETED, IN_PROGRESS, FAILED})


class ScreeningPipelineError(Exception):
    """The pipeline could not run. Carries a candidate-safe message only —
    never resume text, criterion text, evidence, or the access token."""


class PipelineApplicationNotFoundError(ScreeningPipelineError):
    """No application with the given id."""


# Candidate-facing copy. Deliberately vague about internals.
_NO_DOCUMENT = (
    "We don't have your résumé on file yet, so we can't continue. Please "
    "upload it and try again."
)
_STAGE_FAILED = {
    PipelineStage.SCREENING_SESSION: (
        "We couldn't finish setting up your screening just now."
    ),
    PipelineStage.RESUME_PARSING: (
        "We couldn't finish reviewing your résumé just now."
    ),
    PipelineStage.PREQUALIFICATION: (
        "We couldn't finish checking your application just now."
    ),
    PipelineStage.FINALIZE: (
        "We couldn't finish preparing your screening just now."
    ),
    PipelineStage.ROUND_1_QUESTIONS: (
        "We couldn't finish preparing your screening questions just now."
    ),
}
_UNAVAILABLE = (
    "Automated screening is temporarily unavailable. Your application is "
    "safe — please try again shortly."
)


@dataclass(frozen=True)
class PipelineState:
    """Read-only snapshot of how far the pipeline has got. No side effects.

    NOTE: deliberately carries no ``access_token`` — it is a credential and this
    object is passed around / potentially logged.
    """

    application_id: str
    completed_stages: tuple[str, ...] = ()
    next_stage: str | None = None
    screening_session_id: str | None = None
    screening_session_status: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.next_stage is None

    @property
    def has_session(self) -> bool:
        return self.screening_session_id is not None

    def is_done(self, stage: str) -> bool:
        return stage in self.completed_stages


@dataclass(frozen=True)
class PipelineResult:
    """Outcome of one advance, plus the resulting state."""

    outcome: str
    state: PipelineState
    # Set only when ``outcome`` is FAILED.
    failed_stage: str | None = None
    # Candidate-safe. Empty unless FAILED.
    message: str = ""
    # Logs / tests only — never rendered. e.g. "ResumeParsingError".
    internal_reason: str = ""
    # Stages actually executed by THIS call (never more than one). Lets a
    # caller/test prove that a re-invocation did no work. A stage that was
    # satisfied by a concurrent writer counts as NOT run.
    stages_run: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_complete(self) -> bool:
        return self.state.is_complete


# --- candidate re-entry token resolution -----------------------------


class ScreeningAccessOutcome:
    """The two distinguishable results of :func:`resolve_screening_access_token`.

    ``INVALID`` covers *every* non-resumable case — missing token, malformed
    token, wrong format, unknown token, a screening that is already
    ``SCREENING_COMPLETE``, and one whose application was marked
    ``SCREENING_INCOMPLETE`` (abandoned) — with an identical response, so a
    probe cannot learn whether a token ever existed or what became of it. Same
    anti-enumeration discipline as ``application_link_service.resolve_link`` and
    ``authorization._deny``.
    """

    VALID = "VALID"
    INVALID = "INVALID"

    ALL: frozenset[str] = frozenset({VALID, INVALID})


@dataclass(frozen=True)
class ScreeningAccessResolution:
    """Result of resolving a candidate screening ``access_token``.

    On ``VALID`` carries only ``application_id`` — never the session row, never
    the token. On ``INVALID`` carries nothing.
    """

    outcome: str
    application_id: uuid.UUID | None = None

    @property
    def is_valid(self) -> bool:
        return self.outcome == ScreeningAccessOutcome.VALID


def resolve_screening_access_token(
    db: Session, token: str | None
) -> ScreeningAccessResolution:
    """Resolve a candidate screening re-entry token to its ``application_id``.

    Read-only, unauthenticated (the token IS the credential, exactly like
    ``application_link_service.resolve_link``). Returns ``VALID`` **only** for a
    known token whose session is still resumable (:data:`_RESUMABLE_STATUSES`)
    and whose application has not been marked ``SCREENING_INCOMPLETE``.
    Everything else — no token, wrong shape, unknown token, a completed
    screening, an abandoned one — returns the single ``INVALID`` outcome, so a
    probe cannot tell any of those apart. The token value is never logged.
    """
    if not token or not isinstance(token, str) or _ACCESS_TOKEN_RE.match(token) is None:
        logger.info("screening access: token rejected pre-DB")
        return ScreeningAccessResolution(ScreeningAccessOutcome.INVALID)

    row = db.execute(
        select(ScreeningSession.application_id, ScreeningSession.status).where(
            ScreeningSession.access_token == token
        )
    ).one_or_none()

    if row is None:
        logger.info("screening access: token not found")
        return ScreeningAccessResolution(ScreeningAccessOutcome.INVALID)

    application_id, session_status = row
    if session_status not in _RESUMABLE_STATUSES:
        logger.info("screening access: session not resumable (generic INVALID)")
        return ScreeningAccessResolution(ScreeningAccessOutcome.INVALID)

    app_status = db.execute(
        select(Application.status).where(Application.id == application_id)
    ).scalar_one_or_none()
    if app_status == ApplicationStatus.SCREENING_INCOMPLETE:
        logger.info("screening access: application abandoned (generic INVALID)")
        return ScreeningAccessResolution(ScreeningAccessOutcome.INVALID)

    return ScreeningAccessResolution(
        ScreeningAccessOutcome.VALID, application_id=application_id
    )


def get_candidate_screening_token(
    db: Session, *, application_id: uuid.UUID | str
) -> str | None:
    """The ``access_token`` for a candidate's own in-progress screening, or
    ``None`` if no session row exists yet.

    Candidate-facing (no auth guard): the caller is already in the processing
    flow for this ``application_id`` — it submitted the application or resolved a
    valid screening token. Used only so the interstitial page can render the
    "save this link" URL. The return value is a credential — the caller MUST NOT
    log it or put it in an error message.
    """
    return db.execute(
        select(ScreeningSession.access_token).where(
            ScreeningSession.application_id == application_id
        )
    ).scalar_one_or_none()


# --- reads -----------------------------------------------------------


def get_screening_session_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> ScreeningSession | None:
    """Return the application's screening session, or ``None``.

    HR/INTERNAL ONLY — the row carries ``access_token``, a per-candidate
    credential. :func:`~app.utils.authorization.require_internal_user` is
    checked first; the automatic pipeline satisfies it with the SYSTEM user.
    """
    require_internal_user(db, acting_user_id)
    return db.execute(
        select(ScreeningSession).where(
            ScreeningSession.application_id == application_id
        )
    ).scalar_one_or_none()


def _extraction_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
):
    """First resume extraction found across the application's documents.

    Mirrors ``prequalification_service._resume_extraction_for_application`` so
    "has the resume been parsed?" means the same thing in both places.
    """
    for document in list_documents_for_application(db, application_id):
        extraction = get_extraction_for_document(
            db, document.id, acting_user_id=acting_user_id
        )
        if extraction is not None:
            return extraction
    return None


def _build_state(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> PipelineState:
    completed: list[str] = []

    session = get_screening_session_for_application(
        db, application_id, acting_user_id=acting_user_id
    )
    if session is not None:
        completed.append(PipelineStage.SCREENING_SESSION)

    if _extraction_for_application(
        db, application_id, acting_user_id=acting_user_id
    ) is not None:
        completed.append(PipelineStage.RESUME_PARSING)

    if get_prequalification_for_application(
        db, application_id, acting_user_id=acting_user_id
    ) is not None:
        completed.append(PipelineStage.PREQUALIFICATION)

    # A session only moves forward through ScreeningSessionStatus.ORDER, so a
    # stage is "done" once the status is at or past the state that stage
    # produces — robust even after later stages advanced it further.
    if session is not None and ScreeningSessionStatus.at_least(
        session.status, ScreeningSessionStatus.READY_FOR_ROUND_1
    ):
        completed.append(PipelineStage.FINALIZE)

    if session is not None and ScreeningSessionStatus.at_least(
        session.status, ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    ):
        completed.append(PipelineStage.ROUND_1_QUESTIONS)

    next_stage = next(
        (s for s in PipelineStage.ORDER if s not in completed), None
    )
    return PipelineState(
        application_id=str(application_id),
        completed_stages=tuple(completed),
        next_stage=next_stage,
        screening_session_id=str(session.id) if session is not None else None,
        screening_session_status=session.status if session is not None else None,
    )


def get_pipeline_state(
    db: Session, *, application_id: uuid.UUID | str
) -> PipelineState:
    """Where the pipeline has got to. Pure read — runs no stage, writes nothing.

    Raises
    ------
    SystemUserMissingError
        The SYSTEM actor is not seeded (deployment error).
    PipelineApplicationNotFoundError
        No application with ``application_id``.
    """
    system_user_id = get_system_user_id(db)
    if db.get(Application, application_id) is None:
        raise PipelineApplicationNotFoundError(
            f"No application with id {application_id!r}."
        )
    return _build_state(
        db, application_id=application_id, acting_user_id=system_user_id
    )


# --- stalled-pipeline recovery (HR-side) -----------------------------


@dataclass(frozen=True)
class StalledScreening:
    """One application whose screening appears stalled. No token.

    ``status`` distinguishes a *pre-round-1* stall (``PENDING`` — the automatic
    pipeline stopped; recover it with :func:`resume_stalled_pipeline`) from a
    *mid-screening* stall (``ROUND_1_IN_PROGRESS`` / ``ROUND_1_COMPLETE`` /
    ``ROUND_2_IN_PROGRESS`` — the candidate stopped answering; the HR decision
    is :func:`app.services.screening_question_service.abandon_screening`).
    """

    application_id: uuid.UUID
    screening_session_id: uuid.UUID
    status: str
    stalled_since: datetime

    @property
    def is_pre_round_1(self) -> bool:
        return self.status == ScreeningSessionStatus.PENDING


def list_stalled_screening_applications(
    db: Session,
    *,
    acting_user_id: uuid.UUID | str,
    now: datetime | None = None,
) -> list[StalledScreening]:
    """Applications whose ``screening_sessions`` row is in a non-terminal,
    pre-completion state (:data:`_STALLABLE_STATUSES`) and has not been touched
    for longer than :data:`STALLED_PIPELINE_THRESHOLD`.

    ``submit_answer`` bumps ``screening_sessions.updated_at`` on every candidate
    answer, so a candidate who is actively answering is NOT flagged.

    HR/INTERNAL ONLY. ``now`` is injectable for tests.
    """
    require_internal_user(db, acting_user_id)
    cutoff = (now or datetime.now(timezone.utc)) - STALLED_PIPELINE_THRESHOLD
    rows = db.execute(
        select(ScreeningSession)
        .where(
            ScreeningSession.status.in_(_STALLABLE_STATUSES),
            ScreeningSession.updated_at < cutoff,
        )
        .order_by(ScreeningSession.updated_at)
    ).scalars().all()
    return [
        StalledScreening(
            application_id=r.application_id,
            screening_session_id=r.id,
            status=r.status,
            stalled_since=r.updated_at,
        )
        for r in rows
    ]


def resume_stalled_pipeline(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
) -> PipelineResult:
    """HR-triggered recovery for a stalled automatic pipeline.

    ``requested_by_user_id`` authorizes the *click* — it must resolve to an
    active internal user. It is NOT used for any pipeline work and NOT recorded
    as the actor on any audit event: the pipeline stays a SYSTEM actor even when
    a human kicks it (CLAUDE.md §2A item 2). Behaviourally identical to the
    candidate's browser having kept running — same idempotency guarantees: no
    duplicate session row, no duplicate AI call, no duplicate audit event.

    Raises
    ------
    UnauthorizedError
        ``requested_by_user_id`` is missing / unknown / inactive.
    PipelineApplicationNotFoundError
        No application with ``application_id``.
    """
    require_internal_user(db, requested_by_user_id)
    logger.info(
        "screening_pipeline application=%s manual_recovery=by_internal_user",
        application_id,
    )
    return run_screening_pipeline(db, application_id=application_id)


# --- stage 1: screening session -------------------------------------


def _generate_access_token(db: Session) -> str:
    """Fresh URL-safe token. Pre-checked for collision; the UNIQUE index on
    ``screening_sessions.access_token`` is the real backstop."""
    for _ in range(2):
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        already_used = db.execute(
            select(ScreeningSession.id).where(
                ScreeningSession.access_token == token
            )
        ).first()
        if already_used is None:
            return token
    raise ScreeningPipelineError(_UNAVAILABLE)


def _is_unique_application_violation(exc: IntegrityError) -> bool:
    """True iff ``exc`` is the ``uq_screening_sessions_application`` violation
    specifically — not some other integrity failure we should not swallow."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    if constraint == _UNIQUE_APPLICATION_CONSTRAINT:
        return True
    # Fallback for drivers that don't expose diag.constraint_name.
    return _UNIQUE_APPLICATION_CONSTRAINT in str(exc)


def _ensure_screening_session(
    db: Session,
    *,
    application: Application,
    system_user_id: uuid.UUID,
) -> tuple[ScreeningSession, bool]:
    """Return ``(session, created_by_this_call)``.

    Idempotent and concurrency-safe:

    * if a row already exists -> return it, ``created=False``;
    * else insert one (status ``PENDING``), emit ``AI_SCREENING_STARTED``,
      commit -> ``created=True``;
    * if a concurrent caller inserts first and our ``flush`` hits
      ``uq_screening_sessions_application`` -> roll back our insert, re-fetch
      the winner's row, return it with ``created=False`` (NEVER an error).
    """
    existing = get_screening_session_for_application(
        db, application.id, acting_user_id=system_user_id
    )
    if existing is not None:
        return existing, False

    session = ScreeningSession(
        application_id=application.id,
        access_token=_generate_access_token(db),
        status=ScreeningSessionStatus.PENDING,
    )
    db.add(session)
    try:
        db.flush()  # assign id / surface a concurrent UNIQUE violation here
    except IntegrityError as exc:
        if not _is_unique_application_violation(exc):
            raise
        # A concurrent caller inserted the row between our check and our flush.
        # Roll back our aborted insert and adopt their row — never an error.
        db.rollback()
        winner = db.execute(
            select(ScreeningSession).where(
                ScreeningSession.application_id == application.id
            )
        ).scalar_one_or_none()
        if winner is None:  # pragma: no cover - defensively impossible
            raise ScreeningPipelineError(_UNAVAILABLE) from exc
        logger.info(
            "screening_pipeline application=%s stage=%s "
            "outcome=session_created_concurrently",
            application.id, PipelineStage.SCREENING_SESSION,
        )
        return winner, False

    previous_status = application.status  # unchanged here — see FINALIZE

    # NOTE: access_token is deliberately absent from all audit metadata.
    record_event(
        db,
        event_type=AuditEventType.AI_SCREENING_STARTED,
        action=(
            f"AI screening session created for application {application.id} "
            "by the automated pipeline."
        ),
        entity_type="screening_session",
        entity_id=session.id,
        user_id=system_user_id,
        previous_state={"application_status": previous_status},
        new_state={
            "application_id": str(application.id),
            "screening_session_status": ScreeningSessionStatus.PENDING,
            "application_status": previous_status,  # not advanced yet
            "access_token_present": True,  # existence only, never the value
        },
    )

    db.commit()
    db.refresh(session)
    return session, True


# --- stage 4: finalize ---------------------------------------------


def _finalize_ready_for_round_1(
    db: Session,
    *,
    application: Application,
    system_user_id: uuid.UUID,
) -> bool:
    """Flip the session to ``READY_FOR_ROUND_1`` and the application to
    ``SCREENING_IN_PROGRESS``. Returns ``True`` if it changed anything.

    Idempotent: a no-op if the session is already ``READY_FOR_ROUND_1``.

    No dedicated audit event (see module docstring) — the transition is
    reconstructible from the already-audited events + ``screening_sessions``
    columns. ``system_user_id`` is accepted for symmetry / future use.
    """
    session = db.execute(
        select(ScreeningSession).where(
            ScreeningSession.application_id == application.id
        )
    ).scalar_one()

    if session.status == ScreeningSessionStatus.READY_FOR_ROUND_1:
        return False

    session.status = ScreeningSessionStatus.READY_FOR_ROUND_1
    application.status = ApplicationStatus.SCREENING_IN_PROGRESS
    db.commit()
    db.refresh(session)
    logger.info(
        "screening_pipeline application=%s stage=%s outcome=ready_for_round_1",
        application.id, PipelineStage.FINALIZE,
    )
    return True


# --- the pipeline --------------------------------------------------


def advance_screening_pipeline(
    db: Session, *, application_id: uuid.UUID | str
) -> PipelineResult:
    """Run **at most one** pending stage for ``application_id`` and return.

    Safe to call any number of times, in any order, from any number of reruns
    or concurrent requests: every stage is check-first, so an already-completed
    stage is skipped without an AI call, a DB write, or an audit event.

    Never raises for an ordinary stage failure — that comes back as
    ``outcome=FAILED`` with a candidate-safe ``message`` and the pipeline left
    resumable from exactly that stage.

    Raises
    ------
    PipelineApplicationNotFoundError
        No application with ``application_id`` (a programming error).
    """
    try:
        system_user_id = get_system_user_id(db)
    except SystemUserMissingError as exc:
        logger.error(
            "screening_pipeline application=%s failure=system_user_missing",
            application_id,
        )
        return PipelineResult(
            outcome=PipelineOutcome.FAILED,
            state=PipelineState(application_id=str(application_id)),
            failed_stage=None,
            message=_UNAVAILABLE,
            internal_reason=type(exc).__name__,
        )

    application = db.get(Application, application_id)
    if application is None:
        raise PipelineApplicationNotFoundError(
            f"No application with id {application_id!r}."
        )

    state = _build_state(
        db, application_id=application.id, acting_user_id=system_user_id
    )
    if state.is_complete:
        return PipelineResult(PipelineOutcome.COMPLETED, state)

    stage = state.next_stage
    stage_ran = True  # set False when a concurrent writer satisfied the stage

    def _failed(reason: str) -> PipelineResult:
        logger.warning(
            "screening_pipeline application=%s stage=%s outcome=failed kind=%s",
            application_id, stage, reason,
        )
        return PipelineResult(
            outcome=PipelineOutcome.FAILED,
            state=state,
            failed_stage=stage,
            message=_STAGE_FAILED.get(stage, _UNAVAILABLE),
            internal_reason=reason,
        )

    try:
        if stage == PipelineStage.SCREENING_SESSION:
            _, created = _ensure_screening_session(
                db, application=application, system_user_id=system_user_id
            )
            stage_ran = created  # a concurrent create is "not our work"

        elif stage == PipelineStage.RESUME_PARSING:
            documents = list_documents_for_application(db, application.id)
            if not documents:
                logger.warning(
                    "screening_pipeline application=%s stage=%s outcome=failed "
                    "kind=no_document", application_id, stage,
                )
                return PipelineResult(
                    outcome=PipelineOutcome.FAILED,
                    state=state,
                    failed_stage=stage,
                    message=_NO_DOCUMENT,
                    internal_reason="NO_DOCUMENT",
                )
            parse_resume(
                db,
                document_id=documents[0].id,
                requested_by_user_id=system_user_id,
                force=False,
            )

        elif stage == PipelineStage.PREQUALIFICATION:
            # The RESULT is deliberately not inspected. PASS / FAIL / UNKNOWN
            # all continue identically (CLAUDE.md §2A item 7).
            prequalify_application(
                db,
                application_id=application.id,
                requested_by_user_id=system_user_id,
                force=False,
            )

        elif stage == PipelineStage.FINALIZE:
            changed = _finalize_ready_for_round_1(
                db, application=application, system_user_id=system_user_id
            )
            stage_ran = changed

        else:  # PipelineStage.ROUND_1_QUESTIONS
            session = get_screening_session_for_application(
                db, application.id, acting_user_id=system_user_id
            )
            # Idempotent: generate_round_questions returns the existing rows
            # (no AI call) if round-1 questions are already persisted.
            generate_round_questions(
                db,
                screening_session_id=session.id,
                round=1,
                requested_by_user_id=system_user_id,
            )

    except (ResumeParsingError, PrequalificationError, ScreeningQuestionError) as exc:
        db.rollback()
        return _failed(type(exc).__name__)
    except UnauthorizedError as exc:
        db.rollback()
        logger.error(
            "screening_pipeline application=%s stage=%s "
            "failure=system_user_unauthorized", application_id, stage,
        )
        return PipelineResult(
            outcome=PipelineOutcome.FAILED,
            state=state,
            failed_stage=stage,
            message=_UNAVAILABLE,
            internal_reason=type(exc).__name__,
        )
    except ScreeningPipelineError as exc:
        db.rollback()
        return _failed(type(exc).__name__)

    new_state = _build_state(
        db, application_id=application.id, acting_user_id=system_user_id
    )
    logger.info(
        "screening_pipeline application=%s stage=%s outcome=ok ran=%s complete=%s",
        application_id, stage, stage_ran, new_state.is_complete,
    )
    return PipelineResult(
        outcome=(
            PipelineOutcome.COMPLETED
            if new_state.is_complete
            else PipelineOutcome.IN_PROGRESS
        ),
        state=new_state,
        stages_run=(stage,) if stage_ran else (),
    )


def run_screening_pipeline(
    db: Session, *, application_id: uuid.UUID | str
) -> PipelineResult:
    """Advance repeatedly until the pipeline completes or a stage fails.

    Convenience wrapper over :func:`advance_screening_pipeline` with the same
    idempotency guarantees. The public page uses the one-stage-per-rerun form so
    the candidate sees real progress; HR manual recovery and tests use this.
    """
    stages_run: list[str] = []
    result: PipelineResult | None = None

    # Hard-bounded: each successful pass completes exactly one stage, one spare
    # pass lets the final COMPLETED verdict be observed. Cannot spin.
    for _ in range(len(PipelineStage.ORDER) + 1):
        result = advance_screening_pipeline(db, application_id=application_id)
        stages_run.extend(result.stages_run)
        if result.outcome != PipelineOutcome.IN_PROGRESS:
            break

    return PipelineResult(
        outcome=result.outcome,
        state=result.state,
        failed_stage=result.failed_stage,
        message=result.message,
        internal_reason=result.internal_reason,
        stages_run=tuple(stages_run),
    )
