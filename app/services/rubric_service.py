"""Rubric service — generate, edit, and approve a job's evaluation rubric.

Phase 1.3 scope: generation from ``job_requirements``, HR edit while DRAFT, and
explicit approval + versioning. NO scoring/weighting (scoring-engine phase) and
NO application-link gating on approval (Phase 1.4).

Transaction model (matches ``job_service``): the caller passes a ``Session``;
this service does the work, writes audit event(s) via
``audit_service.record_event`` (flush-not-commit), and issues a single
``commit`` per business action. The Streamlit UI wraps each call in
``session_scope()``.

Invariants enforced here in Python (not DB constraints), per the project's
"business rules in Python" principle:

* At most one ``DRAFT`` rubric_version per job at a time.
* At most one ``APPROVED`` rubric_version per job at a time.
* An ``APPROVED`` version is immutable — its criteria are never updated/deleted,
  and it is only ever moved to ``SUPERSEDED`` by an explicit approval of a
  *later* version (CLAUDE.md §F: no silent rubric change after approval).

Audit events
------------
* ``generate_rubric``  -> ``RUBRIC_GENERATED``
* ``update/add/delete_criterion`` -> ``RUBRIC_EDITED``
* ``approve_rubric``   -> ``RUBRIC_APPROVED`` **and** ``RUBRIC_VERSION_CREATED``
  (see the module-level note in the Phase 1.3 summary for the rationale: the
  human decision and the creation of the locked, evaluation-facing artifact are
  two distinct facts, both enumerated in CLAUDE.md §12).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.claude_client import AIError, AIOutputError, get_structured_response
from app.ai.prompts.rubric_generation import build_rubric_generation_prompt
from app.ai.schemas.rubric_generation import RubricGenerationResult
from app.database.models.audit_event import AuditEventType
from app.database.models.job import Job, JobStatus
from app.database.models.job_requirement import RequirementType
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.services.audit_service import record_event
from app.services.job_service import get_current_requirements

logger = logging.getLogger(__name__)


class RubricError(Exception):
    """Base class for rubric-service failures."""


class RubricNotFoundError(RubricError):
    """Target job / rubric version / criterion does not exist."""


class RubricStateError(RubricError):
    """Operation not allowed in the current state (wrong status, no mandatory…)."""


class RubricGenerationError(RubricError):
    """AI rubric generation failed or returned an unusable result.

    When raised, no rubric rows and no job.status change were made.
    """


_GEN_UNUSABLE = "AI rubric generation returned an unusable result. Please try again."
_GEN_FAILED = "AI rubric generation failed — please try again."

_TYPE_ORDER = ["MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_requirement_type(value: str) -> str:
    if not RequirementType.is_valid(value):
        raise RubricStateError(
            f"requirement_type must be one of {sorted(RequirementType.ALL)}, "
            f"got {value!r}"
        )
    return value


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


# --- reads ----------------------------------------------------------------


def get_rubric_version(
    db: Session, rubric_version_id: uuid.UUID | str
) -> RubricVersion | None:
    return db.get(RubricVersion, rubric_version_id)


def get_current_draft(db: Session, job_id: uuid.UUID | str) -> RubricVersion | None:
    return db.execute(
        select(RubricVersion).where(
            RubricVersion.job_id == job_id,
            RubricVersion.status == RubricVersionStatus.DRAFT,
        )
    ).scalar_one_or_none()


def get_approved_rubric(db: Session, job_id: uuid.UUID | str) -> RubricVersion | None:
    return db.execute(
        select(RubricVersion).where(
            RubricVersion.job_id == job_id,
            RubricVersion.status == RubricVersionStatus.APPROVED,
        )
    ).scalar_one_or_none()


def list_criteria(
    db: Session, rubric_version_id: uuid.UUID | str
) -> list[RubricCriterion]:
    """Criteria of a version, ordered by ``display_order``."""
    return list(
        db.execute(
            select(RubricCriterion)
            .where(RubricCriterion.rubric_version_id == rubric_version_id)
            .order_by(RubricCriterion.display_order)
        ).scalars().all()
    )


def _mandatory_count(db: Session, rubric_version_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(RubricCriterion)
        .where(
            RubricCriterion.rubric_version_id == rubric_version_id,
            RubricCriterion.requirement_type == RequirementType.MANDATORY,
        )
    ).scalar_one()


# --- generation ---------------------------------------------------------


def generate_rubric(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
) -> RubricVersion:
    """Generate a proposed DRAFT rubric for a job from its current requirements.

    Manual-trigger only (the "Generate Rubric" button) — never automatic
    (CLAUDE.md §§9, 24).

    An existing DRAFT for the job is marked SUPERSEDED. An existing APPROVED
    version is **left untouched** — the new DRAFT coexists with it, and only an
    explicit :func:`approve_rubric` on the new DRAFT replaces the approved one.

    Ordering guarantee: the AI call happens *before* any DB mutation, so a
    failed generation leaves the prior DRAFT (if any) and the APPROVED version
    (if any) completely unchanged.

    Raises
    ------
    RubricNotFoundError
        No job with ``job_id``.
    RubricStateError
        Job is still DRAFT (JD not analysed) or has no current requirements.
    RubricGenerationError
        The AI call or its output failed. No DB changes were made.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise RubricNotFoundError(f"No job with id {job_id!r}.")

    if job.status == JobStatus.DRAFT:
        raise RubricStateError(
            "Analyse the Job Description before generating a rubric."
        )

    requirements = get_current_requirements(db, job.id)
    if not requirements:
        raise RubricStateError(
            "This job has no current requirements to build a rubric from."
        )
    requirements_version = requirements[0].source_version

    prompt = build_rubric_generation_prompt(requirements)
    try:
        result: RubricGenerationResult = get_structured_response(
            prompt, RubricGenerationResult, task_name="rubric_generation"
        )
    except AIOutputError as exc:
        logger.warning(
            "rubric_generation job=%s failure=validation (invalid/empty/no-mandatory "
            "AI output after retries)",
            job_id,
        )
        raise RubricGenerationError(_GEN_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "rubric_generation job=%s failure=request kind=%s",
            job_id, type(exc).__name__,
        )
        raise RubricGenerationError(_GEN_FAILED) from exc

    # --- AI succeeded: now (and only now) mutate the DB, in one transaction ---
    superseded_draft_version: int | None = None
    existing_draft = get_current_draft(db, job.id)
    if existing_draft is not None:
        existing_draft.status = RubricVersionStatus.SUPERSEDED
        superseded_draft_version = existing_draft.version_number

    prev_max = db.execute(
        select(func.max(RubricVersion.version_number)).where(
            RubricVersion.job_id == job.id
        )
    ).scalar()
    next_version_number = (prev_max or 0) + 1

    version = RubricVersion(
        job_id=job.id,
        version_number=next_version_number,
        status=RubricVersionStatus.DRAFT,
        generated_from_requirements_version=requirements_version,
        created_by=requested_by_user_id,
    )
    db.add(version)
    db.flush()  # assign version.id

    index_to_requirement_id = {i: req.id for i, req in enumerate(requirements, start=1)}
    unresolved = 0
    for order, proposed in enumerate(result.criteria, start=1):
        src_id = None
        idx = proposed.source_requirement_index
        if idx is not None:
            src_id = index_to_requirement_id.get(idx)
            if src_id is None:
                unresolved += 1
                logger.warning(
                    "rubric_generation job=%s version=%d: source_requirement_index=%r "
                    "out of range (have %d requirements) — storing NULL",
                    job_id, next_version_number, idx, len(requirements),
                )
        db.add(
            RubricCriterion(
                rubric_version_id=version.id,
                requirement_type=proposed.requirement_type,
                category=proposed.category,
                criterion_text=proposed.criterion_text,
                display_order=order,
                source_job_requirement_id=src_id,
            )
        )

    # Job status: JD_ANALYZED -> RUBRIC_PENDING on the first draft. If an
    # APPROVED rubric already exists the job stays RUBRIC_APPROVED — the
    # approved rubric remains the effective one until this new draft is itself
    # approved (CLAUDE.md §F).
    previous_status = job.status
    if job.status == JobStatus.JD_ANALYZED:
        job.status = JobStatus.RUBRIC_PENDING
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.RUBRIC_GENERATED,
        action=(
            f"Rubric v{next_version_number} generated for '{job.title}': "
            f"{len(result.criteria)} criteria "
            f"(from requirements v{requirements_version})."
        ),
        entity_type="rubric_version",
        entity_id=version.id,
        user_id=requested_by_user_id,
        previous_state={
            "job_status": previous_status,
            "superseded_draft_version": superseded_draft_version,
        },
        new_state={
            "version_number": next_version_number,
            "status": RubricVersionStatus.DRAFT,
            "criteria_count": len(result.criteria),
            "unresolved_source_indexes": unresolved,
            "generated_from_requirements_version": requirements_version,
            "job_status": job.status,
        },
    )

    db.commit()
    db.refresh(version)
    logger.info(
        "rubric_generation job=%s outcome=ok version=%d criteria=%d",
        job_id, next_version_number, len(result.criteria),
    )
    return version


