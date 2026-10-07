"""Pure helpers for the HR candidate page (HR UI redesign, Increment 3).

No Streamlit, no database, no I/O: everything here takes the frozen
``CandidateHeader`` the page already loaded (read as plain attributes, so this
module imports nothing from the services) and returns plain text and flags. That
keeps the wording and the branch rules testable on their own.

Plain text only — no HTML, no emoji. Status is always stated in words, never by
colour or an icon alone.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from app.utils.ui import label_for

#: The candidate page's tabs, in order. They are the values of the ``tab`` query
#: parameter (``app/utils/workspace_nav.py`` re-exports them); an unknown value
#: falls back to :data:`DEFAULT_TAB`.
CANDIDATE_TABS: tuple[str, ...] = (
    "overview", "screening", "interview", "analysis", "scorecard", "decision",
)
DEFAULT_TAB = "overview"

#: Stage key -> the short word shown for it: the candidate page's header pill and the
#: Candidates list's Stage column. ONE source for the wording; the SQL stage rule used
#: by the lists (``overview_service``) is checked against :func:`stage_name` by a test.
STAGE_LABELS: dict[str, str] = {
    "applied": "Applied",
    "screened": "Screened",
    "shortlisted": "Shortlisted",
    "interviewed": "Interviewed",
    "ranked": "Ranked",
    "decided": "Decided",
}

#: Furthest-stage-wins order used by :func:`stage_name`: the first stage in this
#: sequence that is DONE names the candidate's stage; otherwise "applied".
STAGE_PRECEDENCE: tuple[str, ...] = (
    "decided", "ranked", "interviewed", "shortlisted", "screened",
)

#: Tab key -> label, in tab order.
TAB_LABELS: dict[str, str] = {
    "overview": "Overview",
    "screening": "Screening",
    "interview": "Interview",
    "analysis": "AI analysis",
    "scorecard": "Scorecard",
    "decision": "Decision",
}

#: ``NextStep.action`` value meaning "open the record-decision dialog"; any other
#: action is a tab key to switch to.
ACTION_RECORD_DECISION = "record_decision"

#: Shown wherever a number is missing — plain words, never a bare dash.
NOT_AVAILABLE = "Not available"


def tab_label(key: str) -> str:
    """The plain-text label of one candidate-page tab. Unknown keys are labelled
    from the key itself rather than raising."""
    return TAB_LABELS.get(key, key.replace("_", " ").capitalize())


def tab_labels() -> dict[str, str]:
    """Tab key -> label for every tab, in tab order. Pure."""
    return {key: tab_label(key) for key in CANDIDATE_TABS}


# --- number formatting ----------------------------------------------------------------


def score_text(score: Any) -> str:
    """``7.50 / 10``, or :data:`NOT_AVAILABLE`. The scores are stored 0-10."""
    if score is None:
        return NOT_AVAILABLE
    return f"{float(score):.2f} / 10"


def rank_text(rank: int | None, ranked_count: int | None, entry_status: str | None) -> str:
    """The Rank cell: ``#2 of 5 ranked``, or WHY there is no rank."""
    if rank is not None:
        of = f" of {ranked_count} ranked" if ranked_count else ""
        return f"#{rank}{of}"
    if entry_status == "NOT_RANKED_INELIGIBLE":
        return "Not ranked"
    if entry_status in ("INCOMPLETE_SCREENING", "INCOMPLETE_INTERVIEW"):
        return "Incomplete"
    return "Not ranked yet"


# --- interview facts, shared by Progress and Next step ------------------------------


def _rounds(header: Any) -> int:
    return int(getattr(header, "rounds_count", 0) or 0)


def has_ratings(header: Any) -> bool:
    """At least one interview round AND no round left without competency ratings —
    the condition for an interview score to be calculable."""
    return _rounds(header) > 0 and not getattr(header, "has_unrated_round", False)


def _rounds_text(count: int) -> str:
    return f"{count} round" + ("" if count == 1 else "s")


# --- progress -----------------------------------------------------------------------


class ProgressItem(NamedTuple):
    key: str
    label: str
    done: bool
    #: Short status in words: a count, a score, "not generated", "ratings missing".
    status: str
    #: The tab the item's button switches to.
    tab: str
    #: Needs attention (as opposed to simply not done yet).
    warn: bool = False


