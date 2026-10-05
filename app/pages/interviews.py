"""Interviews page — HR-facing consolidated shortlist view + interview-guide
generation and display (CLAUDE.md §6; Phase 4 Step 7).

Scope (deliberately minimal)
----------------------------
* Per job, a "Shortlisted Candidates" view, partitioned into clearly labeled
  sections by ``rubric_version_id`` — never merged across versions.
* Per candidate: shortlist context, the rubric version, rank at decision time,
  a fresh current rank, a visible rank-drift indicator, and whether a guide
  exists.
* Single-click "Generate / Regenerate interview guide" per candidate — NO
  confirmation dialog (non-destructive, matches Steps 5-6).
* A read-only guide view grouped by category. No editing UI for questions.
* A candidate later unshortlisted keeps their previously-generated guide
  readable here; a NEW generation for them is blocked with an explanatory
  message.
* Step 8: per candidate, a "Human interview feedback" section — the prior
  write-ups (newest first) plus a form to record a new round. Candidate and
  interviewer are resolved and shown read-only, never typed. Unlike guide
  generation, feedback stays available for an unshortlisted candidate: an
  interview that happened must remain recordable.

Rendered via ``st.navigation`` from ``app/main.py`` (never auto-discovered).
100% HR-only — no candidate-facing surface is touched. Business logic lives in
``interview_guide_service``; this file is thin glue.

AI tasks are NEVER triggered on page load — only on an explicit button click
(CLAUDE.md §§9, 29).
"""

from __future__ import annotations

import uuid

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from app.database.database import session_scope
from app.database.models.interview_feedback import RATING_MAX, RATING_MIN
from app.database.models.interview_guide import InterviewQuestionCategory
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysisStatus,
)
from app.database.models.screening_evaluation import ScreeningRecommendation
from app.services.final_scorecard_service import (
    MULTIPLE_RUBRIC_VERSIONS_WARNING,
    FinalScorecardActorError,
    Provenance,
    get_final_scorecard,
)
from app.services.interview_feedback_service import (
    InterviewFeedbackError,
    create_interview_feedback,
    get_feedback_context,
    list_feedback_views,
)
from app.services.interview_guide_service import (
    InterviewGuideError,
    generate_interview_guide,
    get_shortlisted_candidates_for_job,
    list_interview_guides_for_job,
)
from app.services.post_interview_service import (
    PostInterviewAnalysisError,
    create_post_interview_analysis,
    get_current_post_interview_analysis,
    get_post_interview_analysis_history,
)
from app.utils.authorization import UnauthorizedError
from app.utils.session import get_current_user
from app.utils.ui import (
    ai_provenance,
    badge,
    confidence_kind,
    entity_badge,
    interview_round_label,
    label_for,
    recommendation_kind,
    result_kind,
    score_out_of_ten,
)
from app.utils.ui_widgets import (
    HR_JOB_PICKER_KEY,
    detail_lines,
    job_picker,
    load_error,
    load_job_options,
    success_toast,
)

_GUIDE_DB_ERROR = "Couldn't save the interview guide — please try again."
_GUIDE_UNEXPECTED = "Something went wrong generating the interview guide. Please try again."
_FEEDBACK_DB_ERROR = "Couldn't save the interview feedback — please try again."
_FEEDBACK_UNEXPECTED = (
    "Something went wrong saving the interview feedback. Please try again."
)
_ANALYSIS_DB_ERROR = (
    "Couldn't save the post-interview analysis — please try again."
)
_ANALYSIS_UNEXPECTED = (
    "Something went wrong running the post-interview analysis. Please try "
    "again."
)

#: Recommendation options, in the order CLAUDE.md §7 lists them. All three are
#: offered: a human interviewer may recommend REJECT (the automated screening
#: path may not).
_RECOMMENDATION_ORDER = (
    ScreeningRecommendation.PROCEED,
    ScreeningRecommendation.HOLD,
    ScreeningRecommendation.REJECT,
)

#: How many competency rows the form offers. A fixed, generous handful rather
#: than dynamic add/remove: Streamlit reruns the whole script on every
#: interaction, so a growable list needs session-state bookkeeping that buys
#: nothing here. Rows left blank are simply dropped.
_RATING_SLOTS = 5


# --- loaders (thin) -----------------------------------------------