# --- editing (DRAFT only) --------------------------------------------


def _require_draft_parent(db: Session, criterion: RubricCriterion) -> RubricVersion:
    version = db.get(RubricVersion, criterion.rubric_version_id)
    if version is None:  # pragma: no cover - FK makes this unreachable
        raise RubricNotFoundError("Parent rubric version not found.")
    if version.status != RubricVersionStatus.DRAFT:
        raise RubricStateError(
            f"This rubric version is {version.status}; only a DRAFT can be edited."
        )
    return version


def update_criterion(
    db: Session,
    *,
    criterion_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
    requirement_type: str | None = None,
    category: str | None = None,
    criterion_text: str | None = None,
) -> RubricCriterion:
    """Update the provided fields of a criterion. DRAFT versions only.

    Audit metadata records *which* fields changed and old/new
    ``requirement_type`` / ``category`` — never the full ``criterion_text``.
    """
    criterion = db.get(RubricCriterion, criterion_id)
    if criterion is None:
        raise RubricNotFoundError(f"No criterion with id {criterion_id!r}.")
    version = _require_draft_parent(db, criterion)

    changed: dict[str, dict] = {}

    if requirement_type is not None:
        _validate_requirement_type(requirement_type)
        if requirement_type != criterion.requirement_type:
            changed["requirement_type"] = {
                "from": criterion.requirement_type, "to": requirement_type
            }
            criterion.requirement_type = requirement_type

    if category is not None:
        new_category = _clean_optional_text(category)
        if new_category != criterion.category:
            changed["category"] = {"from": criterion.category, "to": new_category}
            criterion.category = new_category

    if criterion_text is not None:
        cleaned = criterion_text.strip()
        if not cleaned:
            raise RubricStateError("criterion_text must not be empty.")
        if cleaned != criterion.criterion_text:
            changed["criterion_text"] = "changed"  # never store the text itself
            criterion.criterion_text = cleaned

    if not changed:
        # No-op edit: don't write a misleading audit event.
        return criterion

    db.flush()
    record_event(
        db,
        event_type=AuditEventType.RUBRIC_EDITED,
        action=(
            f"Rubric v{version.version_number} criterion edited "
            f"({', '.join(sorted(changed))})."
        ),
        entity_type="rubric_criterion",
        entity_id=criterion.id,
        user_id=requested_by_user_id,
        previous_state={"rubric_version_id": str(version.id)},
        new_state={"changed_fields": changed},
    )
    db.commit()
    db.refresh(criterion)
    return criterion


