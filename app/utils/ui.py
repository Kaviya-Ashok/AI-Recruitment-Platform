"""Shared presentation helpers for the HR Streamlit pages (Phase A — the
"stop looking like a database" pass).

WHY THIS MODULE EXISTS
----------------------
Before Phase A the three HR pages (``jobs`` / ``candidates`` / ``interviews``)
each hand-rolled their own status chips, badge colours, and AI-provenance
captions. That produced raw enum strings on screen (``RUBRIC_APPROVED``),
three different badge palettes, and four different verbs for "the AI made
this" (``Reconciled by`` / ``Extracted by`` / ``Judged by`` / ``Generated
by``) — each with the model id inlined.

This module is the single home for all three concerns:

* :func:`label_for`   — one enum-value -> human phrase lookup, with a safe
  title-cased fallback (never raises, never renders blank).
* :func:`badge` (+ the ``*_kind`` resolvers) — ONE fixed colour palette. Gray
  is reserved *exclusively* for neutral / not-applicable; ``LOW`` confidence
  is a caution signal and renders amber, never gray.
* :func:`ai_provenance` — one caption for any AI-generated artifact. No model
  id, no verb variation.

DESIGN NOTES
------------
* **Location.** A single ``app/utils/*.py`` module, matching the existing
  ``session.py`` / ``parsing.py`` / ``validation.py`` / ``authorization.py``
  convention. A new ``app/ui/`` package would be overkill for three pure
  helpers with no state.
* **Pure functions only.** Nothing here imports or calls ``st.*``. Every
  function takes plain strings / datetimes and returns a string, exactly so
  it can be unit-tested (``tests/test_ui_helpers.py``) like the rest of
  ``app/utils``. The pages do the ``st.markdown`` / ``st.caption`` rendering.
* **Streamlit-native.** :func:`badge` emits the built-in ``:colour-badge[…]``
  markdown idiom (Streamlit >= 1.35). No custom CSS/HTML.
"""

from __future__ import annotations

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 1a. Enum-value -> human label registry
# ---------------------------------------------------------------------------
#
# Keys are the EXACT string values of the status/role vocabularies rendered in
# HR-page display code:
#   * JobStatus                (app/database/models/job.py)
#   * ApplicationStatus        (app/database/models/application.py)
#   * ScreeningSessionStatus   (app/database/models/screening_session.py)
#   * RubricVersionStatus      (app/database/models/rubric.py)
#   * UserRole                 (app/database/models/user.py)
#   * InterviewQuestionCategory(app/database/models/interview_guide.py)
#
# This is a DISPLAY-LABEL lookup only. It never invents a status or changes
# what one means. Where two vocabularies share a key (e.g. "DRAFT" is both a
# JobStatus and a RubricVersionStatus) the phrase is deliberately one that
# reads correctly for both.

_LABELS: dict[str, str] = {
    # --- JobStatus -------------------------------------------------------
    "DRAFT": "Draft",
    "JD_ANALYZED": "JD analyzed",
    "RUBRIC_PENDING": "Rubric in review",
    "RUBRIC_APPROVED": "Rubric approved",
    "OPEN": "Open",
    "CLOSED": "Closed",
    "ARCHIVED": "Archived",
    # --- ApplicationStatus --------------------------------------------
    "APPLIED": "Applied",
    "RESUME_PROCESSING": "Parsing résumé",
    "RESUME_PROCESSED": "Résumé parsed",
    "RESUME_FAILED": "Résumé parsing failed",
    "PREQUALIFICATION_COMPLETED": "Prequalified",
    "SCREENING_IN_PROGRESS": "Screening in progress",
    "SCREENING_COMPLETED": "Screening complete",
    "SCREENING_INCOMPLETE": "Screening incomplete",
    "SCREENING_EVALUATED": "Evaluation complete",
    # --- ScreeningSessionStatus -------------------------------------
    # NB: "SCREENING_COMPLETE" (no D) is the session status; the application
    # status is "SCREENING_COMPLETED" (with D) — both map to the same phrase.
    "PENDING": "Preparing screening",
    "READY_FOR_ROUND_1": "Ready to screen",
    "ROUND_1_IN_PROGRESS": "Screening — round 1",
    "ROUND_1_COMPLETE": "Round 1 complete",
    "ROUND_2_IN_PROGRESS": "Screening — round 2",
    "SCREENING_COMPLETE": "Screening complete",
    # --- RubricVersionStatus --------------------------------------
    "APPROVED": "Approved",
    "SUPERSEDED": "Superseded",
    # --- UserRole -----------------------------------------------------
    "HR": "HR",
    "HIRING_MANAGER": "Hiring manager",
    "ADMIN": "Admin",
    "SYSTEM": "Automated pipeline",
    # --- InterviewQuestionCategory ---------------------------------
    "REQUIREMENTS": "Requirements",
    "EXPERIENCE": "Experience",
    "BEHAVIORAL": "Behavioral",
    "RESUME_VALIDATION": "Résumé validation",
    "PROBING": "Probing / follow-up",
}


