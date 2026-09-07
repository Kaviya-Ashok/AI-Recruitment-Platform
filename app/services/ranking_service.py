"""Cross-candidate ranking — deterministic, no AI (CLAUDE.md §§5, 12, 20, 34;
Phase 4 Step 5).

Ranking is a pure read/sort over data that Step 4 already persisted
(``screening_evaluations``). **No Claude call anywhere in this module.** The AI
produced the per-criterion verdicts and the bucket scores were computed by the
documented formula in ``screening_scoring``; ranking just orders candidates by
those already-final numbers and explains the order.

PARTITIONING BY RUBRIC VERSION
------------------------------
A job's approved rubric can be superseded while candidates already exist, so two
candidates for the same job may have been scored against different criteria.
Their scores are **not comparable**. Every function here operates on exactly one
``(job_id, rubric_version_id)`` partition; ``compute_ranking`` refuses input that
mixes versions, and ``generate_ranking`` ranks one partition per call.

WHAT IS DETERMINISTIC vs CARRIED THROUGH
---------------------------------------
* Computed here (Python, from the evaluation rows):
  - ``eligible``           = NOT (any reconciled MANDATORY criterion is FAIL)
  - ``mandatory_unknown_flag`` = any reconciled MANDATORY criterion is UNKNOWN
  - ``overall_score``      = weighted mean of the non-NULL bucket scores
  - ``rank_position``      = 1..N over eligible candidates, in sorted order
* Carried through **unchanged**, never recomputed, never folded into the score
  or the position:
  - ``overall_confidence`` and each bucket's ``coverage`` (they describe how
    much to trust the score; they are not part of it).

THE SCORE FORMULA — verbatim (CLAUDE.md §20)
===========================================
Bucket weights (these are RANKING weights over BUCKET scores — deliberately not
``screening_scoring._WEIGHTS``, which are per-criterion weights for a different
computation):
    Requirements -> 2
    Experience   -> 1
    Behavioral   -> 1

Let PRESENT = the buckets whose ``screening_evaluations`` score is NOT NULL for
this candidate.

    if PRESENT is empty  ->  overall_score = NULL   (fully unassessed candidate)
    else                 ->  overall_score =
                                 sum(weight * bucket_score) over PRESENT
                               / sum(weight)               over PRESENT

A NULL bucket is **omitted from both sums** — never treated as 0. A candidate
with a NULL Behavioral score is averaged over Requirements+Experience only.

THE SORT — verbatim
===================
Eligible candidates only, in this order (all DESC unless noted):
    1. overall_score
    2. requirements_score
    3. experience_score
    4. behavioral_score
    5. application.created_at   ASC  (earlier applicant ranks first)
A NULL sorts below any non-NULL value at every numeric level (so a NULL-score
candidate ranks last among the eligible, but is still ranked, not hidden).
No candidate name / email / phone / any identity attribute is ever read in the
sort key — ``test_ranking_algorithm`` asserts this.

ELIGIBILITY / UNKNOWN
====================
A reconciled MANDATORY criterion with ``result == "UNKNOWN"`` is **never** an
exclusion here — UNKNOWN is not FAIL (CLAUDE.md §B). It only sets
``mandatory_unknown_flag``, which the UI surfaces prominently. Only
``result == "FAIL"`` on a MANDATORY criterion makes a candidate ineligible.

FULL REPLACE, HR-ATTRIBUTED
--------------------------
``generate_ranking`` is an explicit, repeatable HR action (not a check-first
"skip if exists" step like Steps 1-4). Each call: one ``generation_batch_id``,
DELETE every ``candidate_rankings`` row for the partition, INSERT the fresh set,
emit one ``RANKING_GENERATED`` audit event — all in one transaction. The actor
is the **real HR user**; this module never imports or uses the SYSTEM actor.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.candidate import Candidate
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.rubric import RubricVersion
from app.database.models.screening_evaluation import ScreeningEvaluation
from app.database.models.screening_session import ScreeningSession
from app.services.audit_service import record_event
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

_MANDATORY = "MANDATORY"

# RANKING weights over BUCKET scores. See the module docstring — NOT the
# per-criterion weights in ``screening_scoring``.
_BUCKET_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("requirements_score", 2),
    ("experience_score", 1),
    ("behavioral_score", 1),
)


class RankingError(Exception):
    """Ranking could not be produced. User-safe message; never wraps raw
    criterion / evidence / candidate text."""


class RankingTargetNotFoundError(RankingError):
    """No such job, no such rubric version for the job, or the requested
    partition has no evaluated candidates to rank."""


# --- row shapes ---------------------------------------------------------


@dataclass(frozen=True)
class EvaluatedApplicationRow:
    """One SCREENING_EVALUATED application + its screening-evaluation numbers.

    Deliberately carries **no** candidate identity (name / email / phone): the
    ranking algorithm must never see those. ``results`` is the reconciled
    per-criterion list from ``screening_evaluations.results`` — used only to
    read MANDATORY ``result`` values for eligibility / the UNKNOWN flag.
    """

    application_id: uuid.UUID
    rubric_version_id: uuid.UUID
    created_at: datetime
    requirements_score: int | None
    experience_score: int | None
    behavioral_score: int | None
    requirements_coverage: float | None
    experience_coverage: float | None
    behavioral_coverage: float | None
    overall_confidence: str
    results: list[dict]


@dataclass(frozen=True)
class RankedCandidate:
    """One candidate's computed place. ``rank_position`` is None for the
    ineligible. Confidence + coverage are carried straight from the input."""

    application_id: uuid.UUID
    rubric_version_id: uuid.UUID
    rank_position: int | None
    overall_score: float | None
    eligible: bool
    mandatory_unknown_flag: bool
    requirements_score: int | None
    experience_score: int | None
    behavioral_score: int | None
    requirements_coverage: float | None
    experience_coverage: float | None
    behavioral_coverage: float | None
    overall_confidence: str


@dataclass(frozen=True)
class RubricVersionPartition:
    """One ``(rubric_version_id)`` partition of a job's evaluated candidates,
    for the HR UI's per-partition sections."""

    rubric_version_id: uuid.UUID
    version_number: int | None
    evaluated_count: int
    ranking_generated_at: datetime | None