def add_criterion(
    db: Session,
    *,
    rubric_version_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
    requirement_type: str,
    category: str | None,
    criterion_text: str,
) -> RubricCriterion:
    """Add a manually-authored criterion to a DRAFT rubric version."""
    version = db.get(RubricVersion, rubric_version_id)
    if version is None:
        raise RubricNotFoundError(f"No rubric version with id {rubric_version_id!r}.")
    if version.status != RubricVersionStatus.DRAFT:
        raise RubricStateError(
            f"This rubric version is {version.status}; only a DRAFT can be edited."
        )

    _validate_requirement_type(requirement_type)
    text_clean = (criterion_text or "").strip()
    if not text_clean:
        raise RubricStateError("criterion_text must not be empty.")

    prev_max_order = db.execute(
        select(func.max(RubricCriterion.display_order)).where(
            RubricCriterion.rubric_version_id == version.id
        )
    ).scalar()
    next_order = (prev_max_order or 0) + 1

    criterion = RubricCriterion(
        rubric_version_id=version.id,
        requirement_type=requirement_type,
        category=_clean_optional_text(category),
        criterion_text=text_clean,
        display_order=next_order,
        source_job_requirement_id=None,  # manually added
    )
    db.add(criterion)
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.RUBRIC_EDITED,
        action=f"Rubric v{version.version_number}: criterion added.",
        entity_type="rubric_criterion",
        entity_id=criterion.id,
        user_id=requested_by_user_id,
        new_state={
            "operation": "criterion_added",
            "requirement_type": requirement_type,
            "category": criterion.category,
        },
    )
    db.commit()
    db.refresh(criterion)
    return criterion


