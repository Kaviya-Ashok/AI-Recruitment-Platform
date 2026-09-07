"""Final-scorecard service — the consolidated, explainable view a hiring
manager reads before making the final human decision
(CLAUDE.md §10, cross-referencing §§4, 5, 11, 20, 26; Phase 4 Step 10).

ZERO AI. This module contains no Claude call, no prompt, and no schema
validation of model output. It **assembles** evidence that other steps already
produced and persisted; it never re-evaluates a candidate.

COMPUTED-ON-READ — WHY THERE IS NO ``final_scorecards`` TABLE
-------------------------------------------------------------
Every value on this scorecard already lives in a row somewhere else, written by
the step that owns it. Persisting a copy would buy nothing and cost a great
deal: a second source of truth that can disagree with the first, a regeneration
/ idempotency policy, a supersession vocabulary, and a migration — all to store
a projection that is cheap to rebuild from guarded accessors on every read.

It would also *create* the staleness problem this codebase works hard to avoid.
A stored scorecard drifts the moment any upstream artefact is regenerated
(§9 supersession, a re-run ranking, a new interview round), and reconciling that
drift is exactly the kind of complexity CLAUDE.md §37 tells us not to build into
the MVP. Reading live means the scorecard is, by construction, always the
current picture.

Consequently: **no new table, no migration, no audit event.** The last point
matches existing precedent exactly — an AST scan of every ``get_*`` / ``list_*``
in ``app/services`` finds no pure-read accessor emitting an audit event, and
viewing a scorecard changes no state.

WHAT THIS MODULE DOES *NOT* DO
------------------------------
* **No disagreement detection of any kind** (CLAUDE.md §8). The two
  recommendations are carried side by side and never compared, subtracted,
  ranked, or reduced to a verdict. ``AI_HUMAN_DISAGREEMENT_DETECTED`` stays
  declared-but-unemitted. :data:`DISAGREEMENT_NOT_ASSESSED` is a fixed string.
* **No final human decision** (CLAUDE.md §11 — Step 11, not built).
  :data:`FINAL_DECISION_NOT_DECIDED` is a fixed string. Nothing here infers,
  proposes, or records a decision.
* **No new ranking.** Step 5's ranking is displayed as recorded. Nothing is
  re-ranked using interview or post-interview evidence, and no second
  post-interview ranking is produced.
* **No new scoring methodology.** Every score shown is either read verbatim
  from its originating row, or — for the Interview bucket alone, which §10
  requires and no existing step computes — derived by the single documented
  formula in :func:`compute_interview_score`.
* **No writes at all.** No ``Application.status`` change, no rejection, no
  upstream mutation. This module issues reads only.

CONFIDENCE IS NEVER BLENDED INTO A SCORE
----------------------------------------
Confidence values are carried through with their source labelled and are never
multiplied into, weighted against, or averaged with any score — the same
separation ``screening_scoring`` maintains. Confidence describes evidence
quality, not candidate quality.

RUBRIC VERSIONS ARE SHOWN, NEVER RECONCILED
-------------------------------------------
Artefacts for one application can legitimately carry different
``rubric_version_id`` values (prequalification and screening evaluation each
resolve the job's *then*-approved rubric independently, and rubric re-approval
is not blocked while applications are in flight). This module therefore records
each source's own captured version in :attr:`FinalScorecardView.rubric_versions`
and raises :attr:`has_multiple_rubric_versions` when they differ. It does not
pick a winner, merge them, rewrite history, or recompute any upstream result.
The known Step 4 reconciliation defect is out of scope and untouched.

PRIVACY (CLAUDE.md §§12, 22, 24)
--------------------------------
This assembly carries résumé evidence, candidate answers, interviewer notes and
AI prose. It is HR/internal-only, returned to an authenticated internal caller
and never to the public candidate app. Because nothing here is persisted or
audited, none of that text reaches a log line, an audit row, or an exception
message.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.database.models.application import Application
from app.database.models.candidate import Candidate
from app.database.models.interview_feedback import RATING_MAX
from app.database.models.job import Job
from app.database.models.job_requirement import RequirementType
from app.database.models.rubric import RubricVersion
from app.database.models.user import SYSTEM_USER_ID, User, UserRole
from app.services.interview_feedback_service import list_feedback_views
from app.services.interview_guide_service import (
    get_interview_guide_for_application,
)
from app.services.post_interview_service import (
    get_current_post_interview_analysis,
)
from app.services.prequalification_service import (
    get_prequalification_for_application,
)
from app.services.ranking_service import get_ranking_for_job
from app.services.resume_parsing_service import get_extraction_for_document
from app.services.screening_evaluation_service import (
    get_screening_evaluation_for_application,
)
from app.services.shortlist_service import get_shortlist_entry_for_application
from app.services.storage_service import list_documents_for_application
from app.utils.authorization import require_internal_user

logger = logging.getLogger(__name__)


class FinalScorecardActorError(Exception):
    """The actor passed the internal-user guard but may not view a scorecard
    (the SYSTEM pipeline actor). Carries a user-safe message; never wraps
    candidate, résumé, or interviewer text."""


_SYSTEM_ACTOR = (
    "The final scorecard is a decision aid for a person to read, so it must be "
    "viewed by a signed-in HR user. The automated pipeline account cannot open "
    "it."
)


def _reject_system_actor(actor: User) -> None:
    """Refuse the SYSTEM pipeline actor.

    ``require_internal_user`` accepts it by design (it is an active internal
    ``User``), so this is the explicit check its docstring instructs callers to
    make. Both the role and the well-known id are checked — the role is the
    semantic rule, the id constant guards against a mis-seeded row.

    NOTE — this is deliberately stricter than Step 9's *read* accessors, which
    use the plain guard on the reasoning that "reading is not authoring". The
    scorecard is treated as a human-viewer surface rather than a generic read:
    it is the consolidated artefact a person reads immediately before a hiring
    decision, and there is no pipeline code path that should ever assemble one.
    """
    if actor.role is UserRole.SYSTEM or actor.id == SYSTEM_USER_ID:
        logger.warning("final_scorecard rejected: SYSTEM actor user_id=%s", actor.id)
        raise FinalScorecardActorError(_SYSTEM_ACTOR)


# --- fixed, non-computed strings (CLAUDE.md §§8, 11) ------------------

#: CLAUDE.md §10 line 548 names an "AI-Human Disagreement" field. §8's detection
#: is not implemented in this MVP, so the field is rendered with this fixed
#: string rather than omitted (which would hide that the spec names it) or
#: computed (which would be inventing the very capability that was deferred).
DISAGREEMENT_NOT_ASSESSED = (
    "Not assessed — disagreement detection is not implemented in this MVP."
)

#: CLAUDE.md §10 line 552 names a "Final Human Decision" field. §11 is not
#: built; nothing here infers or proposes one.
FINAL_DECISION_NOT_DECIDED = "Not decided yet"

#: Shown where §10 names a score the evidence cannot support.
NOT_ASSESSED_FOR_ROLE = "Not assessed for this role"
INSUFFICIENT_EVIDENCE = "Insufficient evidence"

#: Displayed above the whole scorecard when sources disagree about the rubric.
MULTIPLE_RUBRIC_VERSIONS_WARNING = (
    "This candidate's evaluation contains evidence from multiple rubric "
    "versions. The scorecard shows each source exactly as it was recorded, "
    "against the rubric version it was actually evaluated under. Nothing here "
    "rewrites or recalculates a historical evaluation."
)


# --- provenance vocabulary (CLAUDE.md §26 / brief §8) -----------------


class Provenance:
    """Where a displayed value came from. Every score, verdict and block of
    prose on the scorecard carries one of these, so HR is never left guessing
    whether they are reading a machine's opinion or a person's.
    """

    RESUME_EVIDENCE = "Résumé-derived evidence"
    PREQUALIFICATION = "Prequalification result (AI, résumé only)"
    SCREENING_AI = "Screening AI assessment"
    SCREENING_TRANSCRIPT = "Screening transcript (candidate's own answers)"
    HUMAN_FEEDBACK = "Human interview feedback"
    POST_INTERVIEW_AI = "Post-interview AI analysis"
    SYSTEM_SCORE = "System-calculated score"
    AI_RECOMMENDATION = "AI recommendation"
    HUMAN_RECOMMENDATION = "Human recommendation"
    RANKING = "Pre-interview candidate ranking (system-calculated)"

    ALL: frozenset[str] = frozenset({
        RESUME_EVIDENCE, PREQUALIFICATION, SCREENING_AI, SCREENING_TRANSCRIPT,
        HUMAN_FEEDBACK, POST_INTERVIEW_AI, SYSTEM_SCORE, AI_RECOMMENDATION,
        HUMAN_RECOMMENDATION, RANKING,
    })


# --- view shapes (frozen; safe outside a Session) ---------------------


@dataclass(frozen=True)
class ScorecardCriterion:
    """One rubric criterion as it was actually judged, with its evidence."""

    criterion_text: str
    requirement_type: str
    result: str                 # PASS / FAIL / UNKNOWN — never rewritten
    evidence_summary: str
    confidence: str
    provenance: str


@dataclass(frozen=True)
class ScorecardBucket:
    """One §10 score section: a score plus the evidence that supports it.

    ``score`` is ``None`` whenever the evidence cannot support one — never 0,
    which would read as "scored badly" rather than "not scored" (CLAUDE.md §B:
    unknown stays unknown). ``unavailable_reason`` then carries the explanation
    the UI shows in its place.
    """

    label: str
    score: int | None
    coverage: float | None
    evidence: tuple[str, ...]
    provenance: str
    unavailable_reason: str | None


@dataclass(frozen=True)
class RubricVersionRef:
    """Which rubric version one particular source was evaluated against."""

    source_label: str
    rubric_version_id: uuid.UUID | None
    version_number: int | None
    status: str | None


@dataclass(frozen=True)
class ScorecardRating:
    """One competency rating exactly as the interviewer recorded it."""

    competency_label: str
    rating: int
    rating_max: int
    comment: str | None


@dataclass(frozen=True)
class ScorecardRanking:
    """Step 5's ranking, read as recorded. Never recalculated here."""

    rank_position: int | None
    overall_score: float | None
    eligible: bool | None
    mandatory_unknown_flag: bool | None
    rubric_version_id: uuid.UUID | None
    generated_at: datetime | None
    is_shortlisted: bool | None
    rank_position_at_decision: int | None
    shortlist_reason: str | None


