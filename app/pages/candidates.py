"""Candidates page — minimal HR view of applications per job, with manual
triggers for the ``resume_parsing`` and ``prequalification`` AI tasks.

Scope (deliberately minimal)
----------------------------
This is NOT the full candidate-detail page named in CLAUDE.md §14 (screening,
scorecard, ranking, interview guide all come later), and the prequalification
view here is a per-criterion PASS/FAIL/UNKNOWN list — NOT the initial/final
scorecard (CLAUDE.md §§4, 10), which is separate future scope. It provides
exactly what these steps need:

* pick a job -> see its applications (newest first),
* for an application whose resume has NOT been parsed: a **Parse resume** button,
* once parsed: the extracted evidence inventory, read-only,
* for an application with an extraction but no prequalification: a **Prequalify**
  button,
* once prequalified: the per-criterion results, read-only, MANDATORY and
  PREFERRED shown separately (CLAUDE.md §B — never blended).

Rendered via ``st.navigation`` from ``app/main.py`` (never auto-discovered).

AUTH: this page is only mounted in the HR app's authenticated page tree. It
calls ``resume_parsing_service`` / ``prequalification_service`` which read
candidate resume evidence — it must never be reachable from
``app/public_main.py``.

AI tasks are NEVER triggered on page load — only on an explicit button click
(CLAUDE.md §§9, 29).
"""

from __future__ import annotations

import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.database.models.application import Application
from app.database.models.candidate import Candidate
from app.database.models.screening_evaluation import ScreeningEvaluationBucket
from app.services.application_service import (
    count_applications_for_job,
    list_applications_for_job,
)
from app.services.prequalification_service import (
    PrequalificationError,
    get_prequalification_for_application,
    prequalify_application,
)
from app.services.resume_parsing_service import (
    ResumeParsingError,
    get_extraction_for_document,
    parse_resume,
)
from app.services.screening_pipeline_service import (
    PipelineOutcome,
    ScreeningPipelineError,
    get_screening_session_for_application,
    list_stalled_screening_applications,
    resume_stalled_pipeline,
)
from app.services.screening_evaluation_service import (
    ScreeningEvaluationError,
    evaluate_screening,
    get_screening_evaluation_for_application,
)
from app.services.ranking_service import (
    RankingError,
    generate_ranking,
    get_ranking_display_rows,
    list_rubric_version_partitions_for_job,
)
from app.services.shortlist_service import (
    ShortlistError,
    get_ranking_staleness_for_partition,
    get_shortlist_status_for_job,
    shortlist_candidate,
    unshortlist_candidate,
)
from app.services.screening_question_service import (
    ScreeningQuestionError,
    abandon_screening,
)
from app.services.screening_scoring import bucket_of
from app.services.storage_service import list_documents_for_application
from app.utils.authorization import UnauthorizedError
from app.utils.session import get_current_user
from app.utils.ui import (
    ai_provenance,
    badge,
    confidence_kind,
    entity_badge,
    entity_status_badge,
    label_for,
    recommendation_kind,
    result_kind,
    score_out_of_ten,
    truncate,
)
from app.utils.ui_widgets import (
    HR_JOB_PICKER_KEY,
    detail_lines,
    job_picker,
    load_error,
    load_job_options,
    page_header,
    success_toast,
)

_PARSE_DB_ERROR = "Couldn't save the parsed resume — please try again."
_PARSE_UNEXPECTED = "Something went wrong parsing the resume. Please try again."
_PREQUAL_DB_ERROR = "Couldn't save the prequalification — please try again."
_PREQUAL_UNEXPECTED = "Something went wrong prequalifying. Please try again."
_RECOVER_DB_ERROR = "Couldn't resume the pipeline — please try again."
_RECOVER_UNEXPECTED = "Something went wrong resuming the pipeline. Please try again."
_ABANDON_DB_ERROR = "Couldn't update the screening — please try again."
_ABANDON_UNEXPECTED = "Something went wrong updating the screening. Please try again."
_EVAL_DB_ERROR = "Couldn't save the scorecard — please try again."
_EVAL_UNEXPECTED = "Something went wrong generating the scorecard. Please try again."
_RANK_DB_ERROR = "Couldn't save the ranking — please try again."
_RANK_UNEXPECTED = "Something went wrong generating the ranking. Please try again."
_SHORTLIST_DB_ERROR = "Couldn't save the shortlist change — please try again."
_SHORTLIST_UNEXPECTED = "Something went wrong updating the shortlist. Please try again."

_TYPE_ORDER = ["MANDATORY", "PREFERRED", "EXPERIENCE", "BEHAVIORAL", "OTHER"]

# The 3 score buckets, in the same left-to-right order as the score header.
# Membership (requirement_type -> bucket) is NOT decided here — it comes from
# ``screening_scoring.bucket_of`` so the per-criterion grouping can never drift
# from the scoring engine's own mapping.
_BUCKET_ORDER = [
    ScreeningEvaluationBucket.REQUIREMENTS,
    ScreeningEvaluationBucket.EXPERIENCE,
    ScreeningEvaluationBucket.BEHAVIORAL,
]
# Sub-order inside the Requirements bucket. MANDATORY is shown before PREFERRED
# before OTHER so a strong preferred can never visually mask a failed mandatory
# (CLAUDE.md §5). Derived from _TYPE_ORDER via bucket_of — no second mapping.
_REQUIREMENTS_SUBTYPES = [
    t for t in _TYPE_ORDER
    if bucket_of(t) == ScreeningEvaluationBucket.REQUIREMENTS
]

_REQUIREMENTS_BUCKET_HELP = (
    "Mandatory, preferred, and other role requirements collectively — not a "
    "technical-skills-only score."
)


