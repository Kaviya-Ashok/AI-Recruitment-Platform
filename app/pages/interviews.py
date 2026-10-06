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

* Increment B: each recorded round can carry ONE interview transcript (PDF or
  DOCX, stored in Google Drive). It can be attached from the feedback form
  (saved as a separate step AFTER the feedback commits, so a failed upload never
  loses the feedback) or later from the round's history, where it can also be
  replaced. Replaced versions are kept. Transcripts are only stored and shown
  here — no AI reads them.

* Step 10b: a per-job "Final ranking (post-interview)" section. HR triggers it
  explicitly; it shows final score = 40% screening + 60% interview (all rounds
  equal), with the breakdown, caveats, ties, the ineligible and incomplete lists,
  a stale banner and earlier runs. No AI is involved and nothing is rejected.

* Step 11: under each candidate's final scorecard, a "Final human decision"
  section — the hiring manager's PROCEED / HOLD / REJECT with a required
  rationale. Only a hiring manager or an admin can record one (the service
  enforces it; the page merely hides the form from others). Recording is all it
  does: no status change, no notification. No AI is involved.

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
from app.database.models.final_decision import FinalDecisionStatus
from app.database.models.final_ranking import FinalRankingEntryStatus
from app.database.models.interview_guide import InterviewQuestionCategory
from app.database.models.interview_transcript import InterviewTranscriptStatus
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysisStatus,
)
from app.database.models.screening_evaluation import ScreeningRecommendation
from app.services.final_decision_service import (
    RATIONALE_MAX_CHARS,
    RATIONALE_MIN_CHARS,
    FinalDecisionError,
    get_current_final_decision,
    get_final_decision_staleness,
    list_current_decisions_for_job,
    list_final_decision_history,
    record_final_decision,
    role_may_decide,
)
from app.services.final_ranking_service import (
    FinalRankingError,
    generate_final_ranking,
    get_current_final_ranking,
    get_final_ranking_staleness,
    list_final_ranking_history,
)
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
from app.services.interview_transcript_service import (
    InterviewTranscriptError,
    attach_interview_transcript,
    get_transcript_download_bytes,
    list_transcripts_for_feedback,
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
    confirmed,
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
_TRANSCRIPT_DB_ERROR = (
    "Couldn't save the transcript — please try again."
)
_TRANSCRIPT_UNEXPECTED = (
    "Something went wrong attaching the transcript. Please try again."
)
_TRANSCRIPT_UPLOAD_LABEL = "Interview transcript (PDF or DOCX, up to 10 MB)"
_TRANSCRIPT_NOT_EXTRACTABLE = (
    "This file has no readable text (it is probably a scan). It can be "
    "stored, but an AI analysis will not be able to use it."
)
_DECISION_DB_ERROR = "Couldn't save the final decision — please try again."
_DECISION_UNEXPECTED = (
    "Something went wrong recording the final decision. Please try again."
)
_DECISION_CHOOSE = "Choose Proceed, Hold or Reject."
_DECISION_CONFIRM_FIRST = (
    "Please confirm that you have reviewed the scorecard, interview feedback "
    "and analysis before recording the decision."
)
_DECISION_RECORDED_ONLY = (
    "Recorded only. This does not notify the candidate or change any status."
)
_DECISION_EVIDENCE_ONLY = "Base the decision on job-relevant evidence only."
_DECISION_STALE = "Based on earlier evidence — "
_DECISION_ORDER = (
    ScreeningRecommendation.PROCEED,
    ScreeningRecommendation.HOLD,
    ScreeningRecommendation.REJECT,
)
_FINAL_RANK_DB_ERROR = "Couldn't save the final ranking — please try again."
_FINAL_RANK_UNEXPECTED = (
    "Something went wrong generating the final ranking. Please try again."
)
_FINAL_RANK_STALE_HINT = "Regenerate before relying on the order."
_FINAL_RANK_TIE_CAPTION = (
    "Tied candidates share a rank. The on-screen order within a tie carries no "
    "meaning."
)
_ANALYSIS_STALE = (
    "Feedback or a transcript was added or replaced after this analysis was "
    "generated. Regenerate to include it."
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
        feedback = {}
        for app_id in application_ids:
            history = list_feedback_views(
                db, app_id, acting_user_id=acting_user_id
            )
            feedback[app_id] = {
                "context": get_feedback_context(
                    db, app_id, acting_user_id=acting_user_id
                ),
                "history": history,
                # {feedback_id: [every transcript version, newest first]}
                "transcripts": {
                    item.feedback_id: list_transcripts_for_feedback(
                        db, item.feedback_id, acting_user_id=acting_user_id
                    )
                    for item in history
                },
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
        # Step 11: the final human decision, its history and whether it rests on
        # earlier evidence. Plain SELECTs — nothing is written on page load.
        decisions = {
            app_id: {
                "current": get_current_final_decision(
                    db, app_id, acting_user_id=acting_user_id
                ),
                "history": list_final_decision_history(
                    db, app_id, acting_user_id=acting_user_id
                ),
                "staleness": get_final_decision_staleness(
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
            "decision_by_application": decisions,
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
    transcript: tuple[str, bytes] | None = None,
) -> None:
    """Submit one interview write-up. Same four-way exception ladder as the
    guide action — a validation problem shows its own readable message, and a
    traceback never reaches the page.

    ``transcript`` is an optional ``(file name, bytes)``. It is attached only
    AFTER the feedback commit succeeded, as a SEPARATE transaction: a failed
    upload never loses or blocks the feedback (which is immutable once saved).
    """
    try:
        with session_scope() as db:
            feedback = create_interview_feedback(
                db,
                user_id=acting_user_id,
                application_id=uuid.UUID(application_id),
                interview_guide_id=uuid.UUID(interview_guide_id),
                interview_round=interview_round,
                recommendation=recommendation,
                notes=notes,
                ratings=ratings,
            )
            feedback_id = feedback.id
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

    if transcript is not None:
        problem = _attach_transcript(
            feedback_id, transcript[1], transcript[0], acting_user_id
        )
        if problem is not None:
            # st.rerun() would wipe an inline message, so park it for the
            # section to show on the next run (temporary UI state only).
            st.session_state[_transcript_notice_key(application_id)] = (
                "The interview feedback was saved, but the transcript could "
                f"not be attached: {problem} You can attach it from this "
                "round's history below."
            )

    success_toast("Interview feedback recorded.")
    st.rerun()


def _transcript_notice_key(application_id) -> str:
    return f"interviews_transcript_notice_{application_id}"


def _attach_transcript(
    feedback_id, file_bytes: bytes, file_name: str, acting_user_id
) -> str | None:
    """Attach (or replace) a round's transcript in its own transaction. Returns
    ``None`` on success, otherwise a short user-safe reason. Never raises."""
    try:
        with session_scope() as db:
            attach_interview_transcript(
                db,
                interview_feedback_id=feedback_id,
                file_bytes=file_bytes,
                original_filename=file_name,
                acting_user_id=acting_user_id,
            )
    except UnauthorizedError:
        return "Your account is no longer active — please contact an admin."
    except InterviewTranscriptError as exc:
        return str(exc)
    except SQLAlchemyError:
        return _TRANSCRIPT_DB_ERROR
    except Exception:  # noqa: BLE001 - never surface a traceback
        return _TRANSCRIPT_UNEXPECTED
    return None


def _run_attach_transcript(
    feedback_id, file_bytes: bytes, file_name: str, acting_user_id
) -> None:
    """The history's attach/replace button."""
    with st.spinner("Uploading the transcript…"):
        problem = _attach_transcript(
            feedback_id, file_bytes, file_name, acting_user_id
        )
    if problem is not None:
        st.error(problem)
        return
    # A fresh uploader key on the next run empties the file picker.
    version_key = f"interviews_transcript_uploader_{feedback_id}"
    st.session_state[version_key] = st.session_state.get(version_key, 0) + 1
    success_toast("Transcript attached.")
    st.rerun()


def _transcript_bytes_fn(transcript_id, acting_user_id):
    """Deferred data for ``st.download_button``: Drive is only contacted when
    the button is actually clicked, never on page load or rerun."""

    def _fetch() -> bytes:
        with session_scope() as db:
            return get_transcript_download_bytes(
                db, transcript_id, acting_user_id=acting_user_id
            ).content

    return _fetch


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


def _transcript_line(version) -> str:
    kb = max(1, round(version.file_size_bytes / 1024))
    return (
        f"Version {version.version_number} · uploaded "
        f"{version.created_at:%Y-%m-%d} by {version.uploaded_by_name} · {kb} KB"
    )


def _render_transcript_block(feedback_id, versions: list, acting_user_id) -> None:
    """One round's transcript: the current file, any earlier versions, and an
    attach/replace control. Stored and shown only — no AI reads transcripts."""
    current = next(
        (v for v in versions if v.status == InterviewTranscriptStatus.CURRENT),
        None,
    )
    earlier = [v for v in versions if v is not current]

    st.markdown("*Interview transcript*")
    if current is None:
        st.caption("No transcript attached.")
    else:
        st.markdown(f"`{current.file_name}`")
        st.caption(_transcript_line(current))
        if not current.text_extractable:
            st.warning(_TRANSCRIPT_NOT_EXTRACTABLE)
        st.download_button(
            "Download transcript",
            data=_transcript_bytes_fn(current.transcript_id, acting_user_id),
            file_name=current.file_name,
            mime=current.mime_type,
            key=f"interviews_transcript_dl_{current.transcript_id}",
        )

    if earlier:
        # A popover, not an expander: this block already sits inside the
        # feedback expander and Streamlit does not allow expanders to nest.
        with st.popover(f"Earlier versions ({len(earlier)})"):
            st.caption(
                "Kept, never deleted. Each was the current transcript until it "
                "was replaced."
            )
            for old in earlier:
                st.markdown(f"`{old.file_name}`")
                st.caption(_transcript_line(old))
                st.download_button(
                    "Download",
                    data=_transcript_bytes_fn(old.transcript_id, acting_user_id),
                    file_name=old.file_name,
                    mime=old.mime_type,
                    key=f"interviews_transcript_dl_{old.transcript_id}",
                )

    version_key = f"interviews_transcript_uploader_{feedback_id}"
    upload = st.file_uploader(
        (
            "Replace transcript (PDF or DOCX, up to 10 MB)"
            if current is not None
            else "Attach transcript (PDF or DOCX, up to 10 MB)"
        ),
        type=["pdf", "docx"],
        key=f"{version_key}_{st.session_state.get(version_key, 0)}",
    )
    if st.button(
        "Replace transcript" if current is not None else "Attach transcript",
        key=f"interviews_transcript_btn_{feedback_id}",
        disabled=upload is None,
    ):
        _run_attach_transcript(
            feedback_id, upload.getvalue(), upload.name, acting_user_id
        )


def _render_feedback_history(
    history: list, transcripts: dict | None = None, acting_user_id=None
) -> None:
    """Prior write-ups, newest first. Read-only and never edited: CLAUDE.md §7
    requires the original human feedback to be preserved as written.

    ``transcripts`` maps ``feedback_id`` to that round's transcript versions;
    when it is ``None`` (the pre-Increment-B call shape) no transcript block is
    drawn at all."""
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
            if transcripts is not None:
                _render_transcript_block(
                    item.feedback_id,
                    transcripts.get(item.feedback_id, []),
                    acting_user_id,
                )


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
        transcript_file = st.file_uploader(
            _TRANSCRIPT_UPLOAD_LABEL,
            type=["pdf", "docx"],
            key=f"fb_transcript_{app_id}",
            help=(
                "Optional. Stored in Google Drive and linked to this round. "
                "The feedback is saved first, so a problem with the file "
                "never loses it."
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
                transcript=(
                    (transcript_file.name, transcript_file.getvalue())
                    if transcript_file is not None
                    else None
                ),
            )


def _render_feedback_section(application_id, feedback_state, acting_user_id) -> None:
    """The whole "Human interview feedback" block for one candidate."""
    state = (feedback_state or {}).get(application_id) or {}
    context = state.get("context")
    history = state.get("history") or []
    transcripts = state.get("transcripts")

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
        notice = st.session_state.pop(
            _transcript_notice_key(application_id), None
        )
        if notice:
            st.warning(notice)
        _render_feedback_history(history, transcripts, acting_user_id)
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


def _rounds_phrase(rounds) -> str:
    """``round 1`` / ``rounds 1 and 2`` / ``rounds 1, 2 and 3``."""
    ordered = sorted({int(r) for r in rounds})
    if not ordered:
        return "no rounds"
    if len(ordered) == 1:
        return f"round {ordered[0]}"
    return (
        "rounds " + ", ".join(str(r) for r in ordered[:-1]) + f" and {ordered[-1]}"
    )


def _analysis_scope_text(feedback_rounds, transcript_rounds, legacy: bool) -> str:
    """What a post-interview analysis actually read, in words. Driven by the
    stored provenance — never by anything the AI said.

    ``legacy`` is a pre-Increment-C row: it read only the most recent feedback
    record and no transcripts."""
    if legacy:
        latest = (
            f" (Round {feedback_rounds[0]})" if len(feedback_rounds) == 1 else ""
        )
        return (
            "This earlier analysis read only the most recent feedback record"
            f"{latest} and no transcripts."
        )
    feedback = (
        f"interviewer feedback for {_rounds_phrase(feedback_rounds)}"
        if feedback_rounds else "the recorded interviewer feedback"
    )
    if transcript_rounds:
        return (
            f"Based on {feedback} and the interview transcripts for "
            f"{_rounds_phrase(transcript_rounds)}."
        )
    return (
        f"Based on {feedback}. No interview transcript was available when "
        "this analysis was generated."
    )


def _analysis_is_stale(analysis, feedback_state: dict | None) -> bool:
    """True if the application's feedback records or CURRENT readable transcripts
    differ from the sets this analysis recorded.

    Transcripts flagged as having no readable text are excluded on the current
    side: they can never be used, so a scan would otherwise make every analysis
    look permanently stale. A replaced scan is therefore not flagged. Views that
    carry no provenance (older shapes) are never reported stale.
    """
    if not hasattr(analysis, "feedback_records"):
        return False
    state = feedback_state or {}
    current_feedback = {
        getattr(item, "feedback_id", None) for item in state.get("history") or []
    }
    recorded_feedback = {r.feedback_id for r in analysis.feedback_records}
    if current_feedback != recorded_feedback:
        return True
    transcripts = state.get("transcripts")
    if transcripts is None:
        return False
    current_transcripts = {
        v.transcript_id
        for versions in transcripts.values()
        for v in versions
        if v.status == InterviewTranscriptStatus.CURRENT and v.text_extractable
    }
    recorded_transcripts = {r.transcript_id for r in analysis.transcript_records}
    return current_transcripts != recorded_transcripts


def _load_final_ranking_view(job_id: str, acting_user_id) -> dict:
    with session_scope() as db:
        return {
            "current": get_current_final_ranking(
                db, job_id=job_id, acting_user_id=acting_user_id
            ),
            "history": list_final_ranking_history(
                db, job_id=job_id, acting_user_id=acting_user_id
            ),
            "staleness": get_final_ranking_staleness(
                db, job_id=job_id, acting_user_id=acting_user_id
            ),
            # Step 11: read-only "Decision: …" line on each row.
            "decisions": list_current_decisions_for_job(
                db, job_id, acting_user_id=acting_user_id
            ),
        }


def _run_generate_final_ranking(job_id: str, acting_user_id) -> None:
    """Explicit HR trigger only — never run on page load. Same four-way
    exception ladder as the other HR actions."""
    try:
        with st.spinner("Calculating the final ranking…"):
            with session_scope() as db:
                generate_final_ranking(
                    db, job_id=uuid.UUID(job_id), requested_by_user_id=acting_user_id
                )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except FinalRankingError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_FINAL_RANK_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_FINAL_RANK_UNEXPECTED)
        return

    success_toast("Final ranking generated.")
    st.rerun()


def _render_final_entry_breakdown(entry, run) -> None:
    """The numbers behind one score, so it is never shown bare."""
    if entry.screening_score is not None and entry.interview_score is not None:
        st.caption(
            f"Breakdown: {_weight_pct(run.screening_weight)} × screening "
            f"{_d2(entry.screening_score)} + "
            f"{_weight_pct(run.interview_weight)} × interview "
            f"{_d2(entry.interview_score)}"
            + (f" = {_d2(entry.final_score)}" if entry.final_score is not None else "")
            + ". Screening and interview scores are each rounded to 2 decimals "
            "before weighting."
        )
    else:
        st.caption(
            f"Screening score {_d2(entry.screening_score)} · interview score "
            f"{_d2(entry.interview_score)}"
        )
    if entry.round_means:
        parts = ", ".join(
            f"round {r}: mean {m:.2f}/5" for r, m in entry.round_means
        )
        st.caption(
            f"Interview rounds used — {parts}. Every round counts equally; "
            "rounds without ratings are not scored."
        )
    if entry.screening_rank is not None:
        st.caption(f"Pre-interview screening rank: #{entry.screening_rank}.")


def _render_final_entry_context(entry) -> None:
    """Analysis pointers (AI-generated, context only) and the confidence note."""
    if entry.final_confidence:
        st.markdown(
            badge(
                confidence_kind(entry.final_confidence),
                f"Confidence: {label_for(entry.final_confidence)}",
            )
        )
        st.caption(
            "Confidence is the lowest of the screening confidence "
            f"({label_for(entry.screening_confidence) if entry.screening_confidence else 'unknown'})"
            " and the post-interview analysis confidence; with no analysis it is "
            "capped at Medium. It is not part of the score."
        )
    if entry.analysis_recommendation:
        st.caption(
            "AI-generated post-interview analysis (context only — it is NOT part "
            f"of the score): confidence {label_for(entry.analysis_confidence)}, "
            f"recommends {label_for(entry.analysis_recommendation)}. See the "
            "analysis on the candidate's card."
        )
    else:
        st.caption(
            "No post-interview analysis counted for this candidate (none "
            "generated, or generated for a different rubric version)."
        )


def _decision_line(decision) -> str:
    """A small read-only line for a final-ranking row."""
    if decision is None:
        return "Decision: No final decision yet"
    return (
        f"Decision: {label_for(decision.decision)} "
        f"({decision.decided_by_name}, {decision.created_at:%Y-%m-%d})"
    )


def _render_final_ranked_row(entry, run, decision=None) -> None:
    tie = (
        "  " + entity_badge("ranking", "caution", "Tied — shares this rank")
        if entry.tied else ""
    )
    st.markdown(
        f"**#{entry.rank}. {entry.candidate_name}**  ·  final "
        f"{score_out_of_ten(entry.final_score, places=2)}{tie}"
    )
    st.caption(entry.candidate_email)
    if entry.mandatory_unknown:
        st.warning(
            "⚠️ A mandatory requirement had no evidence either way for this "
            "candidate — the rank reflects only what could be assessed. Not a "
            "failure; worth confirming at interview.",
            icon="⚠️",
        )
    _render_final_entry_breakdown(entry, run)
    _render_final_entry_context(entry)
    if entry.status_reason:
        st.caption(entry.status_reason)
    st.caption(_decision_line(decision))
    st.divider()


def _render_final_unranked_row(entry, run, decision=None) -> None:
    ineligible = entry.entry_status == FinalRankingEntryStatus.NOT_RANKED_INELIGIBLE
    label = (
        "Not ranked — mandatory requirement not met" if ineligible
        else "Incomplete — no final score"
    )
    st.markdown(
        f"**{entry.candidate_name}**  "
        + entity_badge("ranking", "negative" if ineligible else "neutral", label)
        + (
            f"  ·  final (for context) "
            f"{score_out_of_ten(entry.final_score, places=2)}"
            if entry.final_score is not None else ""
        )
    )
    st.caption(entry.candidate_email)
    _render_final_entry_breakdown(entry, run)
    if entry.final_score is not None:
        _render_final_entry_context(entry)
    st.caption(entry.status_reason)
    st.caption(_decision_line(decision))
    st.divider()


def _render_final_ranking_section(job_id: str, acting_user_id) -> None:
    """Per-job post-interview final ranking (Step 10b). Thin glue — every number
    comes from ``final_ranking_service``; nothing is calculated here."""
    st.subheader("Final ranking (post-interview)")
    st.caption(
        "Final score = 40% screening score + 60% interview score, on a 0–10 "
        "scale. The interview score averages every round's ratings, each round "
        "counting equally. Calculated by fixed rules — the AI never assigns a "
        "score, transcripts do not change the number, and the AI analysis and "
        "recommendations are shown as context only. This ranking does not "
        "reject or approve anyone: the hiring manager decides."
    )

    try:
        view = _load_final_ranking_view(job_id, acting_user_id)
    except (UnauthorizedError, SQLAlchemyError, ValueError):
        load_error("Couldn't load the final ranking right now.")
        return

    current = view["current"]
    decisions = view.get("decisions") or {}
    if st.button(
        "Regenerate final ranking" if current else "Generate final ranking",
        key=f"final_rank_{job_id}",
        help="Scores and ranks every candidate who has interview feedback.",
    ):
        _run_generate_final_ranking(job_id, acting_user_id)

    if not current:
        st.caption("No final ranking has been generated for this job yet.")
        return

    staleness = view["staleness"]
    if staleness.is_stale:
        st.markdown(
            entity_badge(
                "ranking",
                "caution",
                "Final ranking may be out of date — "
                + "; ".join(staleness.reasons),
            )
        )
        st.caption(_FINAL_RANK_STALE_HINT)

    for run in current:
        version = (
            f"Rubric v{run.rubric_version_number}"
            if run.rubric_version_number is not None
            else "Rubric (unknown version)"
        )
        st.markdown(f"#### {version}")
        st.caption(
            f"Generated {run.created_at:%Y-%m-%d %H:%M UTC} by "
            f"{run.requested_by_name} · weights: screening "
            f"{_weight_pct(run.screening_weight)}, interview "
            f"{_weight_pct(run.interview_weight)}. Candidates scored against "
            "different rubric versions are never ranked together."
        )
        ranked = [
            e for e in run.entries
            if e.entry_status == FinalRankingEntryStatus.RANKED
        ]
        ineligible = [
            e for e in run.entries
            if e.entry_status == FinalRankingEntryStatus.NOT_RANKED_INELIGIBLE
        ]
        incomplete = [
            e for e in run.entries
            if e.entry_status in (
                FinalRankingEntryStatus.INCOMPLETE_SCREENING,
                FinalRankingEntryStatus.INCOMPLETE_INTERVIEW,
            )
        ]
        if any(e.tied for e in ranked):
            st.caption(_FINAL_RANK_TIE_CAPTION)
        if not ranked:
            st.caption("No candidate in this group could be ranked.")
        for e in ranked:
            _render_final_ranked_row(e, run, decisions.get(e.application_id))
        if ineligible:
            st.markdown(f"##### Not ranked — ineligible ({len(ineligible)})")
            st.caption(
                "A mandatory requirement was assessed as not met at screening. "
                "Scores are shown for context only. This is not a rejection; "
                "that remains a human decision."
            )
            for e in ineligible:
                _render_final_unranked_row(e, run, decisions.get(e.application_id))
        if incomplete:
            st.markdown(f"##### Incomplete ({len(incomplete)})")
            st.caption(
                "No final score — a part is missing. Nothing is scored as zero "
                "and the weights are never redistributed."
            )
            for e in incomplete:
                _render_final_unranked_row(e, run, decisions.get(e.application_id))

    earlier = [h for h in view["history"] if h.status != "CURRENT"]
    if earlier:
        with st.expander(f"Earlier runs ({len(earlier)})", expanded=False):
            st.caption(
                "Kept, never overwritten. Each was the current final ranking "
                "until it was regenerated."
            )
            for h in earlier:
                ranked_n = sum(
                    1 for e in h.entries
                    if e.entry_status == FinalRankingEntryStatus.RANKED
                )
                when = (
                    f"{h.superseded_at:%Y-%m-%d}" if h.superseded_at else "unknown date"
                )
                top = next(
                    (e for e in h.entries
                     if e.entry_status == FinalRankingEntryStatus.RANKED),
                    None,
                )
                st.markdown(
                    f"- Rubric v{h.rubric_version_number} · generated "
                    f"{h.created_at:%Y-%m-%d %H:%M} by {h.requested_by_name} · "
                    f"superseded {when} · {ranked_n} ranked"
                    + (
                        f" · top: {top.candidate_name} "
                        f"({_d2(top.final_score)})" if top else ""
                    )
                )


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

    transcript_notes = getattr(analysis, "transcript_evidence_notes", "") or ""
    if transcript_notes:
        st.markdown("*From the interview transcripts (AI-generated)*")
        st.caption(ai_provenance(analysis.created_at))
        st.markdown(transcript_notes)
    for round_number in getattr(analysis, "transcript_unreadable_rounds", ()) or ():
        st.warning(
            f"The transcript for Round {round_number} had no readable text "
            "(probably a scan) and was not used in this analysis."
        )


def _render_analysis(analysis, stale: bool = False) -> None:
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
    records = getattr(analysis, "feedback_records", ()) or ()
    st.caption(
        _analysis_scope_text(
            [r.interview_round for r in records],
            [
                r.interview_round
                for r in getattr(analysis, "transcript_records", ()) or ()
            ],
            bool(analysis.analyzed_only_latest_feedback),
        )
    )
    if stale:
        st.warning(_ANALYSIS_STALE)

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
    if records:
        st.markdown(
            "Interviewer recommendations: "
            + "  ".join(
                badge(
                    recommendation_kind(r.recommendation_snapshot),
                    f"Round {r.interview_round} "
                    f"{label_for(r.recommendation_snapshot)}",
                )
                for r in records
            )
        )
        if analysis.analyzed_only_latest_feedback:
            st.caption(
                "Shown as recorded, for context. This earlier analysis could "
                "see the interviewer's recommendation when it was generated. "
                "No comparison is made."
            )
        else:
            st.caption(
                "Shown as recorded, for context. The AI analysis did not see "
                "these, and no comparison is made."
            )
    else:
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
            _render_analysis(
                current,
                stale=_analysis_is_stale(
                    current, (feedback_state or {}).get(application_id)
                ),
            )
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


def _d2(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def _weight_pct(weight) -> str:
    return f"{int(round(float(weight) * 100))}%"


def _render_scorecard_final_ranking(final) -> None:
    """The candidate's entry in the latest final ranking, read as stored."""
    st.divider()
    st.markdown("### Final score and rank (post-interview)")
    if final is None:
        st.caption("Final ranking not generated yet.")
        return
    st.caption(
        f"System-calculated, read from the final ranking generated "
        f"{final.generated_at:%Y-%m-%d %H:%M UTC}. Not AI-produced, and not a "
        "hiring decision."
    )
    if final.entry_status == FinalRankingEntryStatus.RANKED:
        tie = " (tied)" if final.tied else ""
        st.markdown(
            f"**Final score:** {score_out_of_ten(final.final_score, places=2)}"
            f" · rank #{final.rank} of {final.ranked_count}{tie}"
        )
    elif final.final_score is not None:
        st.markdown(
            f"**Final score (not ranked):** "
            f"{score_out_of_ten(final.final_score, places=2)}"
        )
    else:
        st.markdown("**No final score**")
    if final.screening_score is not None and final.interview_score is not None:
        st.caption(
            f"{_weight_pct(final.screening_weight)} × screening "
            f"{_d2(final.screening_score)} + "
            f"{_weight_pct(final.interview_weight)} × interview "
            f"{_d2(final.interview_score)}."
        )
    if final.final_confidence:
        st.markdown(
            badge(
                confidence_kind(final.final_confidence),
                f"Confidence: {label_for(final.final_confidence)}",
            )
        )
    if final.status_reason:
        st.caption(final.status_reason)
    if final.tied:
        st.caption(_FINAL_RANK_TIE_CAPTION)


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
        f"**{bucket.label}: "
        f"{score_out_of_ten(bucket.score, places=getattr(bucket, 'score_places', 0))}"
        f"**{coverage}"
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
        st.caption(
            _analysis_scope_text(
                getattr(view, "post_interview_feedback_rounds", ()) or (),
                getattr(view, "post_interview_transcript_rounds", ()) or (),
                bool(getattr(view, "post_interview_read_latest_only", False)),
            )
        )
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
        transcript_name = getattr(view, "interview_transcript_file_name", None)
        if transcript_name:
            st.caption(f"Interview transcript attached: {transcript_name}")
        else:
            st.caption("No interview transcript attached to this round.")
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
        "Interview score is the mean of each round's mean competency rating "
        f"(every round counts equally), as a proportion of the {RATING_MAX}-point "
        "scale, expressed out of 10 and rounded half-up to 2 decimals. "
        "Confidence is not part of any score."
    )
    rounds_used = getattr(view, "interview_rounds_used", ()) or ()
    if rounds_used:
        st.caption(
            f"The Interview score uses {_rounds_phrase(rounds_used)}, every "
            "round counting equally."
        )
    unscored = getattr(view, "interview_rounds_unscored", ()) or ()
    if unscored:
        st.caption(
            f"Not scored (feedback without ratings, never counted as zero): "
            f"{_rounds_phrase(unscored)}."
        )

    _render_scorecard_final_ranking(getattr(view, "final_ranking", None))

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
    recorded = getattr(view, "final_decision", None)
    if recorded is not None:
        st.markdown(
            badge(
                recommendation_kind(recorded.decision),
                f"Final decision: {label_for(recorded.decision)}",
            )
        )
        st.caption(
            f"Recorded by {recorded.decided_by_name} on "
            f"{recorded.decided_at:%Y-%m-%d}. The rationale is in this "
            "candidate's Final human decision section. Recorded only — nothing "
            "else was changed."
        )
    else:
        st.markdown(f"**{view.final_decision_status}**")
        st.caption(
            "No final decision has been recorded. Nothing on this page makes, "
            "proposes, or infers it."
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


def _run_record_final_decision(
    application_id: str, decision: str, rationale: str, acting_user_id
) -> None:
    """Record (or revise) the final decision. Same four-way exception ladder as
    the other HR actions. Never an AI call."""
    try:
        with session_scope() as db:
            record_final_decision(
                db,
                application_id=uuid.UUID(application_id),
                decision=decision,
                rationale=rationale,
                acting_user_id=acting_user_id,
            )
    except UnauthorizedError:
        st.error("Your account is no longer active — please contact an admin.")
        return
    except FinalDecisionError as exc:
        st.error(str(exc))
        return
    except SQLAlchemyError:
        st.error(_DECISION_DB_ERROR)
        return
    except Exception:  # noqa: BLE001 - never surface a traceback
        st.error(_DECISION_UNEXPECTED)
        return

    # A fresh form key on the next run empties the rationale box.
    key = f"fd_ver_{application_id}"
    st.session_state[key] = st.session_state.get(key, 0) + 1
    success_toast("Final decision recorded.")
    st.rerun()


def _decision_badge(decision: str) -> str:
    return badge(
        recommendation_kind(decision), f"Final decision: {label_for(decision)}"
    )


def _render_final_decision_section(
    application_id, decision_state, acting_user_id
) -> None:
    """The per-candidate "Final human decision" block (CLAUDE.md §11).

    Shows the current decision (label + who + when + rationale), whether it rests
    on earlier evidence, earlier decisions, and — for a hiring manager or admin —
    the form to record or change it. Everyone else sees it read-only.
    """
    state = (decision_state or {}).get(application_id) or {}
    current = state.get("current")
    history = state.get("history") or []
    staleness = state.get("staleness")

    st.markdown("#### Final human decision")
    st.caption(
        "The hiring manager's decision (PROCEED / HOLD / REJECT). The AI does "
        "not decide — scores, rankings and analyses are inputs only."
    )

    if not state:
        st.caption("Decision details are not available for this candidate.")
        return

    if current is None:
        st.caption("No final decision has been recorded yet.")
    else:
        st.markdown(_decision_badge(current.decision))
        st.caption(
            f"Decided by {current.decided_by_name} on "
            f"{current.created_at:%Y-%m-%d %H:%M UTC}."
        )
        st.markdown("*Rationale*")
        st.markdown(current.rationale)
        if staleness is not None and staleness.is_stale:
            st.warning(
                _DECISION_STALE + "; ".join(staleness.reasons) + ". Review the "
                "current scorecard and record a new decision if it changes your "
                "view."
            )

    earlier = [h for h in history if h.status != FinalDecisionStatus.CURRENT]
    if earlier:
        with st.expander(f"Earlier decisions ({len(earlier)})", expanded=False):
            st.caption(
                "Kept, never overwritten. Each was the current decision until it "
                "was changed."
            )
            for h in earlier:
                st.markdown(_decision_badge(h.decision))
                when = (
                    f"{h.superseded_at:%Y-%m-%d}" if h.superseded_at else "unknown date"
                )
                st.caption(
                    f"Decided by {h.decided_by_name} on {h.created_at:%Y-%m-%d} · "
                    f"replaced {when}"
                )
                st.markdown(h.rationale)
                st.divider()

    role = (get_current_user(st.session_state) or {}).get("role")
    if not role_may_decide(role):
        st.info(
            "Only a hiring manager or an admin can record the final decision. "
            "You can read it here."
        )
        return

    app_id = str(application_id)
    version = st.session_state.get(f"fd_ver_{app_id}", 0)
    st.markdown("**Change the decision**" if current else "**Record the decision**")
    with st.form(f"fd_form_{app_id}_{version}", clear_on_submit=False):
        choice = st.radio(
            "Your decision",
            _DECISION_ORDER,
            index=None,
            format_func=label_for,
            horizontal=True,
            key=f"fd_choice_{app_id}_{version}",
        )
        rationale = st.text_area(
            "Rationale (required)",
            key=f"fd_why_{app_id}_{version}",
            max_chars=None,
            help=(
                f"Required, {RATIONALE_MIN_CHARS}–{RATIONALE_MAX_CHARS} "
                "characters. Stored exactly as written."
            ),
            placeholder="Why this decision, in your own words.",
        )
        st.caption(
            f"{RATIONALE_MIN_CHARS}–{RATIONALE_MAX_CHARS} characters. "
            + _DECISION_EVIDENCE_ONLY
        )
        reviewed = confirmed(
            "I have reviewed the scorecard, interview feedback and analysis",
            key=f"fd_ok_{app_id}_{version}",
        )
        submitted = st.form_submit_button(
            "Change final decision" if current else "Record final decision"
        )
        st.caption(_DECISION_RECORDED_ONLY)

    if submitted:
        if choice is None:
            st.error(_DECISION_CHOOSE)
        elif not reviewed:
            st.error(_DECISION_CONFIRM_FIRST)
        else:
            _run_record_final_decision(app_id, choice, rationale, acting_user_id)


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
    row, guide, feedback_state, analysis_state, acting_user_id,
    decision_state=None,
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
    _render_final_decision_section(
        row.application_id, decision_state, acting_user_id
    )
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
    decision_state = view.get("decision_by_application")

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
                    analysis_state, acting_user_id, decision_state,
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
                _render_final_decision_section(
                    g.application_id, decision_state, acting_user_id
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
    _render_final_ranking_section(job_id, acting_user_id)
    st.divider()
    _render_shortlisted_section(job_id, acting_user_id)