def _load_view(job_id: str, acting_user_id) -> dict:
    with session_scope() as db:
        shortlisted = get_shortlisted_candidates_for_job(
            db, job_id=job_id, acting_user_id=acting_user_id
        )
        guides = list_interview_guides_for_job(
            db, job_id=job_id, acting_user_id=acting_user_id
        )
        # Feedback for every application on screen — the shortlisted ones and
        # the retained-guide ones alike, since feedback does not depend on
        # current shortlist state.
        application_ids = {r.application_id for r in shortlisted} | {
            g.application_id for g in guides
        }
        feedback = {
            app_id: {
                "context": get_feedback_context(
                    db, app_id, acting_user_id=acting_user_id
                ),
                "history": list_feedback_views(
                    db, app_id, acting_user_id=acting_user_id
                ),
            }
            for app_id in application_ids
        }
        # Post-interview analyses for the same applications. Reading is a
        # plain SELECT — no AI call happens on page load (CLAUDE.md §§9, 29).
        analyses = {
            app_id: {
                "current": get_current_post_interview_analysis(
                    db, app_id, acting_user_id=acting_user_id
                ),
                "history": get_post_interview_analysis_history(
                    db, app_id, acting_user_id=acting_user_id
                ),
            }
            for app_id in application_ids
        }
        # primitives / frozen dataclasses only — safe outside the session
        return {
            "shortlisted": shortlisted,
            "guides_by_application": {g.application_id: g for g in guides},
            "feedback_by_application": feedback,
            "analysis_by_application": analyses,
        }


def _run_generate_guide(application_id: str, acting_user_id, *, force: bool) -> None:
    try:
        with st.spinner("Generating the interview guide — a few seconds…"):
            with session_scope() as db:
                generate_interview_guide(
                    db,
                    application_id=uuid.UUID(application_id),
                    requested_by_user_id=acting_user_id,
                    force=force,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except InterviewGuideError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_GUIDE_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_GUIDE_UNEXPECTED)
        return

    success_toast("Interview guide ready.")
    st.rerun()


def _run_submit_feedback(
    *,
    application_id: str,
    interview_guide_id: str,
    interview_round: int,
    recommendation: str,
    notes: str,
    ratings: list[dict],
    acting_user_id,
) -> None:
    """Submit one interview write-up. Same four-way exception ladder as the
    guide action — a validation problem shows its own readable message, and a
    traceback never reaches the page."""
    try:
        with session_scope() as db:
            create_interview_feedback(
                db,
                user_id=acting_user_id,
                application_id=uuid.UUID(application_id),
                interview_guide_id=uuid.UUID(interview_guide_id),
                interview_round=interview_round,
                recommendation=recommendation,
                notes=notes,
                ratings=ratings,
            )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except InterviewFeedbackError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_FEEDBACK_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_FEEDBACK_UNEXPECTED)
        return

    success_toast("Interview feedback recorded.")
    st.rerun()


def _run_generate_analysis(
    application_id: str, acting_user_id, *, force: bool
) -> None:
    """Run the post-interview AI analysis for one candidate.

    Same four-way exception ladder as the other two actions. Fired ONLY from
    an explicit button click — never on page load, and never as a
    side-effect of submitting interview feedback (CLAUDE.md §§9, 29).
    """
    try:
        with st.spinner(
            "Analysing the full record — this takes a few seconds…"
        ):
            with session_scope() as db:
                create_post_interview_analysis(
                    db,
                    user_id=acting_user_id,
                    application_id=uuid.UUID(application_id),
                    force=force,
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except PostInterviewAnalysisError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_ANALYSIS_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_ANALYSIS_UNEXPECTED)
        return

    success_toast("Post-interview analysis ready.")
    st.rerun()


# --- pure helpers ------------------------------------------------


def _usable_ratings(rows: list[dict]) -> list[dict]:
    """Drop the form's entirely-untouched competency rows.

    A row counts as untouched only when the label, the score AND the comment
    are all empty. A half-filled row (a score with no competency name, say) is
    passed through deliberately, so the service rejects it with a readable
    message — silently discarding it would lose something the interviewer
    actually typed.

    Pure: no ``st.*``, so it is unit-tested like ``app/utils/ui.py``.
    """
    kept: list[dict] = []
    for row in rows:
        label = (row.get("competency_label") or "").strip()
        score = row.get("rating")
        comment = (row.get("comment") or "").strip()
        if not label and score is None and not comment:
            continue
        kept.append(
            {
                "competency_label": label,
                "rating": score,
                "comment": comment or None,
            }
        )
    return kept


def _interviewer_name() -> str:
    """The signed-in user's name, for the read-only "Interviewer" line.

    Read from session state, never typed: CLAUDE.md §7's record must attribute
    the write-up to the person who actually ran the interview, and the service
    independently stores ``submitted_by_user_id`` from the authenticated actor —
    this is only its on-screen echo.
    """
    current = get_current_user(st.session_state) or {}
    return current.get("full_name") or "—"


# --- render ------------------------------------------------------


