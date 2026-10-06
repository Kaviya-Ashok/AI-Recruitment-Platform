"""Final ranking service — gather, score, rank and persist the post-interview final
ranking of one job (CLAUDE.md §§5, 10, 12, 20, 21, 22, 23; Phase 4 Step 10b).

ZERO AI. This module contains no Claude call and imports nothing from ``app/ai``.
It reads data earlier steps already stored, hands numbers to the pure functions in
:mod:`app.services.final_scoring`, and persists the result. The AI recommendation,
the human recommendations and any disagreement are NOT inputs (a structural test
pins that). It never changes ``Application.status`` and never rejects anybody: a
final ranking is one input for the hiring manager (Step 11).

WHAT IS RANKABLE
----------------
An application is in scope if it has AT LEAST ONE ``interview_feedback`` row.
Shortlist status is deliberately not consulted (consistent with Step 8: an
interview that happened stays on the record even after an unshortlist).

PARTITIONING — NEVER MERGING RUBRIC VERSIONS
--------------------------------------------
Runs are per ``(job_id, rubric_version_id)``. The version is the one on the
candidate's Step 5 ``candidate_rankings`` row (the screening ranking used). A
candidate with no such row has no screening ranking to take a version from, so —
only to place them in a partition and show them as INCOMPLETE — the version of
their screening evaluation is used, then (last resort) the version of the
interview guide the feedback was recorded against. Scores from different versions
are never ranked against each other.

A post-interview analysis counts toward final confidence ONLY if its own
``rubric_version_id`` equals the partition's. Otherwise it is treated as absent
(confidence capped at MEDIUM) and ``status_reason`` says so.

WHERE EACH INPUT COMES FROM
---------------------------
* screening score — the Step 5 row's ``overall_score`` (0-10 float, nullable),
  rounded half-up to 2 decimals. No Step 5 row -> INCOMPLETE_SCREENING with
  "Generate the screening ranking first"; this service never runs Step 5.
* eligibility / mandatory-unknown — the Step 5 row's ``eligible`` and
  ``mandatory_unknown_flag``, READ AS STORED, never recomputed.
* interview score — every feedback round's ratings via
  ``interview_feedback_service.list_feedback_views`` (mean of round means).
* confidence — the screening evaluation's ``overall_confidence`` and the CURRENT
  analysis's ``confidence``.

ENTRY STATUSES (one per interviewed candidate, in this order of precedence)
---------------------------------------------------------------------------
INCOMPLETE_SCREENING — no Step 5 row, or its score is NULL.
INCOMPLETE_INTERVIEW — no round has a rating (notes-only rounds do not count).
NOT_RANKED_INELIGIBLE — Step 5 says a mandatory requirement was not met. The score
    is kept for display; there is no rank; shown in its own list.
RANKED — everything else; gets a competition rank (ties share, the next skips).
An incomplete candidate has no score and no rank; weights are never redistributed.

HISTORY AND TRIGGER
-------------------
Explicit HR action only, by a real internal user (the SYSTEM actor is rejected,
exactly as in ``interview_feedback_service``). A run marks the previous CURRENT run
for the same partition SUPERSEDED and inserts a new CURRENT one, in one
transaction with the audit event. Nothing is deleted. Nothing re-runs by itself:
:func:`get_final_ranking_staleness` only REPORTS that a run is out of date.

PRIVACY
-------
No candidate name, notes, evidence or transcript text is stored or audited.
``status_reason`` is system-written. Names are read at DISPLAY time only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.candidate import Candidate
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.final_ranking import (
    FinalRanking,
    FinalRankingEntry,
    FinalRankingEntryStatus,
    FinalRankingStatus,
)
from app.database.models.interview_feedback import InterviewFeedback
from app.database.models.interview_guide import InterviewGuide
from app.database.models.job import Job
from app.database.models.post_interview_analysis import PostInterviewAnalysis
from app.database.models.rubric import RubricVersion
from app.database.models.screening_evaluation import ScreeningEvaluation
from app.database.models.screening_session import ScreeningSession
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
from app.services import final_scoring as fs
from app.services.audit_service import record_event
from app.services.interview_feedback_service import list_feedback_views
from app.services.post_interview_service import get_current_post_interview_analysis
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)

# --- user-safe messages (system-written; never interpolate names or text) -----

_SYSTEM_ACTOR = (
    "A final ranking has to be requested by a signed-in HR user, so there is a "
    "record of who asked for it. The automated pipeline account cannot request "
    "one."
)
_NO_JOB = "No such job — it may have been removed."
_NOTHING_TO_RANK = (
    "No candidate for this job has interview feedback yet, so there is nothing "
    "to rank. Record interview feedback first."
)

REASON_NO_SCREENING_RANKING = (
    "Generate the screening ranking first — on the Candidates page, in the "
    "Ranking & Shortlist tab."
)
REASON_SCREENING_SCORE_NULL = (
    "No screening score is available: nothing could be assessed as PASS or FAIL "
    "at screening."
)
REASON_NO_RATINGS = (
    "No interview round has competency ratings, so there is no interview score. "
    "Rounds with notes only are not scored."
)
REASON_INELIGIBLE = (
    "A mandatory requirement was assessed as not met, so this candidate is not "
    "ranked. The score is shown for context only. This is not a rejection; that "
    "remains a human decision."
)
REASON_ANALYSIS_OTHER_VERSION = (
    "The post-interview analysis was generated against a different rubric "
    "version, so it was not counted toward confidence."
)


class FinalRankingError(Exception):
    """A final ranking could not be produced or read. User-safe message."""


class FinalRankingTargetNotFoundError(FinalRankingError):
    """No such job, or nothing to rank."""


class FinalRankingActorError(FinalRankingError):
    """The actor passed the internal-user guard but may not request a final
    ranking (the SYSTEM pipeline actor)."""


# --- views (frozen primitives; safe outside a Session) ----------------------


@dataclass(frozen=True)
class FinalRankingEntryView:
    entry_id: uuid.UUID
    application_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    rank: int | None
    tied: bool
    screening_score: Decimal | None
    interview_score: Decimal | None
    final_score: Decimal | None
    eligible: bool
    mandatory_unknown: bool
    entry_status: str
    status_reason: str
    final_confidence: str | None
    screening_confidence: str | None
    screening_rank: int | None
    screening_generated_at: datetime | None
    rounds_used: tuple[int, ...]
    round_means: tuple[tuple[int, Decimal], ...]
    # Context from the CURRENT same-version analysis at run time (AI-generated,
    # NOT part of the score). None when no analysis counted.
    analysis_id: uuid.UUID | None
    analysis_confidence: str | None
    analysis_recommendation: str | None


@dataclass(frozen=True)
class FinalRankingView:
    final_ranking_id: uuid.UUID
    job_id: uuid.UUID
    rubric_version_id: uuid.UUID
    rubric_version_number: int | None
    status: str
    screening_weight: Decimal
    interview_weight: Decimal
    created_at: datetime
    superseded_at: datetime | None
    requested_by_name: str
    # Display order: ranked (rank asc, stable order inside a tie), then the
    # ineligible, then the incomplete.
    entries: tuple[FinalRankingEntryView, ...]


@dataclass(frozen=True)
class ApplicationFinalRanking:
    """One application's entry in its latest CURRENT run, for the scorecard."""

    entry: FinalRankingEntryView
    final_ranking_id: uuid.UUID
    screening_weight: Decimal
    interview_weight: Decimal
    generated_at: datetime
    ranked_count: int