def _load_application_summaries(
    job_id: str, acting_user_id, *, limit: int
) -> tuple[list[dict], int]:
    """Just enough for a COLLAPSED card's label, plus the job's total.

    Deliberately cheap: one paged application query, one candidate lookup per
    *visible* row, and ONE stalled-pipeline query for the whole job (so a
    stalled card can still be flagged while collapsed, at no per-row cost).

    Everything a card *body* needs is left to :func:`_load_application_detail`,
    which is what makes a collapsed card actually free (H13). Before this split
    all six per-application reads ran eagerly for every application in the job,
    so an expander alone would have hidden the rows without saving any work.
    """
    with session_scope() as db:
        total = count_applications_for_job(db, job_id)
        stalled = {
            str(s.application_id): s
            for s in list_stalled_screening_applications(
                db, acting_user_id=acting_user_id
            )
        }
        out: list[dict] = []
        for app in list_applications_for_job(db, job_id, limit=limit):
            candidate = db.get(Candidate, app.candidate_id)
            out.append(_summary_row(app, candidate, stalled.get(str(app.id))))
        return out, total


def _summary_row(app, candidate, stall) -> dict:
    """One application's card-label facts. Extracted unchanged from
    :func:`_load_application_summaries` so the candidate page's Screening tab builds
    the identical row for a single application."""
    return {
        "application_id": str(app.id),
        "status": app.status,
        "candidate_name": candidate.full_name if candidate else "—",
        "candidate_email": candidate.email if candidate else "—",
        # None | "PRE_ROUND_1" | "MID_SCREENING"
        "screening_stall_kind": (
            None
            if stall is None
            else ("PRE_ROUND_1" if stall.is_pre_round_1 else "MID_SCREENING")
        ),
    }


def _load_application_summary(application_id: str, acting_user_id) -> dict | None:
    """The :func:`_summary_row` for ONE application (``None`` if it does not
    exist) — what a collapsed card shows, which :func:`_render_application_body`
    also needs (status, stall kind). Read-only."""
    app_uuid = uuid.UUID(application_id)
    with session_scope() as db:
        app = db.get(Application, app_uuid)
        if app is None:
            return None
        candidate = db.get(Candidate, app.candidate_id)
        stalled = {
            str(sa.application_id): sa
            for sa in list_stalled_screening_applications(
                db, acting_user_id=acting_user_id
            )
        }
        return _summary_row(app, candidate, stalled.get(str(app.id)))


def _load_application_detail(application_id: str, acting_user_id) -> dict:
    """The expensive half — five per-application reads, run ONLY for a card the
    user has actually opened. Same keys the old combined view produced, so every
    ``_render_*`` below is unchanged."""
    app_uuid = uuid.UUID(application_id)
    with session_scope() as db:
        docs = list_documents_for_application(db, app_uuid)
        doc = docs[0] if docs else None
        extraction = (
            get_extraction_for_document(db, doc.id, acting_user_id=acting_user_id)
            if doc is not None
            else None
        )
        prequal = get_prequalification_for_application(
            db, app_uuid, acting_user_id=acting_user_id
        )
        screening = get_screening_session_for_application(
            db, app_uuid, acting_user_id=acting_user_id
        )
        evaluation = get_screening_evaluation_for_application(
            db, app_uuid, acting_user_id=acting_user_id
        )
        return {
            "screening_session_id": (
                str(screening.id) if screening is not None else None
            ),
            "screening_status": (
                screening.status if screening is not None else None
            ),
            "document_id": str(doc.id) if doc is not None else None,
            "document_name": doc.original_filename if doc is not None else None,
            "extracted_data": (
                extraction.extracted_data if extraction is not None else None
            ),
            "extraction_created_at": (
                extraction.created_at if extraction is not None else None
            ),
            "prequal_results": prequal.results if prequal is not None else None,
            "prequal_created_at": (
                prequal.created_at if prequal is not None else None
            ),
            # Screening evaluation / scorecard. Strengths / Gaps / Unknowns are
            # NOT projected: the scorecard derives them fresh from ``results``
            # so each card can show criterion_text and evidence_summary
            # separately. The ScreeningEvaluation columns are untouched and
            # still used by ``interview_guide_service``.
            "evaluation": (
                {
                    "results": evaluation.results,
                    "requirements_score": evaluation.requirements_score,
                    "requirements_coverage": evaluation.requirements_coverage,
                    "experience_score": evaluation.experience_score,
                    "experience_coverage": evaluation.experience_coverage,
                    "behavioral_score": evaluation.behavioral_score,
                    "behavioral_coverage": evaluation.behavioral_coverage,
                    "overall_confidence": evaluation.overall_confidence,
                    "ai_recommendation": evaluation.ai_recommendation,
                    "generated_at": evaluation.created_at,
                }
                if evaluation is not None
                else None
            ),
        }


def _run_parse(document_id: str, acting_user_id) -> None:
    try:
        with st.spinner("Parsing resume with AI — this can take a few seconds…"):
            with session_scope() as db:
                parse_resume(
                    db,
                    document_id=uuid.UUID(document_id),
                    requested_by_user_id=acting_user_id,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except ResumeParsingError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_PARSE_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_PARSE_UNEXPECTED)
        return

    success_toast("Resume parsed — evidence inventory extracted.")
    st.rerun()


def _run_prequalify(application_id: str, acting_user_id) -> None:
    try:
        with st.spinner("Prequalifying against the approved rubric — a few seconds…"):
            with session_scope() as db:
                prequalify_application(
                    db,
                    application_id=uuid.UUID(application_id),
                    requested_by_user_id=acting_user_id,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except PrequalificationError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_PREQUAL_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_PREQUAL_UNEXPECTED)
        return

    success_toast("Prequalification complete.")
    st.rerun()