def _render_guide(guide) -> None:
    """Read-only guide view, grouped by category. No editing UI."""
    ver = (
        f"Rubric v{guide.rubric_version_number}"
        if guide.rubric_version_number is not None
        else "Rubric (unknown version)"
    )
    status_note = (
        f" · captured version is now {label_for(guide.rubric_version_status)}"
        if guide.rubric_version_status
        and guide.rubric_version_status != "APPROVED"
        else ""
    )
    st.caption(
        f"{ai_provenance(guide.generated_at)} · grounded in {ver}{status_note} · "
        f"{len(guide.questions)} question(s). For the human interviewer to use, "
        "adapt, and judge."
    )
    by_cat: dict[str, list] = {}
    for q in guide.questions:
        by_cat.setdefault(q.category, []).append(q)

    for category in InterviewQuestionCategory.ORDER:
        items = by_cat.get(category, [])
        if not items:
            continue
        st.markdown(f"#### {label_for(category)} ({len(items)})")
        for q in items:
            st.markdown(f"**{q.question_text}**")
            # Native markdown list indents these — no &nbsp; padding (H22).
            detail_lines(
                (
                    ("Criterion", q.criterion_text),
                    ("Evaluates", q.evaluates),
                    ("Why generated", q.generated_reason),
                )
            )
            st.markdown("")


def _render_feedback_history(history: list) -> None:
    """Prior write-ups, newest first. Read-only and never edited: CLAUDE.md §7
    requires the original human feedback to be preserved as written."""
    if not history:
        st.caption("No interview feedback has been recorded yet.")
        return

    for item in history:
        with st.container(border=True):
            st.markdown(
                f"**{interview_round_label(item.interview_round)}**  ·  "
                + badge(
                    recommendation_kind(item.recommendation),
                    f"Interviewer recommends: {label_for(item.recommendation)}",
                )
            )
            st.caption(
                f"Recorded by {item.submitted_by_name} on "
                f"{item.created_at:%Y-%m-%d}. Human assessment — not AI-generated."
            )
            if item.ratings:
                st.markdown("*Competency ratings*")
                detail_lines(
                    (
                        r.competency_label,
                        f"{r.rating} / {RATING_MAX}"
                        + (f" — {r.comment}" if r.comment else ""),
                    )
                    for r in item.ratings
                )
            if item.notes:
                st.markdown("*Interview notes*")
                st.markdown(item.notes)


def _render_feedback_form(context, acting_user_id) -> None:
    """The capture form. Candidate and interviewer are shown read-only —
    CLAUDE.md §7's record must state who was actually interviewed and who
    actually ran it, so neither is a text input."""
    app_id = str(context.application_id)
    guide_id = str(context.interview_guide_id)

    st.markdown("**Record a new interview round**")
    detail_lines(
        (
            ("Candidate", context.candidate_name),
            ("Interviewer", _interviewer_name()),
        )
    )

    with st.form(f"feedback_form_{app_id}", clear_on_submit=True):
        # Suggested, not imposed — an interviewer writing up an earlier round
        # late must be able to choose it (the stored value is never derived).
        round_options = sorted(
            {context.suggested_round} | set(range(1, context.suggested_round + 2))
        )
        interview_round = st.selectbox(
            "Interview round",
            round_options,
            index=round_options.index(context.suggested_round),
            format_func=interview_round_label,
            help=(
                "Suggested from the rounds already recorded. Change it if you "
                "are writing up a different round."
            ),
            key=f"fb_round_{app_id}",
        )

        st.markdown("**Competency ratings** (leave a row blank to skip it)")
        rating_inputs: list[dict] = []
        for slot in range(_RATING_SLOTS):
            cols = st.columns([3, 1, 3])
            label = cols[0].text_input(
                "Competency",
                key=f"fb_lbl_{app_id}_{slot}",
                label_visibility="collapsed" if slot else "visible",
                placeholder="e.g. System design",
            )
            score = cols[1].selectbox(
                f"{RATING_MIN}–{RATING_MAX}",
                list(range(RATING_MIN, RATING_MAX + 1)),
                index=None,
                key=f"fb_score_{app_id}_{slot}",
                label_visibility="collapsed" if slot else "visible",
                placeholder="—",
            )
            comment = cols[2].text_input(
                "Comment (optional)",
                key=f"fb_cmt_{app_id}_{slot}",
                label_visibility="collapsed" if slot else "visible",
                placeholder="What you observed.",
            )
            rating_inputs.append(
                {"competency_label": label, "rating": score, "comment": comment}
            )

        notes = st.text_area(
            "Interview notes",
            key=f"fb_notes_{app_id}",
            placeholder=(
                "What happened in the interview, in your own words. Stored "
                "exactly as written."
            ),
        )
        recommendation = st.radio(
            "Your recommendation",
            _RECOMMENDATION_ORDER,
            format_func=label_for,
            horizontal=True,
            key=f"fb_rec_{app_id}",
            help=(
                "Your recommendation as the interviewer. It is recorded "
                "alongside the AI's, not merged with it, and it is not the "
                "final hiring decision."
            ),
        )

        if st.form_submit_button("Save interview feedback"):
            _run_submit_feedback(
                application_id=app_id,
                interview_guide_id=guide_id,
                interview_round=interview_round,
                recommendation=recommendation,
                notes=notes,
                ratings=_usable_ratings(rating_inputs),
                acting_user_id=acting_user_id,
            )