@dataclass(frozen=True)
class FinalRankingStaleness:
    is_stale: bool
    new_candidate_count: int
    changed_feedback_count: int
    changed_screening_count: int
    changed_analysis_count: int

    @property
    def reasons(self) -> tuple[str, ...]:
        out = []
        if self.new_candidate_count:
            out.append(f"{self.new_candidate_count} newly interviewed candidate(s)")
        if self.changed_feedback_count:
            out.append(
                f"{self.changed_feedback_count} candidate(s) with new interview "
                "feedback"
            )
        if self.changed_screening_count:
            out.append(
                f"{self.changed_screening_count} candidate(s) with a newer "
                "screening ranking"
            )
        if self.changed_analysis_count:
            out.append(
                f"{self.changed_analysis_count} candidate(s) with a new "
                "post-interview analysis"
            )
        return tuple(out)


# --- helpers -----------------------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _reject_system_actor(actor: User) -> None:
    """``require_internal_user`` accepts the SYSTEM actor by design, so this is
    the explicit check its docstring tells callers to make (role AND id)."""
    if actor.role is UserRole.SYSTEM or actor.id == SYSTEM_USER_ID:
        logger.warning("final_ranking rejected: SYSTEM actor user_id=%s", actor.id)
        raise FinalRankingActorError(_SYSTEM_ACTOR)


