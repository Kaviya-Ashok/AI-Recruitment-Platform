"""Read-only summary of where one job stands, for the job workspace's stage bar
(HR UI redesign, Increment 2).

PURELY A READ. No write, no audit event, no AI call, no ``Application.status``
change, no new table. Every number is a COUNT over rows earlier steps already
stored; nothing is scored, ranked or inferred here, and no candidate name, note or
free text is read at all.

INCREMENT 3 — THE CANDIDATE PAGE
--------------------------------
Four more readers sit below the stage summary, under the same guard and the same
rules: :func:`get_candidate_header` (one application's headline facts),
:func:`list_job_candidates` (the Prev/Next order and the "Open candidate page"
selector), :func:`list_interview_overview` (the Interviews stage table) and
:func:`list_candidate_activity` (event label, time and actor — never an audit
row's metadata, state snapshots or free-text ``action``). They read STORED scores
and ranks exactly as earlier steps wrote them; nothing is recomputed. The first
reads the candidate's header in a handful of point queries on purpose — the final
scorecard assembly makes dozens, which is far too much for a strip that sits above
every tab.

GUARD
-----
The closest read-accessor precedent is ``final_ranking_service.get_current_final_ranking``
/ ``interview_feedback_service.list_feedback_views``: ``require_internal_user``
first (any active internal user — HR, hiring manager, admin — may see a stage bar),
and, like those readers, NO extra SYSTEM-actor rejection: that check exists to stop
the pipeline AUTHORING human testimony, and this module authors nothing.

THE FIVE STAGES AND WHAT EACH SUMMARY MEANS
-------------------------------------------
``count`` is what the stage label shows, ``complete`` earns it a tick, and
``attention`` is the number of items needing a human's eyes.

* **Setup** — count: the approved rubric's version number (0 if none). Complete:
  an APPROVED rubric version exists. Attention: always 0.
* **Applicants** — count: applications for the job. Complete: a Step 5 screening
  ranking exists for the job. Attention: applications with NO document attached
  (no résumé to read).
* **Shortlist** — count: applications currently shortlisted (``is_shortlisted``
  true). Complete: at least one. Attention: always 0.
* **Interviews** — count: applications with at least one feedback round. Complete:
  at least one such application AND no round without ratings. Attention:
  applications that have a round with no competency ratings (notes only), which the
  final score cannot use.
* **Final ranking** — count: applications with a CURRENT final decision, shown as
  "N of M decided" where M = ``interviewed_count``. Complete: M > 0 and every
  interviewed application has a current decision. Attention: interviewed
  applications WITHOUT a current decision.

"Complete" is an HONEST progress hint, not a gate: nothing is blocked by it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.audit_event import AuditEvent
from app.database.models.candidate import Candidate
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.document import Document
from app.database.models.final_decision import FinalDecision, FinalDecisionStatus
from app.database.models.final_ranking import (
    FinalRanking,
    FinalRankingEntry,
    FinalRankingEntryStatus,
    FinalRankingStatus,
)
from app.database.models.interview_feedback import (
    InterviewFeedback,
    InterviewFeedbackRating,
)
from app.database.models.interview_guide import InterviewGuide
from app.database.models.interview_transcript import (
    InterviewTranscript,
    InterviewTranscriptStatus,
)
from app.database.models.job import Job
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysis,
    PostInterviewAnalysisStatus,
)
from app.database.models.rubric import RubricVersion, RubricVersionStatus
from app.database.models.screening_evaluation import ScreeningEvaluation
from app.database.models.screening_session import ScreeningSession
from app.database.models.user import User
from app.utils.authorization import require_internal_user

#: Stage keys, in workspace order. They are the values written to the URL's
#: ``stage`` parameter.
STAGE_KEYS: tuple[str, ...] = (
    "setup", "applicants", "shortlist", "interviews", "final_ranking",
)

#: Human names, same order.
STAGE_NAMES: dict[str, str] = {
    "setup": "Setup",
    "applicants": "Applicants",
    "shortlist": "Shortlist",
    "interviews": "Interviews",
    "final_ranking": "Final ranking",
}


@dataclass(frozen=True)
class StageSummary:
    count: int
    complete: bool
    attention: int


@dataclass(frozen=True)
class JobHeaderFacts:
    job_id: uuid.UUID
    code: str
    title: str
    status: str
    #: Version number of the APPROVED rubric, or ``None``.
    approved_rubric_version: int | None


@dataclass(frozen=True)
class JobStageSummary:
    header: JobHeaderFacts
    setup: StageSummary
    applicants: StageSummary
    shortlist: StageSummary
    interviews: StageSummary
    final_ranking: StageSummary
    #: M in "N of M decided": applications with at least one feedback round.
    interviewed_count: int

    def stage(self, key: str) -> StageSummary:
        if key not in STAGE_KEYS:
            raise KeyError(key)
        return getattr(self, key)


def default_stage(summary: JobStageSummary) -> str:
    """The first INCOMPLETE stage, in order; the last stage if all are complete.
    Pure."""
    for key in STAGE_KEYS:
        if not summary.stage(key).complete:
            return key
    return STAGE_KEYS[-1]


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def get_job_stage_summary(
    db: Session,
    job_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> JobStageSummary | None:
    """Stage-by-stage counts for one job, or ``None`` if there is no such job
    (including a malformed id). HR/INTERNAL ONLY; read-only.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is missing, unknown or inactive.
    """
    require_internal_user(db, acting_user_id)

    job_uuid = _as_uuid(job_id)
    job = db.get(Job, job_uuid) if job_uuid is not None else None
    if job is None:
        return None

    approved = db.execute(
        select(RubricVersion.version_number).where(
            RubricVersion.job_id == job.id,
            RubricVersion.status == RubricVersionStatus.APPROVED,
        ).limit(1)
    ).scalar_one_or_none()

    job_apps = select(Application.id).where(Application.job_id == job.id)

    application_count = db.execute(
        select(func.count()).select_from(job_apps.subquery())
    ).scalar_one()
    without_resume = db.execute(
        select(func.count(Application.id)).where(
            Application.job_id == job.id,
            ~exists().where(Document.application_id == Application.id),
        )
    ).scalar_one()
    has_screening_ranking = db.execute(
        select(exists().where(CandidateRanking.job_id == job.id))
    ).scalar_one()

    shortlisted = db.execute(
        select(func.count(CandidateShortlistEntry.id)).where(
            CandidateShortlistEntry.job_id == job.id,
            CandidateShortlistEntry.is_shortlisted.is_(True),
        )
    ).scalar_one()

    # One row per feedback round: (application, how many ratings it carries).
    rounds = db.execute(
        select(
            InterviewFeedback.application_id,
            func.count(InterviewFeedbackRating.id),
        )
        .join(Application, Application.id == InterviewFeedback.application_id)
        .outerjoin(
            InterviewFeedbackRating,
            InterviewFeedbackRating.interview_feedback_id == InterviewFeedback.id,
        )
        .where(Application.job_id == job.id)
        .group_by(InterviewFeedback.id, InterviewFeedback.application_id)
    ).all()
    interviewed = {app_id for app_id, _ in rounds}
    unrated = {app_id for app_id, rating_count in rounds if rating_count == 0}

    decided = set(
        db.execute(
            select(FinalDecision.application_id)
            .join(Application, Application.id == FinalDecision.application_id)
            .where(
                Application.job_id == job.id,
                FinalDecision.status == FinalDecisionStatus.CURRENT,
            )
        ).scalars().all()
    )
    undecided_interviewed = len(interviewed - decided)

    return JobStageSummary(
        header=JobHeaderFacts(
            job_id=job.id,
            code=job.job_code,
            title=job.title,
            status=job.status,
            approved_rubric_version=approved,
        ),
        setup=StageSummary(
            count=approved or 0, complete=approved is not None, attention=0
        ),
        applicants=StageSummary(
            count=application_count,
            complete=bool(has_screening_ranking),
            attention=without_resume,
        ),
        shortlist=StageSummary(
            count=shortlisted, complete=shortlisted > 0, attention=0
        ),
        interviews=StageSummary(
            count=len(interviewed),
            complete=bool(interviewed) and not unrated,
            attention=len(unrated),
        ),
        final_ranking=StageSummary(
            count=len(decided),
            complete=bool(interviewed) and undecided_interviewed == 0,
            attention=undecided_interviewed,
        ),
        interviewed_count=len(interviewed),
    )


# --- Increment 3: the candidate page -----------------------------------------------

#: Most activity rows the candidate Overview shows (newest first).
ACTIVITY_LIMIT = 50


@dataclass(frozen=True)
class CandidateHeader:
    """Headline facts for one application of one job — everything the candidate
    page's header, summary strip, Progress and Next step need. Frozen primitives
    only, so it can be built inside ``session_scope()`` and used after the session
    closes."""

    application_id: uuid.UUID
    job_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    job_code: str
    job_title: str
    application_status: str
    #: A screening evaluation's overall score exists for this application.
    screened: bool
    is_shortlisted: bool
    #: Screening score (0-10) and rank, as stored. When a CURRENT final ranking
    #: has this application the snapshot it recorded is used; otherwise the latest
    #: screening-ranking row.
    screening_score: Decimal | None
    screening_rank: int | None
    interview_score: Decimal | None
    final_score: Decimal | None
    final_rank: int | None
    #: How many candidates the CURRENT final ranking ranks (for "of N ranked").
    final_ranked_count: int | None
    #: A ``FinalRankingEntryStatus`` value, or ``None`` with no CURRENT final ranking.
    entry_status: str | None
    entry_reason: str | None
    has_current_final_ranking: bool
    has_analysis: bool
    rounds_count: int
    #: At least one round has no competency ratings (notes only).
    has_unrated_round: bool
    #: CURRENT transcripts (a replaced version is not counted).
    transcripts_count: int
    #: The CURRENT final decision, if any.
    decision: str | None
    decided_by_name: str | None
    decided_at: datetime | None
    #: The CURRENT shortlist entry's context (all empty / ``False`` when the
    #: application is not shortlisted): the rubric version it was shortlisted
    #: against, the screening rank recorded then, and the rank in that SAME version
    #: now (``current_rank_available`` is ``False`` when no ranking row exists for
    #: it). Read by the page through ``utils.ranking_drift``.
    shortlist_rubric_version_number: int | None = None
    shortlist_rubric_version_status: str | None = None
    rank_position_at_shortlisting: int | None = None
    current_rank_position: int | None = None
    current_rank_available: bool = False


@dataclass(frozen=True)
class JobCandidate:
    """One application of a job, in the order the Prev/Next switcher walks."""

    application_id: uuid.UUID
    candidate_name: str
    screening_rank: int | None


@dataclass(frozen=True)
class InterviewOverviewRow:
    """One row of the workspace's Interviews table."""

    application_id: uuid.UUID
    candidate_name: str
    rounds_count: int
    has_unrated_round: bool
    transcripts_count: int
    has_analysis: bool
    decision: str | None