def label_for(value: str | None) -> str:
    """Human-readable phrase for an enum/status string.

    Falls back to a title-cased, de-underscored version of the raw value for
    anything not in the registry (e.g. a status added in a later phase before
    this map is updated). Never raises; returns ``"—"`` for ``None`` / blank.
    """
    if value is None:
        return "—"
    key = str(value).strip()
    if not key:
        return "—"
    if key in _LABELS:
        return _LABELS[key]
    return key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# 1b. One badge helper + one fixed palette
# ---------------------------------------------------------------------------
#
# Semantic "kind" -> Streamlit badge colour token. This is the ONLY place a
# badge colour is chosen anywhere in the HR app.
#
#   positive  green   met / passed / proceed / open / done
#   caution   amber   attention / low-or-medium confidence / stalled / hold
#   negative  red     not met / failed / reject / blocking / ineligible
#   neutral   gray    genuinely not-applicable / no data — NEVER "caution"
#   info      blue    informational lifecycle state, no judgment implied
#
# Streamlit's markdown badge supports: blue, green, orange, red, violet, gray.
# "amber" is rendered with the "orange" token.

_PALETTE: dict[str, str] = {
    "positive": "green",
    "caution": "orange",
    "negative": "red",
    "neutral": "gray",
    "info": "blue",
}

#: Every valid ``kind`` accepted by :func:`badge`.
BADGE_KINDS: frozenset[str] = frozenset(_PALETTE)


def badge_color(kind: str | None) -> str:
    """The Streamlit colour token for one palette slot (``st.badge(color=...)``).

    Pure. An unknown or missing ``kind`` falls back to gray, like :func:`badge`.
    """
    return _PALETTE.get(kind or "", "gray")


def badge(kind: str, text: str) -> str:
    """Return the Streamlit coloured-badge markdown for one palette slot.

    ``kind`` is one of :data:`BADGE_KINDS`. An unknown kind falls back to
    ``"neutral"`` rather than raising. ``text`` is whitespace-collapsed and is
    always shown alongside the colour (CLAUDE.md §26 — never colour alone).
    """
    colour = _PALETTE.get(kind, "gray")
    label = " ".join(str(text).split()) or "—"
    return f":{colour}-badge[{label}]"


# --- semantic "kind" resolvers -------------------------------------------
#
# These centralise the H9 rule: LOW/MEDIUM confidence are caution (amber), so
# a real "low confidence" signal can never collapse to the same gray used for
# "not assessed for this role" / "not currently ranked".