def progress_items(header: Any) -> tuple[ProgressItem, ...]:
    """The ordered Progress list: Applied, Screened, Shortlisted, Interviewed, AI
    analysis (optional), Ranked, Decided. Pure."""
    rounds = _rounds(header)
    rated = has_ratings(header)
    unrated = bool(getattr(header, "has_unrated_round", False))
    ranked = getattr(header, "final_rank", None) is not None
    decision = getattr(header, "decision", None)
    entry_status = getattr(header, "entry_status", None)

    if rounds == 0:
        interviewed_status = "not recorded"
    elif unrated:
        interviewed_status = "ratings missing"
    else:
        interviewed_status = _rounds_text(rounds)

    if ranked:
        ranked_status = f"#{header.final_rank}"
    elif entry_status == "NOT_RANKED_INELIGIBLE":
        ranked_status = "not ranked"
    elif entry_status in ("INCOMPLETE_SCREENING", "INCOMPLETE_INTERVIEW"):
        ranked_status = "incomplete"
    else:
        ranked_status = "not ranked yet"

    screened = bool(getattr(header, "screened", False))
    screening_score = getattr(header, "screening_score", None)
    return (
        ProgressItem("applied", STAGE_LABELS["applied"], True, "", "overview"),
        ProgressItem(
            "screened", STAGE_LABELS["screened"], screened,
            f"{float(screening_score):.2f}" if screening_score is not None
            else ("" if screened else "not screened yet"),
            "screening",
        ),
        ProgressItem(
            "shortlisted", STAGE_LABELS["shortlisted"],
            bool(getattr(header, "is_shortlisted", False)),
            "" if getattr(header, "is_shortlisted", False) else "not shortlisted",
            "overview",
        ),
        ProgressItem(
            "interviewed", STAGE_LABELS["interviewed"], rated, interviewed_status,
            "interview",
            warn=unrated,
        ),
        ProgressItem(
            "analysis", "AI analysis (optional)",
            bool(getattr(header, "has_analysis", False)),
            "" if getattr(header, "has_analysis", False) else "not generated",
            "analysis",
        ),
        ProgressItem(
            "ranked", STAGE_LABELS["ranked"], ranked, ranked_status, "scorecard"
        ),
        ProgressItem(
            "decided", STAGE_LABELS["decided"], decision is not None,
            label_for(decision) if decision is not None else "", "decision",
        ),
    )


def stage_name(header: Any) -> str:
    """The furthest stage this candidate has reached, as a short word for the page
    header's pill: Decided, Ranked, Interviewed, Shortlisted, Screened, else
    Applied. Pure."""
    by_key = {item.key: item for item in progress_items(header)}
    for key in STAGE_PRECEDENCE:
        if by_key[key].done:
            return STAGE_LABELS[key]
    return STAGE_LABELS["applied"]


def progress_button_label(item: ProgressItem) -> str:
    """One Progress button's text: the label, then its status in words."""
    return f"{item.label} — {item.status}" if item.status else item.label


# --- next step ------------------------------------------------------------------------


class NextStep(NamedTuple):
    message: str
    button_label: str
    #: ``ACTION_RECORD_DECISION`` or a tab key.
    action: str


def next_step(header: Any) -> NextStep:
    """What to do next, as ``(message, button label, action)``. Pure.

    * no usable ratings        -> go to the Interview tab
    * no current decision      -> record the decision (opens the dialog)
    * a decision is recorded   -> view it on the Decision tab
    """
    if not has_ratings(header):
        if _rounds(header) == 0:
            message = (
                "Record the interview feedback with competency ratings so an "
                "interview score can be calculated."
            )
        else:
            message = (
                "Add competency ratings so an interview score can be calculated."
            )
        return NextStep(message, "Go to Interview", "interview")
    if getattr(header, "decision", None) is None:
        analysis = (
            "" if getattr(header, "has_analysis", False)
            else " (the AI analysis is optional)"
        )
        return NextStep(
            f"Review the scorecard{analysis}, then record the final decision.",
            "Record decision",
            ACTION_RECORD_DECISION,
        )
    return NextStep(
        "A decision is recorded. You can review or change it at any time.",
        "View decision",
        "decision",
    )


# --- activity -------------------------------------------------------------------------

#: Audit event type -> plain-language label for the Overview's Activity block.
ACTIVITY_LABELS: dict[str, str] = {
    "CANDIDATE_APPLIED": "Application submitted",
    "RESUME_UPLOADED": "Résumé uploaded",
    "RESUME_PROCESSED": "Résumé parsed",
    "PREQUALIFICATION_COMPLETED": "Prequalification completed",
    "AI_SCREENING_STARTED": "Screening started",
    "SCREENING_QUESTIONS_GENERATED": "Screening questions prepared",
    "AI_SCREENING_COMPLETED": "Screening completed",
    "AI_SCREENING_INCOMPLETE": "Screening marked incomplete",
    "SCORE_GENERATED": "Screening scorecard generated",
    "CANDIDATE_SHORTLISTED": "Shortlisted",
    "CANDIDATE_UNSHORTLISTED": "Removed from the shortlist",
    "INTERVIEW_GUIDE_GENERATED": "Interview guide generated",
    "HUMAN_FEEDBACK_SUBMITTED": "Interview feedback recorded",
    "INTERVIEW_TRANSCRIPT_UPLOADED": "Interview transcript attached",
    "POST_INTERVIEW_ANALYSIS_COMPLETED": "AI analysis generated",
    "FINAL_DECISION_SUBMITTED": "Final decision recorded",
}


def activity_label(event_type: str | None) -> str:
    """Plain label for an audit event type; unknown types fall back to the shared
    ``label_for`` wording rather than raising."""
    if event_type and event_type in ACTIVITY_LABELS:
        return ACTIVITY_LABELS[event_type]
    return label_for(event_type)
