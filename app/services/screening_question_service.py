"""Screening-question service — generate a round's questions, persist candidate
answers, and drive the round lifecycle to a terminal state
(CLAUDE.md §§3, 12, 18, 19, 20, 23, 27, 29; Phase 4 Step 3).

WHAT THE AI DOES vs WHAT PYTHON DOES
-----------------------------------
* AI: given the approved rubric, the resume evidence, and its own prior
  per-criterion prequalification output, propose a small batch of screening
  questions (category, mapped criterion or none, text, reason).
* Python (here):
  - enforces the per-round length bound (round 1: 4-8, round 2: 0-3) — an LLM
    cannot be trusted to count,
  - rejects a ``criterion_id`` that does not resolve to a criterion of this
    application's approved rubric (hallucination guard, same "AI reasons,
    Python enforces the boundary" rule as prequalification's coverage check),
  - logs (never hard-fails) when the round-1 targeting is wildly off the
    ~70%-on-UNKNOWN/FAIL guidance,
  - assigns ``round`` and ``sequence_index`` (the AI supplies neither),
  - persists, transitions the session status, and audits — one commit.

BATCH, NOT A CHAT LOOP
----------------------
Questions are generated per round in one shot (CLAUDE.md §3 "structured but
conversational" — not a turn-by-turn agent). Round 1 fires automatically from
the pipeline once the session is ``READY_FOR_ROUND_1``. Round 2 is triggered by
the candidate submitting their last round-1 answer — not by a timer — and may
legitimately produce **zero** questions, which ends the screening.

AUTH
----
:func:`generate_round_questions` is HR/INTERNAL ONLY (it reads per-criterion
prequalification judgments): ``require_internal_user`` first, and the automatic
pipeline / round-2 trigger call it as the SYSTEM actor — exactly as every other
AI-task service in this codebase. :func:`submit_answer` and
:func:`get_round_questions` are **candidate-facing** — reached only after the
caller has resolved a valid ``?screening=`` token — and take no internal user;
they never read or return ``generated_reason`` / ``rubric_criterion_id`` /
prequalification text.

PRIVACY / LOGGING (CLAUDE.md §§19, 23, 24)
-----------------------------------------
Question text, ``generated_reason``, and candidate ``answer_text`` are NEVER
logged raw and NEVER put in audit metadata. Audit metadata carries round /
counts / category breakdown / referenced criterion ids / model only. Log lines
carry ids, counts, and a redacted answer length at most.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.claude_client import (
    AIError,
    AIOutputError,
    _resolve_model,
    get_structured_response,
)
from app.ai.prompts.screening_questions import build_screening_question_prompt
from app.ai.schemas.screening_questions import (
    ROUND_1_MAX_QUESTIONS,
    ROUND_1_MIN_QUESTIONS,
    ROUND_2_MAX_QUESTIONS,
    ROUND_2_MIN_QUESTIONS,
    ScreeningQuestionSet,
)
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.document import Document
from app.database.models.resume_extraction import ResumeExtraction
from app.database.models.screening_answer import ScreeningAnswer
from app.database.models.screening_question import (
    ScreeningQuestion,
    ScreeningQuestionCategory,
    ScreeningQuestionRound,
)
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.services.audit_service import record_event
from app.services.prequalification_service import (
    get_prequalification_for_application,
)
from app.services.resume_parsing_service import get_extraction_for_document
from app.services.rubric_service import get_approved_rubric, list_criteria
from app.services.storage_service import list_documents_for_application
from app.services.system_user_service import get_system_user_id
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

_TASK_NAME = "screening_question"

_QUESTIONS_UNUSABLE = (
    "The AI's screening questions were incomplete or unusable. Please try again."
)
_QUESTIONS_FAILED = "Screening question generation failed — please try again."


# --- typed errors --------------------------------------------------


class ScreeningQuestionError(Exception):
    """Round question generation could not be completed. User-safe message;
    never wraps rubric / evidence / question / answer text."""


class ScreeningSessionNotFoundError(ScreeningQuestionError):
    """No screening session with the given id."""


class ScreeningRoundPreconditionError(ScreeningQuestionError):
    """The session / round is not in a state where this round may be generated
    (wrong status, unanswered prior round, etc.)."""


class ScreeningQuestionNotFoundError(ScreeningQuestionError):
    """No screening question with the given id."""


class ScreeningAnswerNotAllowedError(ScreeningQuestionError):
    """The question's round is not open for answers (not yet started, or already
    completed)."""


# --- lightweight read DTOs (primitives only) ----------------------


@dataclass(frozen=True)
class ScreeningQuestionView:
    """Candidate-safe view of one question + its answer. NO generated_reason,
    NO criterion id, NO prequalification data."""

    question_id: str
    round: int
    sequence_index: int
    category: str
    question_text: str
    answer_text: str | None
    answered: bool


@dataclass(frozen=True)
class ScreeningTranscriptItem:
    """HR/INTERNAL view of one question + its answer, **with** the fields
    ``ScreeningQuestionView`` deliberately strips: ``rubric_criterion_id``,
    ``generated_reason``, ``ai_model``. Used only by the screening-evaluation
    step; never returned to a candidate."""

    question_id: str
    round: int
    sequence_index: int
    category: str
    rubric_criterion_id: str | None
    generated_reason: str
    ai_model: str
    question_text: str
    answer_text: str | None
    answered: bool


# --- helpers -----------------------------------------------------


def _get_session(db: Session, screening_session_id: uuid.UUID | str) -> ScreeningSession:
    session = db.get(ScreeningSession, screening_session_id)
    if session is None:
        raise ScreeningSessionNotFoundError(
            f"No screening session with id {screening_session_id!r}."
        )
    return session


def _questions_for_round(
    db: Session, screening_session_id: uuid.UUID | str, round_: int
) -> list[ScreeningQuestion]:
    return list(
        db.execute(
            select(ScreeningQuestion)
            .where(
                ScreeningQuestion.screening_session_id == screening_session_id,
                ScreeningQuestion.round == round_,
            )
            .order_by(ScreeningQuestion.sequence_index)
        ).scalars().all()
    )


def _answers_by_question_id(
    db: Session, question_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ScreeningAnswer]:
    if not question_ids:
        return {}
    rows = db.execute(
        select(ScreeningAnswer).where(
            ScreeningAnswer.screening_question_id.in_(question_ids)
        )
    ).scalars().all()
    return {a.screening_question_id: a for a in rows}


def _resume_evidence(
    db: Session, application_id: uuid.UUID, *, acting_user_id: uuid.UUID | str
) -> dict | None:
    """The extracted resume evidence dict for an application, or None."""
    for document in list_documents_for_application(db, application_id):
        extraction = get_extraction_for_document(
            db, document.id, acting_user_id=acting_user_id
        )
        if extraction is not None:
            return extraction.extracted_data
    return None


def _round_1_qa_pairs(db: Session, screening_session_id: uuid.UUID) -> list[dict[str, str]]:
    questions = _questions_for_round(db, screening_session_id, 1)
    answers = _answers_by_question_id(db, [q.id for q in questions])
    pairs: list[dict[str, str]] = []
    for q in questions:
        a = answers.get(q.id)
        pairs.append(
            {"question": q.question_text, "answer": a.answer_text if a else ""}
        )
    return pairs


def _category_breakdown(rows: list[dict]) -> dict[str, int]:
    out = {c: 0 for c in ScreeningQuestionCategory.ALL}
    for r in rows:
        out[r["category"]] = out.get(r["category"], 0) + 1
    return out


def _audit_metadata(
    *, round_: int, rows: list[dict], ai_model: str
) -> dict:
    """Safe-only audit metadata — counts / criterion ids / model. NEVER text."""
    criterion_ids = sorted(
        {r["rubric_criterion_id"] for r in rows if r["rubric_criterion_id"]}
    )
    return {
        "round": round_,
        "question_count": len(rows),
        "category_breakdown": _category_breakdown(rows),
        "rubric_criterion_ids": criterion_ids,
        "ai_model": ai_model,
    }


def _redact_len(text: str | None) -> str:
    """Log-safe stand-in for candidate free text: length bucket only."""
    n = len(text or "")
    if n == 0:
        return "len=0"
    if n < 20:
        return "len<20"
    if n < 100:
        return "len<100"
    if n < 500:
        return "len<500"
    return "len>=500"


# --- round question generation ---------------------------------


def _validate_and_map_questions(
    question_set: ScreeningQuestionSet,
    *,
    round_: int,
    criteria_by_id: dict[str, uuid.UUID],
    prequal_results: list[dict],
) -> list[dict]:
    """Turn the validated AI output into persistable row dicts.

    Enforces the per-round length bound and the criterion-id resolution.
    Logs (does not raise) when round-1 targeting is well off the guidance.
    """
    questions = question_set.questions

    if round_ == ScreeningQuestionRound.ROUND_1:
        lo, hi = ROUND_1_MIN_QUESTIONS, ROUND_1_MAX_QUESTIONS
    else:
        lo, hi = ROUND_2_MIN_QUESTIONS, ROUND_2_MAX_QUESTIONS
    if not (lo <= len(questions) <= hi):
        raise ScreeningQuestionError(_QUESTIONS_UNUSABLE)

    rows: list[dict] = []
    for i, q in enumerate(questions):
        criterion_uuid: uuid.UUID | None = None
        if q.criterion_id is not None:
            criterion_uuid = criteria_by_id.get(q.criterion_id)
            if criterion_uuid is None:
                # Hallucinated / stale criterion id — do NOT silently drop.
                raise ScreeningQuestionError(_QUESTIONS_UNUSABLE)
        rows.append(
            {
                "sequence_index": i,
                "category": q.category,
                "rubric_criterion_id": criterion_uuid,
                "question_text": q.question_text,
                "generated_reason": q.generated_reason,
            }
        )

    if round_ == ScreeningQuestionRound.ROUND_1 and rows:
        gap_ids = {
            r["criterion_id"]
            for r in prequal_results
            if r.get("result") in ("UNKNOWN", "FAIL")
        }
        if gap_ids:
            on_gap = sum(
                1
                for r in rows
                if r["rubric_criterion_id"]
                and str(r["rubric_criterion_id"]) in gap_ids
            )
            frac = on_gap / len(rows)
            if frac < 0.4:  # target is ~0.7; only warn when clearly off
                logger.warning(
                    "screening_questions session round=%s targeting_low "
                    "on_gap=%d of=%d frac=%.2f",
                    round_, on_gap, len(rows), frac,
                )

    return rows


def generate_round_questions(
    db: Session,
    *,
    screening_session_id: uuid.UUID | str,
    round: int,
    requested_by_user_id: uuid.UUID | str,
) -> list[ScreeningQuestion]:
    """Generate + persist one round's screening questions for a session.

    HR/INTERNAL ONLY. Idempotent (``force=False`` convention): if questions
    already exist for ``(screening_session_id, round)`` they are returned
    unchanged, with **no** AI call.

    Preconditions:
    * round 1 — session status must be ``READY_FOR_ROUND_1``;
    * round 2 — session status must be ``ROUND_1_COMPLETE`` and every round-1
      question must have an answer.

    On success (one transaction): inserts the question rows, moves the session
    status (``ROUND_1_IN_PROGRESS`` / ``ROUND_2_IN_PROGRESS`` / — when round 2
    produced zero questions — straight to ``SCREENING_COMPLETE`` with
    ``AI_SCREENING_COMPLETED``), writes one ``SCREENING_QUESTIONS_GENERATED``
    audit event (safe metadata only), and commits once.

    Raises
    ------
    UnauthorizedError
        ``requested_by_user_id`` missing / unknown / inactive.
    ScreeningSessionNotFoundError / ScreeningRoundPreconditionError
    ScreeningQuestionError
        The AI call / its output was unusable, or a business rule failed. No DB
        changes were made.
    """
    require_internal_user(db, requested_by_user_id)

    if round not in ScreeningQuestionRound.ALL:
        raise ScreeningRoundPreconditionError(f"Unsupported screening round {round!r}.")

    session = _get_session(db, screening_session_id)

    existing = _questions_for_round(db, session.id, round)
    if existing:
        return existing

    # --- preconditions ------------------------------------------------
    if round == ScreeningQuestionRound.ROUND_1:
        if session.status != ScreeningSessionStatus.READY_FOR_ROUND_1:
            raise ScreeningRoundPreconditionError(
                "Round 1 questions can only be generated once the pre-screening "
                "pipeline has finished."
            )
    else:  # ROUND_2
        if session.status != ScreeningSessionStatus.ROUND_1_COMPLETE:
            raise ScreeningRoundPreconditionError(
                "Round 2 questions can only be generated after round 1 is "
                "complete."
            )
        r1 = _questions_for_round(db, session.id, 1)
        answered = _answers_by_question_id(db, [q.id for q in r1])
        if any(q.id not in answered for q in r1):
            raise ScreeningRoundPreconditionError(
                "Round 2 cannot be generated until every round-1 question is "
                "answered."
            )

    application = db.get(Application, session.application_id)
    if application is None:  # pragma: no cover - FK guarantees this
        raise ScreeningQuestionError(_QUESTIONS_FAILED)

    approved_rubric = get_approved_rubric(db, application.job_id)
    if approved_rubric is None:  # near-unreachable (link required an approved rubric)
        raise ScreeningQuestionError(_QUESTIONS_FAILED)
    criteria = list_criteria(db, approved_rubric.id)

    prequal = get_prequalification_for_application(
        db, application.id, acting_user_id=requested_by_user_id
    )
    prequal_results: list[dict] = prequal.results if prequal is not None else []

    resume_evidence = _resume_evidence(
        db, application.id, acting_user_id=requested_by_user_id
    ) or {}

    prior_qa = (
        _round_1_qa_pairs(db, session.id)
        if round == ScreeningQuestionRound.ROUND_2
        else None
    )

    # --- AI call ----------------------------------------------------
    prompt = build_screening_question_prompt(
        round=round,
        rubric_criteria=criteria,
        prequalification_results=prequal_results,
        resume_evidence=resume_evidence,
        prior_round_qa=prior_qa,
    )
    try:
        question_set: ScreeningQuestionSet = get_structured_response(
            prompt, ScreeningQuestionSet, task_name=_TASK_NAME
        )
    except AIOutputError as exc:
        logger.warning(
            "screening_questions session=%s round=%s failure=validation",
            session.id, round,
        )
        raise ScreeningQuestionError(_QUESTIONS_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "screening_questions session=%s round=%s failure=request kind=%s",
            session.id, round, type(exc).__name__,
        )
        raise ScreeningQuestionError(_QUESTIONS_FAILED) from exc

    criteria_by_id = {str(c.id): c.id for c in criteria}
    rows = _validate_and_map_questions(
        question_set,
        round_=round,
        criteria_by_id=criteria_by_id,
        prequal_results=prequal_results,
    )
    resolved_model = _resolve_model(_TASK_NAME, None)

    # --- persist (one transaction) --------------------------------
    persisted: list[ScreeningQuestion] = []
    for row in rows:
        q = ScreeningQuestion(
            screening_session_id=session.id,
            round=round,
            sequence_index=row["sequence_index"],
            category=row["category"],
            rubric_criterion_id=row["rubric_criterion_id"],
            question_text=row["question_text"],
            generated_reason=row["generated_reason"],
            ai_model=resolved_model,
        )
        db.add(q)
        persisted.append(q)
    db.flush()

    audit_rows = [
        {
            "category": row["category"],
            "rubric_criterion_id": (
                str(row["rubric_criterion_id"])
                if row["rubric_criterion_id"]
                else None
            ),
        }
        for row in rows
    ]

    previous_status = session.status
    zero_round_2 = round == ScreeningQuestionRound.ROUND_2 and not rows

    if round == ScreeningQuestionRound.ROUND_1:
        session.status = ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    elif zero_round_2:
        session.status = ScreeningSessionStatus.SCREENING_COMPLETE
    else:
        session.status = ScreeningSessionStatus.ROUND_2_IN_PROGRESS

    record_event(
        db,
        event_type=AuditEventType.SCREENING_QUESTIONS_GENERATED,
        action=(
            f"Screening round {round} questions generated for session "
            f"{session.id} ({len(rows)} question(s))."
        ),
        entity_type="screening_session",
        entity_id=session.id,
        user_id=requested_by_user_id,
        previous_state={"screening_session_status": previous_status},
        new_state={
            "screening_session_status": session.status,
            **_audit_metadata(round_=round, rows=audit_rows, ai_model=resolved_model),
        },
    )

    if zero_round_2:
        _mark_screening_complete(
            db,
            session=session,
            application=application,
            user_id=requested_by_user_id,
            reason="round_2_no_followups",
        )

    db.commit()
    for q in persisted:
        db.refresh(q)
    logger.info(
        "screening_questions session=%s round=%s outcome=ok count=%d model=%s "
        "status=%s",
        session.id, round, len(rows), resolved_model, session.status,
    )
    if zero_round_2:
        # Screening ended with no round-2 questions -> evaluate now (own unit).
        _trigger_screening_evaluation(db, application_id=session.application_id)
    return persisted


# --- terminal transition -------------------------------------


def _mark_screening_complete(
    db: Session,
    *,
    session: ScreeningSession,
    application: Application,
    user_id: uuid.UUID | str,
    reason: str,
) -> bool:
    """Move the session to SCREENING_COMPLETE + application to
    SCREENING_COMPLETED + emit AI_SCREENING_COMPLETED, exactly once.

    Idempotent: a no-op (returns False) if the session is already
    ``SCREENING_COMPLETE``. Flushes the audit event; the **caller** commits.
    """
    if session.status == ScreeningSessionStatus.SCREENING_COMPLETE and (
        application.status == ApplicationStatus.SCREENING_COMPLETED
    ):
        return False

    prev_session = session.status
    prev_app = application.status
    session.status = ScreeningSessionStatus.SCREENING_COMPLETE
    application.status = ApplicationStatus.SCREENING_COMPLETED

    record_event(
        db,
        event_type=AuditEventType.AI_SCREENING_COMPLETED,
        action=(
            f"AI screening completed for application {application.id} "
            f"({reason})."
        ),
        entity_type="screening_session",
        entity_id=session.id,
        user_id=user_id,
        previous_state={
            "screening_session_status": prev_session,
            "application_status": prev_app,
        },
        new_state={
            "screening_session_status": ScreeningSessionStatus.SCREENING_COMPLETE,
            "application_status": ApplicationStatus.SCREENING_COMPLETED,
            "reason": reason,
        },
    )
    logger.info(
        "screening session=%s outcome=complete reason=%s", session.id, reason
    )
    return True


# --- candidate answer submission -----------------------------


def _round_is_open_for(session: ScreeningSession, question_round: int) -> bool:
    if question_round == ScreeningQuestionRound.ROUND_1:
        return session.status == ScreeningSessionStatus.ROUND_1_IN_PROGRESS
    if question_round == ScreeningQuestionRound.ROUND_2:
        return session.status == ScreeningSessionStatus.ROUND_2_IN_PROGRESS
    return False


def submit_answer(
    db: Session,
    *,
    screening_question_id: uuid.UUID | str,
    answer_text: str,
) -> ScreeningAnswer:
    """Persist (or edit) a candidate's answer to one screening question.

    Candidate-facing — NO ``require_internal_user``. The caller has already
    resolved a valid ``?screening=`` token for this session.

    * The question's round must currently be open
      (``ROUND_1_IN_PROGRESS`` for a round-1 question, ``ROUND_2_IN_PROGRESS``
      for a round-2 question) — otherwise :class:`ScreeningAnswerNotAllowedError`.
    * Idempotent upsert on ``screening_question_id`` (unique). Editable while the
      round is open; the unique index is the concurrency backstop.
    * When this is the last unanswered question in the open round, the round is
      completed: round 1 -> ``ROUND_1_COMPLETE`` then round-2 generation is
      triggered (as SYSTEM); round 2 -> ``SCREENING_COMPLETE`` +
      ``AI_SCREENING_COMPLETED``. The transition + emit are check-first and
      idempotent — submitting the last answer twice does neither twice.
    """
    cleaned = (answer_text or "").strip()
    if not cleaned:
        raise ScreeningQuestionError("An answer cannot be empty.")

    question = db.get(ScreeningQuestion, screening_question_id)
    if question is None:
        raise ScreeningQuestionNotFoundError(
            f"No screening question with id {screening_question_id!r}."
        )
    session = _get_session(db, question.screening_session_id)

    if not _round_is_open_for(session, question.round):
        raise ScreeningAnswerNotAllowedError(
            "This screening round is not open for answers."
        )

    # --- upsert the answer ------------------------------------------
    answer = db.execute(
        select(ScreeningAnswer).where(
            ScreeningAnswer.screening_question_id == question.id
        )
    ).scalar_one_or_none()

    if answer is None:
        answer = ScreeningAnswer(
            screening_question_id=question.id,
            answer_text=cleaned,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(answer)
        try:
            db.flush()
        except IntegrityError:
            # A concurrent submit inserted first — adopt + update its row.
            db.rollback()
            question = db.get(ScreeningQuestion, screening_question_id)
            session = _get_session(db, question.screening_session_id)
            if not _round_is_open_for(session, question.round):
                raise ScreeningAnswerNotAllowedError(
                    "This screening round is not open for answers."
                )
            answer = db.execute(
                select(ScreeningAnswer).where(
                    ScreeningAnswer.screening_question_id == question.id
                )
            ).scalar_one()
            answer.answer_text = cleaned
    else:
        answer.answer_text = cleaned  # edit while the round is open

    # Keep the stalled-detection clock honest while the candidate is answering.
    session.updated_at = datetime.now(timezone.utc)
    db.flush()

    # --- round-completion check (idempotent) ----------------------
    round_questions = _questions_for_round(db, session.id, question.round)
    answered = _answers_by_question_id(db, [q.id for q in round_questions])
    all_answered = all(q.id in answered for q in round_questions)

    if all_answered and _round_is_open_for(session, question.round):
        application = db.get(Application, session.application_id)
        if question.round == ScreeningQuestionRound.ROUND_1:
            answer_id = answer.id
            session.status = ScreeningSessionStatus.ROUND_1_COMPLETE
            db.commit()
            # Round-2 generation commits (or safely no-ops) in its own unit.
            _trigger_round_2(db, screening_session_id=session.id)
            refreshed = db.get(ScreeningAnswer, answer_id)
            logger.info(
                "screening_answer session=%s round=1 question=%s outcome=ok "
                "round_1_complete %s",
                session.id, question.id, _redact_len(cleaned),
            )
            return refreshed
        else:  # ROUND_2
            completed = _mark_screening_complete(
                db,
                session=session,
                application=application,
                user_id=get_system_user_id(db),
                reason="round_2_answers_complete",
            )
            if completed:
                answer_id = answer.id
                app_id = session.application_id
                db.commit()
                # Auto-evaluation commits (or safely no-ops) in its own unit.
                _trigger_screening_evaluation(db, application_id=app_id)
                refreshed = db.get(ScreeningAnswer, answer_id)
                logger.info(
                    "screening_answer session=%s round=2 question=%s outcome=ok "
                    "screening_complete %s",
                    session.id, question.id, _redact_len(cleaned),
                )
                return refreshed

    db.commit()
    db.refresh(answer)
    logger.info(
        "screening_answer session=%s round=%s question=%s outcome=ok %s",
        session.id, question.round, question.id, _redact_len(cleaned),
    )
    return answer


def _trigger_round_2(db: Session, *, screening_session_id: uuid.UUID) -> None:
    """Generate round-2 questions as SYSTEM, right after round 1 completes.

    Best-effort: if generation fails the answer is already saved and the session
    sits at ``ROUND_1_COMPLETE`` — :func:`ensure_round_2_generated` retries it on
    the next page load. Never raises to the candidate.
    """
    try:
        generate_round_questions(
            db,
            screening_session_id=screening_session_id,
            round=ScreeningQuestionRound.ROUND_2,
            requested_by_user_id=get_system_user_id(db),
        )
    except ScreeningQuestionError as exc:
        db.rollback()
        logger.warning(
            "screening_questions session=%s round=2 trigger_failed kind=%s "
            "(will retry on next load)",
            screening_session_id, type(exc).__name__,
        )
    except Exception:  # noqa: BLE001 - never surface to the candidate here
        db.rollback()
        logger.exception(
            "screening_questions session=%s round=2 trigger_unexpected",
            screening_session_id,
        )


def _trigger_screening_evaluation(
    db: Session, *, application_id: uuid.UUID | str
) -> None:
    """Run the per-candidate screening evaluation as SYSTEM, right after the
    session reaches ``SCREENING_COMPLETE`` (Phase 4 Step 4).

    Best-effort and mirrors :func:`_trigger_round_2`: the ``SCREENING_COMPLETE``
    transition is already committed, so a failure here corrupts nothing —
    ``screening_evaluation_service.ensure_screening_evaluated`` retries on the
    next page load, and HR has an explicit recovery button. Never raises to the
    candidate. The import is deferred to break the module cycle
    (``screening_evaluation_service`` imports this module for the transcript
    accessor).
    """
    try:
        from app.services.screening_evaluation_service import evaluate_screening

        evaluate_screening(
            db,
            application_id=application_id,
            requested_by_user_id=get_system_user_id(db),
        )
    except Exception:  # noqa: BLE001 - never surface to the candidate here
        db.rollback()
        logger.warning(
            "screening_evaluation application=%s auto_trigger_failed "
            "(will retry on next load)",
            application_id,
        )


def ensure_round_2_generated(
    db: Session, *, screening_session_id: uuid.UUID | str
) -> None:
    """Candidate-safe retry for round-2 generation.

    Called by the public page when it finds a session parked at
    ``ROUND_1_COMPLETE`` (round-2 generation failed on the submit path).
    Resolves SYSTEM internally and calls the idempotent generator. A no-op if
    the session has already moved past ``ROUND_1_COMPLETE``.
    """
    session = _get_session(db, screening_session_id)
    if session.status != ScreeningSessionStatus.ROUND_1_COMPLETE:
        return
    generate_round_questions(
        db,
        screening_session_id=session.id,
        round=ScreeningQuestionRound.ROUND_2,
        requested_by_user_id=get_system_user_id(db),
    )


# --- candidate-facing reads ---------------------------------


def get_round_questions(
    db: Session, *, screening_session_id: uuid.UUID | str, round: int
) -> list[ScreeningQuestionView]:
    """Candidate-safe questions + answers for one round. No guard, no
    ``generated_reason``, no criterion ids."""
    questions = _questions_for_round(db, screening_session_id, round)
    answers = _answers_by_question_id(db, [q.id for q in questions])
    out: list[ScreeningQuestionView] = []
    for q in questions:
        a = answers.get(q.id)
        out.append(
            ScreeningQuestionView(
                question_id=str(q.id),
                round=q.round,
                sequence_index=q.sequence_index,
                category=q.category,
                question_text=q.question_text,
                answer_text=a.answer_text if a else None,
                answered=a is not None,
            )
        )
    return out


def get_screening_transcript(
    db: Session,
    *,
    screening_session_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[ScreeningTranscriptItem]:
    """Both rounds' questions (full ORM fields — ``rubric_criterion_id``,
    ``generated_reason``, ``ai_model``) joined with their answers.

    HR/INTERNAL ONLY — :func:`~app.utils.authorization.require_internal_user` is
    checked first; the screening-evaluation task calls it as the SYSTEM actor.
    This exposes exactly what the candidate-facing :func:`get_round_questions`
    deliberately strips, so it must never be reachable from the public app.

    Reuses the private per-round helpers rather than re-querying. Rounds are
    concatenated in order (round 1 then round 2), each ordered by
    ``sequence_index``.
    """
    require_internal_user(db, acting_user_id)
    out: list[ScreeningTranscriptItem] = []
    for round_ in (ScreeningQuestionRound.ROUND_1, ScreeningQuestionRound.ROUND_2):
        questions = _questions_for_round(db, screening_session_id, round_)
        answers = _answers_by_question_id(db, [q.id for q in questions])
        for q in questions:
            a = answers.get(q.id)
            out.append(
                ScreeningTranscriptItem(
                    question_id=str(q.id),
                    round=q.round,
                    sequence_index=q.sequence_index,
                    category=q.category,
                    rubric_criterion_id=(
                        str(q.rubric_criterion_id)
                        if q.rubric_criterion_id
                        else None
                    ),
                    generated_reason=q.generated_reason,
                    ai_model=q.ai_model,
                    question_text=q.question_text,
                    answer_text=a.answer_text if a else None,
                    answered=a is not None,
                )
            )
    return out


# --- HR: mark a stalled mid-screening session abandoned -----


def abandon_screening(
    db: Session,
    *,
    screening_session_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
) -> bool:
    """Explicit HR action: record that a stalled screening was abandoned.

    Sets ``ApplicationStatus.SCREENING_INCOMPLETE`` and emits
    ``AI_SCREENING_INCOMPLETE`` — attributed to the HR user who made the call
    (this IS a human decision, unlike the automatic pipeline). It NEVER sets any
    rejection-equivalent status (there is none in this MVP — CLAUDE.md §§3, 11).

    This is the single, explicit decision point for "abandoned" — there is no
    automatic status flip during page loads. Idempotent: returns ``False`` if
    the application is already ``SCREENING_INCOMPLETE`` or the screening already
    ``SCREENING_COMPLETE``.

    Raises
    ------
    UnauthorizedError / ScreeningSessionNotFoundError
    ScreeningRoundPreconditionError
        The session is not in an abandonable state (already complete).
    """
    require_internal_user(db, requested_by_user_id)
    session = _get_session(db, screening_session_id)
    application = db.get(Application, session.application_id)

    if application.status == ApplicationStatus.SCREENING_INCOMPLETE:
        return False
    if session.status == ScreeningSessionStatus.SCREENING_COMPLETE:
        raise ScreeningRoundPreconditionError(
            "This screening is already complete; it cannot be marked abandoned."
        )

    prev_app = application.status
    application.status = ApplicationStatus.SCREENING_INCOMPLETE

    record_event(
        db,
        event_type=AuditEventType.AI_SCREENING_INCOMPLETE,
        action=(
            f"Screening marked abandoned for application {application.id} "
            f"(session status {session.status})."
        ),
        entity_type="screening_session",
        entity_id=session.id,
        user_id=requested_by_user_id,
        previous_state={"application_status": prev_app},
        new_state={
            "application_status": ApplicationStatus.SCREENING_INCOMPLETE,
            "screening_session_status": session.status,
        },
    )
    db.commit()
    logger.info(
        "screening session=%s outcome=abandoned by=internal_user", session.id
    )
    return True
