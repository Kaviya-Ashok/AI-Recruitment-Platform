"""Pure helpers for the HR overview pages (HR UI redesign, Increment 4): Dashboard,
Jobs table, Candidates list and Quick find.

No Streamlit, no database, no I/O. Everything here takes and returns plain values,
so the rules the pages rely on — filter normalisation, pagination arithmetic, the
Needs-attention order and cap, wildcard escaping, the deep links and the key that
resets a stale table selection — are tested on their own.

Plain text only: no HTML, no emoji.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from app.utils.candidate_progress import STAGE_LABELS

# The job workspace's five stages (must equal ``job_workspace_service.STAGE_KEYS`` /
# ``STAGE_NAMES``; a test pins that). Local copies keep this module free of the
# service layer — and so of any database import.
STAGE_KEYS: tuple[str, ...] = (
    "setup", "applicants", "shortlist", "interviews", "final_ranking",
)
STAGE_NAMES: dict[str, str] = {
    "setup": "Setup",
    "applicants": "Applicants",
    "shortlist": "Shortlist",
    "interviews": "Interviews",
    "final_ranking": "Final ranking",
}

# --- URL scheme (must equal app/utils/workspace_nav.py; a test pins that) --------------

_QP_JOB = "job"
_QP_STAGE = "stage"
_QP_CANDIDATE = "candidate"
_QP_TAB = "tab"


def build_link(
    job_id: uuid.UUID | str,
    *,
    stage: str | None = None,
    candidate: uuid.UUID | str | None = None,
    tab: str | None = None,
) -> dict[str, str]:
    """Query parameters for the existing deep-link scheme (``job``, ``stage``,
    ``candidate``, ``tab``) — what ``st.switch_page(..., query_params=...)`` is
    given. Only the parameters that were asked for are included. Pure."""
    link = {_QP_JOB: str(job_id)}
    if stage:
        link[_QP_STAGE] = stage
    if candidate is not None:
        link[_QP_CANDIDATE] = str(candidate)
    if tab:
        link[_QP_TAB] = tab
    return link


# --- text filters -------------------------------------------------------------------------

#: Longest filter / query text honoured (the rest is ignored, never an error).
MAX_FILTER_CHARS = 100
#: Quick find needs at least this many characters.
QUICK_FIND_MIN_CHARS = 2


def normalise_filter_text(value: object) -> str:
    """Trimmed text with runs of whitespace collapsed to one space, capped at
    :data:`MAX_FILTER_CHARS`; anything that is not text is empty. Pure."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_FILTER_CHARS]


def normalise_query(value: object) -> str | None:
    """The Quick find query, or ``None`` when it is shorter than
    :data:`QUICK_FIND_MIN_CHARS` after normalisation. Pure."""
    text = normalise_filter_text(value)
    return text if len(text) >= QUICK_FIND_MIN_CHARS else None