def _render_feedback_section(application_id, feedback_state, acting_user_id) -> None:
    """The whole "Human interview feedback" block for one candidate."""
    state = (feedback_state or {}).get(application_id) or {}
    context = state.get("context")
    history = state.get("history") or []

    count = len(history)
    title = (
        f"Human interview feedback ({count})" if count
        else "Human interview feedback"
    )
    with st.expander(title, expanded=False):
        st.caption(
            "Recorded by the human interviewer. Preserved exactly as written — "
            "never rewritten or summarised by AI."
        )
        _render_feedback_history(history)
        if context is None:
            load_error("Couldn't load this candidate's interview feedback.")
            return
        if context.interview_guide_id is None:
            st.info(
                "Generate the interview guide for this candidate before "
                "recording feedback."
            )
            return
        st.divider()
        _render_feedback_form(context, acting_user_id)


def _render_analysis_body(analysis) -> None:
    """The prose of one analysis. AI-generated throughout — the caller is
    responsible for saying so before calling this."""
    st.markdown("*Consolidated summary*")
    st.markdown(analysis.summary)

    for heading, items, empty_note in (
        ("Strengths", analysis.strengths, "None recorded."),
        ("Gaps", analysis.gaps, "None recorded."),
        (
            "Unknowns",
            analysis.unknowns,
            "Nothing outstanding — every rubric area was established.",
        ),
    ):
        st.markdown(f"*{heading}*")
        if items:
            for item in items:
                st.markdown(f"- {item}")
        else:
            st.caption(empty_note)

    st.markdown("*Evidence consistency*")
    st.markdown(analysis.evidence_consistency_notes)


def _render_analysis(analysis) -> None:
    """Read-only view of the CURRENT analysis.

    The AI recommendation and the interviewer's are shown as two separate,
    separately-labeled facts. This page deliberately does NOT state whether
    they agree: AI/human comparison (CLAUDE.md §8) is a later step, and
    nothing in this MVP computes a disagreement.
    """
    st.caption(
        f"{ai_provenance(analysis.created_at)} · model {analysis.ai_model} · "
        f"requested by {analysis.requested_by_name}. AI assessment — one "
        "input for the hiring manager, not a hiring decision."
    )
    if analysis.analyzed_only_latest_feedback:
        st.caption(
            "Based on the most recent interview-feedback record only. If a "
            "later round has since been recorded, regenerate to include it."
        )

    st.markdown(
        badge(
            recommendation_kind(analysis.ai_recommendation),
            f"AI recommends: {label_for(analysis.ai_recommendation)}",
        )
        + "  "
        + badge(
            confidence_kind(analysis.confidence),
            f"Confidence: {label_for(analysis.confidence)}",
        )
    )
    st.markdown(
        badge(
            recommendation_kind(analysis.human_recommendation_snapshot),
            f"Interviewer recommended: "
            f"{label_for(analysis.human_recommendation_snapshot)}",
        )
    )
    st.caption(
        "The interviewer's recommendation is shown as recorded, alongside "
        "the AI's — not merged with it, and not compared against it here."
    )
    st.divider()
    _render_analysis_body(analysis)


def _render_analysis_history(history) -> None:
    """Earlier analyses, if any. Regeneration here is non-destructive: a
    superseded analysis a decision may have been discussed against stays
    readable."""
    superseded = [
        a for a in history
        if a.status != PostInterviewAnalysisStatus.CURRENT
    ]
    if not superseded:
        return
    with st.expander(
        f"Earlier analyses ({len(superseded)})", expanded=False
    ):
        st.caption(
            "Kept, never overwritten. Each was the current analysis until it "
            "was regenerated."
        )
        for item in superseded:
            with st.container(border=True):
                when = (
                    f"{item.superseded_at:%Y-%m-%d}"
                    if item.superseded_at is not None
                    else "unknown date"
                )
                st.caption(
                    f"{ai_provenance(item.created_at)} · superseded {when} · "
                    f"requested by {item.requested_by_name}"
                )
                st.markdown(
                    badge(
                        recommendation_kind(item.ai_recommendation),
                        f"AI recommended: {label_for(item.ai_recommendation)}",
                    )
                    + "  "
                    + badge(
                        confidence_kind(item.confidence),
                        f"Confidence: {label_for(item.confidence)}",
                    )
                )
                _render_analysis_body(item)


def _render_analysis_section(
    application_id, feedback_state, analysis_state, acting_user_id
) -> None:
    """The whole "Post-interview AI analysis" block for one candidate."""
    state = (analysis_state or {}).get(application_id) or {}
    current = state.get("current")
    history = state.get("history") or []
    has_feedback = bool(
        ((feedback_state or {}).get(application_id) or {}).get("history")
    )

    with st.expander("Post-interview AI analysis", expanded=False):
        st.caption(
            "Generated by AI from the whole record — résumé, prequalification, "
            "screening and the interviewer's own feedback. It assists the "
            "hiring manager; it does not decide."
        )
        if not has_feedback:
            st.info(
                "Record the human interview feedback first — the analysis "
                "reads it."
            )
            return

        if current is not None:
            _render_analysis(current)
            st.divider()

        label = (
            "Regenerate post-interview analysis" if current is not None
            else "Run post-interview analysis"
        )
        if st.button(label, key=f"pia_{application_id}"):
            _run_generate_analysis(
                str(application_id), acting_user_id, force=current is not None
            )
        if current is not None:
            st.caption(
                "Regenerating keeps the current analysis — it is marked "
                "superseded and stays readable below, never deleted."
            )
        _render_analysis_history(history)