def _require_human_actor(db: Session, user_id: uuid.UUID | str | None) -> User:
    actor = require_internal_user(db, user_id)
    _reject_system_actor(actor)
    return actor


@dataclass(frozen=True)
class _Inputs:
    """Everything one interviewed application contributes, gathered read-only."""

    application_id: uuid.UUID
    created_at: datetime
    version_id: uuid.UUID
    feedback_ids: tuple[str, ...]
    ratings_by_round: dict[int, list[int]]
    has_screening_row: bool
    screening_overall: float | None
    eligible: bool
    mandatory_unknown: bool
    screening_rank: int | None
    screening_batch_id: uuid.UUID | None
    screening_generated_at: datetime | None
    screening_confidence: str | None
    analysis_id: uuid.UUID | None          # the one that COUNTS (same version)
    analysis_confidence: str | None
    analysis_other_version: bool


def _interviewed_application_ids(db: Session, job_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(Application.id)
            .join(InterviewFeedback, InterviewFeedback.application_id == Application.id)
            .where(Application.job_id == job_id)
            .distinct()
        ).scalars().all()
    )


def _gather(
    db: Session, job_id: uuid.UUID, *, acting_user_id: uuid.UUID
) -> list[_Inputs]:
    out: list[_Inputs] = []
    for app_id in _interviewed_application_ids(db, job_id):
        application = db.get(Application, app_id)
        feedback = list_feedback_views(db, app_id, acting_user_id=acting_user_id)

        ratings_by_round = {
            f.interview_round: [r.rating for r in f.ratings] for f in feedback
        }

        ranking_row = db.execute(
            select(CandidateRanking)
            .where(
                CandidateRanking.application_id == app_id,
                CandidateRanking.job_id == job_id,
            )
            .order_by(CandidateRanking.generated_at.desc(), CandidateRanking.id.desc())
            .limit(1)
        ).scalar_one_or_none()

        evaluation = db.execute(
            select(ScreeningEvaluation)
            .join(
                ScreeningSession,
                ScreeningSession.id == ScreeningEvaluation.screening_session_id,
            )
            .where(ScreeningSession.application_id == app_id)
            .limit(1)
        ).scalar_one_or_none()

        if ranking_row is not None:
            version_id = ranking_row.rubric_version_id
        elif evaluation is not None:
            version_id = evaluation.rubric_version_id
        else:
            guide = db.get(InterviewGuide, feedback[0].interview_guide_id)
            version_id = guide.rubric_version_id

        analysis = get_current_post_interview_analysis(
            db, app_id, acting_user_id=acting_user_id
        )
        counts = analysis is not None and analysis.rubric_version_id == version_id

        out.append(
            _Inputs(
                application_id=app_id,
                created_at=application.created_at,
                version_id=version_id,
                feedback_ids=tuple(sorted(str(f.feedback_id) for f in feedback)),
                ratings_by_round=ratings_by_round,
                has_screening_row=ranking_row is not None,
                screening_overall=(
                    ranking_row.overall_score if ranking_row is not None else None
                ),
                eligible=ranking_row.eligible if ranking_row is not None else True,
                mandatory_unknown=(
                    ranking_row.mandatory_unknown_flag
                    if ranking_row is not None else False
                ),
                screening_rank=(
                    ranking_row.rank_position if ranking_row is not None else None
                ),
                screening_batch_id=(
                    ranking_row.generation_batch_id if ranking_row is not None
                    else None
                ),
                screening_generated_at=(
                    ranking_row.generated_at if ranking_row is not None else None
                ),
                screening_confidence=(
                    evaluation.overall_confidence if evaluation is not None else None
                ),
                analysis_id=analysis.analysis_id if counts else None,
                analysis_confidence=analysis.confidence if counts else None,
                analysis_other_version=analysis is not None and not counts,
            )
        )
    return out


