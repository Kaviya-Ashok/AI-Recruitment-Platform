"""Post-interview analysis service — the AI's consolidated read of a candidate
*after* the human interview (CLAUDE.md §§7, 9, 12, 20, 21, 22, 23, 24;
Phase 4 Step 9).

WHAT THIS STEP DOES
-------------------
Gathers everything already on record for one application — the rubric criteria
the candidate was evaluated against, the résumé evidence, the prequalification
verdicts, the screening evaluation, the full screening transcript, and the ONE
latest human interview-feedback write-up — asks Claude for a consolidated prose
read, validates it, computes ``confidence`` and ``ai_recommendation`` in Python,
and stores the result with exactly one
``POST_INTERVIEW_ANALYSIS_COMPLETED`` audit event.

WHAT THIS STEP DELIBERATELY DOES *NOT* DO
-----------------------------------------
* **No AI/human disagreement detection of any kind.** No flag, no severity, no
  comparison. ``AI_HUMAN_DISAGREEMENT_DETECTED`` stays declared-but-unemitted,
  and ``human_recommendation_snapshot`` is a plain copy carried for display
  context — nothing here compares it to ``ai_recommendation``. §8 is a separate
  step.
* **No interview transcript.** Out of scope entirely. The only transcript read
  here is the *screening* transcript, which is prompt input and is not stored.
* **No final scorecard, no ranking, no final decision, no candidate-facing
  output.** This artefact is internal/HR-only.
* **No ``Application.status`` write** — this module never touches that column,
  matching ``interview_feedback_service`` and ``shortlist_service``.

AI REASONS, PYTHON DECIDES (CLAUDE.md §20)
------------------------------------------
The AI returns prose ONLY — ``summary`` / ``strengths`` / ``gaps`` /
``unknowns`` / ``evidence_consistency_notes``. It has no field for a score, a
confidence level, a recommendation, or a disagreement verdict. Python computes
``confidence`` and ``ai_recommendation`` from the rules documented on
:func:`compute_post_interview_confidence` and
:func:`compute_post_interview_recommendation` below.

Those two pure functions live in this module rather than in a new scoring file:
Step 7's ``interview_guide_service`` likewise keeps its own small deterministic
rules local, and adding a module the step did not call for would widen its
surface for no gain. They are pure — same inputs, same output, no I/O.

AUTH — HR/INTERNAL ONLY, AND NEVER THE SYSTEM ACTOR
---------------------------------------------------
``require_internal_user`` guards every entry point. It accepts *any* active
internal user **including the SYSTEM pipeline actor**, so — exactly as
``interview_feedback_service`` does — this module adds an explicit
:func:`_reject_system_actor`.

The reason is the same in substance and stronger in degree. This analysis reads
and synthesises a named human interviewer's testimony, and it is the last AI
artefact a person sees before deciding on a candidate (§11). Nothing should be
able to manufacture one without an accountable human having asked for it. It is
therefore **never** auto-triggered by Step 8's feedback submission and never
fires from the unauthenticated public app — HR asks for it explicitly, and
``requested_by_user_id`` records who.

NON-DESTRUCTIVE REGENERATION
----------------------------
Unlike every other regenerable AI artefact here, ``force=True`` does **not**
delete the previous row. It marks it ``SUPERSEDED`` (stamping ``superseded_at``)
and inserts a new ``CURRENT`` one, in the same transaction. See the model's
module docstring for why. The "exactly one CURRENT row per application"
invariant is upheld here, not by a database constraint.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
``summary`` / ``strengths`` / ``gaps`` / ``unknowns`` /
``evidence_consistency_notes`` synthesise résumé content, candidate answers and
an interviewer's own words. They are stored on the row and **nowhere else**:
never in audit ``action`` / ``previous_state`` / ``new_state`` /
``event_metadata``, never in a log line, never in an exception message. Audit
metadata carries ids, enum values, and counts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.claude_client import (
    AIError,
    AIOutputError,
    _resolve_model,
    get_structured_response,
)
from app.ai.prompts.post_interview_analysis import (
    build_post_interview_analysis_prompt,
)
from app.ai.schemas.post_interview_analysis import PostInterviewAnalysisAssessment
from app.database.models.application import Application
from app.database.models.audit_event import AuditEventType
from app.database.models.interview_feedback import (
    InterviewFeedback,
    InterviewFeedbackRating,
)
from app.database.models.interview_guide import InterviewGuide
from app.database.models.job_requirement import RequirementType
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysis,
    PostInterviewAnalysisStatus,
)
from app.database.models.screening_evaluation import ScreeningRecommendation
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
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

_TASK_NAME = "post_interview_analysis"

# --- user-safe messages (never interpolate candidate / interviewer text) ----

_SYSTEM_ACTOR = (
    "A post-interview analysis has to be requested by a signed-in HR user, so "
    "there is a record of who asked for it. The automated pipeline account "
    "cannot request one."
)
_NO_APPLICATION = "No such application — it may have been removed."
_NO_FEEDBACK = (
    "No human interview feedback has been recorded for this candidate yet. "
    "Record the interview feedback before running the post-interview analysis."
)
_NO_INPUTS = (
    "This candidate's evaluation record is incomplete, so a post-interview "
    "analysis cannot be produced. It needs the approved rubric, the résumé "
    "evidence, the prequalification result and the screening evaluation."
)
_ANALYSIS_UNAVAILABLE = (
    "The post-interview analysis could not be generated just now. Nothing was "
    "changed — the existing analysis and all interview feedback are untouched. "
    "Please try again."
)
_ANALYSIS_UNUSABLE = (
    "The post-interview analysis came back in a form we could not use, so "
    "nothing was saved. Please try again."
)


class PostInterviewAnalysisError(Exception):
    """The analysis could not be produced or read. Carries a user-safe message;
    never wraps interviewer notes, candidate text, résumé content, or raw model
    output."""


class PostInterviewAnalysisTargetNotFoundError(PostInterviewAnalysisError):
    """No such application."""


class PostInterviewAnalysisPreconditionError(PostInterviewAnalysisError):
    """A required input is missing (no interview feedback, or an incomplete
    evaluation record)."""


class PostInterviewAnalysisActorError(PostInterviewAnalysisError):
    """The actor passed the internal-user guard but may not request an analysis
    (the SYSTEM pipeline actor)."""


# --- row shapes (safe outside a Session) ------------------------------


@dataclass(frozen=True)
class PostInterviewAnalysisView:
    """One analysis, with display names already resolved.

    Frozen primitives only, so a Streamlit page can build this inside a
    ``session_scope()`` and render it after the session closes (the same pattern
    as ``InterviewFeedbackView``).
    """

    analysis_id: uuid.UUID
    application_id: uuid.UUID
    interview_feedback_id: uuid.UUID
    interview_guide_id: uuid.UUID
    rubric_version_id: uuid.UUID
    summary: str
    strengths: list[str]
    gaps: list[str]
    unknowns: list[str]
    evidence_consistency_notes: str
    confidence: str
    ai_recommendation: str
    human_recommendation_snapshot: str
    analyzed_only_latest_feedback: bool
    ai_model: str
    status: str
    superseded_at: datetime | None
    requested_by_user_id: uuid.UUID
    requested_by_name: str
    created_at: datetime


# --- deterministic rules (pure functions — CLAUDE.md §§20, 21) --------


#: Interviewer notes shorter than this (whitespace-stripped) do not count as a
#: substantive interview record. Same order of magnitude, and the same
#: "length as a crude deterministic proxy for substance" caveat, as
#: ``prequalification_confidence.MIN_EVIDENCE_CHARS``.
MIN_NOTES_CHARS = 40


def compute_post_interview_confidence(
    *,
    screening_confidence: str,
    has_substantive_notes: bool,
    rating_count: int,
    unknown_count: int,
) -> str:
    """Return HIGH / MEDIUM / LOW for the consolidated post-interview read.

    CLAUDE.md §21 forbids an unexplained confidence value, so the rule set is
    stated here in full and an HR reviewer can read it and understand any value
    the system assigned. Pure function: no I/O, no AI call.

    The question this answers is *"how complete and consistent is the evidence
    base underneath this consolidated read?"* — not "how good is the candidate".

    THE RULE SET (evaluated in order)
    ---------------------------------
    1. **LOW** — the interviewer recorded neither substantive notes nor a single
       competency rating. The interview added no checkable evidence, so a
       "post-interview" analysis rests on exactly the pre-interview record and
       must not claim more certainty than the screening already had.
    2. **LOW** — the screening evaluation's own overall confidence was LOW *and*
       unknowns remain after the interview. A weak evidence base that the
       interview did not close out stays weak.
    3. **HIGH** — the screening evaluation was HIGH-confidence, the interviewer
       left substantive notes *and* at least one competency rating, *and* the
       analysis lists no remaining unknowns. Every source is present and nothing
       is outstanding.
    4. **MEDIUM** — everything else.

    KNOWN LIMITATION (documented on purpose)
    ----------------------------------------
    ``has_substantive_notes`` is a character-count proxy for substance, and
    ``unknown_count`` comes from the AI's own list. Both are the same kind of
    crude-but-reproducible signal ``compute_confidence`` uses, and the rules are
    likewise biased toward MEDIUM/LOW rather than toward HIGH.
    """
    interview_added_evidence = has_substantive_notes or rating_count > 0

    # Rule 1: the interview contributed nothing checkable.
    if not interview_added_evidence:
        return "LOW"

    # Rule 2: a weak base the interview did not resolve.
    if screening_confidence == "LOW" and unknown_count > 0:
        return "LOW"

    # Rule 3: everything present, nothing outstanding.
    if (
        screening_confidence == "HIGH"
        and has_substantive_notes
        and rating_count > 0
        and unknown_count == 0
    ):
        return "HIGH"

    # Rule 4.
    return "MEDIUM"


def compute_post_interview_recommendation(
    *,
    screening_results: list[dict],
    post_interview_confidence: str,
    unknown_count: int,
) -> str:
    """PROCEED or HOLD — **never** REJECT.

    REJECT is a human decision (CLAUDE.md §§4, 11) and is unreachable on this
    path by construction, exactly as in ``screening_scoring``.

    The human interviewer's own recommendation is deliberately **not an input**.
    Echoing it would make this a restatement of the human's view rather than an
    independent AI read, and comparing against it would be §8's disagreement
    detection, which this step does not implement.

    THE RULE SET (evaluated in order)
    ---------------------------------
    1. **HOLD** — any MANDATORY criterion FAILed in the screening evaluation. A
       mandatory shortfall is not cured by an interview going well; only a human
       may set that aside, and only a human may reject. This carries
       ``screening_scoring.compute_recommendation``'s rule 1 forward rather than
       quietly dropping it, and is checked against the stored per-criterion
       results (the AI's prose has no per-criterion verdicts to check).
    2. **HOLD** — post-interview confidence is LOW.
    3. **HOLD** — unknowns remain after the interview. CLAUDE.md §37: unknown
       stays unknown, and an unresolved unknown is a reason to pause, never a
       reason to fail the candidate.
    4. **PROCEED** — otherwise.

    ``screening_results`` are the stored per-criterion rows, each carrying
    ``requirement_type`` and ``result``.
    """
    mandatory_failed = any(
        row.get("requirement_type") == RequirementType.MANDATORY
        and row.get("result") == "FAIL"
        for row in screening_results or []
    )
    if mandatory_failed:
        return ScreeningRecommendation.HOLD          # rule 1
    if post_interview_confidence == "LOW":
        return ScreeningRecommendation.HOLD          # rule 2
    if unknown_count > 0:
        return ScreeningRecommendation.HOLD          # rule 3
    return ScreeningRecommendation.PROCEED           # rule 4


# --- helpers ----------------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _reject_system_actor(actor: User) -> None:
    """Refuse the SYSTEM pipeline actor.

    ``require_internal_user`` accepts it by design (it is an active internal
    ``User``), so this is the explicit check its docstring instructs callers to
    make. Both the role and the well-known id are checked: the role is the
    semantic rule, the id constant is a belt-and-braces guard in case a row is
    ever mis-seeded with the wrong role.
    """
    if actor.role is UserRole.SYSTEM or actor.id == SYSTEM_USER_ID:
        logger.warning(
            "post_interview_analysis rejected: SYSTEM actor user_id=%s", actor.id
        )
        raise PostInterviewAnalysisActorError(_SYSTEM_ACTOR)


def _require_human_actor(db: Session, user_id: uuid.UUID | str | None) -> User:
    """``require_internal_user`` plus the SYSTEM rejection, in that order."""
    actor = require_internal_user(db, user_id)
    _reject_system_actor(actor)
    return actor


def _resume_evidence(
    db: Session, application_id: uuid.UUID, *, acting_user_id: uuid.UUID | str
) -> dict:
    """First extracted résumé evidence for the application, or ``{}``.

    Mirrors ``interview_guide_service._resume_evidence`` — the two-call
    ``list_documents_for_application`` -> ``get_extraction_for_document`` pattern
    (no public per-application accessor exists).
    """
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


def _latest_feedback_row(
    db: Session, application_id: uuid.UUID
) -> InterviewFeedback | None:
    """The most recent feedback write-up for the application, or ``None``.

    ``created_at DESC, id DESC`` — byte-for-byte the ordering
    ``interview_feedback_service._ordered_feedback_rows`` uses, so "latest" means
    the same thing in both modules. The id breaks the tie for two rows written in
    one transaction, which share Postgres' transaction-start ``now()``.

    Read directly rather than through the Step 8 accessor because the actor has
    already been guarded by :func:`_require_human_actor` at the entry point;
    going through the accessor would re-run ``require_internal_user`` for no
    added protection.
    """
    return db.execute(
        select(InterviewFeedback)
        .where(InterviewFeedback.application_id == application_id)
        .order_by(
            InterviewFeedback.created_at.desc(),
            InterviewFeedback.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _feedback_projection(db: Session, feedback: InterviewFeedback) -> dict:
    """Plain dict of the ONE feedback row for the prompt.

    The interviewer's ``notes`` and each rating ``comment`` are passed through
    **verbatim** — CLAUDE.md §7: the original human feedback must be preserved,
    not paraphrased on its way into the prompt.
    """
    ratings = list(
        db.execute(
            select(InterviewFeedbackRating)
            .where(InterviewFeedbackRating.interview_feedback_id == feedback.id)
            .order_by(InterviewFeedbackRating.competency_label.asc())
        ).scalars().all()
    )
    return {
        "interview_round": feedback.interview_round,
        "recommendation": feedback.recommendation,
        "notes": feedback.notes,
        "ratings": [
            {
                "competency_label": r.competency_label,
                "rating": r.rating,
                "comment": r.comment,
            }
            for r in ratings
        ],
    }


def _current_analysis_row(
    db: Session, application_id: uuid.UUID
) -> PostInterviewAnalysis | None:
    """The single ``CURRENT`` analysis for the application, or ``None``.

    ``created_at DESC, id DESC`` on top of the status filter: the invariant says
    there is at most one, and the ordering makes the read deterministic anyway
    rather than trusting the invariant to be the only thing holding.
    """
    return db.execute(
        select(PostInterviewAnalysis)
        .where(
            PostInterviewAnalysis.application_id == application_id,
            PostInterviewAnalysis.status == PostInterviewAnalysisStatus.CURRENT,
        )
        .order_by(
            PostInterviewAnalysis.created_at.desc(),
            PostInterviewAnalysis.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _to_view(
    analysis: PostInterviewAnalysis, requested_by_name: str
) -> PostInterviewAnalysisView:
    return PostInterviewAnalysisView(
        analysis_id=analysis.id,
        application_id=analysis.application_id,
        interview_feedback_id=analysis.interview_feedback_id,
        interview_guide_id=analysis.interview_guide_id,
        rubric_version_id=analysis.rubric_version_id,
        summary=analysis.summary,
        strengths=list(analysis.strengths or []),
        gaps=list(analysis.gaps or []),
        unknowns=list(analysis.unknowns or []),
        evidence_consistency_notes=analysis.evidence_consistency_notes,
        confidence=analysis.confidence,
        ai_recommendation=analysis.ai_recommendation,
        human_recommendation_snapshot=analysis.human_recommendation_snapshot,
        analyzed_only_latest_feedback=analysis.analyzed_only_latest_feedback,
        ai_model=analysis.ai_model,
        status=analysis.status,
        superseded_at=analysis.superseded_at,
        requested_by_user_id=analysis.requested_by_user_id,
        requested_by_name=requested_by_name,
        created_at=analysis.created_at,
    )


def _display_name(db: Session, user_id: uuid.UUID) -> str:
    """Best available label for a user id; never raises, never leaks an email
    when a name exists."""
    user = db.get(User, user_id)
    if user is None:
        return "Unknown user"
    return (getattr(user, "full_name", None) or user.email or "Unknown user")


# --- write ------------------------------------------------------------


def create_post_interview_analysis(
    db: Session,
    *,
    user_id: uuid.UUID | str,
    application_id: uuid.UUID | str,
    force: bool = False,
) -> PostInterviewAnalysis:
    """Generate and store the AI's consolidated post-interview analysis.

    HR/INTERNAL ONLY, and never the SYSTEM actor. Never auto-triggered: a human
    asks for this, and ``requested_by_user_id`` records who.

    Idempotent by default — with ``force=False`` an existing ``CURRENT`` analysis
    is returned unchanged and **no Claude call is made** (CLAUDE.md §29). With
    ``force=True`` the existing row is marked ``SUPERSEDED`` and a new ``CURRENT``
    row is inserted in the same transaction; nothing is ever deleted.

    Only the ONE latest interview-feedback record is analysed. That is recorded
    on the row as ``analyzed_only_latest_feedback`` so a reader never has to
    assume it.

    The rubric version is resolved through the guide the feedback was recorded
    against (``interview_feedback.interview_guide_id ->
    interview_guides.rubric_version_id``) and frozen on the row — never
    re-derived from the job's currently-approved rubric.

    Every input is resolved and every precondition checked **before** the Claude
    call, and the Claude call happens **before** the first ``db.add`` — so a
    failure at any stage leaves the existing analysis and all interview feedback
    exactly as they were.

    Raises
    ------
    UnauthorizedError
        ``user_id`` is not an active internal user.
    PostInterviewAnalysisActorError
        The actor is the SYSTEM pipeline account.
    PostInterviewAnalysisTargetNotFoundError
        No such application.
    PostInterviewAnalysisPreconditionError
        No interview feedback yet, or an incomplete evaluation record.
    PostInterviewAnalysisError
        The Claude call failed, or its output failed validation.
    """
    # 1. actor — internal user, and a human one.
    actor = _require_human_actor(db, user_id)
    application_uuid = _as_uuid(application_id)

    # 2. application exists.
    application = db.get(Application, application_uuid)
    if application is None:
        raise PostInterviewAnalysisTargetNotFoundError(_NO_APPLICATION)

    # 3. idempotency — no AI call when a CURRENT analysis already exists.
    existing = _current_analysis_row(db, application_uuid)
    if existing is not None and not force:
        return existing

    # 4. the ONE latest human feedback record. Required: this step exists only
    #    because an interview happened.
    feedback = _latest_feedback_row(db, application_uuid)
    if feedback is None:
        raise PostInterviewAnalysisPreconditionError(_NO_FEEDBACK)

    # 5. the guide that feedback was recorded against, and — through it — the
    #    rubric version. interview_feedback carries no rubric_version_id, and
    #    this step does not modify that table.
    guide = db.get(InterviewGuide, feedback.interview_guide_id)
    if guide is None or guide.rubric_version_id is None:
        logger.warning(
            "post_interview_analysis application=%s failure=no_guide_rubric",
            application_uuid,
        )
        raise PostInterviewAnalysisPreconditionError(_NO_INPUTS)

    criteria = list_criteria(db, guide.rubric_version_id)
    if not criteria:
        logger.warning(
            "post_interview_analysis application=%s failure=no_criteria",
            application_uuid,
        )
        raise PostInterviewAnalysisPreconditionError(_NO_INPUTS)

    # 6. the rest of the evaluation record.
    evaluation = get_screening_evaluation_for_application(
        db, application_uuid, acting_user_id=actor.id
    )
    if evaluation is None:
        raise PostInterviewAnalysisPreconditionError(_NO_INPUTS)

    prequal = get_prequalification_for_application(
        db, application_uuid, acting_user_id=actor.id
    )
    if prequal is None:
        raise PostInterviewAnalysisPreconditionError(_NO_INPUTS)

    resume_evidence = _resume_evidence(
        db, application_uuid, acting_user_id=actor.id
    )

    session = get_screening_session_for_application(
        db, application_uuid, acting_user_id=actor.id
    )
    transcript_dicts: list[dict] = []
    if session is not None:
        transcript_dicts = [
            vars(item)
            for item in get_screening_transcript(
                db, screening_session_id=session.id, acting_user_id=actor.id
            )
        ]

    feedback_projection = _feedback_projection(db, feedback)

    # --- AI call (still no write) --------------------------------------
    prompt = build_post_interview_analysis_prompt(
        rubric_criteria=criteria,
        resume_evidence=resume_evidence,
        prequalification_result=prequal.results,
        screening_evaluation=_evaluation_projection(evaluation),
        screening_transcript=transcript_dicts,
        interview_feedback=feedback_projection,
    )
    try:
        assessment: PostInterviewAnalysisAssessment = get_structured_response(
            prompt, PostInterviewAnalysisAssessment, task_name=_TASK_NAME
        )
    except AIOutputError as exc:
        logger.warning(
            "post_interview_analysis application=%s failure=validation",
            application_uuid,
        )
        raise PostInterviewAnalysisError(_ANALYSIS_UNUSABLE) from exc
    except AIError as exc:
        logger.warning(
            "post_interview_analysis application=%s failure=ai", application_uuid
        )
        raise PostInterviewAnalysisError(_ANALYSIS_UNAVAILABLE) from exc

    # --- Python decides (§20) ------------------------------------------
    notes = (feedback.notes or "").strip()
    confidence = compute_post_interview_confidence(
        screening_confidence=evaluation.overall_confidence,
        has_substantive_notes=len(notes) >= MIN_NOTES_CHARS,
        rating_count=len(feedback_projection["ratings"]),
        unknown_count=len(assessment.unknowns),
    )
    ai_recommendation = compute_post_interview_recommendation(
        screening_results=list(evaluation.results or []),
        post_interview_confidence=confidence,
        unknown_count=len(assessment.unknowns),
    )
    # Belt-and-braces: REJECT is unreachable above by construction, but the
    # column would accept it, so the invariant is asserted before it is stored.
    if ai_recommendation not in ScreeningRecommendation.AUTOMATED:  # pragma: no cover
        raise PostInterviewAnalysisError(_ANALYSIS_UNUSABLE)

    # --- persist (one transaction) -------------------------------------
    now = datetime.now(timezone.utc)
    superseded_id: uuid.UUID | None = None
    if existing is not None:
        # Non-destructive: mark, never delete.
        existing.status = PostInterviewAnalysisStatus.SUPERSEDED
        existing.superseded_at = now
        db.add(existing)
        superseded_id = existing.id

    analysis = PostInterviewAnalysis(
        application_id=application_uuid,
        interview_feedback_id=feedback.id,
        interview_guide_id=guide.id,
        rubric_version_id=guide.rubric_version_id,
        requested_by_user_id=actor.id,
        summary=assessment.summary,
        strengths=list(assessment.strengths),
        gaps=list(assessment.gaps),
        unknowns=list(assessment.unknowns),
        evidence_consistency_notes=assessment.evidence_consistency_notes,
        confidence=confidence,
        ai_recommendation=ai_recommendation,
        # A plain copy for display context. NOT compared with ai_recommendation
        # anywhere in this step — §8 is a separate concern.
        human_recommendation_snapshot=feedback.recommendation,
        analyzed_only_latest_feedback=True,
        ai_model=_resolve_model(_TASK_NAME, None),
        status=PostInterviewAnalysisStatus.CURRENT,
    )
    db.add(analysis)
    db.flush()

    # Exactly one audit event — structural metadata only. No summary, no
    # strengths/gaps/unknowns, no notes, no candidate name.
    record_event(
        db,
        event_type=AuditEventType.POST_INTERVIEW_ANALYSIS_COMPLETED,
        action=(
            f"Post-interview AI analysis generated for application "
            f"{application_uuid}."
        ),
        entity_type="application",
        entity_id=application_uuid,
        user_id=actor.id,
        new_state={
            "application_id": str(application_uuid),
            "post_interview_analysis_id": str(analysis.id),
            "interview_feedback_id": str(feedback.id),
            "interview_guide_id": str(guide.id),
            "rubric_version_id": str(guide.rubric_version_id),
            "interview_round": feedback.interview_round,
            "confidence": confidence,
            "ai_recommendation": ai_recommendation,
            "human_recommendation_snapshot": feedback.recommendation,
            "analyzed_only_latest_feedback": True,
            "strength_count": len(assessment.strengths),
            "gap_count": len(assessment.gaps),
            "unknown_count": len(assessment.unknowns),
            "regenerated": existing is not None,
            "superseded_analysis_id": (
                str(superseded_id) if superseded_id is not None else None
            ),
            "ai_model": analysis.ai_model,
        },
    )

    db.commit()
    db.refresh(analysis)
    logger.info(
        "post_interview_analysis application=%s confidence=%s recommendation=%s "
        "unknowns=%d regenerated=%s actor=%s",
        application_uuid, confidence, ai_recommendation,
        len(assessment.unknowns), existing is not None, actor.id,
    )
    return analysis


# --- reads ------------------------------------------------------------


def get_current_post_interview_analysis(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> PostInterviewAnalysisView | None:
    """The single ``CURRENT`` analysis for the application, or ``None``.

    HR/INTERNAL ONLY. Guarded with ``require_internal_user`` (not
    ``_require_human_actor``): reading is not authoring, and the SYSTEM rejection
    exists to stop the pipeline *manufacturing* an analysis, not to stop an
    internal reader seeing one.
    """
    require_internal_user(db, acting_user_id)
    analysis = _current_analysis_row(db, _as_uuid(application_id))
    if analysis is None:
        return None
    return _to_view(analysis, _display_name(db, analysis.requested_by_user_id))


def get_post_interview_analysis_history(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> list[PostInterviewAnalysisView]:
    """Every analysis for the application, newest first
    (``created_at DESC, id DESC``). HR/INTERNAL ONLY.

    Includes the ``CURRENT`` row and every ``SUPERSEDED`` one: regeneration here
    is non-destructive precisely so an earlier analysis a decision may have been
    discussed against stays readable.
    """
    require_internal_user(db, acting_user_id)
    rows = list(
        db.execute(
            select(PostInterviewAnalysis)
            .where(PostInterviewAnalysis.application_id == _as_uuid(application_id))
            .order_by(
                PostInterviewAnalysis.created_at.desc(),
                PostInterviewAnalysis.id.desc(),
            )
        ).scalars().all()
    )
    return [
        _to_view(row, _display_name(db, row.requested_by_user_id)) for row in rows
    ]