def _scorecard_open_key(application_id) -> str:
    """Session-state key for one candidate's scorecard toggle.

    ``st.session_state`` is UI state only (CLAUDE.md §16) — it holds whether the
    panel is open, never any scorecard content.
    """
    return f"final_scorecard_open_{application_id}"


def _bucket_line(bucket) -> None:
    """One §10 score section: the number AND the evidence behind it.

    CLAUDE.md line 1074 — "Do not display a score without supporting context."
    A missing score is rendered as its explanation, never as 0.
    """
    if bucket.score is None:
        st.markdown(f"**{bucket.label}** — {bucket.unavailable_reason}")
        st.caption(f"Source: {bucket.provenance}")
        return

    coverage = (
        f" · {round(bucket.coverage * 100)}% of this section assessed"
        if bucket.coverage is not None
        else ""
    )
    st.markdown(
        f"**{bucket.label}: {score_out_of_ten(bucket.score)}**{coverage}"
    )
    st.caption(f"Source: {bucket.provenance}")
    if bucket.evidence:
        for line in bucket.evidence:
            st.markdown(f"- {line}")
    else:
        st.caption("No supporting evidence lines recorded for this section.")


def _criteria_block(title: str, rows) -> None:
    """A §10 per-criterion block — criterion, result, evidence, confidence."""
    st.markdown(f"#### {title} ({len(rows)})")
    if not rows:
        st.caption("No criteria of this type on the rubric version used.")
        return
    for c in rows:
        st.markdown(
            f"**{c.criterion_text}**  ·  "
            + badge(result_kind(c.result), f"Result: {label_for(c.result)}")
            + "  "
            + badge(
                confidence_kind(c.confidence),
                f"Confidence: {label_for(c.confidence)}",
            )
        )
        detail_lines(
            (
                ("Evidence", c.evidence_summary or "—"),
                ("Source", c.provenance),
            )
        )


def _str_list_block(title: str, items, provenance: str, empty: str) -> None:
    st.markdown(f"**{title}**")
    st.caption(f"Source: {provenance}")
    if not items:
        st.caption(empty)
        return
    for item in items:
        st.markdown(f"- {item}")


