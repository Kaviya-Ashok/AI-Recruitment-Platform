"""Read-only summary of where one job stands, for the job workspace's stage bar
(HR UI redesign, Increment 2).

PURELY A READ. No write, no audit event, no AI call, no ``Application.status``
change, no new table. Every number is a COUNT over rows earlier steps already
stored; nothing is scored, ranked or inferred here, and no candidate name, note or
free text is read at all.

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

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.candidate_ranking import CandidateRanking
from app.database.models.candidate_shortlist_entry import CandidateShortlistEntry
from app.database.models.document import Document
from app.database.models.final_decision import FinalDecision, FinalDecisionStatus
from app.database.models.interview_feedback import (
    InterviewFeedback,
    InterviewFeedbackRating,
)
from app.database.models.job import Job
from app.database.models.rubric import RubricVersion, RubricVersionStatus
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
