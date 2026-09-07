"""Prequalification service — compare one application's resume evidence against
the approved rubric, per criterion (CLAUDE.md §§4, 12, 18, 20, 23, 27, 29, B).

WHAT THE AI DOES vs WHAT PYTHON DOES (the core boundary of this step)
-------------------------------------------------------------------
* AI: for each approved-rubric criterion, look at the extracted evidence and
  return one reasoned ``PASS`` / ``FAIL`` / ``UNKNOWN`` with supporting text.
  That is genuine evidence-matching reasoning.
* Python (here):
  - validates the AI covered exactly every criterion, once (no silent fill),
  - computes each criterion's HIGH/MEDIUM/LOW **confidence** deterministically
    (``prequalification_confidence.compute_confidence`` — a documented rule set,
    NOT an AI output),
  - persists the combined result, linked to the exact rubric version + resume
    extraction it ran against.
  Python does NOT compute an overall recommendation / eligibility / score here —
  those are separate later steps (scorecard, ranking) and remain a
  recommendation, never an autonomous decision (CLAUDE.md §§4, 11).

Transaction model (matches ``resume_parsing_service`` / ``rubric_service``): the
caller passes a ``Session``; :func:`prequalify_application` is the top-level
business action — it resolves inputs, calls Claude, validates, then on success
only: inserts the ``prequalification_results`` row, moves the application status
to ``PREQUALIFICATION_COMPLETED``, writes one ``PREQUALIFICATION_COMPLETED``
audit event, and issues a single ``db.commit()``.

Manual-trigger only — the "Prequalify" button. Never auto-triggered.

AUTH BOUNDARY (CLAUDE.md §23) — ENFORCED IN CODE
-----------------------------------------------
:func:`prequalify_application` and :func:`get_prequalification_for_application`
are **HR/INTERNAL ONLY** (they read per-criterion PASS/FAIL/UNKNOWN judgments
about candidates). Both call
:func:`app.utils.authorization.require_internal_user` before any other work, and
:func:`prequalify_application` threads its validated ``requested_by_user_id``
into every downstream HR-only call (``get_prequalification_for_application``,
``get_extraction_for_document``). The whole chain
(prequalification -> resume_parsing -> storage) is now code-guarded end to end;
each link re-checks (defense in depth). A caller from ``app/public_main.py`` has
no user in session and gets ``UnauthorizedError`` immediately.

Scope of that guard: accidental exposure only (wrong import, future mistake) —
NOT a defense against a malicious authenticated internal user (different threat
model, out of scope).

PRIVACY / LOGGING (CLAUDE.md §§19, 23, 24)
-----------------------------------------
Raw rubric criterion text, resume evidence, and the AI's evidence/reasoning text
are NEVER logged or put in audit metadata. Log lines and audit metadata carry
task/model/counts/ids only.
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
from app.ai.prompts.prequalification import build_prequalification_prompt
from app.ai.schemas.prequalification import (
    CriterionAssessment,
    PrequalificationAssessment,
)
from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.prequalification_result import PrequalificationResult
from app.database.models.rubric import RubricCriterion
from app.services.audit_service import record_event
from app.services.prequalification_confidence import compute_confidence
from app.services.resume_parsing_service import get_extraction_for_document
from app.services.rubric_service import get_approved_rubric, list_criteria
from app.services.storage_service import list_documents_for_application
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

_TASK_NAME = "prequalification"

_PREQUAL_UNUSABLE = (
    "The AI's prequalification result was incomplete or unusable. Please try "
    "again."
)
_PREQUAL_FAILED = "Prequalification failed — please try again."
_NO_EXTRACTION = (
    "This application's resume has not been parsed yet. Run resume parsing "
    "first, then prequalify."
)
_NO_RUBRIC = (
    "This job has no approved evaluation rubric. Approve a rubric before "
    "prequalifying candidates."
)


class PrequalificationError(Exception):
    """Prequalification could not be completed. User-safe message; never wraps
    raw criterion / evidence / reasoning text."""


class PrequalificationTargetNotFoundError(PrequalificationError):
    """No application with the given id."""


class PrequalificationAlreadyExistsError(PrequalificationError):
    """A prequalification result already exists and ``force`` was not set."""


# --- reads --------------------------------------------------------------


def get_prequalification_for_application(
    db: Session, application_id: uuid.UUID | str, *, acting_user_id: uuid.UUID | str
) -> PrequalificationResult | None:
    """Return the prequalification result for an application, or ``None``.

    HR/INTERNAL ONLY — returns per-criterion candidate judgments.
    :func:`~app.utils.authorization.require_internal_user` is checked first;
    ``acting_user_id`` must resolve to an active internal user (raises
    :class:`~app.utils.authorization.UnauthorizedError` otherwise).
    """
    require_internal_user(db, acting_user_id)
    return db.execute(
        select(PrequalificationResult).where(
            PrequalificationResult.application_id == application_id
        )
    ).scalar_one_or_none()


def _resume_extraction_for_application(
    db: Session, application_id: uuid.UUID, *, acting_user_id: uuid.UUID | str
):
    """Resolve the application's resume extraction via its first document.

    ``acting_user_id`` is threaded into the HR-only ``get_extraction_for_document``
    call (already authorization-checked by the public caller; re-checked there).
    """
    documents = list_documents_for_application(db, application_id)
    for document in documents:
        extraction = get_extraction_for_document(
            db, document.id, acting_user_id=acting_user_id
        )
        if extraction is not None:
            return extraction
    return None


# --- helpers -----------------------------------------------------------


def _build_result_rows(
    criteria: list[RubricCriterion],
    assessments: list[CriterionAssessment],
) -> list[dict]:
    """Map AI assessments (by 1-based index) onto criteria, add Python
    confidence, and denormalize criterion fields.

    Raises
    ------
    PrequalificationError
        The AI did not return exactly one assessment per criterion (missing,
        duplicated, or out-of-range index). We do NOT silently back-fill — that
        would mask a broken prompt (CLAUDE.md §27).
    """
    by_index: dict[int, CriterionAssessment] = {}
    for a in assessments:
        if a.criterion_index in by_index:
            raise PrequalificationError(_PREQUAL_UNUSABLE)
        by_index[a.criterion_index] = a

    expected = set(range(1, len(criteria) + 1))
    if set(by_index) != expected:
        raise PrequalificationError(_PREQUAL_UNUSABLE)

    rows: list[dict] = []
    for i, criterion in enumerate(criteria, start=1):
        a = by_index[i]
        confidence = compute_confidence(
            result=a.result,
            evidence_summary=a.evidence_summary,
            reasoning=a.reasoning,
        )
        rows.append(
            {
                "criterion_id": str(criterion.id),
                "criterion_index": i,
                "requirement_type": criterion.requirement_type,
                "category": criterion.category,
                "criterion_text": criterion.criterion_text,
                "result": a.result,
                "evidence_summary": a.evidence_summary,
                "reasoning": a.reasoning,
                "confidence": confidence,  # Python-computed, NOT from the AI
            }
        )
    return rows


def _audit_counts(rows: list[dict]) -> dict:
    """Aggregate counts for the audit event — ids/counts only, no text."""
    by_result: dict[str, int] = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    by_confidence: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    mandatory_fail = 0
    mandatory_unknown = 0
    for r in rows:
        by_result[r["result"]] = by_result.get(r["result"], 0) + 1
        by_confidence[r["confidence"]] = by_confidence.get(r["confidence"], 0) + 1
        if r["requirement_type"] == "MANDATORY":
            if r["result"] == "FAIL":
                mandatory_fail += 1
            elif r["result"] == "UNKNOWN":
                mandatory_unknown += 1
    return {
        "criteria_count": len(rows),
        "by_result": by_result,
        "by_confidence": by_confidence,
        "mandatory_fail_count": mandatory_fail,
        "mandatory_unknown_count": mandatory_unknown,
    }


# --- main action -----------------------------------------------------


def prequalify_application(
    db: Session,
    *,
    application_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
    force: bool = False,
) -> PrequalificationResult:
    """Run the prequalification AI task for one application and persist it.

    Manual-trigger only. HR/INTERNAL ONLY —
    :func:`~app.utils.authorization.require_internal_user` is checked as the
    first line, before any business DB read, any AI call, or any downstream
    HR-only call. ``requested_by_user_id`` is required (non-``None``) and doubles
    as the authorization subject and the audit actor.

    Re-run behaviour (CLAUDE.md §29, mirrors ``resume_parsing_service``): if a
    result already exists for ``application_id`` and ``force`` is False, raises
    :class:`PrequalificationAlreadyExistsError` **without** an AI call or any DB
    write. With ``force=True`` the existing row is deleted and replaced in the
    same transaction.

    On success: inserts one ``prequalification_results`` row, moves the
    application to ``PREQUALIFICATION_COMPLETED``, writes one
    ``PREQUALIFICATION_COMPLETED`` audit event, and commits once.

    Raises
    ------
    UnauthorizedError
        ``requested_by_user_id`` is missing / malformed / unknown / inactive.
        Raised before any DB read, AI call or downstream call.
    PrequalificationTargetNotFoundError
        No application with ``application_id``.
    PrequalificationAlreadyExistsError
        A result exists and ``force`` is False.
    PrequalificationError
        No resume extraction yet (run resume parsing first — NOT auto-triggered),
        no approved rubric, or the AI call / its output was unusable. No DB
        changes were made.
    """
    require_internal_user(db, requested_by_user_id)

    application = db.get(Application, application_id)
    if application is None:
        raise PrequalificationTargetNotFoundError(
            f"No application with id {application_id!r}."
        )

    existing = get_prequalification_for_application(
        db, application.id, acting_user_id=requested_by_user_id
    )
    if existing is not None and not force:
        raise PrequalificationAlreadyExistsError(
            "This application has already been prequalified. Re-running must be "
            "explicitly requested."
        )

    # --- resolve inputs (all before any AI call / DB mutation) -----------
    extraction = _resume_extraction_for_application(
        db, application.id, acting_user_id=requested_by_user_id
    )
    if extraction is None:
        logger.warning(
            "prequalification application=%s failure=no_extraction", application_id
        )
        raise PrequalificationError(_NO_EXTRACTION)

    approved_rubric = get_approved_rubric(db, application.job_id)
    if approved_rubric is None:
        # Structurally near-unreachable: an application can only exist if a link
        # was generated, which requires an approved rubric (see
        # application_link_service.generate_link). Kept as a defensive guard.
        logger.warning(
            "prequalification application=%s failure=no_approved_rubric",
            application_id,
        )
        raise PrequalificationError(_NO_RUBRIC)

    criteria = list_criteria(db, approved_rubric.id)  # ordered by display_order
    if not criteria:
        # Also near-unreachable: an approved rubric always has >= 1 MANDATORY
        # criterion (rubric_service.approve_rubric enforces it). Defensive.
        logger.warning(
            "prequalification application=%s failure=empty_rubric", application_id
        )
        raise PrequalificationError(_NO_RUBRIC)

    # --- AI call --------------------------------------------------------
    prompt = build_prequalification_prompt(criteria, extraction.extracted_data)
    try:
        assessment: PrequalificationAssessment = get_structured_response(
            prompt, PrequalificationAssessment, task_name=_TASK_NAME
        )
    except AIOutputError as exc:
        logger.warning(
            "prequalification application=%s failure=validation "
            "(invalid AI output after retries)",
            application_id,
        )
        raise PrequalificationError(_PREQUAL_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "prequalification application=%s failure=request kind=%s",
            application_id, type(exc).__name__,
        )
        raise PrequalificationError(_PREQUAL_FAILED) from exc

    # --- Python-side: coverage validation + deterministic confidence -----
    result_rows = _build_result_rows(criteria, assessment.assessments)
    resolved_model = _resolve_model(_TASK_NAME, None)

    # --- persist (one transaction) -------------------------------------
    replaced_id: uuid.UUID | None = None
    if existing is not None:
        replaced_id = existing.id
        db.delete(existing)
        db.flush()

    prequalification = PrequalificationResult(
        application_id=application.id,
        rubric_version_id=approved_rubric.id,
        resume_extraction_id=extraction.id,
        results=result_rows,
        ai_model=resolved_model,
    )
    db.add(prequalification)
    db.flush()  # assign id

    previous_status = application.status
    application.status = ApplicationStatus.PREQUALIFICATION_COMPLETED

    counts = _audit_counts(result_rows)
    record_event(
        db,
        event_type=AuditEventType.PREQUALIFICATION_COMPLETED,
        action=(
            f"Prequalification completed for application {application.id} "
            f"against rubric v{approved_rubric.version_number}"
            f"{' (re-run)' if existing is not None else ''}."
        ),
        entity_type="application",
        entity_id=application.id,
        user_id=requested_by_user_id,
        previous_state={"application_status": previous_status},
        new_state={
            "prequalification_id": str(prequalification.id),
            "rubric_version_id": str(approved_rubric.id),
            "rubric_version_number": approved_rubric.version_number,
            "resume_extraction_id": str(extraction.id),
            "ai_model": resolved_model,
            "forced": force,
            "replaced_prequalification_id": (
                str(replaced_id) if replaced_id else None
            ),
            "application_status": ApplicationStatus.PREQUALIFICATION_COMPLETED,
            **counts,
        },
    )

    db.commit()
    db.refresh(prequalification)
    logger.info(
        "prequalification application=%s outcome=ok model=%s forced=%s counts=%s",
        application_id, resolved_model, force, counts,
    )
    return prequalification