@dataclass(frozen=True)
class RankingDisplayRow:
    """Display-ready ranking row for the HR page (thin-glue rule).

    ``candidate_name`` / ``candidate_email`` are attached here for HR display
    **only** — they were never read by the ranking algorithm and are not part of
    any ordering. Rows come back ordered by ``rank_position`` (ineligible last).
    """

    rank_position: int | None
    application_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    overall_score: float | None
    eligible: bool
    mandatory_unknown_flag: bool
    requirements_score: int | None
    experience_score: int | None
    behavioral_score: int | None
    requirements_coverage: float | None
    experience_coverage: float | None
    behavioral_coverage: float | None
    overall_confidence: str
    generation_batch_id: uuid.UUID
    generated_at: datetime


# --- pure ranking algorithm (no DB) ----------------------------------


def _mandatory_results(row: EvaluatedApplicationRow) -> list[str]:
    return [
        str(c.get("result"))
        for c in row.results
        if c.get("requirement_type") == _MANDATORY
    ]


def _overall_score(row: EvaluatedApplicationRow) -> float | None:
    """Weighted mean of the non-NULL bucket scores. NULL buckets are omitted
    from both the numerator and the denominator — never counted as 0."""
    present: list[tuple[int, int]] = []
    for attr, weight in _BUCKET_WEIGHTS:
        value = getattr(row, attr)
        if value is not None:
            present.append((weight, value))
    if not present:
        return None
    return sum(w * s for w, s in present) / sum(w for w, _ in present)


def _desc_key(value: float | int | None) -> tuple[int, float]:
    """Sort component for a DESC numeric field where NULL is worst. Used under
    ``sorted(reverse=True)``: ``(1, v)`` beats ``(0, 0.0)``, so None sorts last."""
    if value is None:
        return (0, 0.0)
    return (1, float(value))