def _render_final_scorecard(view) -> None:
    """The whole §10 scorecard, ordered Evidence -> AI assessment -> Human
    feedback -> System scores -> Recommendations -> Final decision.

    Every value here is read from its originating source. Nothing on this page
    computes a recommendation, compares the two recommendations, or decides
    anything.
    """
    if view.has_multiple_rubric_versions:
        st.warning(MULTIPLE_RUBRIC_VERSIONS_WARNING)

    st.markdown(f"### {view.candidate_name}")
    st.caption(
        f"{view.candidate_email} · {view.job_title} · application status "
        f"{label_for(view.application_status)}"
    )

    # --- headline numbers ------------------------------------------
    if view.overall_score is not None:
        st.markdown(f"**Overall score: {view.overall_score:.2f}**")
        st.caption(
            f"Source: {view.overall_score_provenance}. This is the "
            "pre-interview ranking score exactly as Step 5 recorded it — it is "
            "not recalculated here and does not include interview evidence."
        )
    else:
        st.markdown(f"**Overall score** — {view.overall_score_unavailable_reason}")

    conf_bits = []
    if view.screening_confidence:
        conf_bits.append(
            badge(
                confidence_kind(view.screening_confidence),
                f"Screening confidence: {label_for(view.screening_confidence)}",
            )
        )
    if view.post_interview_confidence:
        conf_bits.append(
            badge(
                confidence_kind(view.post_interview_confidence),
                "Post-interview confidence: "
                f"{label_for(view.post_interview_confidence)}",
            )
        )
    if conf_bits:
        st.markdown("  ".join(conf_bits))
    st.caption(
        "Confidence describes how complete and consistent the *evidence* is — "
        "it is not a measure of the candidate, and it is never blended into any "
        "score above or below."
    )

    # --- rubric provenance ------------------------------------------
    st.markdown("#### Rubric version used by each source")
    if view.rubric_versions:
        detail_lines(
            (
                r.source_label,
                (
                    f"Rubric v{r.version_number}"
                    if r.version_number is not None
                    else "unknown version"
                )
                + (
                    f" ({label_for(r.status)})"
                    if r.status and r.status != "APPROVED"
                    else ""
                ),
            )
            for r in view.rubric_versions
        )
    else:
        st.caption("No rubric-versioned evidence recorded yet.")

    st.divider()

    # --- EVIDENCE ---------------------------------------------------
    st.markdown("### Evidence")
    st.caption(f"Source: {Provenance.RESUME_EVIDENCE}")
    if view.resume_evidence:
        with st.expander("Résumé evidence extracted from the CV", expanded=False):
            for key, values in view.resume_evidence.items():
                st.markdown(f"**{label_for(key)}**")
                if isinstance(values, list) and values:
                    for v in values:
                        st.markdown(f"- {v if isinstance(v, str) else str(v)}")
                elif values:
                    st.markdown(f"- {values}")
                else:
                    st.caption("(none stated)")
    else:
        st.caption("No résumé evidence has been extracted for this candidate.")

    st.caption(f"Source: {Provenance.SCREENING_TRANSCRIPT}")
    if view.has_screening_transcript:
        _n = view.screening_question_count
        with st.expander(
            f"Screening transcript ({_n} question{'s' if _n != 1 else ''})",
            expanded=False,
        ):
            for entry in view.screening_transcript:
                st.markdown(
                    f"**Round {entry.round} · {label_for(entry.category)}**"
                )
                st.markdown(entry.question_text)
                if entry.answered:
                    st.markdown(f"> {entry.answer_text}")
                else:
                    st.caption("(not answered)")
    else:
        st.caption("No screening transcript is available for this candidate yet.")

    # --- AI ASSESSMENT ----------------------------------------------
    st.divider()
    st.markdown("### AI assessment")
    _criteria_block("Mandatory requirements", view.mandatory_criteria)
    _criteria_block("Preferred requirements", view.preferred_criteria)
    if view.other_criteria:
        _criteria_block("Other requirements", view.other_criteria)

    st.markdown("#### Screening AI narrative")
    _str_list_block(
        "Strengths", view.screening_strengths, Provenance.SCREENING_AI,
        "None recorded.",
    )
    _str_list_block(
        "Gaps", view.screening_gaps, Provenance.SCREENING_AI, "None recorded.",
    )
    _str_list_block(
        "Unknowns", view.screening_unknowns, Provenance.SCREENING_AI,
        "Nothing outstanding was recorded at screening.",
    )

    if view.post_interview_summary:
        st.markdown("#### Post-interview AI analysis")
        st.caption(f"Source: {Provenance.POST_INTERVIEW_AI}")
        st.markdown(view.post_interview_summary)
        _str_list_block(
            "Strengths", view.post_interview_strengths,
            Provenance.POST_INTERVIEW_AI, "None recorded.",
        )
        _str_list_block(
            "Gaps", view.post_interview_gaps, Provenance.POST_INTERVIEW_AI,
            "None recorded.",
        )
        _str_list_block(
            "Unknowns", view.post_interview_unknowns,
            Provenance.POST_INTERVIEW_AI,
            "Nothing outstanding — every rubric area was established.",
        )
        if view.post_interview_evidence_consistency:
            st.markdown("**Evidence consistency**")
            st.markdown(view.post_interview_evidence_consistency)
    else:
        st.markdown("#### Post-interview AI analysis")
        st.caption("No post-interview analysis has been generated yet.")

    # --- HUMAN FEEDBACK ---------------------------------------------
    st.divider()
    st.markdown("### Human interview feedback")
    st.caption(
        f"Source: {Provenance.HUMAN_FEEDBACK}. Written by a person and shown "
        "exactly as recorded — never rewritten or summarised by AI."
    )
    if view.interview_round is None:
        st.caption("No interview feedback has been recorded yet.")
    else:
        recorded = (
            f" on {view.interview_recorded_at:%Y-%m-%d}"
            if view.interview_recorded_at is not None
            else ""
        )
        st.markdown(
            f"**{interview_round_label(view.interview_round)}** — recorded by "
            f"{view.interviewer_name or 'unknown'}{recorded}"
        )
        if view.interview_ratings:
            detail_lines(
                (
                    r.competency_label,
                    f"{r.rating} / {r.rating_max}"
                    + (f" — {r.comment}" if r.comment else ""),
                )
                for r in view.interview_ratings
            )
        else:
            st.caption("No competency ratings were recorded.")
        if view.interview_notes:
            st.markdown("*Interview notes*")
            st.markdown(view.interview_notes)

    # --- SYSTEM-CALCULATED SCORES -----------------------------------
    st.divider()
    st.markdown("### System-calculated scores")
    st.caption(
        "Calculated by deterministic Python rules from the evidence above — "
        "not produced by AI, and not a human judgement."
    )
    for bucket in (
        view.requirements, view.experience, view.behavioral, view.interview
    ):
        _bucket_line(bucket)
        st.markdown("")
    st.caption(
        "Interview score is the mean competency rating as a proportion of the "
        f"{RATING_MAX}-point scale, expressed out of 10. Confidence is not part "
        "of any score."
    )

    # --- RECOMMENDATIONS --------------------------------------------
    st.divider()
    st.markdown("### Recommendations")
    st.caption(
        "Three separately-sourced recommendations, each shown as recorded. "
        "They are not merged, ranked, or compared."
    )

    if view.screening_ai_recommendation:
        st.markdown(
            badge(
                recommendation_kind(view.screening_ai_recommendation),
                "Screening AI recommendation: "
                f"{label_for(view.screening_ai_recommendation)}",
            )
        )
        st.caption(f"Source: {Provenance.SCREENING_AI} (pre-interview)")
    else:
        st.markdown("**Screening AI recommendation** — not available")

    if view.post_interview_ai_recommendation:
        st.markdown(
            badge(
                recommendation_kind(view.post_interview_ai_recommendation),
                "Post-interview AI recommendation: "
                f"{label_for(view.post_interview_ai_recommendation)}",
            )
        )
        st.caption(f"Source: {Provenance.POST_INTERVIEW_AI}")
    else:
        st.markdown("**Post-interview AI recommendation** — not available")

    if view.human_recommendation:
        st.markdown(
            badge(
                recommendation_kind(view.human_recommendation),
                "Human recommendation: "
                f"{label_for(view.human_recommendation)}",
            )
        )
        st.caption(
            f"Source: {Provenance.HUMAN_RECOMMENDATION}. The interviewer's own "
            "recommendation, unmodified."
        )
    else:
        st.markdown("**Human recommendation** — not available")

    st.markdown("**AI-Human disagreement**")
    st.caption(view.disagreement_status)

    # --- FINAL DECISION ---------------------------------------------
    st.divider()
    st.markdown("### Final human decision")
    st.markdown(f"**{view.final_decision_status}**")
    st.caption(
        "The hiring manager's decision is recorded in a later step. Nothing on "
        "this page makes, proposes, or infers it."
    )

    # --- RANKING CONTEXT --------------------------------------------
    st.divider()
    st.markdown("### Pre-Interview Candidate Ranking")
    st.caption(
        f"Source: {Provenance.RANKING}. Shown as recorded for context — it is "
        "not recalculated here and does not take interview evidence into "
        "account."
    )
    r = view.ranking
    detail_lines(
        (
            (
                "Rank",
                f"#{r.rank_position}" if r.rank_position is not None
                else "not currently ranked",
            ),
            (
                "Ranking score",
                f"{r.overall_score:.2f}" if r.overall_score is not None else "—",
            ),
            (
                "Eligible",
                "—" if r.eligible is None else ("Yes" if r.eligible else "No"),
            ),
            (
                "Mandatory unknown",
                "—" if r.mandatory_unknown_flag is None
                else ("Yes" if r.mandatory_unknown_flag else "No"),
            ),
            (
                "Shortlisted",
                "—" if r.is_shortlisted is None
                else ("Yes" if r.is_shortlisted else "No"),
            ),
            (
                "Rank at shortlist decision",
                f"#{r.rank_position_at_decision}"
                if r.rank_position_at_decision is not None else "—",
            ),
        )
    )
    if r.shortlist_reason:
        st.caption(f"HR note at shortlisting: {r.shortlist_reason}")

    # --- WHAT IS MISSING --------------------------------------------
    if view.missing_sources:
        st.divider()
        st.markdown("### Not yet available")
        st.caption(
            "These sources have not been produced for this candidate. Nothing "
            "below has been estimated or filled in."
        )
        for src in view.missing_sources:
            st.markdown(f"- {src}")