def _run_resume_pipeline(application_id: str, acting_user_id) -> None:
    """Manually resume a stalled automatic screening pipeline (RECOVERY ONLY).

    Delegates to ``resume_stalled_pipeline`` — which authorises this HR user's
    click but runs every stage as the SYSTEM actor, with the same idempotency
    guarantees as the candidate's own browser (no duplicate row / AI call /
    audit event).
    """
    try:
        with st.spinner("Resuming the automatic pipeline — this can take a few seconds…"):
            with session_scope() as db:
                result = resume_stalled_pipeline(
                    db,
                    application_id=uuid.UUID(application_id),
                    requested_by_user_id=acting_user_id,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except ScreeningPipelineError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_RECOVER_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_RECOVER_UNEXPECTED)
        return

    if result.outcome == PipelineOutcome.COMPLETED:
        success_toast("Pipeline resumed and completed — screening is ready.")
    elif result.outcome == PipelineOutcome.FAILED:
        # ``result.message`` is candidate-safe; fine for HR too.
        st.warning(f"Pipeline resumed but stalled again: {result.message}")
    else:
        st.info("Pipeline advanced — run again if it has not completed.")
    st.rerun()


def _run_abandon_screening(screening_session_id: str, acting_user_id) -> None:
    """Explicit HR decision: mark a stalled mid-screening as abandoned.

    Sets ApplicationStatus.SCREENING_INCOMPLETE and emits AI_SCREENING_INCOMPLETE
    — attributed to this HR user (a human decision, not the automated pipeline).
    NEVER a rejection (no such status exists in this MVP).
    """
    try:
        with session_scope() as db:
            changed = abandon_screening(
                db,
                screening_session_id=uuid.UUID(screening_session_id),
                requested_by_user_id=acting_user_id,
            )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except ScreeningQuestionError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_ABANDON_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_ABANDON_UNEXPECTED)
        return

    success_toast(
        "Marked as incomplete." if changed else "Already recorded as incomplete."
    )
    st.rerun()


def _run_evaluate(application_id: str, acting_user_id) -> None:
    """Manually run (recover) the scorecard generation.

    Normally fires automatically when screening completes; this button is the
    recovery path if that auto-trigger failed. Runs once; result is saved.
    """
    try:
        with st.spinner("Building the scorecard — a few seconds…"):
            with session_scope() as db:
                evaluate_screening(
                    db,
                    application_id=uuid.UUID(application_id),
                    requested_by_user_id=acting_user_id,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except ScreeningEvaluationError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_EVAL_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_EVAL_UNEXPECTED)
        return

    success_toast("Scorecard generated.")
    st.rerun()


def _fmt_bucket(score, coverage) -> str:
    """A bucket score line. NULL score -> 'Not assessed', never 0 or blank.

    The "/ 10" itself comes from ``score_out_of_ten`` so this and the ranking
    row's overall score always show the same scale (H7).
    """
    if score is None:
        return badge("neutral", "Not assessed for this role")
    cov = f" · {round((coverage or 0.0) * 100)}% of this bucket assessed"
    return f"{score_out_of_ten(score)}{cov}"


def _render_scorecard(view: dict) -> None:
    """The per-candidate initial scorecard (CLAUDE.md §§4, 26). Thin glue — all
    numbers come from ``screening_evaluation_service``; nothing is computed here.
    """
    ev = view["evaluation"]
    rows: list[dict] = ev["results"] or []

    rec = ev["ai_recommendation"]
    conf = ev["overall_confidence"]
    # Not a heading — a badge pair. Rendered as a plain markdown line (H12).
    st.markdown(
        f"{badge(recommendation_kind(rec), f'AI recommendation: {rec}')} "
        f"{badge(confidence_kind(conf), f'overall confidence: {conf}')}"
    )
    st.caption(
        f"{ai_provenance(ev['generated_at'])} · the résumé-based "
        "prequalification checked again against what the candidate said in "
        "screening, one requirement at a time. Met requirements count fully, "
        "unmet ones count as zero, and requirements with no evidence either "
        "way are left out rather than counted against the candidate. This is a "
        "recommendation for HR — not a hiring decision."
    )

    cols = st.columns(3)
    cols[0].markdown("**Requirements**", help=_REQUIREMENTS_BUCKET_HELP)
    cols[0].markdown(
        _fmt_bucket(ev["requirements_score"], ev["requirements_coverage"])
    )
    cols[1].markdown("**Experience**")
    cols[1].markdown(
        _fmt_bucket(ev["experience_score"], ev["experience_coverage"])
    )
    cols[2].markdown("**Behavioral**")
    cols[2].markdown(
        _fmt_bucket(ev["behavioral_score"], ev["behavioral_coverage"])
    )

    # Scannable summary first: Strengths / Gaps / Unknowns as three columns.
    st.markdown("")
    _render_sgu_columns(view)

    # The dense per-criterion prose (Evidence + Reasoning for every criterion)
    # is one explicit click away — not dumped on open (CLAUDE.md §26).
    st.markdown("")
    if st.toggle(
        "Show full evidence per requirement",
        key=f"scorecard_evidence_{view['application_id']}",
        help=(
            "Every requirement's result, with the evidence and reasoning "
            "behind it, grouped by the three score buckets above."
        ),
    ):
        _render_criteria_by_bucket(rows)


_SGU_SPEC: list[tuple[str, str, str]] = [
    # (results 'result' value, column label, empty-state message)
    ("PASS", "Strengths", "No strengths surfaced yet."),
    ("FAIL", "Gaps", "No significant gaps identified."),
    ("UNKNOWN", "Unknowns", "Nothing left unresolved."),
]

