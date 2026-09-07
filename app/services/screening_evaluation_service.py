"""Screening-evaluation service — the per-candidate initial scorecard
(CLAUDE.md §§4, 12, 18, 20, 21, 23, 27, 29, B; Phase 4 Step 4).

WHAT THE AI DOES vs WHAT PYTHON DOES
-----------------------------------
* AI: for each rubric criterion, reconcile the resume-only prequalification
  verdict against any screening question (with a submitted answer) that
  targeted it, and return one reasoned PASS/FAIL/UNKNOWN with supporting text.
* Python (here):
  - validates the AI covered exactly every criterion, once (no silent fill —
    same rule as ``prequalification_service._build_result_rows``),
  - **enforces reconciliation**: a criterion with NO answered screening
    question is copied verbatim from the prequalification row and the AI's
    output for it is discarded — so an UNKNOWN with no answer can never be
    promoted/demoted here (CLAUDE.md §B),
  - re-derives each criterion's HIGH/MEDIUM/LOW confidence deterministically
    (``prequalification_confidence.compute_confidence`` on the final strings),
  - computes the bucket scores + coverage (``screening_scoring`` — the
    documented formula, NOT an AI output),
  - computes the aggregate confidence (``compute_overall_confidence``),
  - computes the PROCEED/HOLD recommendation (``compute_recommendation`` —
    can never be REJECT),
  - assembles strengths/gaps/unknowns as a projection of the per-criterion
    list (never AI free text),
  - persists, moves the application to ``SCREENING_EVALUATED``, writes one
    ``SCORE_GENERATED`` audit event (SAFE metadata only), commits once.

AUTH — HR/INTERNAL ONLY
----------------------
:func:`evaluate_screening` and :func:`get_screening_evaluation_for_application`
call :func:`~app.utils.authorization.require_internal_user` first. The automatic
trigger (:func:`ensure_screening_evaluated` /
``screening_question_service._trigger_screening_evaluation``) calls in as the
SYSTEM actor — exactly like every other AI-task service.

IDEMPOTENCY (CLAUDE.md §29)
--------------------------
``force=False`` (the default) is check-first: if a ``screening_evaluations`` row
already exists for the session it is returned unchanged, with **no** AI call.
``force=True`` deletes and replaces in one transaction (same as
``prequalification_service``). The automatic trigger is best-effort and safely
retryable — durable state up to ``SCREENING_COMPLETE`` is never corrupted.

PRIVACY / LOGGING (CLAUDE.md §§19, 23, 24)
-----------------------------------------
Raw criterion / evidence / reasoning / answer / question text is NEVER logged
or put in audit metadata. Audit metadata carries bucket scores/coverage,
overall confidence, recommendation, and result counts only.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.claude_client import (
    AIError,
    AIOutputError,
    _resolve_model,
    get_structured_response,
)
from app.ai.prompts.screening_evaluation import build_screening_evaluation_prompt
from app.ai.schemas.screening_evaluation import (
    CriterionEvaluation,
    ScreeningEvaluationAssessment,
)
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.rubric import RubricCriterion
from app.database.models.screening_evaluation import (
    ScreeningEvaluation,
    ScreeningRecommendation,
)
from app.database.models.screening_session import (
    ScreeningSession,
    ScreeningSessionStatus,
)
from app.services import screening_scoring
from app.services.audit_service import record_event
from app.services.prequalification_confidence import (
    compute_confidence,
    compute_overall_confidence,
)
from app.services.prequalification_service import (
    get_prequalification_for_application,
)
from app.services.resume_parsing_service import get_extraction_for_document
from app.services.rubric_service import get_approved_rubric, list_criteria
from app.services.screening_question_service import get_screening_transcript
from app.services.storage_service import list_documents_for_application
from app.services.system_user_service import get_system_user_id
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

_TASK_NAME = "screening_evaluation"

_EVAL_UNUSABLE = (
    "The AI's screening evaluation was incomplete or unusable. Please try again."
)
_EVAL_FAILED = "Screening evaluation failed — please try again."
_NOT_COMPLETE = (
    "This candidate's screening is not complete, so it cannot be evaluated yet."
)
_NO_INPUTS = (
    "Screening evaluation is missing an upstream input (prequalification, "
    "resume, or approved rubric). Re-run the earlier steps first."
)

_UNTARGETED_UNKNOWN_EVIDENCE = (
    "No prequalification result and no screening question covered this "
    "criterion, so it could not be assessed."
)
_UNTARGETED_UNKNOWN_REASONING = (
    "Criterion not evaluated at the resume or screening stage."
)


class ScreeningEvaluationError(Exception):
    """Screening evaluation could not be completed. User-safe message; never
    wraps raw criterion / evidence / reasoning / answer text."""


class ScreeningEvaluationTargetNotFoundError(ScreeningEvaluationError):
    """No application, or no screening session for it."""


class ScreeningEvaluationPreconditionError(ScreeningEvaluationError):
    """The screening session is not ``SCREENING_COMPLETE``."""


# --- reads --------------------------------------------------------


def get_screening_evaluation_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> ScreeningEvaluation | None:
    """Return the screening evaluation for an application, or ``None``.

    HR/INTERNAL ONLY — returns per-criterion candidate judgments + scores.
    """
    require_internal_user(db, acting_user_id)
    return db.execute(
        select(ScreeningEvaluation)
        .join(
            ScreeningSession,
            ScreeningSession.id == ScreeningEvaluation.screening_session_id,
        )
        .where(ScreeningSession.application_id == application_id)
    ).scalar_one_or_none()


def _session_for_application(
    db: Session, application_id: uuid.UUID | str
) -> ScreeningSession | None:
    return db.execute(
        select(ScreeningSession).where(
            ScreeningSession.application_id == application_id
        )
    ).scalar_one_or_none()


def _resume_evidence(
    db: Session, application_id: uuid.UUID, *, acting_user_id: uuid.UUID | str
) -> dict | None:
    for document in list_documents_for_application(db, application_id):
        extraction = get_extraction_for_document(
            db, document.id, acting_user_id=acting_user_id
        )
        if extraction is not None:
            return extraction.extracted_data
    return None


# --- helpers -----------------------------------------------------


def _index_ai_assessments(
    criteria: list[RubricCriterion],
    assessments: list[CriterionEvaluation],
) -> dict[int, CriterionEvaluation]:
    """1-based index -> assessment, with exactly-one-per-criterion enforcement.

    Same discipline as ``prequalification_service._build_result_rows`` — no
    silent back-fill; a missing/duplicated/out-of-range index is a broken
    prompt, surfaced as an error (CLAUDE.md §27).
    """
    by_index: dict[int, CriterionEvaluation] = {}
    for a in assessments:
        if a.criterion_index in by_index:
            raise ScreeningEvaluationError(_EVAL_UNUSABLE)
        by_index[a.criterion_index] = a
    if set(by_index) != set(range(1, len(criteria) + 1)):
        raise ScreeningEvaluationError(_EVAL_UNUSABLE)
    return by_index


def _reconcile_rows(
    *,
    criteria: list[RubricCriterion],
    ai_by_index: dict[int, CriterionEvaluation],
    prequal_by_criterion_id: dict[str, dict],
    targeted_criterion_ids: set[str],
) -> list[dict]:
    """Build the final per-criterion list.

    * targeted (an answered screening question maps to it) -> AI's reconciled
      verdict,
    * else present in prequalification -> copied verbatim,
    * else -> UNKNOWN with an explicit note.
    Confidence is re-derived from the FINAL strings in every case.
    """
    rows: list[dict] = []
    for i, criterion in enumerate(criteria, start=1):
        cid = str(criterion.id)
        if cid in targeted_criterion_ids:
            a = ai_by_index[i]
            result = a.result
            evidence_summary = a.evidence_summary
            reasoning = a.reasoning
            source = "reconciled"
        elif cid in prequal_by_criterion_id:
            pr = prequal_by_criterion_id[cid]
            result = pr["result"]
            evidence_summary = pr.get("evidence_summary", "")
            reasoning = pr.get("reasoning", "")
            source = "prequalification_passthrough"
        else:
            result = "UNKNOWN"
            evidence_summary = _UNTARGETED_UNKNOWN_EVIDENCE
            reasoning = _UNTARGETED_UNKNOWN_REASONING
            source = "not_assessed"

        confidence = compute_confidence(
            result=result,
            evidence_summary=evidence_summary,
            reasoning=reasoning,
        )
        rows.append(
            {
                "criterion_id": cid,
                "criterion_index": i,
                "requirement_type": criterion.requirement_type,
                "category": criterion.category,
                "criterion_text": criterion.criterion_text,
                "result": result,
                "evidence_summary": evidence_summary,
                "reasoning": reasoning,
                "confidence": confidence,
                "source": source,  # provenance for the HR view / audit-free
            }
        )
    return rows


def _audit_metadata(
    *,
    rows: list[dict],
    buckets: dict,
    overall_confidence: str,
    recommendation: str,
    ai_model: str,
) -> dict:
    """SAFE-only audit metadata — scores / counts / model. NEVER text."""
    return {
        "criteria_count": len(rows),
        "by_result": screening_scoring.result_counts(rows),
        "by_source": {
            s: sum(1 for r in rows if r["source"] == s)
            for s in ("reconciled", "prequalification_passthrough", "not_assessed")
        },
        "requirements_score": buckets["requirements_score"],
        "requirements_coverage": buckets["requirements_coverage"],
        "experience_score": buckets["experience_score"],
        "experience_coverage": buckets["experience_coverage"],
        "behavioral_score": buckets["behavioral_score"],
        "behavioral_coverage": buckets["behavioral_coverage"],
        "overall_confidence": overall_confidence,
        "ai_recommendation": recommendation,
        "ai_model": ai_model,
    }


# --- main action ---------------------------------------------


def evaluate_screening(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
    force: bool = False,
) -> ScreeningEvaluation:
    """Run the screening-evaluation AI task for one application and persist it.

    HR/INTERNAL ONLY. Check-first idempotent (``force=False``): an existing row
    is returned with no AI call. Precondition: the screening session must be
    ``SCREENING_COMPLETE``.

    On success (one transaction): inserts one ``screening_evaluations`` row,
    moves the application to ``SCREENING_EVALUATED``, writes one
    ``SCORE_GENERATED`` audit event (safe metadata only), commits once.

    Raises
    ------
    UnauthorizedError
    ScreeningEvaluationTargetNotFoundError
    ScreeningEvaluationPreconditionError
    ScreeningEvaluationError
        Missing upstream input, or the AI call / its output was unusable.
    """
    require_internal_user(db, requested_by_user_id)

    application = db.get(Application, application_id)
    if application is None:
        raise ScreeningEvaluationTargetNotFoundError(
            f"No application with id {application_id!r}."
        )

    session = _session_for_application(db, application.id)
    if session is None:
        raise ScreeningEvaluationTargetNotFoundError(
            f"No screening session for application {application_id!r}."
        )

    if session.status != ScreeningSessionStatus.SCREENING_COMPLETE:
        raise ScreeningEvaluationPreconditionError(_NOT_COMPLETE)

    existing = db.execute(
        select(ScreeningEvaluation).where(
            ScreeningEvaluation.screening_session_id == session.id
        )
    ).scalar_one_or_none()
    if existing is not None and not force:
        return existing

    # --- resolve inputs (all before any AI call / DB mutation) -----------
    approved_rubric = get_approved_rubric(db, application.job_id)
    if approved_rubric is None:
        logger.warning(
            "screening_evaluation application=%s failure=no_approved_rubric",
            application_id,
        )
        raise ScreeningEvaluationError(_NO_INPUTS)
    criteria = list_criteria(db, approved_rubric.id)  # ordered by display_order
    if not criteria:
        raise ScreeningEvaluationError(_NO_INPUTS)

    prequal = get_prequalification_for_application(
        db, application.id, acting_user_id=requested_by_user_id
    )
    if prequal is None:
        logger.warning(
            "screening_evaluation application=%s failure=no_prequalification",
            application_id,
        )
        raise ScreeningEvaluationError(_NO_INPUTS)

    resume_evidence = _resume_evidence(
        db, application.id, acting_user_id=requested_by_user_id
    ) or {}

    transcript = get_screening_transcript(
        db, screening_session_id=session.id, acting_user_id=requested_by_user_id
    )
    transcript_dicts = [vars(item) for item in transcript]

    # --- AI call --------------------------------------------------------
    prompt = build_screening_evaluation_prompt(
        rubric_criteria=criteria,
        prequalification_results=prequal.results,
        resume_evidence=resume_evidence,
        screening_transcript=transcript_dicts,
    )
    try:
        assessment: ScreeningEvaluationAssessment = get_structured_response(
            prompt, ScreeningEvaluationAssessment, task_name=_TASK_NAME
        )
    except AIOutputError as exc:
        logger.warning(
            "screening_evaluation application=%s failure=validation", application_id
        )
        raise ScreeningEvaluationError(_EVAL_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "screening_evaluation application=%s failure=request kind=%s",
            application_id, type(exc).__name__,
        )
        raise ScreeningEvaluationError(_EVAL_FAILED) from exc

    # --- Python-side reconciliation + scoring -------------------------
    ai_by_index = _index_ai_assessments(criteria, assessment.assessments)
    prequal_by_criterion_id = {
        r["criterion_id"]: r for r in prequal.results if r.get("criterion_id")
    }
    targeted_criterion_ids = {
        item.rubric_criterion_id
        for item in transcript
        if item.answered and item.rubric_criterion_id
    }

    rows = _reconcile_rows(
        criteria=criteria,
        ai_by_index=ai_by_index,
        prequal_by_criterion_id=prequal_by_criterion_id,
        targeted_criterion_ids=targeted_criterion_ids,
    )

    buckets = screening_scoring.compute_bucket_scores(rows)
    overall_confidence = compute_overall_confidence(rows)
    recommendation = screening_scoring.compute_recommendation(
        rows, overall_confidence
    )
    # Defensive: the automated path must never produce REJECT (CLAUDE.md §11).
    if recommendation not in ScreeningRecommendation.AUTOMATED:  # pragma: no cover
        raise ScreeningEvaluationError(_EVAL_FAILED)

    strengths, gaps, unknowns = screening_scoring.assemble_strengths_gaps_unknowns(
        rows
    )
    resolved_model = _resolve_model(_TASK_NAME, None)

    # --- persist (one transaction) --------------------------------
    replaced_id: uuid.UUID | None = None
    if existing is not None:
        replaced_id = existing.id
        db.delete(existing)
        db.flush()

    # Strip the internal-only ``source`` key before persisting the JSON so the
    # stored shape matches PrequalificationResult.results exactly.
    stored_rows = [{k: v for k, v in r.items() if k != "source"} for r in rows]

    evaluation = ScreeningEvaluation(
        screening_session_id=session.id,
        rubric_version_id=approved_rubric.id,
        results=stored_rows,
        requirements_score=buckets["requirements_score"],
        requirements_coverage=buckets["requirements_coverage"],
        experience_score=buckets["experience_score"],
        experience_coverage=buckets["experience_coverage"],
        behavioral_score=buckets["behavioral_score"],
        behavioral_coverage=buckets["behavioral_coverage"],
        strengths=strengths,
        gaps=gaps,
        unknowns=unknowns,
        overall_confidence=overall_confidence,
        ai_recommendation=recommendation,
        ai_model=resolved_model,
    )
    db.add(evaluation)
    db.flush()

    previous_status = application.status
    application.status = ApplicationStatus.SCREENING_EVALUATED

    record_event(
        db,
        event_type=AuditEventType.SCORE_GENERATED,
        action=(
            f"Screening evaluation completed for application {application.id} "
            f"against rubric v{approved_rubric.version_number}"
            f"{' (re-run)' if existing is not None else ''}."
        ),
        entity_type="application",
        entity_id=application.id,
        user_id=requested_by_user_id,
        previous_state={"application_status": previous_status},
        new_state={
            "screening_evaluation_id": str(evaluation.id),
            "rubric_version_id": str(approved_rubric.id),
            "rubric_version_number": approved_rubric.version_number,
            "forced": force,
            "replaced_evaluation_id": (
                str(replaced_id) if replaced_id else None
            ),
            "application_status": ApplicationStatus.SCREENING_EVALUATED,
            **_audit_metadata(
                rows=rows,
                buckets=buckets,
                overall_confidence=overall_confidence,
                recommendation=recommendation,
                ai_model=resolved_model,
            ),
        },
    )

    db.commit()
    db.refresh(evaluation)
    logger.info(
        "screening_evaluation application=%s outcome=ok model=%s forced=%s "
        "rec=%s conf=%s scores=req:%s/exp:%s/beh:%s",
        application_id, resolved_model, force, recommendation, overall_confidence,
        buckets["requirements_score"], buckets["experience_score"],
        buckets["behavioral_score"],
    )
    return evaluation


def ensure_screening_evaluated(
    db: Session, *, application_id: uuid.UUID | str
) -> None:
    """Best-effort, SYSTEM-attributed retry for the automatic evaluation.

    Called by the public page / HR page when a screening is ``SCREENING_
    COMPLETE`` but no ``screening_evaluations`` row exists (the auto-trigger on
    the completion path failed). Idempotent — a no-op if an evaluation already
    exists or the session is not yet complete. Never raises to a candidate.
    """
    session = _session_for_application(db, application_id)
    if session is None:
        return
    if session.status != ScreeningSessionStatus.SCREENING_COMPLETE:
        return
    evaluate_screening(
        db,
        application_id=application_id,
        requested_by_user_id=get_system_user_id(db),
    )