def _render_final_scorecard_section(application_id, acting_user_id) -> None:
    """The per-candidate "Final scorecard" block.

    Loaded **on demand** rather than on every rerun. The assembly issues around
    eight guarded reads per candidate, and this page renders every shortlisted
    candidate at once — the same cost reasoning behind the Candidates page's
    lazy application cards. Nothing here makes an AI call.
    """
    key = _scorecard_open_key(application_id)
    is_open = bool(st.session_state.get(key))

    if st.button(
        "Hide final scorecard" if is_open else "Show final scorecard",
        key=f"fsc_btn_{application_id}",
    ):
        st.session_state[key] = not is_open
        st.rerun()

    if not is_open:
        st.caption(
            "The consolidated scorecard for the hiring manager — assembled from "
            "evidence already on record. Loaded when you open it."
        )
        return

    try:
        with session_scope() as db:
            view = get_final_scorecard(
                db, application_id, acting_user_id=acting_user_id
            )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except FinalScorecardActorError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        load_error("Couldn't load the final scorecard right now.")
        return

    if view is None:
        load_error("Couldn't find this application.")
        return

    with st.container(border=True):
        _render_final_scorecard(view)


def _render_candidate_row(
    row, guide, feedback_state, analysis_state, acting_user_id
) -> None:
    drift = ""
    if row.current_rank_available and row.current_rank_position is not None:
        if (
            row.rank_position_at_decision is not None
            and row.current_rank_position != row.rank_position_at_decision
        ):
            drift = "  " + entity_badge(
                "ranking",
                "caution",
                f"Ranking changed — shortlisted at "
                f"#{row.rank_position_at_decision}, now "
                f"#{row.current_rank_position}",
            )
        else:
            drift = f"  ·  currently #{row.current_rank_position}"
    elif not row.current_rank_available:
        drift = "  ·  " + entity_badge(
            "ranking", "neutral", "Ranking not regenerated since shortlisting"
        )
    else:  # ranked row exists but position is NULL (ineligible)
        drift = "  ·  " + entity_badge("ranking", "neutral", "Not currently ranked")

    at = (
        f"shortlisted at #{row.rank_position_at_decision}"
        if row.rank_position_at_decision is not None
        else "shortlisted (was not ranked)"
    )
    st.markdown(f"**{row.candidate_name}**  ·  {at}{drift}")
    st.caption(row.candidate_email)
    if row.shortlist_reason:
        st.caption(f"HR note: {row.shortlist_reason}")

    label = (
        "Regenerate interview guide" if row.guide_exists
        else "Generate interview guide"
    )
    if st.button(label, key=f"guide_{row.application_id}"):
        _run_generate_guide(
            str(row.application_id), acting_user_id, force=row.guide_exists
        )

    if guide is not None:
        with st.expander("Interview guide", expanded=not row.guide_exists):
            _render_guide(guide)
    _render_feedback_section(row.application_id, feedback_state, acting_user_id)
    _render_analysis_section(
        row.application_id, feedback_state, analysis_state, acting_user_id
    )
    _render_final_scorecard_section(row.application_id, acting_user_id)
    st.divider()