# Cards shown per column before a "Show all N" toggle. 4 keeps the section
# scannable near the top of the scorecard (3 columns x 4 = 12 cards worst case)
# while still showing enough of the fullest column (usually Strengths) to convey
# its shape; the toggle reveals the rest in place.
_SGU_DEFAULT_CAP = 4

# evidence_summary longer than this is shown truncated on the card. The full,
# untruncated text is always one click away in the "Show full evidence per
# requirement" section directly below. ~280 chars ≈ 3-4 lines in a 1/3-width
# column — enough for a scannable blurb, short of a paragraph.
_SGU_EVIDENCE_CHAR_CAP = 280


def _render_sgu_card(col, item: dict) -> None:
    """One compact card: criterion_text as a small bold label, evidence_summary
    as the body below it — the two strings from ``results`` shown separately,
    never colon-joined. Both are verbatim from the evaluation (evidence only
    shortened for length, never re-worded)."""
    crit = str(item.get("criterion_text") or "").strip()
    evidence = truncate(item.get("evidence_summary"), _SGU_EVIDENCE_CHAR_CAP)
    with col.container(border=True):
        if crit:
            st.caption(f"**{crit}**")
        st.markdown(evidence or "—")


def _render_sgu_columns(view: dict) -> None:
    """Strengths / Gaps / Unknowns as three columns of compact cards.

    Source of truth is ``view["evaluation"]["results"]`` — the same per-criterion
    list the rest of the scorecard uses — partitioned here by ``result``
    (PASS -> Strengths, FAIL -> Gaps, else -> Unknowns), the identical split
    ``screening_scoring.assemble_strengths_gaps_unknowns`` performs. No new
    query, no service change.
    """
    ev = view["evaluation"]
    app_id = view["application_id"]
    rows: list[dict] = ev["results"] or []

    by_result: dict[str, list[dict]] = {"PASS": [], "FAIL": [], "UNKNOWN": []}
    for r in rows:
        by_result.get(r.get("result", "UNKNOWN"), by_result["UNKNOWN"]).append(r)

    cols = st.columns(3)
    for col, (result, label, empty_msg) in zip(cols, _SGU_SPEC):
        items = by_result[result]
        col.markdown(badge(result_kind(result), f"{label} ({len(items)})"))
        if not items:
            col.caption(empty_msg)
            continue

        show_all_key = f"sgu_all_{result}_{app_id}"
        show_all = bool(st.session_state.get(show_all_key, False))
        for it in (items if show_all else items[:_SGU_DEFAULT_CAP]):
            _render_sgu_card(col, it)
        if len(items) > _SGU_DEFAULT_CAP:
            col.toggle(f"Show all {len(items)}", key=show_all_key)


def _result_count_badges(items: list[dict]) -> str:
    """A ' '-joined row of pass/fail/unknown count badges for a group header."""
    counts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    for it in items:
        r = it.get("result", "UNKNOWN")
        counts[r] = counts.get(r, 0) + 1
    return " ".join(
        badge(result_kind(r), f"{counts[r]} {r.lower()}")
        for r in ("PASS", "FAIL", "UNKNOWN")
    )


def _render_criteria_by_bucket(rows: list[dict]) -> None:
    """Per-criterion detail grouped by the SAME 3 score buckets as the header
    (Requirements / Experience / Behavioral), via ``screening_scoring.bucket_of``
    — never a mapping re-invented here. Inside Requirements, criteria are
    sub-grouped MANDATORY -> PREFERRED -> OTHER (CLAUDE.md §5).

    Shared by the scorecard's "full evidence" toggle and by
    ``_render_prequalification`` so the two views stay structurally identical.
    """
    if not rows:
        st.caption("No criteria to show.")
        return

    by_bucket: dict[str, list[dict]] = {b: [] for b in _BUCKET_ORDER}
    for row in rows:
        by_bucket[bucket_of(row.get("requirement_type", "OTHER"))].append(row)

    for bucket in _BUCKET_ORDER:
        items = by_bucket[bucket]
        if not items:
            continue
        st.markdown(f"#### {label_for(bucket)} ({len(items)})")
        st.markdown(_result_count_badges(items))

        if bucket == ScreeningEvaluationBucket.REQUIREMENTS:
            sub_grouped: dict[str, list[dict]] = {}
            for it in items:
                sub_grouped.setdefault(
                    it.get("requirement_type", "OTHER"), []
                ).append(it)
            ordered = [s for s in _REQUIREMENTS_SUBTYPES if s in sub_grouped] + [
                s for s in sub_grouped if s not in _REQUIREMENTS_SUBTYPES
            ]
            for subtype in ordered:
                sub = sub_grouped[subtype]
                st.markdown(f"**{label_for(subtype)}** · {len(sub)}")
                for it in sub:
                    _render_prequal_criterion(it)
                    st.markdown("")
        else:
            for it in items:
                _render_prequal_criterion(it)
                st.markdown("")


def _render_str_list(label: str, values: list) -> None:
    st.markdown(f"**{label}** ({len(values or [])})")
    if not values:
        st.caption("None stated.")
        return
    for v in values:
        st.markdown(f"- {v}")


def _render_entries(label: str, entries: list, fields: list[tuple[str, str]]) -> None:
    st.markdown(f"**{label}** ({len(entries or [])})")
    if not entries:
        st.caption("None stated.")
        return
    for entry in entries:
        parts = []
        for key, human in fields:
            val = entry.get(key)
            if isinstance(val, list):
                val = ", ".join(val) if val else None
            if val:
                parts.append(f"*{human}:* {val}")
        if not parts:
            st.markdown("- —")
            continue
        # Native nested markdown bullets — no &nbsp; padding (H22). The first
        # field is the entry's bullet; the rest indent under it, exactly as the
        # manual spacing used to look.
        head, *rest = parts
        nested = "".join(f"\n  - {p}" for p in rest)
        st.markdown(f"- {head}{nested}")