def compute_ranking(
    evaluated_rows: list[EvaluatedApplicationRow],
) -> list[RankedCandidate]:
    """Deterministically rank one rubric-version partition. Pure function.

    Ineligible candidates (a reconciled MANDATORY criterion is FAIL) get
    ``rank_position = None`` and are returned after the numbered rows.

    Raises
    ------
    RankingError
        ``evaluated_rows`` mixes more than one ``rubric_version_id`` — the
        caller must partition first; silently mixing versions would produce a
        meaningless ranking.
    """
    if not evaluated_rows:
        return []

    distinct_versions = {r.rubric_version_id for r in evaluated_rows}
    if len(distinct_versions) > 1:
        raise RankingError(
            "compute_ranking received candidates from more than one rubric "
            "version; rank one partition at a time."
        )

    computed: list[dict] = []
    for row in evaluated_rows:
        mandatory = _mandatory_results(row)
        # UNKNOWN is NEVER FAIL here (CLAUDE.md §B). Eligibility tests the
        # literal "FAIL" only; an UNKNOWN MANDATORY criterion sets the flag but
        # keeps the candidate in the ranking.
        eligible = "FAIL" not in mandatory
        mandatory_unknown_flag = "UNKNOWN" in mandatory
        computed.append(
            {
                "row": row,
                "eligible": eligible,
                "mandatory_unknown_flag": mandatory_unknown_flag,
                "overall_score": _overall_score(row),
            }
        )

    def _sort_key(item: dict) -> tuple:
        row: EvaluatedApplicationRow = item["row"]
        # created_at ASC under reverse=True -> negate so earlier sorts first.
        return (
            _desc_key(item["overall_score"]),
            _desc_key(row.requirements_score),
            _desc_key(row.experience_score),
            _desc_key(row.behavioral_score),
            -row.created_at.timestamp(),
        )

    eligible_sorted = sorted(
        (c for c in computed if c["eligible"]), key=_sort_key, reverse=True
    )
    ineligible_sorted = sorted(
        (c for c in computed if not c["eligible"]), key=_sort_key, reverse=True
    )

    out: list[RankedCandidate] = []
    for position, item in enumerate(eligible_sorted, start=1):
        out.append(_to_ranked(item, rank_position=position))
    for item in ineligible_sorted:
        out.append(_to_ranked(item, rank_position=None))
    return out


def _to_ranked(item: dict, *, rank_position: int | None) -> RankedCandidate:
    row: EvaluatedApplicationRow = item["row"]
    return RankedCandidate(
        application_id=row.application_id,
        rubric_version_id=row.rubric_version_id,
        rank_position=rank_position,
        overall_score=item["overall_score"],
        eligible=item["eligible"],
        mandatory_unknown_flag=item["mandatory_unknown_flag"],
        requirements_score=row.requirements_score,
        experience_score=row.experience_score,
        behavioral_score=row.behavioral_score,
        requirements_coverage=row.requirements_coverage,
        experience_coverage=row.experience_coverage,
        behavioral_coverage=row.behavioral_coverage,
        overall_confidence=row.overall_confidence,
    )


# --- eligibility query (HR-only) -----------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def list_evaluated_applications_for_job(
    db: Session,
    job_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[EvaluatedApplicationRow]:
    """Every application for ``job_id`` that has reached
    ``ApplicationStatus.SCREENING_EVALUATED``, with its screening-evaluation
    numbers. HR/INTERNAL ONLY.

    Filters strictly on ``ApplicationStatus.SCREENING_EVALUATED``. It does NOT
    read the screening-session status vocabulary at all — the join to
    ``screening_sessions`` is by id only, purely to reach the
    ``screening_evaluations`` row.

    Separate from ``application_service.list_applications_for_job`` (which stays
    unfiltered and untouched) — this one carries the evaluation payload a
    ranking run needs and applies the status gate.
    """
    require_internal_user(db, acting_user_id)

    stmt = (
        select(Application, ScreeningEvaluation)
        .join(
            ScreeningSession,
            ScreeningSession.application_id == Application.id,
        )
        .join(
            ScreeningEvaluation,
            ScreeningEvaluation.screening_session_id == ScreeningSession.id,
        )
        .where(
            Application.job_id == _as_uuid(job_id),
            Application.status == ApplicationStatus.SCREENING_EVALUATED,
        )
    )

    rows: list[EvaluatedApplicationRow] = []
    for application, evaluation in db.execute(stmt).all():
        rows.append(
            EvaluatedApplicationRow(
                application_id=application.id,
                rubric_version_id=evaluation.rubric_version_id,
                created_at=application.created_at,
                requirements_score=evaluation.requirements_score,
                experience_score=evaluation.experience_score,
                behavioral_score=evaluation.behavioral_score,
                requirements_coverage=evaluation.requirements_coverage,
                experience_coverage=evaluation.experience_coverage,
                behavioral_coverage=evaluation.behavioral_coverage,
                overall_confidence=evaluation.overall_confidence,
                results=list(evaluation.results or []),
            )
        )
    return rows