@dataclass(frozen=True)
class FinalScorecardView:
    """The whole §10 scorecard for one application. Frozen primitives and
    frozen children only, so a Streamlit page can build this inside a
    ``session_scope()`` and render it after the session closes — the same
    pattern as ``InterviewFeedbackView`` / ``PostInterviewAnalysisView``.
    """

    # --- identity ---
    application_id: uuid.UUID
    candidate_name: str
    candidate_email: str
    job_id: uuid.UUID
    job_title: str
    application_status: str

    # --- rubric provenance (never reconciled) ---
    rubric_versions: tuple[RubricVersionRef, ...]
    has_multiple_rubric_versions: bool

    # --- headline numbers, all read as-is ---
    overall_score: float | None
    overall_score_provenance: str
    overall_score_unavailable_reason: str | None
    screening_confidence: str | None
    post_interview_confidence: str | None

    # --- per-criterion (§10 Mandatory / Preferred sections) ---
    mandatory_criteria: tuple[ScorecardCriterion, ...]
    preferred_criteria: tuple[ScorecardCriterion, ...]
    other_criteria: tuple[ScorecardCriterion, ...]

    # --- §10 score sections ---
    requirements: ScorecardBucket
    experience: ScorecardBucket
    behavioral: ScorecardBucket
    interview: ScorecardBucket

    # --- narrative, with provenance kept separate ---
    screening_strengths: tuple[str, ...]
    screening_gaps: tuple[str, ...]
    screening_unknowns: tuple[str, ...]
    post_interview_strengths: tuple[str, ...]
    post_interview_gaps: tuple[str, ...]
    post_interview_unknowns: tuple[str, ...]
    post_interview_summary: str | None
    post_interview_evidence_consistency: str | None

    # --- résumé + screening evidence ---
    resume_evidence: dict
    has_screening_transcript: bool
    screening_question_count: int

    # --- interview ---
    interview_guide_question_count: int | None
    interview_round: int | None
    interview_notes: str | None
    interview_ratings: tuple[ScorecardRating, ...]
    interviewer_name: str | None
    interview_recorded_at: datetime | None

    # --- recommendations: carried, never compared ---
    screening_ai_recommendation: str | None
    post_interview_ai_recommendation: str | None
    human_recommendation: str | None

    # --- §10 fields this MVP does not compute ---
    disagreement_status: str
    final_decision_status: str

    # --- ranking context ---
    ranking: ScorecardRanking

    # --- what simply isn't there yet ---
    missing_sources: tuple[str, ...]