def _render_extraction(view: dict) -> None:
    data = view["extracted_data"] or {}
    st.caption(
        f"{ai_provenance(view['extraction_created_at'])} · evidence inventory "
        "only (not an assessment)."
    )
    _render_str_list("Skills", data.get("skills"))
    _render_str_list("Technologies", data.get("technologies"))
    _render_entries(
        "Experience",
        data.get("experience"),
        [
            ("role", "Role"),
            ("organization", "Organization"),
            ("dates", "Dates"),
            ("description", "Details"),
        ],
    )
    _render_entries(
        "Projects",
        data.get("projects"),
        [("name", "Name"), ("description", "Details"), ("technologies", "Tech")],
    )
    _render_entries(
        "Certifications",
        data.get("certifications"),
        [("name", "Name"), ("issuer", "Issuer"), ("date", "Date")],
    )
    _render_entries(
        "Education",
        data.get("education"),
        [
            ("qualification", "Qualification"),
            ("institution", "Institution"),
            ("dates", "Dates"),
        ],
    )
    _render_str_list("Other relevant claims", data.get("other_relevant_claims"))


def _render_prequal_criterion(row: dict) -> None:
    result = row.get("result", "UNKNOWN")
    confidence = row.get("confidence", "LOW")
    cat = f" · {row['category']}" if row.get("category") else ""
    # Colour is paired with the text label in every badge (CLAUDE.md §26 — never
    # colour alone).
    st.markdown(
        f"{badge(result_kind(result), result)} "
        f"{badge(confidence_kind(confidence), f'confidence: {confidence}')}  \n"
        f"**{row.get('criterion_text', '')}**{cat}"
    )
    # Native markdown list indents these — no &nbsp; padding (H22).
    detail_lines(
        (
            ("Evidence", row.get("evidence_summary")),
            ("Reasoning", row.get("reasoning")),
        )
    )


def _render_prequalification(view: dict) -> None:
    rows: list[dict] = view["prequal_results"] or []
    st.caption(
        f"{ai_provenance(view['prequal_created_at'])} · judged against the "
        "approved rubric, one requirement at a time, from the résumé alone. "
        "Each result says how well the résumé evidence matches; the confidence "
        "level reflects how complete that evidence is. Not an overall hiring "
        "decision."
    )
    # Same 3-bucket grouping as the scorecard's evidence view (kept identical
    # on purpose — one shared renderer).
    _render_criteria_by_bucket(rows)


def _render_screening_row(view: dict, acting_user_id) -> None:
    """Screening status line + the RECOVERY-ONLY controls for a stalled session.

    Two kinds of stall (CLAUDE.md §2A item 6, §3):
    * ``PRE_ROUND_1`` — the automatic pipeline stopped before questions were
      ready. Recovery = re-run the pipeline (SYSTEM actor).
    * ``MID_SCREENING`` — the candidate stopped answering. There is no auto-
      recovery; the HR decision is to mark the screening abandoned
      (``SCREENING_INCOMPLETE``), which is NEVER a rejection.
    """
    status = view["screening_status"]
    if status is None:
        return

    kind = view["screening_stall_kind"]

    if kind == "PRE_ROUND_1":
        # Text label carries the meaning, not colour alone (CLAUDE.md §26).
        st.markdown(
            f"{badge('caution', 'Pipeline stalled')} — the automatic "
            "pre-screening steps have not finished. Resuming picks up exactly "
            "where it stopped and never repeats work already done."
        )
        if st.button(
            "Resume pipeline",
            key=f"resume_pipe_{view['application_id']}",
            help=(
                "Runs the remaining automatic pre-screening steps for this "
                "candidate. Safe to click again — it won't repeat anything "
                "already done."
            ),
        ):
            _run_resume_pipeline(view["application_id"], acting_user_id)
    elif kind == "MID_SCREENING":
        st.markdown(
            f"{badge('caution', 'Screening stalled')} — {label_for(status)}; "
            "the candidate has not answered for a while. Their saved link still "
            "works if they return. Marking it incomplete is **not** a rejection."
        )
        if st.button(
            "Mark screening abandoned",
            key=f"abandon_{view['application_id']}",
            help=(
                "Marks the screening as incomplete rather than rejecting the "
                "candidate — they can still resume it later from their saved "
                "link."
            ),
        ):
            _run_abandon_screening(view["screening_session_id"], acting_user_id)
    else:
        st.markdown(entity_status_badge("screening", status))


def _application_card_label(summary: dict) -> str:
    """The always-visible summary line on a collapsed application card."""
    stall = ""
    if summary["screening_stall_kind"]:
        stall = "  " + entity_badge("screening", "caution", "Needs attention")
    return (
        f"**{summary['candidate_name']}** — {summary['candidate_email']}  "
        f"{entity_status_badge('application', summary['status'])}{stall}"
    )


def _render_application_card(
    summary: dict, acting_user_id, *, default_expanded: bool
) -> None:
    """One application as a lazy collapsible card.

    ``on_change="rerun"`` + the ``.open`` check mean a collapsed card runs NONE
    of the five per-application reads in :func:`_load_application_detail` — the
    same pattern, and the same reason, as the Jobs page's job cards.
    """
    card = st.expander(
        _application_card_label(summary),
        expanded=default_expanded,
        key=f"appcard_{summary['application_id']}",
        on_change="rerun",
    )
    if not card.open:
        return

    with card:
        try:
            detail = _load_application_detail(
                summary["application_id"], acting_user_id
            )
        except UnauthorizedError:
            st.error("Your account is no longer active — please contact an admin.")
            return
        except SQLAlchemyError:
            load_error("Couldn't load this application right now.")
            return
        _render_application_body({**summary, **detail}, acting_user_id)


