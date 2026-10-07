"""Read-only overview of the whole pipeline for the HR dashboard, the Jobs table,
the Candidates list and Quick find (HR UI redesign, Increment 4).

PURELY A READ. No write, no audit event, no AI call, no status change, no new
table. Every figure is a COUNT or an EXISTS over rows earlier steps already stored;
nothing is scored, ranked or inferred here. Candidate notes, interview notes,
decision rationales, transcript text and password hashes are never selected — the
queries name their columns, and the view dataclasses have no field that could hold
them.

GUARD
-----
Each reader starts with ``require_internal_user`` (any active internal user — HR,
hiring manager, admin — may see the overview), the same as the existing readers in
``job_workspace_service``.

ONE STATEMENT PER LIST — THE QUERY COUNT DOES NOT GROW WITH ROWS
----------------------------------------------------------------
Each list is built from correlated ``EXISTS`` / ``COUNT`` sub-selects inside ONE
statement (plus one count for paging), never from a query per row:

* :func:`get_dashboard_summary`    guard + one per-job counts statement + one
                                   Needs-attention statement (a UNION ALL carrying a
                                   window total)                          = 3 queries
* :func:`list_jobs_overview`       guard + one page statement + one count = 3 queries
* :func:`list_candidates_overview` guard + one page statement + one count = 3 queries
* :func:`quick_find`               guard + one candidates + one jobs      = 3 queries

(tests/test_overview_service.py proves each is the same with 2 rows and with 12.)

DEFINITIONS — reused, not invented
----------------------------------
* A job's CURRENT stage: ``overview_helpers.job_stage_key`` — the first incomplete
  workspace stage, with completeness defined exactly as ``get_job_stage_summary``
  defines it (a test compares the two on real data).
* A candidate's STAGE: the furthest-stage-wins rule of
  ``candidate_progress.stage_name`` (Decided > Ranked > Interviewed > Shortlisted >
  Screened > Applied), written once here as a SQL ``CASE`` so the list can filter and
  page in the database. A test compares the ``CASE`` with ``stage_name`` on real
  candidates in every state, so the two cannot drift.
* "Interviewed" in the dashboard and Jobs counts means "has at least one recorded
  round" — the same number the workspace's Interviews stage shows. The candidate
  STAGE "Interviewed" is stricter (every round carries ratings), exactly as on the
  candidate page.
* Needs attention has three item types and no others: RATINGS_MISSING (a recorded
  round has no competency ratings), RESUME_MISSING (no document attached) and
  DECISION_PENDING (a round exists but no CURRENT final decision).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, case, exists, func, literal, select, union_all
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.candidate import Candidate
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.document import Document
from app.database.models.final_decision import FinalDecision, FinalDecisionStatus
from app.database.models.final_ranking import (
    FinalRanking,
    FinalRankingEntry,
    FinalRankingStatus,
)
from app.database.models.interview_feedback import (
    InterviewFeedback,
    InterviewFeedbackRating,
)
from app.database.models.job import Job, JobStatus
from app.database.models.rubric import RubricVersion, RubricVersionStatus
from app.database.models.screening_evaluation import ScreeningEvaluation
from app.database.models.screening_session import ScreeningSession
from app.utils.authorization import require_internal_user
from app.utils.candidate_progress import STAGE_LABELS
from app.utils.overview_helpers import (
    ATTENTION_LIMIT,
    ATTENTION_ORDER,
    DECISION_PENDING,
    DEFAULT_PAGE_SIZE,
    RATINGS_MISSING,
    RESUME_MISSING,
    job_stage_key,
    like_pattern,
    normalise_filter_text,
    normalise_query,
    stage_filter_value,
)

#: Hard ceiling on one page, whatever a caller asks for.
MAX_PAGE_SIZE = 100
#: Quick find shows at most this many matches in all.
QUICK_FIND_LIMIT = 8


# --- views (frozen; no hash, no note, no rationale, no transcript) --------------------------


@dataclass(frozen=True)
class AttentionItem:
    kind: str
    job_id: uuid.UUID
    job_code: str
    job_title: str
    application_id: uuid.UUID
    candidate_name: str


@dataclass(frozen=True)
class JobPipeline:
    """How far an open job's applications have got — cumulative counts."""

    job_id: uuid.UUID
    job_code: str
    job_title: str
    applicants: int
    screened: int
    shortlisted: int
    interviewed: int
    decided: int