# --- the one score this module derives (CLAUDE.md §§10, 20) -----------


def compute_interview_score(ratings: list[int]) -> int | None:
    """Deterministic Interview score, 0-10, from competency ratings.

    CLAUDE.md §10 names an ``## Interview / Score:`` section, and **no existing
    step computes one** — the only interview numbers in the schema are
    ``interview_feedback_ratings.rating`` (1-5, one per competency). This is the
    single derivation this module performs, and it is pure Python: no AI is ever
    asked for a numeric interview score.

    THE FORMULA — verbatim
    ----------------------
        if there are no ratings          -> None  (never 0 — see below)
        else  mean   = sum(ratings) / count
              score  = round(10 * mean / RATING_MAX)     # RATING_MAX = 5
              clamped to [0, 10]

    WHY ``mean / RATING_MAX`` (proportion of the maximum)
    -----------------------------------------------------
    It is the direct analogue of ``screening_scoring``'s bucket formula, which
    is ``10 * achieved_points / possible_points``. There, a PASS contributes
    1.0 of a possible 1.0; here a rating contributes ``rating`` of a possible
    ``RATING_MAX``. Keeping the same "proportion of what was achievable × 10"
    shape means the Interview score reads on the same scale as the
    Requirements / Experience / Behavioral scores beside it.

    A rejected alternative, recorded so the choice is auditable: mapping the
    1-5 band onto 0-10 as ``10 * (mean - 1) / (RATING_MAX - 1)`` would make a
    straight-1s interview score 0. That treats the bottom of the interviewer's
    scale as "zero merit", which is a stronger claim than the interviewer made —
    they chose the lowest *available* rating, and 1/5 is not 0/5.

    WHY THE MEAN IS UNWEIGHTED
    --------------------------
    ``competency_label`` is free text with no controlled taxonomy and is
    deliberately not linked to ``rubric_criteria`` (see the
    ``InterviewFeedbackRating`` model docstring). There is therefore no
    principled basis on which to weight one competency above another, and
    inventing one would be exactly the fabricated methodology CLAUDE.md §20
    forbids.

    WHY EMPTY IS ``None`` AND NOT ``0``
    -----------------------------------
    Notes-only feedback is legitimate (Step 8 allows it). No ratings means the
    interview was not scored, not that it scored badly — CLAUDE.md's
    unknown-is-not-fail rule. The caller renders
    :data:`INSUFFICIENT_EVIDENCE` instead of a number.

    Confidence is not an input and is never blended in.
    """
    usable = [r for r in ratings if isinstance(r, int) and not isinstance(r, bool)]
    if not usable:
        return None
    mean = sum(usable) / len(usable)
    return max(0, min(10, round(10.0 * mean / RATING_MAX)))