@dataclass(frozen=True)
class _Computed:
    inputs: _Inputs
    screening_score: Decimal | None
    interview_score: Decimal | None
    final_score: Decimal | None
    entry_status: str
    status_reason: str
    final_confidence: str | None
    round_means: dict[int, Decimal]
    unscored_rounds: list[int]


def _compute(inp: _Inputs) -> _Computed:
    screening = fs.round_screening_score(inp.screening_overall)
    means = fs.interview_round_means(inp.ratings_by_round)
    interview = fs.compute_interview_score_all_rounds(inp.ratings_by_round)
    final = fs.compute_final_score(
        screening_score=screening, interview_score=interview
    )
    unscored = sorted(r for r in inp.ratings_by_round if r not in means)

    if not inp.has_screening_row:
        status, reason = (
            FinalRankingEntryStatus.INCOMPLETE_SCREENING, REASON_NO_SCREENING_RANKING
        )
    elif screening is None:
        status, reason = (
            FinalRankingEntryStatus.INCOMPLETE_SCREENING, REASON_SCREENING_SCORE_NULL
        )
    elif interview is None:
        status, reason = (
            FinalRankingEntryStatus.INCOMPLETE_INTERVIEW, REASON_NO_RATINGS
        )
    elif not inp.eligible:
        status, reason = (
            FinalRankingEntryStatus.NOT_RANKED_INELIGIBLE, REASON_INELIGIBLE
        )
    else:
        status, reason = FinalRankingEntryStatus.RANKED, ""

    notes: list[str] = []
    if status in (
        FinalRankingEntryStatus.INCOMPLETE_SCREENING,
        FinalRankingEntryStatus.INCOMPLETE_INTERVIEW,
    ) and not inp.eligible:
        notes.append(
            "A mandatory requirement was also assessed as not met at screening."
        )
    if unscored and interview is not None:
        listed = ", ".join(str(r) for r in unscored)
        notes.append(f"Not scored (no ratings): round {listed}.")

    confidence: str | None = None
    if final is not None:
        confidence, analysis_missing = fs.compute_final_confidence(
            screening_confidence=inp.screening_confidence,
            analysis_confidence=inp.analysis_confidence,
        )
        if analysis_missing:
            notes.append(
                REASON_ANALYSIS_OTHER_VERSION
                if inp.analysis_other_version
                else fs.NO_ANALYSIS_NOTE
            )

    full_reason = " ".join(p for p in [reason, *notes] if p)
    return _Computed(
        inputs=inp, screening_score=screening, interview_score=interview,
        final_score=final, entry_status=status, status_reason=full_reason,
        final_confidence=confidence, round_means=means, unscored_rounds=unscored,
    )


def _rank(computed: list[_Computed]) -> dict[uuid.UUID, fs.RankedEntry]:
    ranked = fs.rank_candidates(
        [
            fs.RankInput(
                application_id=c.inputs.application_id,
                final_score=c.final_score,
                screening_score=c.screening_score,
                created_at=c.inputs.created_at,
                # Ranking requires RANKED status: ineligible/incomplete excluded.
                eligible=c.entry_status == FinalRankingEntryStatus.RANKED,
            )
            for c in computed
        ]
    )
    return {r.application_id: r for r in ranked}


# --- generate ------------------------------------------------------------------