def escape_like(term: str) -> str:
    """Neutralise the LIKE wildcards in ``term`` (``%``, ``_``) and the escape
    character itself, so a typed ``%`` or ``_`` matches only itself. Pair it with
    ``ilike(pattern, escape="\\\\")``. Pure."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_pattern(term: str) -> str:
    """``%<escaped term>%`` — a case-insensitive "contains" pattern. Pure."""
    return f"%{escape_like(term)}%"


# --- pagination ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 25


def page_count(total: int, page_size: int = DEFAULT_PAGE_SIZE) -> int:
    """How many pages ``total`` rows need (0 for none). Pure."""
    if total <= 0 or page_size <= 0:
        return 0
    return (total + page_size - 1) // page_size


def clamp_page(page: object, total: int, page_size: int = DEFAULT_PAGE_SIZE) -> int:
    """A valid zero-based page index: never negative, never past the last page, and
    0 for anything that is not an integer. Pure."""
    if not isinstance(page, int) or isinstance(page, bool):
        return 0
    last = max(page_count(total, page_size) - 1, 0)
    return min(max(page, 0), last)


def page_offset(page: int, page_size: int = DEFAULT_PAGE_SIZE) -> int:
    """The row offset of a zero-based page. Pure."""
    return max(page, 0) * page_size


def page_range_text(total: int, page: int, page_size: int = DEFAULT_PAGE_SIZE) -> str:
    """``26-50 of 83`` (1-based, inclusive), or ``0 of 0`` when empty. Pure."""
    if total <= 0:
        return "0 of 0"
    first = page_offset(page, page_size) + 1
    last = min(page_offset(page, page_size) + page_size, total)
    return f"{first}-{last} of {total}"


def has_previous(page: int) -> bool:
    return page > 0


def has_next(total: int, page: int, page_size: int = DEFAULT_PAGE_SIZE) -> bool:
    return page_offset(page, page_size) + page_size < total


# --- stale-selection key --------------------------------------------------------------------


def table_key(prefix: str, *state: Any) -> str:
    """A widget key that changes whenever the filter/page ``state`` changes.

    A ``st.dataframe`` row selection lives in its widget state; when the rows behind
    it change (a new filter, another page) the old row INDEX would point at a
    different person. Tying the widget key to the state makes Streamlit start a
    fresh, unselected table instead. Stable across runs and processes (a content
    hash, not Python's randomised ``hash``). Pure."""
    digest = hashlib.sha1(
        json.dumps(state, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"{prefix}_{digest}"


def selected_row_id(selected_rows: Iterable[object] | None, ids: Sequence[Any]) -> str | None:
    """The id of the row a single-row ``st.dataframe`` selection points at, or
    ``None`` (nothing selected, or an index outside the table). Pure."""
    rows = list(selected_rows or [])
    if not rows:
        return None
    index = rows[0]
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(ids):
        return None
    return str(ids[index])


# --- Needs attention --------------------------------------------------------------------------

RATINGS_MISSING = "RATINGS_MISSING"
RESUME_MISSING = "RESUME_MISSING"
DECISION_PENDING = "DECISION_PENDING"

#: The ONLY three item types, in display order.
ATTENTION_ORDER: tuple[str, ...] = (RATINGS_MISSING, RESUME_MISSING, DECISION_PENDING)

ATTENTION_LABELS: dict[str, str] = {
    RATINGS_MISSING: "Ratings missing",
    RESUME_MISSING: "Résumé missing",
    DECISION_PENDING: "Decision pending",
}

ATTENTION_ACTIONS: dict[str, str] = {
    RATINGS_MISSING: "Add ratings",
    RESUME_MISSING: "Review",
    DECISION_PENDING: "Open decision",
}

ATTENTION_LIMIT = 25


def attention_sort_key(item: Any) -> tuple:
    """Order: ratings missing, résumé missing, decision pending; then job code, then
    candidate name (case-insensitive), then application id as a stable tie-break. An
    unknown kind sorts last. Pure."""
    kind = getattr(item, "kind", None)
    rank = ATTENTION_ORDER.index(kind) if kind in ATTENTION_ORDER else len(ATTENTION_ORDER)
    return (
        rank,
        str(getattr(item, "job_code", "") or "").lower(),
        str(getattr(item, "candidate_name", "") or "").lower(),
        str(getattr(item, "application_id", "")),
    )


def order_attention(items: Iterable[Any]) -> list[Any]:
    """``items`` in the Needs-attention order. Pure and idempotent."""
    return sorted(items, key=attention_sort_key)


def cap_attention(
    items: Iterable[Any], limit: int = ATTENTION_LIMIT, *, total: int | None = None
) -> tuple[list[Any], int]:
    """``(shown, total)``: the first ``limit`` items in order, and how many there are
    in all (``total`` when the caller already counted them, else ``len(items)``).
    Pure."""
    ordered = order_attention(items)
    return ordered[: max(limit, 0)], (len(ordered) if total is None else total)


def attention_count_text(shown: int, total: int) -> str:
    """``3 items`` / ``1 item`` / ``Showing 25 of 61 items`` — the total is always
    stated. Pure."""
    if total == shown:
        return f"{total} item" + ("" if total == 1 else "s")
    return f"Showing {shown} of {total} items"


def attention_link(kind: str, job_id: uuid.UUID | str, application_id: uuid.UUID | str) -> dict[str, str]:
    """Where an attention item's button goes, in the existing URL scheme:

    * résumé missing   -> the job workspace, Applicants stage
    * ratings missing  -> the candidate page, Interview tab
    * decision pending -> the candidate page, Decision tab

    Raises ``ValueError`` for an unknown kind. Pure."""
    if kind == RESUME_MISSING:
        return build_link(job_id, stage="applicants")
    if kind == RATINGS_MISSING:
        return build_link(job_id, candidate=application_id, tab="interview")
    if kind == DECISION_PENDING:
        return build_link(job_id, candidate=application_id, tab="decision")
    raise ValueError(f"unknown attention kind: {kind!r}")


# --- jobs: current stage -------------------------------------------------------------------------


def job_stage_key(
    *,
    approved_rubric: bool,
    has_screening_ranking: bool,
    shortlisted: int,
    interviewed: int,
    unrated: int,
    undecided_interviewed: int,
) -> str:
    """A job's CURRENT workspace stage: the first stage that is not complete, or the
    last one when all are. Completeness follows ``get_job_stage_summary`` exactly:

    * Setup         — an approved rubric exists
    * Applicants    — a screening ranking exists
    * Shortlist     — at least one candidate is shortlisted
    * Interviews    — someone is interviewed and no round lacks ratings
    * Final ranking — someone is interviewed and every interviewed one is decided

    (a test checks this against the real summary). Pure."""
    complete = {
        "setup": approved_rubric,
        "applicants": has_screening_ranking,
        "shortlist": shortlisted > 0,
        "interviews": interviewed > 0 and unrated == 0,
        "final_ranking": interviewed > 0 and undecided_interviewed == 0,
    }
    for key in STAGE_KEYS:
        if not complete[key]:
            return key
    return STAGE_KEYS[-1]


def stage_text(key: str) -> str:
    """A workspace stage's name for the Jobs table. Pure."""
    return STAGE_NAMES.get(key, key)


# --- candidates: stage filter ---------------------------------------------------------------------

ALL_STAGES = "All stages"
ALL_JOBS = "All jobs"


def stage_filter_options() -> list[str]:
    """The Stage select's options: "All stages" then each stage word in progress
    order (the same words as the candidate page's pill). Pure."""
    return [ALL_STAGES, *STAGE_LABELS.values()]


def stage_filter_value(selection: object) -> str | None:
    """The stage word to filter on, or ``None`` for "All stages" / anything that is
    not a known stage. Pure."""
    return selection if selection in STAGE_LABELS.values() else None