@dataclass(frozen=True)
class ActivityItem:
    """One audit event, reduced to what a reader needs: what, when, who."""

    event_type: str
    timestamp: datetime
    actor_name: str | None


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def get_candidate_header(
    db: Session,
    job_id: uuid.UUID | str,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> CandidateHeader | None:
    """The header facts for one application, or ``None`` when there is no such
    application OR it does not belong to ``job_id`` (a malformed id counts as
    none). HR/INTERNAL ONLY; read-only.

    Query budget: the guard, one joined read (with correlated counts) for the
    application, the CURRENT final-ranking entry (plus, only when it is ranked,
    one count), the latest screening-ranking row (only when no final entry
    supplies the screening snapshot), and the CURRENT decision — at most six.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is missing, unknown or inactive.
    """
    require_internal_user(db, acting_user_id)

    job_uuid = _as_uuid(job_id)
    app_uuid = _as_uuid(application_id)
    if job_uuid is None or app_uuid is None:
        return None

    rounds_q = (
        select(func.count(InterviewFeedback.id))
        .where(InterviewFeedback.application_id == Application.id)
        .scalar_subquery()
    )
    unrated_q = (
        select(func.count(InterviewFeedback.id))
        .where(
            InterviewFeedback.application_id == Application.id,
            ~exists().where(
                InterviewFeedbackRating.interview_feedback_id == InterviewFeedback.id
            ),
        )
        .scalar_subquery()
    )
    transcripts_q = (
        select(func.count(InterviewTranscript.id))
        .join(
            InterviewFeedback,
            InterviewFeedback.id == InterviewTranscript.interview_feedback_id,
        )
        .where(
            InterviewFeedback.application_id == Application.id,
            InterviewTranscript.status == InterviewTranscriptStatus.CURRENT,
        )
        .scalar_subquery()
    )
    analysis_q = exists().where(
        PostInterviewAnalysis.application_id == Application.id,
        PostInterviewAnalysis.status == PostInterviewAnalysisStatus.CURRENT,
    )
    shortlisted_q = exists().where(
        CandidateShortlistEntry.application_id == Application.id,
        CandidateShortlistEntry.is_shortlisted.is_(True),
    )
    screened_q = exists().where(
        ScreeningSession.application_id == Application.id,
        ScreeningSession.id.in_(
            select(ScreeningEvaluation.screening_session_id)
        ),
    )

    # The shortlist context rides in the same statement as correlated scalars, so
    # the query budget below is unchanged.
    _entry_here = (
        CandidateShortlistEntry.application_id == Application.id,
        CandidateShortlistEntry.is_shortlisted.is_(True),
    )
    sl_rank_q = (
        select(CandidateShortlistEntry.rank_position_at_decision)
        .where(*_entry_here).limit(1).scalar_subquery()
    )
    sl_version_q = (
        select(RubricVersion.version_number)
        .join(
            CandidateShortlistEntry,
            CandidateShortlistEntry.rubric_version_id == RubricVersion.id,
        )
        .where(*_entry_here).limit(1).scalar_subquery()
    )
    sl_status_q = (
        select(RubricVersion.status)
        .join(
            CandidateShortlistEntry,
            CandidateShortlistEntry.rubric_version_id == RubricVersion.id,
        )
        .where(*_entry_here).limit(1).scalar_subquery()
    )
    _ranking_here = (
        CandidateRanking.job_id == CandidateShortlistEntry.job_id,
        CandidateRanking.rubric_version_id == CandidateShortlistEntry.rubric_version_id,
        CandidateRanking.application_id == CandidateShortlistEntry.application_id,
    )
    cur_rank_q = (
        select(CandidateRanking.rank_position)
        .where(*_ranking_here, *_entry_here).limit(1).scalar_subquery()
    )
    cur_available_q = exists().where(*_ranking_here, *_entry_here)

    row = db.execute(
        select(
            Application.status,
            Candidate.full_name,
            Candidate.email,
            Job.job_code,
            Job.title,
            rounds_q,
            unrated_q,
            transcripts_q,
            analysis_q,
            shortlisted_q,
            screened_q,
            sl_rank_q,
            sl_version_q,
            sl_status_q,
            cur_rank_q,
            cur_available_q,
        )
        .join(Candidate, Candidate.id == Application.candidate_id)
        .join(Job, Job.id == Application.job_id)
        .where(Application.id == app_uuid, Application.job_id == job_uuid)
    ).first()
    if row is None:
        return None
    (
        status, name, email, code, title, rounds, unrated, transcripts,
        has_analysis, shortlisted, screened,
        sl_rank, sl_version, sl_status, cur_rank, cur_available,
    ) = row

    final = db.execute(
        select(FinalRankingEntry, FinalRanking.id)
        .join(FinalRanking, FinalRanking.id == FinalRankingEntry.final_ranking_id)
        .where(
            FinalRankingEntry.application_id == app_uuid,
            FinalRanking.status == FinalRankingStatus.CURRENT,
        )
        .order_by(FinalRanking.created_at.desc(), FinalRanking.id.desc())
        .limit(1)
    ).first()

    entry = final[0] if final is not None else None
    ranked_count = None
    if entry is not None and entry.rank is not None:
        ranked_count = db.execute(
            select(func.count(FinalRankingEntry.id)).where(
                FinalRankingEntry.final_ranking_id == final[1],
                FinalRankingEntry.entry_status == FinalRankingEntryStatus.RANKED,
            )
        ).scalar_one()

    if entry is not None:
        screening_score = _dec(entry.screening_score)
        screening_rank = entry.screening_rank
    else:
        ranking = db.execute(
            select(CandidateRanking.overall_score, CandidateRanking.rank_position)
            .where(CandidateRanking.application_id == app_uuid)
            .order_by(CandidateRanking.generated_at.desc())
            .limit(1)
        ).first()
        screening_score = _dec(ranking[0]) if ranking is not None else None
        screening_rank = ranking[1] if ranking is not None else None

    decision = db.execute(
        select(FinalDecision.decision, FinalDecision.created_at, User.full_name)
        .outerjoin(User, User.id == FinalDecision.decided_by_user_id)
        .where(
            FinalDecision.application_id == app_uuid,
            FinalDecision.status == FinalDecisionStatus.CURRENT,
        )
        .limit(1)
    ).first()

    return CandidateHeader(
        application_id=app_uuid,
        job_id=job_uuid,
        candidate_name=name,
        candidate_email=email,
        job_code=code,
        job_title=title,
        application_status=status,
        screened=bool(screened),
        is_shortlisted=bool(shortlisted),
        screening_score=screening_score,
        screening_rank=screening_rank,
        interview_score=_dec(entry.interview_score) if entry is not None else None,
        final_score=_dec(entry.final_score) if entry is not None else None,
        final_rank=entry.rank if entry is not None else None,
        final_ranked_count=ranked_count,
        entry_status=entry.entry_status if entry is not None else None,
        entry_reason=entry.status_reason if entry is not None else None,
        has_current_final_ranking=entry is not None,
        has_analysis=bool(has_analysis),
        rounds_count=int(rounds or 0),
        has_unrated_round=bool(unrated),
        transcripts_count=int(transcripts or 0),
        decision=decision[0] if decision is not None else None,
        decided_by_name=(decision[2] or "—") if decision is not None else None,
        decided_at=decision[1] if decision is not None else None,
        shortlist_rubric_version_number=sl_version if shortlisted else None,
        shortlist_rubric_version_status=sl_status if shortlisted else None,
        rank_position_at_shortlisting=sl_rank if shortlisted else None,
        current_rank_position=cur_rank if shortlisted else None,
        current_rank_available=bool(cur_available) if shortlisted else False,
    )


def list_job_candidates(
    db: Session,
    job_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[JobCandidate]:
    """Every application of the job, ordered for the Prev/Next switcher: screening
    rank ascending (unranked last), then earliest applied, then id (a stable
    tie-break). Empty for an unknown or malformed job. HR/INTERNAL ONLY; read-only.

    The rank is the application's latest screening-ranking row, as stored.
    """
    require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)
    if job_uuid is None:
        return []

    apps = db.execute(
        select(Application.id, Candidate.full_name, Application.created_at)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(Application.job_id == job_uuid)
    ).all()
    if not apps:
        return []

    latest: dict[uuid.UUID, int | None] = {}
    for app_id, rank in db.execute(
        select(CandidateRanking.application_id, CandidateRanking.rank_position)
        .where(CandidateRanking.job_id == job_uuid)
        .order_by(CandidateRanking.generated_at.asc())
    ).all():
        latest[app_id] = rank        # ascending by time, so the newest row wins

    rows = [
        JobCandidate(app_id, name, latest.get(app_id))
        for app_id, name, _created in apps
    ]
    created = {app_id: c for app_id, _n, c in apps}
    rows.sort(
        key=lambda r: (
            r.screening_rank is None,
            r.screening_rank or 0,
            created[r.application_id],
            str(r.application_id),
        )
    )
    return rows


def list_interview_overview(
    db: Session,
    job_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[InterviewOverviewRow]:
    """One row per application of the job that is in the interview stage — shortlisted
    now, holding an interview guide, or with at least one recorded round — in the
    Prev/Next order. HR/INTERNAL ONLY; read-only; set-based (no per-row queries)."""
    require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)
    if job_uuid is None:
        return []
    candidates = list_job_candidates(db, job_uuid, acting_user_id=acting_user_id)
    if not candidates:
        return []

    job_apps = select(Application.id).where(Application.job_id == job_uuid)

    rounds: dict[uuid.UUID, int] = {}
    unrated: dict[uuid.UUID, bool] = {}
    for app_id, rating_count in db.execute(
        select(InterviewFeedback.application_id, func.count(InterviewFeedbackRating.id))
        .outerjoin(
            InterviewFeedbackRating,
            InterviewFeedbackRating.interview_feedback_id == InterviewFeedback.id,
        )
        .where(InterviewFeedback.application_id.in_(job_apps))
        .group_by(InterviewFeedback.id, InterviewFeedback.application_id)
    ).all():
        rounds[app_id] = rounds.get(app_id, 0) + 1
        if rating_count == 0:
            unrated[app_id] = True

    transcripts = dict(
        db.execute(
            select(InterviewFeedback.application_id, func.count(InterviewTranscript.id))
            .join(
                InterviewTranscript,
                InterviewTranscript.interview_feedback_id == InterviewFeedback.id,
            )
            .where(
                InterviewFeedback.application_id.in_(job_apps),
                InterviewTranscript.status == InterviewTranscriptStatus.CURRENT,
            )
            .group_by(InterviewFeedback.application_id)
        ).all()
    )
    analysed = set(
        db.execute(
            select(PostInterviewAnalysis.application_id).where(
                PostInterviewAnalysis.application_id.in_(job_apps),
                PostInterviewAnalysis.status == PostInterviewAnalysisStatus.CURRENT,
            )
        ).scalars().all()
    )
    decisions = dict(
        db.execute(
            select(FinalDecision.application_id, FinalDecision.decision).where(
                FinalDecision.application_id.in_(job_apps),
                FinalDecision.status == FinalDecisionStatus.CURRENT,
            )
        ).all()
    )
    shortlisted = set(
        db.execute(
            select(CandidateShortlistEntry.application_id).where(
                CandidateShortlistEntry.job_id == job_uuid,
                CandidateShortlistEntry.is_shortlisted.is_(True),
            )
        ).scalars().all()
    )
    with_guide = set(
        db.execute(
            select(InterviewGuide.application_id).where(
                InterviewGuide.job_id == job_uuid
            )
        ).scalars().all()
    )

    return [
        InterviewOverviewRow(
            application_id=c.application_id,
            candidate_name=c.candidate_name,
            rounds_count=rounds.get(c.application_id, 0),
            has_unrated_round=unrated.get(c.application_id, False),
            transcripts_count=transcripts.get(c.application_id, 0),
            has_analysis=c.application_id in analysed,
            decision=decisions.get(c.application_id),
        )
        for c in candidates
        if c.application_id in shortlisted
        or c.application_id in with_guide
        or c.application_id in rounds
    ]


def list_candidate_activity(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
    limit: int = ACTIVITY_LIMIT,
) -> list[ActivityItem]:
    """Audit events about one application — newest first — as (event type, time,
    actor name). Events are found STRUCTURALLY, by what they point at: the
    application itself, its résumé documents, its screening session and its
    interview transcripts. This reads ONLY ``event_type``, ``timestamp`` and the
    actor's name; an event's ``action`` sentence, metadata and state snapshots are
    never selected. HR/INTERNAL ONLY; read-only.
    """
    require_internal_user(db, acting_user_id)
    app_uuid = _as_uuid(application_id)
    if app_uuid is None:
        return []

    own = (AuditEvent.entity_type == "application") & (
        AuditEvent.entity_id == app_uuid
    )
    documents = (AuditEvent.entity_type == "document") & AuditEvent.entity_id.in_(
        select(Document.id).where(Document.application_id == app_uuid)
    )
    sessions = (
        AuditEvent.entity_type == "screening_session"
    ) & AuditEvent.entity_id.in_(
        select(ScreeningSession.id).where(ScreeningSession.application_id == app_uuid)
    )
    transcripts = (
        AuditEvent.entity_type == "interview_transcript"
    ) & AuditEvent.entity_id.in_(
        select(InterviewTranscript.id)
        .join(
            InterviewFeedback,
            InterviewFeedback.id == InterviewTranscript.interview_feedback_id,
        )
        .where(InterviewFeedback.application_id == app_uuid)
    )

    rows = db.execute(
        select(AuditEvent.event_type, AuditEvent.timestamp, User.full_name)
        .outerjoin(User, User.id == AuditEvent.user_id)
        .where(own | documents | sessions | transcripts)
        .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return [ActivityItem(t, ts, who) for t, ts, who in rows]


# --- Increment 5: the Shortlist stage, in one statement ----------------------------


@dataclass(frozen=True)
class ShortlistOverviewRow:
    """One currently-shortlisted candidate of a job, with what the Shortlist stage
    shows: the rubric version shortlisted against, the rank then and the rank in that
    SAME version now, whether a guide exists and how many rounds are recorded."""

    application_id: uuid.UUID
    candidate_name: str
    rubric_version_number: int | None
    rubric_version_status: str | None
    rank_position_at_decision: int | None
    current_rank_position: int | None
    current_rank_available: bool
    guide_exists: bool
    rounds_count: int


def list_shortlist_overview(
    db: Session,
    job_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[ShortlistOverviewRow]:
    """Every currently-shortlisted candidate of the job — HR/INTERNAL ONLY, read-only.

    Query budget: the guard plus ONE statement, whatever the number of rows (the
    older ``interview_guide_service.get_shortlisted_candidates_for_job`` is unchanged
    and still serves the guide flow; it reads each row separately). The rank is read
    from the ranking of the SAME rubric version the candidate was shortlisted
    against — versions are never merged. Order: rubric version (unknown last), then
    the current rank, then the rank at shortlisting (unranked last), then name.
    """
    require_internal_user(db, acting_user_id)
    job_uuid = _as_uuid(job_id)
    if job_uuid is None:
        return []

    guide_q = exists().where(
        InterviewGuide.application_id == CandidateShortlistEntry.application_id
    )
    rounds_q = (
        select(func.count(InterviewFeedback.id))
        .where(InterviewFeedback.application_id == CandidateShortlistEntry.application_id)
        .scalar_subquery()
    )
    rows = db.execute(
        select(
            CandidateShortlistEntry.application_id,
            Candidate.full_name,
            RubricVersion.version_number,
            RubricVersion.status,
            CandidateShortlistEntry.rank_position_at_decision,
            CandidateRanking.rank_position,
            CandidateRanking.id.is_not(None),
            guide_q,
            rounds_q,
        )
        .join(Application, Application.id == CandidateShortlistEntry.application_id)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .outerjoin(
            RubricVersion, RubricVersion.id == CandidateShortlistEntry.rubric_version_id
        )
        .outerjoin(
            CandidateRanking,
            (CandidateRanking.job_id == CandidateShortlistEntry.job_id)
            & (CandidateRanking.rubric_version_id == CandidateShortlistEntry.rubric_version_id)
            & (CandidateRanking.application_id == CandidateShortlistEntry.application_id),
        )
        .where(
            CandidateShortlistEntry.job_id == job_uuid,
            CandidateShortlistEntry.is_shortlisted.is_(True),
        )
    ).all()

    out = [
        ShortlistOverviewRow(
            application_id=app_id,
            candidate_name=name,
            rubric_version_number=version,
            rubric_version_status=status,
            rank_position_at_decision=at_decision,
            current_rank_position=current,
            current_rank_available=bool(available),
            guide_exists=bool(guide),
            rounds_count=int(rounds or 0),
        )
        for app_id, name, version, status, at_decision, current, available, guide, rounds
        in rows
    ]
    out.sort(
        key=lambda r: (
            r.rubric_version_number is None,
            r.rubric_version_number or 0,
            r.current_rank_position is None,
            r.current_rank_position or 0,
            r.rank_position_at_decision is None,
            r.rank_position_at_decision or 0,
            r.candidate_name.lower(),
        )
    )
    return out
