"""Interview-guide service — the personalized human-interview guide
(CLAUDE.md §§3, 6, 12, 18, 19, 22, 29; Phase 4 Step 7).

WHAT THE AI DOES vs WHAT PYTHON DOES
-----------------------------------
* AI: propose a set of interview questions across five categories
  (REQUIREMENTS / EXPERIENCE / BEHAVIORAL / RESUME_VALIDATION / PROBING), each
  mapped to a rubric criterion (or ``None``), with what it evaluates and why it
  was generated.
* Python (here):
  - enforces the shortlist precondition (generation blocked if the candidate is
    not currently shortlisted),
  - re-reads the rubric version from the **shortlist entry's captured id** —
    never the job's current-approved-rubric accessor — so the guide is always
    grounded in the criteria the candidate was evaluated and ranked against,
    even if that version is now SUPERSEDED,
  - enforces the total question-count bound (6-14),
  - rejects a hallucinated ``rubric_criterion_id`` (not in the captured
    version's criteria),
  - assigns ``sequence_index``,
  - persists ``interview_guides`` + ``interview_questions`` (replace-in-place on
    regeneration), emits one ``INTERVIEW_GUIDE_GENERATED`` audit event with SAFE
    metadata only, commits once.

AUTH — HR/INTERNAL ONLY
----------------------
Every function calls :func:`~app.utils.authorization.require_internal_user`
first. ``generate_interview_guide`` is an explicit HR action — attribution is
always the **real HR user** id passed in; this module never imports or uses the
SYSTEM actor.

IDEMPOTENCY (CLAUDE.md §29)
--------------------------
``force=False`` (the default) is check-first: an existing ``interview_guides``
row for the application is returned unchanged, with NO AI call, NO row change,
NO audit event. ``force=True`` deletes the old guide + its questions and inserts
a fresh set in one transaction.

SHORTLIST STATE
--------------
Generation requires ``is_shortlisted == True``. A guide already generated stays
readable via :func:`get_interview_guide_for_application` regardless of later
shortlist changes — an unshortlist does NOT delete or hide it (out of scope for
this step). A NEW generation for a now-unshortlisted application IS blocked.

PRIVACY (CLAUDE.md §§12, 19, 24)
------------------------------
``question_text`` / ``evaluates`` / ``generated_reason`` / résumé text /
screening answers / evidence text are NEVER logged or put in audit metadata.
Audit metadata carries ids, the rubric version number/status, and question
counts only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ai.claude_client import (
    AIError,
    AIOutputError,
    _resolve_model,
    get_structured_response,
)
from app.ai.prompts.interview_guide import build_interview_guide_prompt
from app.ai.schemas.interview_guide import (
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    InterviewGuideAssessment,
)
from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.candidate import Candidate
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.interview_guide import (
    InterviewGuide,
    InterviewQuestion,
    InterviewQuestionCategory,
)
from app.database.models.rubric import RubricCriterion, RubricVersion
from app.services.audit_service import record_event
from app.services.prequalification_service import (
    get_prequalification_for_application,
)
from app.services.resume_parsing_service import get_extraction_for_document
from app.services.rubric_service import list_criteria
from app.services.screening_evaluation_service import (
    get_screening_evaluation_for_application,
)
from app.services.screening_pipeline_service import (
    get_screening_session_for_application,
)
from app.services.screening_question_service import get_screening_transcript
from app.services.storage_service import list_documents_for_application
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

_TASK_NAME = "interview_guide"

_NOT_SHORTLISTED = (
    "This candidate is not currently shortlisted, so an interview guide cannot "
    "be generated. Shortlist them first."
)
_NO_INPUTS = (
    "The interview guide is missing an upstream input (shortlist rubric "
    "version, screening evaluation, prequalification, or resume). Re-run the "
    "earlier steps first."
)
_GUIDE_UNUSABLE = (
    "The AI's interview guide was incomplete or unusable. Please try again."
)
_GUIDE_FAILED = "Interview guide generation failed — please try again."


class InterviewGuideError(Exception):
    """Interview guide could not be produced. User-safe message; never wraps
    résumé / screening / AI free text."""


class InterviewGuideTargetNotFoundError(InterviewGuideError):
    """No such application."""


class InterviewGuidePreconditionError(InterviewGuideError):
    """The application is not currently shortlisted."""


# --- row shapes ---------------------------------------------------


@dataclass(frozen=True)
class InterviewQuestionRow:
    category: str
    rubric_criterion_id: uuid.UUID | None
    criterion_text: str | None
    sequence_index: int
    question_text: str
    evaluates: str
    generated_reason: str


@dataclass(frozen=True)
class InterviewGuideWithQuestions:
    guide_id: uuid.UUID
    application_id: uuid.UUID
    job_id: uuid.UUID
    shortlist_entry_id: uuid.UUID
    rubric_version_id: uuid.UUID
    rubric_version_number: int | None
    rubric_version_status: str | None
    ai_model: str
    generated_at: datetime
    questions: list[InterviewQuestionRow]


@dataclass(frozen=True)
class ShortlistedCandidateRow:
    application_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    shortlist_entry_id: uuid.UUID
    shortlist_reason: str | None
    rubric_version_id: uuid.UUID
    rubric_version_number: int | None
    rubric_version_status: str | None
    rank_position_at_decision: int | None
    current_rank_position: int | None
    current_rank_available: bool
    guide_exists: bool
    guide_id: uuid.UUID | None


# --- helpers ----------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _current_shortlist_entry(
    db: Session, application: Application
) -> CandidateShortlistEntry | None:
    return db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.job_id == application.job_id,
            CandidateShortlistEntry.application_id == application.id,
        )
    ).scalar_one_or_none()


def _resume_evidence(
    db: Session, application_id: uuid.UUID, *, acting_user_id: uuid.UUID | str
) -> dict:
    """First extracted résumé evidence for the application, or ``{}``.

    Mirrors ``screening_evaluation_service._resume_evidence`` — the two-call
    ``list_documents_for_application`` -> ``get_extraction_for_document`` pattern
    (no public per-application accessor exists)."""
    for document in list_documents_for_application(db, application_id):
        extraction = get_extraction_for_document(
            db, document.id, acting_user_id=acting_user_id
        )
        if extraction is not None:
            return extraction.extracted_data or {}
    return {}


def _evaluation_projection(evaluation) -> dict:
    """Plain dict of the ScreeningEvaluation summary fields for the prompt —
    never the per-criterion ``results`` JSON verbatim."""
    return {
        "requirements_score": evaluation.requirements_score,
        "requirements_coverage": evaluation.requirements_coverage,
        "experience_score": evaluation.experience_score,
        "experience_coverage": evaluation.experience_coverage,
        "behavioral_score": evaluation.behavioral_score,
        "behavioral_coverage": evaluation.behavioral_coverage,
        "strengths": list(evaluation.strengths or []),
        "gaps": list(evaluation.gaps or []),
        "unknowns": list(evaluation.unknowns or []),
        "overall_confidence": evaluation.overall_confidence,
        "ai_recommendation": evaluation.ai_recommendation,
    }


def _category_counts(questions) -> dict[str, int]:
    out = {c: 0 for c in InterviewQuestionCategory.ORDER}
    for q in questions:
        out[q.category] = out.get(q.category, 0) + 1
    return out


# --- reads ----------------------------------------------------


def _assemble_guide(db: Session, guide: InterviewGuide) -> InterviewGuideWithQuestions:
    version = db.get(RubricVersion, guide.rubric_version_id)
    rows = db.execute(
        select(InterviewQuestion, RubricCriterion)
        .outerjoin(
            RubricCriterion,
            RubricCriterion.id == InterviewQuestion.rubric_criterion_id,
        )
        .where(InterviewQuestion.interview_guide_id == guide.id)
    ).all()

    order = {c: i for i, c in enumerate(InterviewQuestionCategory.ORDER)}
    questions = sorted(
        (
            InterviewQuestionRow(
                category=q.category,
                rubric_criterion_id=q.rubric_criterion_id,
                criterion_text=(c.criterion_text if c is not None else None),
                sequence_index=q.sequence_index,
                question_text=q.question_text,
                evaluates=q.evaluates,
                generated_reason=q.generated_reason,
            )
            for q, c in rows
        ),
        key=lambda r: (order.get(r.category, 99), r.sequence_index),
    )

    return InterviewGuideWithQuestions(
        guide_id=guide.id,
        application_id=guide.application_id,
        job_id=guide.job_id,
        shortlist_entry_id=guide.shortlist_entry_id,
        rubric_version_id=guide.rubric_version_id,
        rubric_version_number=version.version_number if version else None,
        rubric_version_status=version.status if version else None,
        ai_model=guide.ai_model,
        generated_at=guide.generated_at,
        questions=questions,
    )


def get_interview_guide_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> InterviewGuideWithQuestions | None:
    """The current interview guide + its questions (grouped by category order,
    then ``sequence_index``), or ``None``. HR/INTERNAL ONLY.

    Works regardless of the application's current shortlist state — a guide
    generated earlier stays readable if the candidate is later unshortlisted.
    """
    require_internal_user(db, acting_user_id)
    guide = db.execute(
        select(InterviewGuide).where(
            InterviewGuide.application_id == _as_uuid(application_id)
        )
    ).scalar_one_or_none()
    if guide is None:
        return None
    return _assemble_guide(db, guide)


def list_interview_guides_for_job(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[InterviewGuideWithQuestions]:
    """Every interview guide for the job, each with its questions. HR/INTERNAL
    ONLY. Used by the UI to surface a guide that was generated while the
    candidate was shortlisted and must stay readable now that they are not."""
    require_internal_user(db, acting_user_id)
    guides = db.execute(
        select(InterviewGuide).where(InterviewGuide.job_id == _as_uuid(job_id))
    ).scalars().all()
    return [_assemble_guide(db, g) for g in guides]


def get_shortlisted_candidates_for_job(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[ShortlistedCandidateRow]:
    """Every currently-shortlisted candidate for the job, with the rubric-version
    partition the decision was made against, the rank at decision time, a fresh
    read-time current rank for that same partition, and whether a guide exists.
    HR/INTERNAL ONLY.

    Never merges or compares across ``rubric_version_id`` — each row carries its
    own partition; the caller renders one section per version.
    """
    require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)

    entries = db.execute(
        select(CandidateShortlistEntry).where(
            CandidateShortlistEntry.job_id == job_uuid,
            CandidateShortlistEntry.is_shortlisted.is_(True),
        )
    ).scalars().all()

    out: list[ShortlistedCandidateRow] = []
    for entry in entries:
        application = db.get(Application, entry.application_id)
        candidate = (
            db.get(Candidate, application.candidate_id)
            if application is not None
            else None
        )
        version = db.get(RubricVersion, entry.rubric_version_id)

        ranking_row = db.execute(
            select(CandidateRanking).where(
                CandidateRanking.job_id == job_uuid,
                CandidateRanking.rubric_version_id == entry.rubric_version_id,
                CandidateRanking.application_id == entry.application_id,
            )
        ).scalar_one_or_none()

        guide = db.execute(
            select(InterviewGuide.id).where(
                InterviewGuide.application_id == entry.application_id
            )
        ).scalar_one_or_none()

        out.append(
            ShortlistedCandidateRow(
                application_id=entry.application_id,
                candidate_name=candidate.full_name if candidate else "—",
                candidate_email=candidate.email if candidate else "—",
                shortlist_entry_id=entry.id,
                shortlist_reason=entry.reason,
                rubric_version_id=entry.rubric_version_id,
                rubric_version_number=version.version_number if version else None,
                rubric_version_status=version.status if version else None,
                rank_position_at_decision=entry.rank_position_at_decision,
                current_rank_position=(
                    ranking_row.rank_position if ranking_row else None
                ),
                current_rank_available=ranking_row is not None,
                guide_exists=guide is not None,
                guide_id=guide,
            )
        )

    out.sort(
        key=lambda r: (
            r.rubric_version_number is None,
            r.rubric_version_number or 0,
            r.current_rank_position is None,
            r.current_rank_position or 0,
            r.rank_position_at_decision is None,
            r.rank_position_at_decision or 0,
        )
    )
    return out


# --- main action -------------------------------------------


def generate_interview_guide(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
    force: bool = False,
) -> InterviewGuide:
    """Generate (or, with ``force=True``, regenerate) the interview guide for a
    shortlisted candidate and persist it.

    HR/INTERNAL ONLY. Attribution is the real HR user, never SYSTEM.

    Precondition (hard gate): the application must have a
    ``candidate_shortlist_entries`` row with ``is_shortlisted == True``.

    ``force=False``: an existing guide is returned unchanged (no AI call, no
    audit event). ``force=True``: the old guide + questions are deleted and a
    fresh set inserted, in one transaction.

    The rubric criteria are ALWAYS fetched via the shortlist entry's **captured**
    ``rubric_version_id`` — the job's current-approved-rubric accessor is never
    called here — so the guide is grounded in the criteria the candidate was
    evaluated and ranked against, even when that version is now SUPERSEDED.

    Raises
    ------
    UnauthorizedError
    InterviewGuideTargetNotFoundError
    InterviewGuidePreconditionError
    InterviewGuideError
        Missing upstream input, or the AI call / its output was unusable.
    """
    require_internal_user(db, requested_by_user_id)

    application = db.get(Application, application_id)
    if application is None:
        raise InterviewGuideTargetNotFoundError(
            f"No application with id {application_id!r}."
        )

    entry = _current_shortlist_entry(db, application)
    if entry is None or not entry.is_shortlisted:
        raise InterviewGuidePreconditionError(_NOT_SHORTLISTED)

    captured_rubric_version_id = entry.rubric_version_id

    existing = db.execute(
        select(InterviewGuide).where(
            InterviewGuide.application_id == application.id
        )
    ).scalar_one_or_none()
    if existing is not None and not force:
        return existing

    # --- resolve inputs (all before any AI call / DB mutation) -----------
    # NOTE: criteria come from the CAPTURED version id on the shortlist entry —
    # never the job's current approved rubric.
    version = db.get(RubricVersion, captured_rubric_version_id)
    criteria = list_criteria(db, captured_rubric_version_id)
    if not criteria:
        logger.warning(
            "interview_guide application=%s failure=no_criteria", application_id
        )
        raise InterviewGuideError(_NO_INPUTS)

    session = get_screening_session_for_application(
        db, application.id, acting_user_id=requested_by_user_id
    )
    if session is None:
        raise InterviewGuideError(_NO_INPUTS)

    evaluation = get_screening_evaluation_for_application(
        db, application.id, acting_user_id=requested_by_user_id
    )
    if evaluation is None:
        raise InterviewGuideError(_NO_INPUTS)

    prequal = get_prequalification_for_application(
        db, application.id, acting_user_id=requested_by_user_id
    )
    if prequal is None:
        raise InterviewGuideError(_NO_INPUTS)

    resume_evidence = _resume_evidence(
        db, application.id, acting_user_id=requested_by_user_id
    )
    transcript = get_screening_transcript(
        db, screening_session_id=session.id, acting_user_id=requested_by_user_id
    )
    transcript_dicts = [vars(item) for item in transcript]

    # --- AI call ------------------------------------------------------
    prompt = build_interview_guide_prompt(
        rubric_criteria=criteria,
        resume_evidence=resume_evidence,
        prequalification_results=prequal.results,
        screening_transcript=transcript_dicts,
        screening_evaluation=_evaluation_projection(evaluation),
    )
    try:
        assessment: InterviewGuideAssessment = get_structured_response(
            prompt, InterviewGuideAssessment, task_name=_TASK_NAME
        )
    except AIOutputError as exc:
        logger.warning(
            "interview_guide application=%s failure=validation", application_id
        )
        raise InterviewGuideError(_GUIDE_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "interview_guide application=%s failure=request kind=%s",
            application_id, type(exc).__name__,
        )
        raise InterviewGuideError(_GUIDE_FAILED) from exc

    # --- Python-side validation --------------------------------------
    questions = assessment.questions
    if not (MIN_QUESTIONS <= len(questions) <= MAX_QUESTIONS):
        logger.warning(
            "interview_guide application=%s failure=count n=%d",
            application_id, len(questions),
        )
        raise InterviewGuideError(_GUIDE_UNUSABLE)

    criteria_by_id: dict[str, uuid.UUID] = {str(c.id): c.id for c in criteria}
    mapped: list[uuid.UUID | None] = []
    for q in questions:
        if q.rubric_criterion_id is None:
            mapped.append(None)
            continue
        resolved = criteria_by_id.get(q.rubric_criterion_id)
        if resolved is None:
            # Hallucinated / stale criterion id — do NOT silently drop.
            logger.warning(
                "interview_guide application=%s failure=bad_criterion_id",
                application_id,
            )
            raise InterviewGuideError(_GUIDE_UNUSABLE)
        mapped.append(resolved)

    resolved_model = _resolve_model(_TASK_NAME, None)
    now = datetime.now(timezone.utc)

    # --- persist (one transaction) ---------------------------------
    replaced_id: uuid.UUID | None = None
    if existing is not None:
        replaced_id = existing.id
        db.execute(
            delete(InterviewQuestion).where(
                InterviewQuestion.interview_guide_id == existing.id
            )
        )
        db.delete(existing)
        db.flush()  # clear the UNIQUE(application_id) slot before re-insert

    guide = InterviewGuide(
        job_id=application.job_id,
        application_id=application.id,
        shortlist_entry_id=entry.id,
        rubric_version_id=captured_rubric_version_id,
        ai_model=resolved_model,
        generated_at=now,
    )
    db.add(guide)
    db.flush()

    for i, (q, criterion_uuid) in enumerate(zip(questions, mapped)):
        db.add(
            InterviewQuestion(
                interview_guide_id=guide.id,
                category=q.category,
                rubric_criterion_id=criterion_uuid,
                sequence_index=i,
                question_text=q.question_text,
                evaluates=q.evaluates,
                generated_reason=q.generated_reason,
            )
        )
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.INTERVIEW_GUIDE_GENERATED,
        action=(
            f"Interview guide generated for application {application.id} "
            f"against rubric v{version.version_number if version else '?'}"
            f"{' (re-run)' if existing is not None else ''}: "
            f"{len(questions)} question(s)."
        ),
        entity_type="application",
        entity_id=application.id,
        user_id=_as_uuid(requested_by_user_id),
        new_state={
            "job_id": str(application.job_id),
            "application_id": str(application.id),
            "interview_guide_id": str(guide.id),
            "shortlist_entry_id": str(entry.id),
            "rubric_version_id": str(captured_rubric_version_id),
            "rubric_version_number": (
                version.version_number if version else None
            ),
            "rubric_version_status": version.status if version else None,
            "forced": force,
            "replaced_guide_id": str(replaced_id) if replaced_id else None,
            "question_count": len(questions),
            "by_category": _category_counts(questions),
        },
    )

    db.commit()
    db.refresh(guide)
    logger.info(
        "interview_guide application=%s outcome=ok model=%s forced=%s "
        "questions=%d rubric_version=%s status=%s",
        application_id, resolved_model, force, len(questions),
        captured_rubric_version_id,
        version.status if version else "?",
    )
    return guide