def _render_application_body(view: dict, acting_user_id) -> None:
    _render_screening_row(view, acting_user_id)

    if view["document_id"] is None:
        st.caption("No resume document on file for this application.")
        return

    st.caption(f"Resume: {view['document_name']}")

    if view["extracted_data"] is None:
        if st.button(
            "Parse resume",
            key=f"parse_{view['document_id']}",
            help="Reads the résumé and pulls out its evidence. Runs once; the result is saved.",
        ):
            _run_parse(view["document_id"], acting_user_id)
        return

    with st.expander("Extracted evidence inventory", expanded=False):
        _render_extraction(view)

    if view["prequal_results"] is None:
        if st.button(
            "Prequalify",
            key=f"prequal_{view['application_id']}",
            help=(
                "Compares the extracted evidence against the approved "
                "rubric, per criterion. Runs once; result is saved."
            ),
        ):
            _run_prequalify(view["application_id"], acting_user_id)
    else:
        with st.expander("Prequalification results", expanded=False):
            _render_prequalification(view)

    _render_scorecard_section(view, acting_user_id)


def _render_scorecard_section(view: dict, acting_user_id) -> None:
    """The initial scorecard, or its recovery button, once screening is done."""
    screening_done = view["screening_status"] == "SCREENING_COMPLETE" or (
        view["status"] in ("SCREENING_COMPLETED", "SCREENING_EVALUATED")
    )
    if not screening_done:
        return

    if view["evaluation"] is None:
        st.caption(
            "Screening is complete. The scorecard normally generates "
            "automatically — use this if it did not."
        )
        if st.button(
            "Generate scorecard",
            key=f"evaluate_{view['application_id']}",
            help=(
                "Builds the scorecard by checking the prequalification results "
                "against what the candidate said in screening. Runs once; the "
                "result is saved."
            ),
        ):
            _run_evaluate(view["application_id"], acting_user_id)
        return

    with st.expander("Initial scorecard", expanded=False):
        _render_scorecard(view)


def _run_generate_ranking(job_id: str, rubric_version_id: str, acting_user_id) -> None:
    """Generate (or regenerate) the ranking for one rubric-version partition.

    Explicit, repeatable HR action — no AI call, attributed to the logged-in HR
    user. Each click is a fresh full regeneration.
    """
    try:
        with st.spinner("Ranking the evaluated candidates…"):
            with session_scope() as db:
                generate_ranking(
                    db,
                    job_id=uuid.UUID(job_id),
                    rubric_version_id=uuid.UUID(rubric_version_id),
                    requested_by_user_id=acting_user_id,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except RankingError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_RANK_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_RANK_UNEXPECTED)
        return

    success_toast("Ranking generated.")
    st.rerun()


def _render_buckets(row) -> None:
    cols = st.columns(3)
    cols[0].markdown("**Requirements**", help=_REQUIREMENTS_BUCKET_HELP)
    cols[0].markdown(_fmt_bucket(row.requirements_score, row.requirements_coverage))
    cols[1].markdown("**Experience**")
    cols[1].markdown(_fmt_bucket(row.experience_score, row.experience_coverage))
    cols[2].markdown("**Behavioral**")
    cols[2].markdown(_fmt_bucket(row.behavioral_score, row.behavioral_coverage))


def _render_shortlist_control(
    row, *, job_id: str, rubric_version_id: str, shortlist_state, acting_user_id
) -> None:
    """Single-click shortlist / unshortlist toggle + optional reason. No
    confirmation dialog — shortlisting is reversible and non-destructive.

    Actions are visually contained in this partition's section, and the key
    includes ``rubric_version_id`` so the same candidate under a different
    rubric version is a distinct control.
    """
    app_id = str(row.application_id)
    is_shortlisted = bool(shortlist_state and shortlist_state.is_shortlisted)

    if is_shortlisted:
        st.markdown(
            entity_badge(
                "shortlist",
                "info",
                "Shortlisted — selected to proceed toward interview",
            )
        )
    elif shortlist_state is not None:
        st.markdown(
            entity_badge("shortlist", "neutral", "Previously shortlisted, then removed")
        )

    # H17: opt-in. A text box on every ranked and ineligible row read as a
    # required field; the note is optional and rarely used, so it hides until
    # someone actually wants to write one.
    with st.expander("Add a note (optional)", expanded=False):
        reason = st.text_input(
            "Note",
            key=f"sl_reason_{job_id}_{rubric_version_id}_{app_id}",
            label_visibility="collapsed",
            placeholder="Add a short note for your own reference.",
        )
    if is_shortlisted:
        if st.button(
            "Remove from shortlist",
            key=f"sl_btn_{job_id}_{rubric_version_id}_{app_id}",
        ):
            _run_unshortlist(job_id, app_id, reason or None, acting_user_id)
    else:
        if st.button(
            "Shortlist",
            key=f"sl_btn_{job_id}_{rubric_version_id}_{app_id}",
            help="Mark this candidate as selected to proceed toward a human interview.",
        ):
            _run_shortlist(
                job_id, app_id, rubric_version_id, reason or None, acting_user_id
            )