@dataclass(frozen=True)
class DashboardSummary:
    #: Every job, in any status (0 means the platform has no jobs yet).
    jobs: int
    open_jobs: int
    applications: int
    interviewed: int
    decided: int
    #: Up to :data:`ATTENTION_LIMIT` items, in the Needs-attention order.
    attention: tuple[AttentionItem, ...]
    #: How many items there are in all (``>= len(attention)``).
    attention_total: int
    pipelines: tuple[JobPipeline, ...]


@dataclass(frozen=True)
class JobOverviewRow:
    job_id: uuid.UUID
    job_code: str
    title: str
    status: str
    #: A workspace stage key (``job_workspace_service.STAGE_KEYS``).
    stage_key: str
    applicants: int
    interviewed: int
    decided: int
    created_at: datetime | None


@dataclass(frozen=True)
class JobsOverview:
    rows: tuple[JobOverviewRow, ...]
    total: int


@dataclass(frozen=True)
class CandidateOverviewRow:
    application_id: uuid.UUID
    job_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    job_code: str
    job_title: str
    #: One of ``candidate_progress.STAGE_LABELS`` values.
    stage: str
    #: The CURRENT final decision value, or ``None``.
    decision: str | None


@dataclass(frozen=True)
class CandidatesOverview:
    rows: tuple[CandidateOverviewRow, ...]
    total: int


@dataclass(frozen=True)
class JobOption:
    """One entry of the Candidates list's job filter."""

    job_id: uuid.UUID
    job_code: str
    title: str


@dataclass(frozen=True)
class FindMatch:
    """One Quick find result. ``kind`` is ``"candidate"`` or ``"job"``."""

    kind: str
    job_id: uuid.UUID
    #: The application for a candidate match; ``None`` for a job.
    application_id: uuid.UUID | None
    label: str
    detail: str


# --- shared SQL fragments (each call builds a fresh, correlated expression) ----------------


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _no_document():
    return ~exists().where(Document.application_id == Application.id)


def _rounds_exist():
    return exists().where(InterviewFeedback.application_id == Application.id)


def _unrated_round_exists():
    return exists().where(
        InterviewFeedback.application_id == Application.id,
        ~exists().where(
            InterviewFeedbackRating.interview_feedback_id == InterviewFeedback.id
        ),
    )


def _decided():
    return exists().where(
        FinalDecision.application_id == Application.id,
        FinalDecision.status == FinalDecisionStatus.CURRENT,
    )


def _shortlisted():
    return exists().where(
        CandidateShortlistEntry.application_id == Application.id,
        CandidateShortlistEntry.is_shortlisted.is_(True),
    )


def _screened():
    return exists().where(
        ScreeningSession.application_id == Application.id,
        ScreeningSession.id.in_(select(ScreeningEvaluation.screening_session_id)),
    )


def _ranked():
    return exists().where(
        FinalRankingEntry.application_id == Application.id,
        FinalRankingEntry.rank.is_not(None),
        FinalRankingEntry.final_ranking_id.in_(
            select(FinalRanking.id).where(
                FinalRanking.status == FinalRankingStatus.CURRENT
            )
        ),
    )