def generate_final_ranking(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    requested_by_user_id: uuid.UUID | str,
) -> list[FinalRanking]:
    """Generate the post-interview final ranking for every rubric-version
    partition of a job's interviewed candidates. HR/INTERNAL ONLY, and never the
    SYSTEM actor. Explicit action only — never auto-run.

    For each ``(job, rubric version)`` partition, in ONE transaction: mark the
    previous CURRENT run SUPERSEDED, insert the new CURRENT run and its entries,
    and record one ``FINAL_RANKING_GENERATED`` audit event. Nothing is deleted.

    Raises
    ------
    UnauthorizedError
        ``requested_by_user_id`` is not an active internal user.
    FinalRankingActorError
        The actor is the SYSTEM account.
    FinalRankingTargetNotFoundError
        No such job, or no candidate has interview feedback.
    """
    actor = _require_human_actor(db, requested_by_user_id)
    job_uuid = _as_uuid(job_id)
    if db.get(Job, job_uuid) is None:
        raise FinalRankingTargetNotFoundError(_NO_JOB)

    inputs = _gather(db, job_uuid, acting_user_id=actor.id)
    if not inputs:
        raise FinalRankingTargetNotFoundError(_NOTHING_TO_RANK)

    by_version: dict[uuid.UUID, list[_Inputs]] = {}
    for inp in inputs:
        by_version.setdefault(inp.version_id, []).append(inp)

    now = datetime.now(timezone.utc)
    runs: list[FinalRanking] = []
    audit_payloads: list[tuple[FinalRanking, dict]] = []

    for version_id in sorted(by_version, key=str):
        computed = [_compute(i) for i in by_version[version_id]]
        ranks = _rank(computed)

        previous = db.execute(
            select(FinalRanking).where(
                FinalRanking.job_id == job_uuid,
                FinalRanking.rubric_version_id == version_id,
                FinalRanking.status == FinalRankingStatus.CURRENT,
            )
        ).scalar_one_or_none()
        if previous is not None:
            previous.status = FinalRankingStatus.SUPERSEDED
            previous.superseded_at = now
            # Flush the supersede BEFORE the insert so the partial unique index
            # never sees two CURRENT runs.
            db.flush()

        run = FinalRanking(
            job_id=job_uuid,
            rubric_version_id=version_id,
            requested_by_user_id=actor.id,
            screening_weight=fs.SCREENING_WEIGHT,
            interview_weight=fs.INTERVIEW_WEIGHT,
            status=FinalRankingStatus.CURRENT,
            created_at=now,
        )
        db.add(run)
        db.flush()

        for c in computed:
            inp = c.inputs
            db.add(
                FinalRankingEntry(
                    final_ranking_id=run.id,
                    application_id=inp.application_id,
                    screening_score=c.screening_score,
                    interview_score=c.interview_score,
                    final_score=c.final_score,
                    rank=ranks[inp.application_id].rank,
                    eligible=inp.eligible,
                    mandatory_unknown=inp.mandatory_unknown,
                    entry_status=c.entry_status,
                    status_reason=c.status_reason,
                    final_confidence=c.final_confidence,
                    screening_confidence=inp.screening_confidence,
                    screening_generation_batch_id=inp.screening_batch_id,
                    screening_generated_at=inp.screening_generated_at,
                    screening_rank=inp.screening_rank,
                    post_interview_analysis_id=inp.analysis_id,
                    rounds_used=sorted(c.round_means),
                    round_means={
                        str(r): str(fs.round_half_up(m, 2))
                        for r, m in c.round_means.items()
                    },
                    feedback_ids=list(inp.feedback_ids),
                )
            )
        db.flush()

        statuses = [c.entry_status for c in computed]
        audit_payloads.append(
            (
                run,
                {
                    "job_id": str(job_uuid),
                    "final_ranking_id": str(run.id),
                    "rubric_version_id": str(version_id),
                    "screening_weight": str(fs.SCREENING_WEIGHT),
                    "interview_weight": str(fs.INTERVIEW_WEIGHT),
                    "candidate_count": len(computed),
                    "ranked_count": statuses.count(FinalRankingEntryStatus.RANKED),
                    "ineligible_count": statuses.count(
                        FinalRankingEntryStatus.NOT_RANKED_INELIGIBLE
                    ),
                    "incomplete_count": statuses.count(
                        FinalRankingEntryStatus.INCOMPLETE_SCREENING
                    ) + statuses.count(FinalRankingEntryStatus.INCOMPLETE_INTERVIEW),
                    "superseded_run_id": (
                        str(previous.id) if previous is not None else None
                    ),
                },
            )
        )
        runs.append(run)

    for run, payload in audit_payloads:
        record_event(
            db,
            event_type=AuditEventType.FINAL_RANKING_GENERATED,
            action=(
                f"Final ranking generated for job {job_uuid}: "
                f"{payload['ranked_count']} ranked, "
                f"{payload['ineligible_count']} not ranked (ineligible), "
                f"{payload['incomplete_count']} incomplete."
            ),
            entity_type="job",
            entity_id=job_uuid,
            user_id=actor.id,
            new_state=payload,
        )

    db.commit()
    for run in runs:
        db.refresh(run)
    logger.info(
        "final_ranking job=%s runs=%d candidates=%d actor=%s",
        job_uuid, len(runs), len(inputs), actor.id,
    )
    return runs