def _render_ranked_row(
    row, *, job_id: str, rubric_version_id: str, shortlist_state, acting_user_id
) -> None:
    """One ranked candidate — rank, score, the three bucket scores individually
    (CLAUDE.md §5: the ranking must be explainable per candidate), confidence and
    per-bucket coverage carried through, the mandatory-UNKNOWN flag surfaced
    prominently, and the shortlist toggle."""
    # Same "/ 10" scale as the bucket scores rendered directly below (H7).
    score_txt = (
        score_out_of_ten(row.overall_score, places=2)
        if row.overall_score is not None
        else "—"
    )
    header = (
        f"**#{row.rank_position}. {row.candidate_name}**  ·  "
        f"overall {score_txt}  "
        f"{badge(confidence_kind(row.overall_confidence), f'confidence: {row.overall_confidence}')}"
    )
    if row.overall_score is None:
        header += "  " + entity_badge("ranking", "neutral", "No bucket assessed")
    st.markdown(header)
    st.caption(row.candidate_email)
    if row.mandatory_unknown_flag:
        st.warning(
            "⚠️ A mandatory requirement had no evidence either way for this "
            "candidate — the rank reflects only what could be assessed. Not a "
            "failure; worth confirming at interview.",
            icon="⚠️",
        )
    _render_buckets(row)
    _render_shortlist_control(
        row, job_id=job_id, rubric_version_id=rubric_version_id,
        shortlist_state=shortlist_state, acting_user_id=acting_user_id,
    )
    st.divider()


def _render_ineligible_row(
    row, *, job_id: str, rubric_version_id: str, shortlist_state, acting_user_id
) -> None:
    st.markdown(
        f"**{row.candidate_name}**  "
        + entity_badge(
            "ranking", "negative", "Ineligible — mandatory requirement not met"
        )
    )
    st.caption(row.candidate_email)
    _render_buckets(row)
    st.caption(
        "Not ranked because a mandatory requirement was not met. Shortlisting "
        "is a separate HR judgment — you may still shortlist this candidate to "
        "proceed toward an interview."
    )
    _render_shortlist_control(
        row, job_id=job_id, rubric_version_id=rubric_version_id,
        shortlist_state=shortlist_state, acting_user_id=acting_user_id,
    )
    st.divider()


def _render_ranking_section(job_id: str, acting_user_id) -> None:
    """Cross-candidate ranking (CLAUDE.md §5), one section per rubric-version
    partition. Thin glue — every number comes from ``ranking_service``.
    """
    st.subheader("Candidate ranking")
    try:
        partitions = _load_partitions(job_id, acting_user_id)
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except SQLAlchemyError:
        load_error("Couldn't load ranking partitions right now.")
        return

    if not partitions:
        st.caption("No candidates have a scorecard for this job yet.")
        return

    multi = len(partitions) > 1
    if multi:
        st.caption(
            "This job has evaluated candidates under more than one approved "
            "rubric version. They are ranked separately — scores across "
            "versions are not comparable."
        )

    for part in partitions:
        label = (
            f"Rubric v{part['version_number']}"
            if part["version_number"] is not None
            else "Rubric (unknown version)"
        )
        with st.container(border=True):
            st.markdown(
                f"**{label}** — {part['evaluated_count']} evaluated candidate(s)"
            )
            st.caption(
                "Ranking and shortlist actions in this section apply only to "
                f"candidates evaluated against {label.lower()}."
            )
            if part["ranking_generated_at"] is not None:
                st.caption(
                    f"Last generated {part['ranking_generated_at']:%Y-%m-%d %H:%M UTC}"
                )
            btn_label = (
                "Regenerate ranking" if part["ranking_generated_at"]
                else "Generate ranking"
            )
            if st.button(
                btn_label,
                key=f"rank_{job_id}_{part['rubric_version_id']}",
                help="Ranks candidates using their existing scorecards.",
            ):
                _run_generate_ranking(
                    job_id, part["rubric_version_id"], acting_user_id
                )

            if part["ranking_generated_at"] is None:
                continue

            try:
                view = _load_partition_view(
                    job_id, part["rubric_version_id"], acting_user_id
                )
            except (UnauthorizedError, SQLAlchemyError):
                load_error("Couldn't load this ranking right now.")
                continue

            rows = view["rows"]
            shortlist = view["shortlist"]
            stale_count = view["stale_count"]
            if stale_count > 0:
                # Same widget shape, palette and "Ranking …" wording as the
                # rank-drift badge on the Interviews page (H20).
                st.markdown(
                    entity_badge(
                        "ranking",
                        "caution",
                        f"Ranking may be out of date — {stale_count} more "
                        "candidate(s) got a scorecard since it was generated",
                    )
                )
                st.caption(
                    "Regenerate before shortlisting to see the current order."
                )

            rvid = part["rubric_version_id"]
            ranked = [r for r in rows if r.eligible]
            ineligible = [r for r in rows if not r.eligible]

            for r in ranked:
                _render_ranked_row(
                    r, job_id=job_id, rubric_version_id=rvid,
                    shortlist_state=shortlist.get(r.application_id),
                    acting_user_id=acting_user_id,
                )
            if ineligible:
                st.markdown(f"#### Ineligible ({len(ineligible)})")
                st.caption(
                    "Excluded from the numbered ranking — a mandatory "
                    "requirement was assessed as not met. Not a rejection; that "
                    "remains a human decision."
                )
                for r in ineligible:
                    _render_ineligible_row(
                        r, job_id=job_id, rubric_version_id=rvid,
                        shortlist_state=shortlist.get(r.application_id),
                        acting_user_id=acting_user_id,
                    )


def _load_partitions(job_id: str, acting_user_id) -> list[dict]:
    with session_scope() as db:
        return [
            {
                "rubric_version_id": str(p.rubric_version_id),
                "version_number": p.version_number,
                "evaluated_count": p.evaluated_count,
                "ranking_generated_at": p.ranking_generated_at,
            }
            for p in list_rubric_version_partitions_for_job(
                db, job_id=job_id, acting_user_id=acting_user_id
            )
        ]