def delete_criterion(
    db: Session,
    *,
    criterion_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
) -> None:
    """Hard-delete a criterion from a DRAFT rubric version.

    Acceptable to hard-delete here: an unapproved draft criterion has no
    downstream consequences yet (unlike approved rubric versions or
    job_requirements, which are preserved).
    """
    criterion = db.get(RubricCriterion, criterion_id)
    if criterion is None:
        raise RubricNotFoundError(f"No criterion with id {criterion_id!r}.")
    version = _require_draft_parent(db, criterion)

    removed_type = criterion.requirement_type
    removed_category = criterion.category
    removed_id = criterion.id

    db.delete(criterion)
    db.flush()

    record_event(
        db,
        event_type=AuditEventType.RUBRIC_EDITED,
        action=f"Rubric v{version.version_number}: criterion removed.",
        entity_type="rubric_criterion",
        entity_id=removed_id,
        user_id=requested_by_user_id,
        previous_state={
            "operation": "criterion_removed",
            "requirement_type": removed_type,
            "category": removed_category,
        },
    )
    db.commit()


# --- approval ----------------------------------------------------------


def approve_rubric(
    db: Session,
    *,
    rubric_version_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | None,
) -> RubricVersion:
    """Approve a DRAFT rubric version, locking it as the job's effective rubric.

    Re-checks the "at least one MANDATORY criterion" rule at approval time — HR
    may have deleted mandatory criteria after generation.

    Any prior APPROVED version for the job is moved to SUPERSEDED (this is the
    explicit, HR-initiated replacement CLAUDE.md §F allows).

    Raises
    ------
    RubricNotFoundError
        No such rubric version.
    RubricStateError
        Version is not DRAFT, or has zero MANDATORY criteria.
    """
    version = db.get(RubricVersion, rubric_version_id)
    if version is None:
        raise RubricNotFoundError(f"No rubric version with id {rubric_version_id!r}.")
    if version.status != RubricVersionStatus.DRAFT:
        raise RubricStateError(
            f"Only a DRAFT rubric can be approved; this version is {version.status}."
        )

    if _mandatory_count(db, version.id) == 0:
        raise RubricStateError(
            "A rubric needs at least one MANDATORY criterion before it can be "
            "approved. Add one, then approve."
        )

    job = db.get(Job, version.job_id)

    superseded_prior_approved: int | None = None
    prior_approved = get_approved_rubric(db, version.job_id)
    if prior_approved is not None and prior_approved.id != version.id:
        prior_approved.status = RubricVersionStatus.SUPERSEDED
        superseded_prior_approved = prior_approved.version_number

    version.status = RubricVersionStatus.APPROVED
    version.approved_by = requested_by_user_id
    version.approved_at = _now()

    previous_job_status = job.status if job is not None else None
    if job is not None:
        job.status = JobStatus.RUBRIC_APPROVED
    db.flush()

    criteria_count = db.execute(
        select(func.count())
        .select_from(RubricCriterion)
        .where(RubricCriterion.rubric_version_id == version.id)
    ).scalar_one()

    common_new_state = {
        "version_number": version.version_number,
        "criteria_count": criteria_count,
        "mandatory_count": _mandatory_count(db, version.id),
        "superseded_prior_approved_version": superseded_prior_approved,
        "job_status": job.status if job is not None else None,
    }

    record_event(
        db,
        event_type=AuditEventType.RUBRIC_APPROVED,
        action=(
            f"Rubric v{version.version_number} approved"
            + (f" for '{job.title}'" if job is not None else "")
            + "."
        ),
        entity_type="rubric_version",
        entity_id=version.id,
        user_id=requested_by_user_id,
        previous_state={"status": RubricVersionStatus.DRAFT, "job_status": previous_job_status},
        new_state=common_new_state,
    )
    # Distinct fact: a locked, evaluation-facing rubric version now exists.
    record_event(
        db,
        event_type=AuditEventType.RUBRIC_VERSION_CREATED,
        action=(
            f"Rubric version v{version.version_number} locked as the effective "
            "evaluation rubric."
        ),
        entity_type="rubric_version",
        entity_id=version.id,
        user_id=requested_by_user_id,
        new_state=common_new_state,
    )

    db.commit()
    db.refresh(version)
    logger.info(
        "rubric_approve job=%s version=%d outcome=ok superseded_prior_approved=%s",
        version.job_id, version.version_number, superseded_prior_approved,
    )
    return version