# --- reads (HR/INTERNAL ONLY) -----------------------------------------------------


def _entry_views(
    db: Session, run: FinalRanking, entries: list[FinalRankingEntry]
) -> tuple[FinalRankingEntryView, ...]:
    ranked_scores = [
        e.final_score for e in entries
        if e.entry_status == FinalRankingEntryStatus.RANKED
    ]
    views: list[tuple[tuple, FinalRankingEntryView]] = []
    for e in entries:
        application = db.get(Application, e.application_id)
        candidate = db.get(Candidate, application.candidate_id) if application else None
        analysis = (
            db.get(PostInterviewAnalysis, e.post_interview_analysis_id)
            if e.post_interview_analysis_id is not None else None
        )
        tied = (
            e.entry_status == FinalRankingEntryStatus.RANKED
            and ranked_scores.count(e.final_score) > 1
        )
        view = FinalRankingEntryView(
            entry_id=e.id,
            application_id=e.application_id,
            candidate_name=candidate.full_name if candidate else "—",
            candidate_email=candidate.email if candidate else "—",
            rank=e.rank,
            tied=tied,
            screening_score=e.screening_score,
            interview_score=e.interview_score,
            final_score=e.final_score,
            eligible=e.eligible,
            mandatory_unknown=e.mandatory_unknown,
            entry_status=e.entry_status,
            status_reason=e.status_reason,
            final_confidence=e.final_confidence,
            screening_confidence=e.screening_confidence,
            screening_rank=e.screening_rank,
            screening_generated_at=e.screening_generated_at,
            rounds_used=tuple(int(r) for r in (e.rounds_used or [])),
            round_means=tuple(
                (int(r), Decimal(m)) for r, m in sorted(
                    (e.round_means or {}).items(), key=lambda kv: int(kv[0])
                )
            ),
            analysis_id=e.post_interview_analysis_id,
            analysis_confidence=analysis.confidence if analysis else None,
            analysis_recommendation=analysis.ai_recommendation if analysis else None,
        )
        group = {
            FinalRankingEntryStatus.RANKED: 0,
            FinalRankingEntryStatus.NOT_RANKED_INELIGIBLE: 1,
            FinalRankingEntryStatus.INCOMPLETE_SCREENING: 2,
            FinalRankingEntryStatus.INCOMPLETE_INTERVIEW: 2,
        }.get(e.entry_status, 3)
        views.append((
            (
                group,
                e.rank if e.rank is not None else 0,
                # inside a tie / a group: screening score desc, then applied
                # earlier first. Stable, and meaningless within a tie.
                -(e.screening_score if e.screening_score is not None
                  else Decimal(-1)),
                application.created_at.timestamp() if application else 0.0,
                str(e.application_id),
            ),
            view,
        ))
    views.sort(key=lambda pair: pair[0])
    return tuple(v for _, v in views)


def _run_view(db: Session, run: FinalRanking) -> FinalRankingView:
    entries = list(
        db.execute(
            select(FinalRankingEntry).where(
                FinalRankingEntry.final_ranking_id == run.id
            )
        ).scalars().all()
    )
    version = db.get(RubricVersion, run.rubric_version_id)
    author = db.get(User, run.requested_by_user_id)
    return FinalRankingView(
        final_ranking_id=run.id,
        job_id=run.job_id,
        rubric_version_id=run.rubric_version_id,
        rubric_version_number=version.version_number if version else None,
        status=run.status,
        screening_weight=run.screening_weight,
        interview_weight=run.interview_weight,
        created_at=run.created_at,
        superseded_at=run.superseded_at,
        requested_by_name=(author.full_name if author else "—"),
        entries=_entry_views(db, run, entries),
    )