def list_rubric_version_partitions_for_job(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[RubricVersionPartition]:
    """The distinct rubric-version partitions among a job's evaluated
    candidates, so the HR UI can show and rank each one separately.
    HR/INTERNAL ONLY. Ordered by rubric ``version_number``."""
    require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)

    counts = db.execute(
        select(
            ScreeningEvaluation.rubric_version_id,
            func.count(Application.id),
        )
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
        )
        .group_by(ScreeningEvaluation.rubric_version_id)
    ).all()

    partitions: list[RubricVersionPartition] = []
    for rubric_version_id, evaluated_count in counts:
        version = db.get(RubricVersion, rubric_version_id)
        last_generated = db.execute(
            select(func.max(CandidateRanking.generated_at)).where(
                CandidateRanking.job_id == job_uuid,
                CandidateRanking.rubric_version_id == rubric_version_id,
            )
        ).scalar()
        partitions.append(
            RubricVersionPartition(
                rubric_version_id=rubric_version_id,
                version_number=version.version_number if version else None,
                evaluated_count=evaluated_count,
                ranking_generated_at=last_generated,
            )
        )
    partitions.sort(key=lambda p: (p.version_number is None, p.version_number or 0))
    return partitions


# --- generate / read ------------------------------------------------


def generate_ranking(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    rubric_version_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
) -> list[CandidateRanking]:
    """Rank one ``(job_id, rubric_version_id)`` partition and persist it.

    HR/INTERNAL ONLY — ``requested_by_user_id`` is the real HR user, never the
    SYSTEM actor. This is the first ranking-related audit event attributed to a
    human.

    Each call is a fresh, full regeneration (new ``generation_batch_id``, new
    ``RANKING_GENERATED`` event) — NOT a check-first "skip if exists" step. In
    one transaction: DELETE every existing ``candidate_rankings`` row for the
    partition, INSERT the fresh set, emit one audit event, commit once. A
    failure before the commit leaves the prior ranking fully intact.

    Raises
    ------
    UnauthorizedError
    RankingTargetNotFoundError
        No such job/rubric-version pair, or the partition has no evaluated
        candidates.
    RankingError
        The pure ranker rejected the input (should not happen — the partition
        is single-version by construction here).
    """
    require_internal_user(db, requested_by_user_id)
    job_uuid = _as_uuid(job_id)
    rubric_uuid = _as_uuid(rubric_version_id)

    version = db.get(RubricVersion, rubric_uuid)
    if version is None or version.job_id != job_uuid:
        raise RankingTargetNotFoundError(
            "No such rubric version for this job."
        )

    all_rows = list_evaluated_applications_for_job(
        db, job_uuid, acting_user_id=requested_by_user_id
    )
    partition = [r for r in all_rows if r.rubric_version_id == rubric_uuid]
    if not partition:
        raise RankingTargetNotFoundError(
            "No evaluated candidates for this rubric version yet."
        )

    ranked = compute_ranking(partition)

    batch_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    # --- one transaction: replace the partition wholesale ---------------
    db.execute(
        delete(CandidateRanking).where(
            CandidateRanking.job_id == job_uuid,
            CandidateRanking.rubric_version_id == rubric_uuid,
        )
    )
    db.flush()

    persisted: list[CandidateRanking] = []
    for rc in ranked:
        row = CandidateRanking(
            job_id=job_uuid,
            rubric_version_id=rubric_uuid,
            application_id=rc.application_id,
            rank_position=rc.rank_position,
            overall_score=rc.overall_score,
            eligible=rc.eligible,
            mandatory_unknown_flag=rc.mandatory_unknown_flag,
            generated_at=now,
            generation_batch_id=batch_id,
        )
        db.add(row)
        persisted.append(row)
    db.flush()

    eligible_count = sum(1 for rc in ranked if rc.eligible)
    ineligible_count = len(ranked) - eligible_count
    record_event(
        db,
        event_type=AuditEventType.RANKING_GENERATED,
        action=(
            f"Ranking generated for job {job_uuid} against rubric "
            f"v{version.version_number}: {len(ranked)} candidate(s), "
            f"{eligible_count} eligible, {ineligible_count} ineligible."
        ),
        entity_type="job",
        entity_id=job_uuid,
        user_id=_as_uuid(requested_by_user_id),
        new_state={
            "job_id": str(job_uuid),
            "rubric_version_id": str(rubric_uuid),
            "rubric_version_number": version.version_number,
            "generation_batch_id": str(batch_id),
            "candidate_count": len(ranked),
            "eligible_count": eligible_count,
            "ineligible_count": ineligible_count,
            "mandatory_unknown_count": sum(
                1 for rc in ranked if rc.mandatory_unknown_flag
            ),
            # id + position only — no résumé text, no answers, no evidence.
            "positions": [
                {
                    "application_id": str(rc.application_id),
                    "rank_position": rc.rank_position,
                    "eligible": rc.eligible,
                }
                for rc in ranked
            ],
        },
    )

    db.commit()
    for row in persisted:
        db.refresh(row)
    logger.info(
        "ranking_generated job=%s rubric_version=%s batch=%s candidates=%d "
        "eligible=%d",
        job_uuid, rubric_uuid, batch_id, len(ranked), eligible_count,
    )
    return _ordered(persisted)