# --- helpers ----------------------------------------------------------


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _criterion_rows(results: list[dict] | None, provenance: str) -> list[
    ScorecardCriterion
]:
    """Project stored per-criterion dicts into view rows, verbatim.

    ``result``, ``evidence_summary`` and ``confidence`` are copied unchanged —
    this module never re-judges a criterion or recomputes its confidence.
    """
    out: list[ScorecardCriterion] = []
    for r in results or []:
        out.append(
            ScorecardCriterion(
                criterion_text=r.get("criterion_text") or "",
                requirement_type=r.get("requirement_type") or "",
                result=r.get("result") or "UNKNOWN",
                evidence_summary=r.get("evidence_summary") or "",
                confidence=r.get("confidence") or "",
                provenance=provenance,
            )
        )
    return out


def _bucket(
    label: str,
    score: int | None,
    coverage: float | None,
    evidence: list[str],
    provenance: str,
    *,
    unavailable_reason: str | None,
) -> ScorecardBucket:
    return ScorecardBucket(
        label=label,
        score=score,
        coverage=coverage,
        evidence=tuple(evidence),
        provenance=provenance,
        unavailable_reason=unavailable_reason if score is None else None,
    )


def _version_ref(
    db: Session, source_label: str, rubric_version_id: uuid.UUID | None
) -> RubricVersionRef | None:
    """Resolve one source's captured rubric version for display. Reads the
    version row only to show its number/status — never to re-derive anything."""
    if rubric_version_id is None:
        return None
    version = db.get(RubricVersion, rubric_version_id)
    return RubricVersionRef(
        source_label=source_label,
        rubric_version_id=rubric_version_id,
        version_number=getattr(version, "version_number", None),
        status=getattr(version, "status", None),
    )