_RESULT_KIND: dict[str, str] = {
    "PASS": "positive",
    "FAIL": "negative",
    "UNKNOWN": "neutral",
}
_CONFIDENCE_KIND: dict[str, str] = {
    "HIGH": "positive",
    "MEDIUM": "caution",
    "LOW": "caution",
}
_RECOMMENDATION_KIND: dict[str, str] = {
    "PROCEED": "positive",
    "HOLD": "caution",
    "REJECT": "negative",
}
_STATUS_KIND: dict[str, str] = {
    # JobStatus
    "DRAFT": "neutral",
    "JD_ANALYZED": "info",
    "RUBRIC_PENDING": "caution",
    "RUBRIC_APPROVED": "info",
    "OPEN": "positive",
    "CLOSED": "neutral",
    "ARCHIVED": "neutral",
    # ApplicationStatus
    "APPLIED": "info",
    "RESUME_PROCESSING": "info",
    "RESUME_PROCESSED": "info",
    "RESUME_FAILED": "negative",
    "PREQUALIFICATION_COMPLETED": "info",
    "SCREENING_IN_PROGRESS": "info",
    "SCREENING_COMPLETED": "info",
    "SCREENING_INCOMPLETE": "caution",
    "SCREENING_EVALUATED": "positive",
    # ScreeningSessionStatus
    "PENDING": "caution",
    "READY_FOR_ROUND_1": "info",
    "ROUND_1_IN_PROGRESS": "info",
    "ROUND_1_COMPLETE": "info",
    "ROUND_2_IN_PROGRESS": "info",
    "SCREENING_COMPLETE": "positive",
    # RubricVersionStatus
    "APPROVED": "positive",
    "SUPERSEDED": "neutral",
}


def result_kind(value: str | None) -> str:
    """Badge kind for a PASS / FAIL / UNKNOWN criterion result."""
    return _RESULT_KIND.get(str(value).strip().upper() if value else "", "neutral")


def confidence_kind(value: str | None) -> str:
    """Badge kind for a HIGH / MEDIUM / LOW confidence level.

    LOW and MEDIUM are ``"caution"`` (amber) by design — a real low-confidence
    signal must never render in the gray reserved for "not applicable" (H9).
    """
    return _CONFIDENCE_KIND.get(
        str(value).strip().upper() if value else "", "caution"
    )


def recommendation_kind(value: str | None) -> str:
    """Badge kind for a PROCEED / HOLD / REJECT recommendation."""
    return _RECOMMENDATION_KIND.get(
        str(value).strip().upper() if value else "", "neutral"
    )


def status_kind(value: str | None) -> str:
    """Badge kind for a job / application / screening-session / rubric status.

    Unknown values fall back to ``"info"`` — a lifecycle state we do not yet
    have an opinion on is informational, not a warning.
    """
    if value is None:
        return "neutral"
    return _STATUS_KIND.get(str(value).strip(), "info")


def status_badge(value: str | None) -> str:
    """Convenience: a status string rendered as ``label_for`` text in its
    ``status_kind`` colour. Replaces the old unstyled ``\\`RAW_ENUM\\``` chips.
    """
    return badge(status_kind(value), label_for(value))


# ---------------------------------------------------------------------------
# 1d. Entity-type markers
# ---------------------------------------------------------------------------
#
# The palette above encodes SENTIMENT (is this good / bad / neutral?). It says
# nothing about WHAT is being described, so a job status, an application
# status, a screening state, a shortlist marker and a ranking note all render
# as interchangeable coloured chips (audit H8).
#
# One icon per entity type fixes that without touching badge()'s palette or
# signature. Deliberately five icons and no more — this is a wayfinding cue,
# not decoration. The icon NEVER carries meaning on its own: the badge's text
# label always states the status in words (CLAUDE.md §26).

ENTITY_ICONS: dict[str, str] = {
    "job": "📁",         # a requisition and its lifecycle
    "application": "👤",  # one candidate's application
    "screening": "💬",    # the AI screening conversation / session
    "shortlist": "⭐",    # an HR selection decision
    "ranking": "📊",      # rank position, eligibility, drift, staleness
}


def entity_icon(entity: str | None) -> str:
    """Icon for an entity type, or ``""`` for an unknown/absent one.

    Never raises — an unrecognised entity simply renders without an icon
    rather than blocking the badge it belongs to.
    """
    if not entity:
        return ""
    return ENTITY_ICONS.get(str(entity).strip().lower(), "")


def entity_badge(entity: str, kind: str, text: str) -> str:
    """A :func:`badge` prefixed with its entity type's icon.

    ``entity`` is a key of :data:`ENTITY_ICONS`; ``kind`` and ``text`` mean
    exactly what they do for :func:`badge`. An unknown entity degrades to a
    plain badge.
    """
    icon = entity_icon(entity)
    return badge(kind, f"{icon} {text}" if icon else text)