def get_current_final_ranking(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[FinalRankingView]:
    """The CURRENT run of each rubric-version partition of the job (ordered by
    version number). Empty if none has been generated. HR/INTERNAL ONLY."""
    require_internal_user(db, acting_user_id)
    runs = db.execute(
        select(FinalRanking).where(
            FinalRanking.job_id == _as_uuid(job_id),
            FinalRanking.status == FinalRankingStatus.CURRENT,
        )
    ).scalars().all()
    views = [_run_view(db, r) for r in runs]
    views.sort(key=lambda v: (v.rubric_version_number is None,
                              v.rubric_version_number or 0))
    return views


def list_final_ranking_history(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> list[FinalRankingView]:
    """Every run of the job, CURRENT and SUPERSEDED, newest first
    (``created_at DESC, id DESC``). HR/INTERNAL ONLY."""
    require_internal_user(db, acting_user_id)
    runs = db.execute(
        select(FinalRanking)
        .where(FinalRanking.job_id == _as_uuid(job_id))
        .order_by(FinalRanking.created_at.desc(), FinalRanking.id.desc())
    ).scalars().all()
    return [_run_view(db, r) for r in runs]


def get_final_ranking_for_application(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> ApplicationFinalRanking | None:
    """The application's entry in its latest CURRENT run, or ``None`` if no
    final ranking has been generated for it. Read-only. HR/INTERNAL ONLY."""
    require_internal_user(db, acting_user_id)
    row = db.execute(
        select(FinalRankingEntry, FinalRanking)
        .join(FinalRanking, FinalRanking.id == FinalRankingEntry.final_ranking_id)
        .where(
            FinalRankingEntry.application_id == _as_uuid(application_id),
            FinalRanking.status == FinalRankingStatus.CURRENT,
        )
        .order_by(FinalRanking.created_at.desc(), FinalRanking.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    entry, run = row
    view = _run_view(db, run)
    mine = next(e for e in view.entries if e.entry_id == entry.id)
    return ApplicationFinalRanking(
        entry=mine,
        final_ranking_id=run.id,
        screening_weight=run.screening_weight,
        interview_weight=run.interview_weight,
        generated_at=run.created_at,
        ranked_count=sum(
            1 for e in view.entries
            if e.entry_status == FinalRankingEntryStatus.RANKED
        ),
    )


def get_final_ranking_staleness(
    db: Session,
    *,
    job_id: uuid.UUID | str,
    acting_user_id: uuid.UUID | str,
) -> FinalRankingStaleness:
    """Is the job's CURRENT final ranking out of date? Read-only; never
    regenerates anything.

    A run is stale if, for any candidate, the set of feedback ids, the screening
    ranking batch, or the CURRENT counted analysis differs from what the run
    recorded — or a candidate with interview feedback is not in a run. With no
    CURRENT run there is nothing to be stale relative to, so not stale.
    """
    actor = require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)

    runs = db.execute(
        select(FinalRanking).where(
            FinalRanking.job_id == job_uuid,
            FinalRanking.status == FinalRankingStatus.CURRENT,
        )
    ).scalars().all()
    if not runs:
        return FinalRankingStaleness(False, 0, 0, 0, 0)

    recorded: dict[tuple[uuid.UUID, uuid.UUID], FinalRankingEntry] = {}
    for run in runs:
        for entry in db.execute(
            select(FinalRankingEntry).where(
                FinalRankingEntry.final_ranking_id == run.id
            )
        ).scalars().all():
            recorded[(run.rubric_version_id, entry.application_id)] = entry

    new = fb = scr = ana = 0
    for inp in _gather(db, job_uuid, acting_user_id=actor.id):
        entry = recorded.get((inp.version_id, inp.application_id))
        if entry is None:
            new += 1
            continue
        if sorted(entry.feedback_ids or []) != sorted(inp.feedback_ids):
            fb += 1
        if entry.screening_generation_batch_id != inp.screening_batch_id:
            scr += 1
        if entry.post_interview_analysis_id != inp.analysis_id:
            ana += 1
    return FinalRankingStaleness(
        is_stale=bool(new or fb or scr or ana),
        new_candidate_count=new,
        changed_feedback_count=fb,
        changed_screening_count=scr,
        changed_analysis_count=ana,
    )