def _resume_evidence(
    db: Session, application_id: uuid.UUID, *, acting_user_id: uuid.UUID | str
) -> dict:
    """First extracted résumé evidence, or ``{}``.

    Same two-call ``list_documents_for_application`` ->
    ``get_extraction_for_document`` pattern the other services use; no public
    per-application accessor exists.
    """
    for document in list_documents_for_application(db, application_id):
        extraction = get_extraction_for_document(
            db, document.id, acting_user_id=acting_user_id
        )
        if extraction is not None:
            return extraction.extracted_data or {}
    return {}


# --- the assembly -----------------------------------------------------


def get_final_scorecard(
    db: Session,
    application_id: uuid.UUID | str,
    *,
    acting_user_id: uuid.UUID | str,
) -> FinalScorecardView | None:
    """Assemble the §10 final scorecard for one application.

    HR/INTERNAL ONLY. Returns ``None`` if the application does not exist.

    Guarded with ``require_internal_user`` **plus** an explicit SYSTEM
    rejection (:func:`_reject_system_actor`). The scorecard is a human-viewer
    surface: it is the consolidated artefact a person reads immediately before a
    hiring decision, and no pipeline code path should ever assemble one. This is
    intentionally stricter than Step 9's read accessors, which use the plain
    guard — see :func:`_reject_system_actor`. The scorecard is reachable only
    from the authenticated HR app; the public candidate app never imports this
    module.

    Every later-stage source is optional. An application with no interview
    feedback and no post-interview analysis still produces a complete scorecard
    — the sections that need those sources report themselves unavailable, by
    name, in ``missing_sources``. Nothing is fabricated and nothing raises.

    Raises
    ------
    UnauthorizedError
        ``acting_user_id`` is not an active internal user.
    FinalScorecardActorError
        The actor is the SYSTEM pipeline account.
    """
    actor = require_internal_user(db, acting_user_id)
    _reject_system_actor(actor)
    app_uuid = _as_uuid(application_id)

    application = db.get(Application, app_uuid)
    if application is None:
        return None

    candidate = db.get(Candidate, application.candidate_id)
    job = db.get(Job, application.job_id)
    missing: list[str] = []

    # --- gather every source through its own guarded accessor ---------
    prequal = get_prequalification_for_application(
        db, app_uuid, acting_user_id=acting_user_id
    )
    if prequal is None:
        missing.append("Prequalification result")

    evaluation = get_screening_evaluation_for_application(
        db, app_uuid, acting_user_id=acting_user_id
    )
    if evaluation is None:
        missing.append("Screening evaluation")

    guide = get_interview_guide_for_application(
        db, app_uuid, acting_user_id=acting_user_id
    )
    if guide is None:
        missing.append("Interview guide")

    feedback_history = list_feedback_views(
        db, app_uuid, acting_user_id=acting_user_id
    )
    # "Latest" is the accessor's own ordering (created_at DESC, id DESC) — NOT
    # the highest round number. Step 8 is explicit that rounds can be written up
    # out of order, and this module does not redefine that.
    latest_feedback = feedback_history[0] if feedback_history else None
    if latest_feedback is None:
        missing.append("Human interview feedback")

    analysis = get_current_post_interview_analysis(
        db, app_uuid, acting_user_id=acting_user_id
    )
    if analysis is None:
        missing.append("Post-interview AI analysis")

    resume_evidence = _resume_evidence(
        db, app_uuid, acting_user_id=acting_user_id
    )
    if not resume_evidence:
        missing.append("Résumé evidence")

    shortlist_entry = get_shortlist_entry_for_application(
        db, job_id=application.job_id, application_id=app_uuid,
        acting_user_id=acting_user_id,
    )

    # --- ranking: read as recorded, never recomputed ------------------
    ranking_row = None
    if shortlist_entry is not None and shortlist_entry.rubric_version_id:
        ranking_rubric_id = shortlist_entry.rubric_version_id
    elif evaluation is not None:
        ranking_rubric_id = evaluation.rubric_version_id
    else:
        ranking_rubric_id = None
    if ranking_rubric_id is not None:
        for row in get_ranking_for_job(
            db, job_id=application.job_id, rubric_version_id=ranking_rubric_id,
            acting_user_id=acting_user_id,
        ):
            if row.application_id == app_uuid:
                ranking_row = row
                break
    if ranking_row is None:
        missing.append("Pre-interview candidate ranking")

    ranking = ScorecardRanking(
        rank_position=getattr(ranking_row, "rank_position", None),
        overall_score=getattr(ranking_row, "overall_score", None),
        eligible=getattr(ranking_row, "eligible", None),
        mandatory_unknown_flag=getattr(ranking_row, "mandatory_unknown_flag", None),
        rubric_version_id=getattr(ranking_row, "rubric_version_id", None),
        generated_at=getattr(ranking_row, "generated_at", None),
        is_shortlisted=getattr(shortlist_entry, "is_shortlisted", None),
        rank_position_at_decision=getattr(
            shortlist_entry, "rank_position_at_decision", None
        ),
        shortlist_reason=getattr(shortlist_entry, "reason", None),
    )

    # --- rubric provenance: record each source's own version ----------
    refs: list[RubricVersionRef] = []
    for label, rv_id in (
        ("Prequalification", getattr(prequal, "rubric_version_id", None)),
        ("Screening evaluation", getattr(evaluation, "rubric_version_id", None)),
        ("Ranking", getattr(ranking_row, "rubric_version_id", None)),
        ("Shortlist decision", getattr(shortlist_entry, "rubric_version_id", None)),
        ("Interview guide", getattr(guide, "rubric_version_id", None)),
        ("Post-interview analysis", getattr(analysis, "rubric_version_id", None)),
    ):
        ref = _version_ref(db, label, rv_id)
        if ref is not None:
            refs.append(ref)
    distinct_versions = {r.rubric_version_id for r in refs}

    # --- per-criterion sections, copied verbatim ----------------------
    # Prefer the screening evaluation (it reconciles résumé + answers); fall
    # back to prequalification when screening has not happened. Never merged.
    if evaluation is not None:
        criterion_rows = _criterion_rows(evaluation.results, Provenance.SCREENING_AI)
    elif prequal is not None:
        criterion_rows = _criterion_rows(prequal.results, Provenance.PREQUALIFICATION)
    else:
        criterion_rows = []

    mandatory = tuple(
        c for c in criterion_rows if c.requirement_type == RequirementType.MANDATORY
    )
    preferred = tuple(
        c for c in criterion_rows if c.requirement_type == RequirementType.PREFERRED
    )
    other = tuple(
        c for c in criterion_rows
        if c.requirement_type not in (
            RequirementType.MANDATORY, RequirementType.PREFERRED
        )
    )

    # --- §10 score sections: read as-is from the evaluation -----------
    ev_strengths = list(getattr(evaluation, "strengths", None) or [])
    ev_gaps = list(getattr(evaluation, "gaps", None) or [])
    ev_unknowns = list(getattr(evaluation, "unknowns", None) or [])

    def _bucket_evidence(types: set[str]) -> list[str]:
        """Evidence lines for a bucket — the criteria that actually make it up."""
        return [
            f"{c.criterion_text}: {c.evidence_summary}"
            for c in criterion_rows if c.requirement_type in types
        ]

    no_eval_reason = (
        NOT_ASSESSED_FOR_ROLE if evaluation is not None else INSUFFICIENT_EVIDENCE
    )
    requirements = _bucket(
        "Requirements",
        getattr(evaluation, "requirements_score", None),
        getattr(evaluation, "requirements_coverage", None),
        _bucket_evidence({
            RequirementType.MANDATORY, RequirementType.PREFERRED,
            RequirementType.OTHER,
        }),
        Provenance.SYSTEM_SCORE,
        unavailable_reason=no_eval_reason,
    )
    experience = _bucket(
        "Experience",
        getattr(evaluation, "experience_score", None),
        getattr(evaluation, "experience_coverage", None),
        _bucket_evidence({RequirementType.EXPERIENCE}),
        Provenance.SYSTEM_SCORE,
        unavailable_reason=no_eval_reason,
    )
    behavioral = _bucket(
        "Behavioral",
        getattr(evaluation, "behavioral_score", None),
        getattr(evaluation, "behavioral_coverage", None),
        _bucket_evidence({RequirementType.BEHAVIORAL}),
        Provenance.SYSTEM_SCORE,
        unavailable_reason=no_eval_reason,
    )

    # --- Interview: the one derived score, from human ratings ---------
    ratings = tuple(
        ScorecardRating(
            competency_label=r.competency_label,
            rating=r.rating,
            rating_max=RATING_MAX,
            comment=r.comment,
        )
        for r in (getattr(latest_feedback, "ratings", None) or [])
    )
    interview_score = compute_interview_score([r.rating for r in ratings])
    interview_evidence = [
        f"{r.competency_label}: {r.rating}/{r.rating_max}"
        + (f" — {r.comment}" if r.comment else "")
        for r in ratings
    ]
    interview = _bucket(
        "Interview",
        interview_score,
        None,   # no coverage concept: competencies are not a closed rubric set
        interview_evidence,
        Provenance.SYSTEM_SCORE,
        unavailable_reason=(
            INSUFFICIENT_EVIDENCE if latest_feedback is not None
            else "No interview feedback recorded yet"
        ),
    )

    overall_unavailable = (
        None if ranking.overall_score is not None
        else "No ranking has been generated for this candidate yet"
    )

    return FinalScorecardView(
        application_id=app_uuid,
        candidate_name=getattr(candidate, "full_name", "") or "",
        candidate_email=getattr(candidate, "email", "") or "",
        job_id=application.job_id,
        job_title=getattr(job, "title", "") or "",
        application_status=application.status,
        rubric_versions=tuple(refs),
        has_multiple_rubric_versions=len(distinct_versions) > 1,
        # Read verbatim from Step 5's ranking row — not recomputed here.
        overall_score=ranking.overall_score,
        overall_score_provenance=Provenance.RANKING,
        overall_score_unavailable_reason=overall_unavailable,
        screening_confidence=getattr(evaluation, "overall_confidence", None),
        post_interview_confidence=getattr(analysis, "confidence", None),
        mandatory_criteria=mandatory,
        preferred_criteria=preferred,
        other_criteria=other,
        requirements=requirements,
        experience=experience,
        behavioral=behavioral,
        interview=interview,
        screening_strengths=tuple(ev_strengths),
        screening_gaps=tuple(ev_gaps),
        screening_unknowns=tuple(ev_unknowns),
        post_interview_strengths=tuple(getattr(analysis, "strengths", None) or []),
        post_interview_gaps=tuple(getattr(analysis, "gaps", None) or []),
        post_interview_unknowns=tuple(getattr(analysis, "unknowns", None) or []),
        post_interview_summary=getattr(analysis, "summary", None),
        post_interview_evidence_consistency=getattr(
            analysis, "evidence_consistency_notes", None
        ),
        resume_evidence=resume_evidence,
        has_screening_transcript=evaluation is not None,
        screening_question_count=len(criterion_rows),
        interview_guide_question_count=(
            len(guide.questions) if guide is not None else None
        ),
        interview_round=getattr(latest_feedback, "interview_round", None),
        interview_notes=getattr(latest_feedback, "notes", None),
        interview_ratings=ratings,
        interviewer_name=getattr(latest_feedback, "submitted_by_name", None),
        interview_recorded_at=getattr(latest_feedback, "created_at", None),
        # Two AI recommendations, carried separately with their own provenance.
        # Never merged into one invented recommendation, never compared.
        screening_ai_recommendation=getattr(evaluation, "ai_recommendation", None),
        post_interview_ai_recommendation=getattr(
            analysis, "ai_recommendation", None
        ),
        # The interviewer's own value, unmodified and un-normalised.
        human_recommendation=getattr(latest_feedback, "recommendation", None),
        disagreement_status=DISAGREEMENT_NOT_ASSESSED,
        final_decision_status=FINAL_DECISION_NOT_DECIDED,
        ranking=ranking,
        missing_sources=tuple(missing),
    )