def entity_status_badge(entity: str, value: str | None) -> str:
    """The common case: a status string as an entity-marked badge — the icon
    for ``entity``, :func:`label_for` text, :func:`status_kind` colour."""
    return entity_badge(entity, status_kind(value), label_for(value))


# ---------------------------------------------------------------------------
# 1c. One AI-provenance caption
# ---------------------------------------------------------------------------


#: Top of the 0-10 scale every displayed score in this app is measured against.
_SCORE_MAX = 10


def score_out_of_ten(score: float | int, *, places: int = 0) -> str:
    """Render a score with its scale, e.g. ``**8 / 10**`` or ``**7.33 / 10**``.

    The single place the "/ 10" scale is written, so a per-bucket score and an
    overall ranking score can never drift into showing the same number against
    different (or invisible) scales. ``places`` sets the decimals — bucket
    scores are whole numbers, the ranking's weighted mean shows two.

    Callers decide what to render when there is no score; this function is only
    for the case where one exists.
    """
    return f"**{score:.{places}f} / {_SCORE_MAX}**"


def truncate(text: str | None, limit: int, *, suffix: str = "…") -> str:
    """Shorten ``text`` to at most ``limit`` characters on a word boundary.

    Returns the text unchanged when it is already within ``limit``. Otherwise it
    is cut at the last whitespace at or before ``limit``, trailing punctuation is
    stripped, and ``suffix`` is appended. ``None`` / blank yields ``""``.

    Pure display helper — the full text always lives elsewhere (a detail view,
    the database); this only decides what a compact card shows.
    """
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[:limit]
    if not cleaned[limit].isspace():  # cut fell mid-word — back up to last space
        head = head.rsplit(" ", 1)[0]
    head = head.rstrip(",;:.—- ") or cleaned[:limit].rstrip()
    return f"{head}{suffix}"


def interview_round_label(value: object) -> str:
    """Render a stored ``interview_feedback.interview_round`` as ``"Round 2"``.

    The one place a round number becomes a phrase, so a raw ``2`` never reaches
    the screen bare. Rounds are 1-based whole numbers; anything else (``None``,
    a blank, a non-numeric value) degrades to ``"—"`` rather than raising —
    ``label_for``'s never-blow-up-in-a-render contract.
    """
    if isinstance(value, bool) or value is None:
        return "—"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "—"
    if number < 1:
        return "—"
    return f"Round {number}"


def ai_provenance(when: datetime | str | None = None) -> str:
    """One consistent caption for an AI-generated artifact.

    ``"AI-generated"`` on its own, or ``"AI-generated · 2026-09-04"`` when a
    timestamp is supplied. Deliberately carries **no model identifier** and
    **no verb variation** — every "Reconciled by / Extracted by / Judged by /
    Generated by ``{model}``" caption collapses to this. The model id stays in
    the database and audit trail, unaffected.

    A naive datetime is treated as UTC. A string is passed through as-is (for
    a pre-formatted date); anything unparseable degrades to the bare phrase.
    """
    if when is None:
        return "AI-generated"
    if isinstance(when, str):
        text = when.strip()
        return f"AI-generated · {text}" if text else "AI-generated"
    try:
        moment = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        return f"AI-generated · {moment:%Y-%m-%d}"
    except (AttributeError, ValueError):
        return "AI-generated"


def stage_label(
    number: int,
    name: str,
    *,
    count_text: str | None = None,
    complete: bool = False,
    attention: int = 0,
) -> str:
    """The text of one job-workspace stage option, e.g.
    ``"2. Applicants · 5 · 1 to review"`` or ``"✓ 1. Setup · v2"``.

    Plain text only — no markup, no HTML, no emoji: the optional tick is the plain
    text mark ``✓`` (U+2713), shown BEFORE the number when the stage is complete,
    so completion is stated in a character, never by colour alone. ``count_text`` is
    what the caller wants shown after the name (already formatted: ``"5"``,
    ``"v2"``, ``"1 of 3 decided"``); ``attention`` adds ``"N to review"`` only when
    it is positive. Pure.
    """
    parts = [f"{number}. {name}"]
    if count_text:
        parts.append(count_text)
    if attention and attention > 0:
        parts.append(f"{attention} to review")
    label = " · ".join(parts)
    return f"✓ {label}" if complete else label