def _stage_expr():
    """The candidate STAGE as a SQL ``CASE`` — the furthest stage reached wins, in
    ``candidate_progress.STAGE_PRECEDENCE`` order. "Interviewed" needs a recorded
    round AND no round without ratings (``candidate_progress.has_ratings``)."""
    return case(
        (_decided(), STAGE_LABELS["decided"]),
        (_ranked(), STAGE_LABELS["ranked"]),
        (_rounds_exist() & ~_unrated_round_exists(), STAGE_LABELS["interviewed"]),
        (_shortlisted(), STAGE_LABELS["shortlisted"]),
        (_screened(), STAGE_LABELS["screened"]),
        else_=STAGE_LABELS["applied"],
    )


def _count_apps(*conditions):
    """Correlated ``COUNT`` of this job's applications satisfying ``conditions``."""
    return (
        select(func.count(Application.id))
        .where(Application.job_id == Job.id, *conditions)
        .scalar_subquery()
    )


def _count_shortlisted():
    return (
        select(func.count(CandidateShortlistEntry.id))
        .where(
            CandidateShortlistEntry.job_id == Job.id,
            CandidateShortlistEntry.is_shortlisted.is_(True),
        )
        .scalar_subquery()
    )


# --- dashboard --------------------------------------------------------------------------------


def _attention_branch(kind: str, *conditions):
    return (
        select(
            literal(kind, type_=String).label("kind"),
            Application.id.label("application_id"),
            Job.id.label("job_id"),
            Job.job_code.label("job_code"),
            Job.title.label("job_title"),
            Candidate.full_name.label("candidate_name"),
        )
        .select_from(Application)
        .join(Job, Job.id == Application.job_id)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .where(*conditions)
    )


def get_dashboard_summary(
    db: Session, *, acting_user_id: uuid.UUID | str | None
) -> DashboardSummary:
    """The dashboard in three queries (guard, per-job counts, Needs attention).
    HR/INTERNAL ONLY; read-only.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is missing, unknown or inactive.
    """
    require_internal_user(db, acting_user_id)

    job_rows = db.execute(
        select(
            Job.id, Job.job_code, Job.title, Job.status,
            _count_apps().label("applicants"),
            _count_apps(_screened()).label("screened"),
            _count_shortlisted().label("shortlisted"),
            _count_apps(_rounds_exist()).label("interviewed"),
            _count_apps(_decided()).label("decided"),
        ).order_by(Job.created_at.desc(), Job.id)
    ).all()

    open_jobs = [r for r in job_rows if r.status == JobStatus.OPEN]

    union = union_all(
        _attention_branch(RATINGS_MISSING, _unrated_round_exists()),
        _attention_branch(RESUME_MISSING, _no_document()),
        _attention_branch(DECISION_PENDING, _rounds_exist(), ~_decided()),
    ).subquery()
    kind_rank = case(
        {kind: index for index, kind in enumerate(ATTENTION_ORDER)},
        value=union.c.kind,
        else_=len(ATTENTION_ORDER),
    )
    attention_rows = db.execute(
        select(
            union.c.kind, union.c.application_id, union.c.job_id, union.c.job_code,
            union.c.job_title, union.c.candidate_name,
            func.count().over().label("total"),
        )
        .order_by(
            kind_rank, func.lower(union.c.job_code), func.lower(union.c.candidate_name),
            union.c.application_id,
        )
        .limit(ATTENTION_LIMIT)
    ).all()

    return DashboardSummary(
        jobs=len(job_rows),
        open_jobs=len(open_jobs),
        applications=sum(r.applicants for r in job_rows),
        interviewed=sum(r.interviewed for r in job_rows),
        decided=sum(r.decided for r in job_rows),
        attention=tuple(
            AttentionItem(r.kind, r.job_id, r.job_code, r.job_title,
                          r.application_id, r.candidate_name)
            for r in attention_rows
        ),
        attention_total=attention_rows[0].total if attention_rows else 0,
        pipelines=tuple(
            JobPipeline(r.id, r.job_code, r.title, r.applicants, r.screened,
                        r.shortlisted, r.interviewed, r.decided)
            for r in open_jobs
        ),
    )