def get_ranking_for_job(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    rubric_version_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[CandidateRanking]:
    """The persisted ranking for one partition, ranked rows first
    (ineligible / NULL ``rank_position`` last). HR/INTERNAL ONLY."""
    require_internal_user(db, acting_user_id)
    rows = db.execute(
        select(CandidateRanking).where(
            CandidateRanking.job_id == _as_uuid(job_id),
            CandidateRanking.rubric_version_id == _as_uuid(rubric_version_id),
        )
    ).scalars().all()
    return _ordered(rows)


def get_ranking_display_rows(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    rubric_version_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[RankingDisplayRow]:
    """Display-ready ranking rows for the HR page — the persisted ranking joined
    to each candidate's name/email (for display only) and the carried-through
    confidence + bucket scores/coverage from ``screening_evaluations``.
    HR/INTERNAL ONLY. Ordered by ``rank_position`` (ineligible last)."""
    require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)
    rubric_uuid = _as_uuid(rubric_version_id)

    stmt = (
        select(CandidateRanking, Application, Candidate, ScreeningEvaluation)
        .join(Application, Application.id == CandidateRanking.application_id)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .join(
            ScreeningSession,
            ScreeningSession.application_id == Application.id,
        )
        .join(
            ScreeningEvaluation,
            ScreeningEvaluation.screening_session_id == ScreeningSession.id,
        )
        .where(
            CandidateRanking.job_id == job_uuid,
            CandidateRanking.rubric_version_id == rubric_uuid,
        )
    )

    out: list[RankingDisplayRow] = []
    for ranking, application, candidate, evaluation in db.execute(stmt).all():
        out.append(
            RankingDisplayRow(
                rank_position=ranking.rank_position,
                application_id=application.id,
                candidate_name=candidate.full_name,
                candidate_email=candidate.email,
                overall_score=ranking.overall_score,
                eligible=ranking.eligible,
                mandatory_unknown_flag=ranking.mandatory_unknown_flag,
                requirements_score=evaluation.requirements_score,
                experience_score=evaluation.experience_score,
                behavioral_score=evaluation.behavioral_score,
                requirements_coverage=evaluation.requirements_coverage,
                experience_coverage=evaluation.experience_coverage,
                behavioral_coverage=evaluation.behavioral_coverage,
                overall_confidence=evaluation.overall_confidence,
                generation_batch_id=ranking.generation_batch_id,
                generated_at=ranking.generated_at,
            )
        )
    out.sort(key=lambda r: (r.rank_position is None, r.rank_position or 0))
    return out


def _ordered(rows) -> list[CandidateRanking]:
    """rank_position ascending, NULLs (ineligible) last."""
    return sorted(
        rows, key=lambda r: (r.rank_position is None, r.rank_position or 0)
    )