def _load_partition_view(job_id: str, rubric_version_id: str, acting_user_id) -> dict:
    """Ranking rows + current shortlist state (keyed by application_id) + the
    staleness count for one rubric-version partition, in one session."""
    with session_scope() as db:
        rows = get_ranking_display_rows(
            db,
            job_id=job_id,
            rubric_version_id=rubric_version_id,
            acting_user_id=acting_user_id,
        )
        shortlist = get_shortlist_status_for_job(
            db, job_id=job_id, acting_user_id=acting_user_id
        )
        staleness = get_ranking_staleness_for_partition(
            db, job_id=job_id, rubric_version_id=rubric_version_id,
            acting_user_id=acting_user_id,
        )
        return {
            "rows": rows,
            "shortlist": shortlist,
            "stale_count": staleness.stale_count,
        }


def _run_shortlist(
    job_id: str, application_id: str, rubric_version_id: str, reason, acting_user_id
) -> None:
    try:
        with st.spinner("Updating the shortlist…"):
            with session_scope() as db:
                shortlist_candidate(
                    db,
                    job_id=uuid.UUID(job_id),
                    application_id=uuid.UUID(application_id),
                    rubric_version_id=uuid.UUID(rubric_version_id),
                    reason=reason,
                    requested_by_user_id=acting_user_id,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except ShortlistError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_SHORTLIST_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_SHORTLIST_UNEXPECTED)
        return

    success_toast("Candidate shortlisted.")
    st.rerun()


def _run_unshortlist(
    job_id: str, application_id: str, reason, acting_user_id
) -> None:
    try:
        with st.spinner("Updating the shortlist…"):
            with session_scope() as db:
                unshortlist_candidate(
                    db,
                    job_id=uuid.UUID(job_id),
                    application_id=uuid.UUID(application_id),
                    reason=reason,
                    requested_by_user_id=acting_user_id,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except ShortlistError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_SHORTLIST_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_SHORTLIST_UNEXPECTED)
        return

    success_toast("Candidate removed from the shortlist.")
    st.rerun()



# --- Applications tab: lazy cards + paging (mirrors the Jobs page) --------

#: Applications loaded per "Show more" click (and on first paint).
_APP_PAGE_SIZE = 10

#: More than this many cards on screen and they all default to collapsed.
_APP_EXPAND_THRESHOLD = 5


def _applications_shown_count(job_id: str) -> int:
    """How many application cards this tab shows, in session state so a
    "Show more" click survives the rerun.

    Resets to one page when the selected job changes — a count carried over
    from another job would silently skip the first applications of the new one.
    The Jobs page resets on a search/sort change for the same reason; this tab
    has no search or sort, so the job id is the only thing to scope to.
    """
    count_key = "candidates_apps_shown"
    scope_key = "candidates_apps_job"
    if st.session_state.get(scope_key) != job_id:
        st.session_state[scope_key] = job_id
        st.session_state[count_key] = _APP_PAGE_SIZE
    return int(st.session_state.get(count_key, _APP_PAGE_SIZE))


def _render_applications_tab(job_id: str, acting_user_id) -> None:
    shown = _applications_shown_count(job_id)

    try:
        # One extra row tells us whether a "Show more" is warranted, without a
        # second query.
        summaries, total = _load_application_summaries(
            job_id, acting_user_id, limit=shown + 1
        )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except SQLAlchemyError:
        load_error("Couldn't load applications right now.")
        return

    st.subheader(f"Applications ({total})")
    if not summaries:
        st.caption("No applications for this job yet.")
        return

    has_more = len(summaries) > shown
    summaries = summaries[:shown]

    # Same rule as the Jobs page, on what is actually rendered right now.
    default_expanded = len(summaries) <= _APP_EXPAND_THRESHOLD

    for summary in summaries:
        _render_application_card(
            summary, acting_user_id, default_expanded=default_expanded
        )

    if has_more:
        if st.button(
            f"Show more ({_APP_PAGE_SIZE} at a time)", key="apps_more"
        ):
            st.session_state["candidates_apps_shown"] = shown + _APP_PAGE_SIZE
            st.rerun()


def render_candidates_page() -> None:
    current = get_current_user(st.session_state)
    if current is None:  # defensive: gate is in main.py
        st.error("Please sign in.")
        return

    try:
        acting_user_id = uuid.UUID(current["id"])
    except (ValueError, KeyError, TypeError):
        # Session data is unusable — do not proceed with a None identity; the
        # HR-only services would (correctly) reject it anyway.
        st.error("Your session looks invalid — please sign out and back in.")
        return

    page_header("Candidates")

    try:
        jobs = load_job_options()
    except SQLAlchemyError:
        load_error("Couldn't load jobs right now.")
        return

    if not jobs:
        st.caption("No jobs yet — create one on the Jobs page first.")
        return

    # One picker above the tabs, so both tabs always agree on which job they
    # show. The shared key also carries the choice to the Interviews page
    # (see ui_widgets.job_picker).
    job_id = job_picker(jobs, key=HR_JOB_PICKER_KEY)
    if job_id is None:
        return

    # H13: ranking and applications used to run down one unbounded page,
    # rendering the same candidates twice. Two tabs, so each candidate appears
    # once per view.
    ranking_tab, applications_tab = st.tabs(
        ["Ranking & Shortlist", "Applications"]
    )
    with ranking_tab:
        _render_ranking_section(job_id, acting_user_id)
    with applications_tab:
        _render_applications_tab(job_id, acting_user_id)