def _render_shortlisted_section(job_id: str, acting_user_id) -> None:
    st.subheader("Shortlisted candidates")
    try:
        view = _load_view(job_id, acting_user_id)
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except SQLAlchemyError:
        load_error("Couldn't load the shortlist right now.")
        return

    shortlisted = view["shortlisted"]
    guides = view["guides_by_application"]
    feedback_state = view["feedback_by_application"]
    analysis_state = view["analysis_by_application"]

    if not shortlisted:
        st.caption("No candidates are currently shortlisted for this job.")
    else:
        # group by rubric version, preserving the service's sort order
        _unset = object()
        current_version = _unset
        for row in shortlisted:
            if current_version is _unset or row.rubric_version_number != current_version:
                current_version = row.rubric_version_number
                label = (
                    f"Rubric v{row.rubric_version_number}"
                    if row.rubric_version_number is not None
                    else "Rubric (unknown version)"
                )
                status = (
                    f" — {label_for(row.rubric_version_status)}"
                    if row.rubric_version_status
                    and row.rubric_version_status != "APPROVED"
                    else ""
                )
                st.markdown(f"#### {label}{status}")
                st.caption(
                    "Guide actions in this section are grounded in this rubric "
                    "version — never merged with candidates from another version."
                )
            with st.container(border=True):
                _render_candidate_row(
                    row, guides.get(row.application_id), feedback_state,
                    analysis_state, acting_user_id,
                )

    # Guides retained for candidates no longer shortlisted — readable, but no
    # new generation is offered.
    retained = [
        g for aid, g in guides.items()
        if not shortlisted or aid not in {r.application_id for r in shortlisted}
    ]
    if retained:
        st.subheader("Retained guides — candidates no longer shortlisted")
        st.caption(
            "These candidates have since been removed from the shortlist. Their "
            "previously generated guide stays readable; a new guide cannot be "
            "generated unless they are shortlisted again."
        )
        for g in retained:
            with st.container(border=True):
                st.info(
                    "Not currently shortlisted — interview-guide generation is "
                    "blocked for this candidate. Interview feedback can still "
                    "be recorded: an interview that happened stays recordable."
                )
                with st.expander("Interview guide", expanded=False):
                    _render_guide(g)
                _render_feedback_section(
                    g.application_id, feedback_state, acting_user_id
                )
                _render_analysis_section(
                    g.application_id, feedback_state, analysis_state,
                    acting_user_id,
                )
                _render_final_scorecard_section(
                    g.application_id, acting_user_id
                )


def render_interviews_page() -> None:
    current = get_current_user(st.session_state)
    if current is None:  # defensive: gate is in main.py
        st.error("Please sign in.")
        return

    try:
        acting_user_id = uuid.UUID(current["id"])
    except (ValueError, KeyError, TypeError):
        st.error("Your session looks invalid — please sign out and back in.")
        return

    st.title("Interviews")
    st.caption(
        "Interview guides for shortlisted candidates. The MVP has no live AI "
        "interviewer — the guide is generated here for a human interviewer to "
        "run the interview."
    )

    try:
        jobs = load_job_options()
    except SQLAlchemyError:
        load_error("Couldn't load jobs right now.")
        return
    if not jobs:
        st.caption("No jobs yet — create one on the Jobs page first.")
        return

    job_id = job_picker(jobs, key=HR_JOB_PICKER_KEY)
    if job_id is None:
        return
    _render_shortlisted_section(job_id, acting_user_id)