# --- jobs table -----------------------------------------------------------------------------------


def _page_args(limit: int, offset: int) -> tuple[int, int]:
    return min(max(int(limit), 1), MAX_PAGE_SIZE), max(int(offset), 0)


def list_jobs_overview(
    db: Session,
    *,
    acting_user_id: uuid.UUID | str | None,
    statuses: Iterable[str] | None = None,
    search: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> JobsOverview:
    """One page of jobs (newest first) with applicant / interviewed / decided counts
    and the current workspace stage, plus the total that match. Three queries.

    ``statuses`` restricts to those job statuses (an empty iterable matches
    nothing); ``search`` is a case-insensitive "contains" on title, job code or
    department, with LIKE wildcards matched literally. HR/INTERNAL ONLY; read-only.
    """
    require_internal_user(db, acting_user_id)
    limit, offset = _page_args(limit, offset)

    conditions = []
    if statuses is not None:
        conditions.append(Job.status.in_(list(statuses)))
    text = normalise_filter_text(search)
    if text:
        pattern = like_pattern(text)
        conditions.append(
            Job.title.ilike(pattern, escape="\\")
            | Job.job_code.ilike(pattern, escape="\\")
            | Job.department.ilike(pattern, escape="\\")
        )

    total = db.execute(select(func.count(Job.id)).where(*conditions)).scalar_one()

    interviewed = _count_apps(_rounds_exist())
    rows = db.execute(
        select(
            Job.id, Job.job_code, Job.title, Job.status, Job.created_at,
            _count_apps().label("applicants"),
            interviewed.label("interviewed"),
            _count_apps(_decided()).label("decided"),
            _count_apps(_unrated_round_exists()).label("unrated"),
            _count_apps(_rounds_exist(), _decided()).label("decided_interviewed"),
            _count_shortlisted().label("shortlisted"),
            exists().where(
                RubricVersion.job_id == Job.id,
                RubricVersion.status == RubricVersionStatus.APPROVED,
            ).label("approved"),
            exists().where(CandidateRanking.job_id == Job.id).label("ranked"),
        )
        .where(*conditions)
        .order_by(Job.created_at.desc(), Job.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return JobsOverview(
        rows=tuple(
            JobOverviewRow(
                job_id=r.id, job_code=r.job_code, title=r.title, status=r.status,
                stage_key=job_stage_key(
                    approved_rubric=bool(r.approved),
                    has_screening_ranking=bool(r.ranked),
                    shortlisted=r.shortlisted,
                    interviewed=r.interviewed,
                    unrated=r.unrated,
                    undecided_interviewed=r.interviewed - r.decided_interviewed,
                ),
                applicants=r.applicants, interviewed=r.interviewed,
                decided=r.decided, created_at=r.created_at,
            )
            for r in rows
        ),
        total=total,
    )


# --- candidates list ---------------------------------------------------------------------------------


def list_candidates_overview(
    db: Session,
    *,
    acting_user_id: uuid.UUID | str | None,
    search: str | None = None,
    job_id: uuid.UUID | str | None = None,
    stage: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> CandidatesOverview:
    """One page of applications across all jobs, newest first, with the candidate's
    stage and current final decision, plus the total that match. Three queries.

    ``search`` is a case-insensitive "contains" on the candidate's name or e-mail
    (wildcards literal); ``job_id`` restricts to one job (a malformed id matches
    nothing); ``stage`` is one of ``STAGE_LABELS`` values (anything else is
    ignored). HR/INTERNAL ONLY; read-only.
    """
    require_internal_user(db, acting_user_id)
    limit, offset = _page_args(limit, offset)

    conditions = []
    text = normalise_filter_text(search)
    if text:
        pattern = like_pattern(text)
        conditions.append(
            Candidate.full_name.ilike(pattern, escape="\\")
            | Candidate.email.ilike(pattern, escape="\\")
        )
    if job_id is not None:
        job_uuid = _as_uuid(job_id)
        if job_uuid is None:
            return CandidatesOverview(rows=(), total=0)
        conditions.append(Application.job_id == job_uuid)
    stage_word = stage_filter_value(stage)
    if stage_word is not None:
        conditions.append(_stage_expr() == stage_word)

    base = (
        select(Application.id)
        .join(Candidate, Candidate.id == Application.candidate_id)
        .join(Job, Job.id == Application.job_id)
        .where(*conditions)
    )
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    decision = (
        select(FinalDecision.decision)
        .where(
            FinalDecision.application_id == Application.id,
            FinalDecision.status == FinalDecisionStatus.CURRENT,
        )
        .limit(1)
        .scalar_subquery()
    )
    rows = db.execute(
        select(
            Application.id, Application.job_id, Candidate.full_name, Candidate.email,
            Job.job_code, Job.title,
            _stage_expr().label("stage"), decision.label("decision"),
        )
        .join(Candidate, Candidate.id == Application.candidate_id)
        .join(Job, Job.id == Application.job_id)
        .where(*conditions)
        .order_by(Application.created_at.desc(), Application.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return CandidatesOverview(
        rows=tuple(
            CandidateOverviewRow(
                application_id=r.id, job_id=r.job_id, candidate_name=r.full_name,
                candidate_email=r.email, job_code=r.job_code, job_title=r.title,
                stage=r.stage, decision=r.decision,
            )
            for r in rows
        ),
        total=total,
    )


def list_job_options(
    db: Session, *, acting_user_id: uuid.UUID | str | None
) -> list[JobOption]:
    """Every job as ``(id, code, title)``, newest first — the job filter's options.
    One statement; HR/INTERNAL ONLY; read-only."""
    require_internal_user(db, acting_user_id)
    return [
        JobOption(r.id, r.job_code, r.title)
        for r in db.execute(
            select(Job.id, Job.job_code, Job.title).order_by(Job.created_at.desc(), Job.id)
        ).all()
    ]


# --- quick find ------------------------------------------------------------------------------------------


def quick_find(
    db: Session,
    query: str | None,
    *,
    acting_user_id: uuid.UUID | str | None,
    limit: int = QUICK_FIND_LIMIT,
) -> list[FindMatch]:
    """Up to ``limit`` matches: jobs by code or title, then candidates by name or
    e-mail. A query shorter than two characters (after trimming) finds nothing. The
    text is matched literally and case-insensitively (LIKE wildcards escaped,
    parameters bound — never interpolated). HR/INTERNAL ONLY; read-only.
    """
    require_internal_user(db, acting_user_id)
    text = normalise_query(query)
    if text is None:
        return []
    limit = min(max(int(limit), 1), QUICK_FIND_LIMIT)
    pattern = like_pattern(text)

    jobs = db.execute(
        select(Job.id, Job.job_code, Job.title)
        .where(
            Job.job_code.ilike(pattern, escape="\\")
            | Job.title.ilike(pattern, escape="\\")
        )
        .order_by(Job.job_code, Job.id)
        .limit(limit)
    ).all()
    candidates = db.execute(
        select(
            Application.id, Application.job_id, Candidate.full_name, Candidate.email,
            Job.job_code,
        )
        .join(Candidate, Candidate.id == Application.candidate_id)
        .join(Job, Job.id == Application.job_id)
        .where(
            Candidate.full_name.ilike(pattern, escape="\\")
            | Candidate.email.ilike(pattern, escape="\\")
        )
        .order_by(func.lower(Candidate.full_name), Application.id)
        .limit(limit)
    ).all()

    matches = [
        FindMatch("job", j.id, None, j.title, j.job_code) for j in jobs
    ] + [
        FindMatch("candidate", c.job_id, c.id, c.full_name, f"{c.email} · {c.job_code}")
        for c in candidates
    ]
    return matches[:limit]
