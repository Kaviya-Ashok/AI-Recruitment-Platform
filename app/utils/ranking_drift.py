"""Ranking drift and rubric-version wording for shortlisted candidates (pure).

A candidate is shortlisted against ONE rubric version, at ONE rank. Later the screening
ranking can be regenerated, so the rank "now" may differ from the rank "at shortlisting",
or may not exist for that version at all. HR must be told — never silently shown a stale
position. This used to live inside the Interviews page's candidate row; it is the same
logic and the same wording, extracted so the workspace's Shortlist stage and the
candidate page can share it.

No Streamlit, no database, no I/O: every function takes primitives and returns
primitives, so every branch is unit-tested directly.

The messages are the product — change one only on purpose:

- ``Ranking changed — shortlisted at #A, now #B``   (caution)
- ``Ranking not regenerated since shortlisting``    (neutral)
- ``Not currently ranked``                          (neutral)
- ``Currently #N``                                  (no change; informational)

Nothing here compares across rubric versions: the "now" rank is the rank in the SAME
version the candidate was shortlisted against, and the helpers that look at several
versions only say whether more than one is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Drift kinds ------------------------------------------------------------------------

CHANGED = "changed"
CURRENT = "current"
NOT_REGENERATED = "not_regenerated"
NOT_RANKED = "not_ranked"

KINDS = (CHANGED, CURRENT, NOT_REGENERATED, NOT_RANKED)

#: Messages that do not depend on a rank.
NOT_REGENERATED_TEXT = "Ranking not regenerated since shortlisting"
NOT_RANKED_TEXT = "Not currently ranked"

#: One line above the Shortlist table when a job's shortlist spans rubric versions.
VERSION_NOTE = (
    "Interview guides are tied to their own rubric version. Versions are never merged "
    "or compared."
)

UNKNOWN_VERSION_LABEL = "Rubric (unknown version)"


@dataclass(frozen=True)
class Drift:
    """The outcome of comparing the rank at shortlisting with the rank now."""

    kind: str
    text: str
    #: ``True`` only for :data:`CHANGED` — the one state that should draw the eye.
    caution: bool


def ranking_drift(
    current_available: bool,
    current_position: int | None,
    position_at_decision: int | None,
) -> Drift:
    """Compare the rank at shortlisting with the rank in the same rubric version now.

    - A current ranking row with a position:
        - and a rank recorded at shortlisting that differs -> ``CHANGED``;
        - otherwise (same, or none was recorded) -> ``CURRENT`` ("Currently #N").
    - No ranking row for that version at all -> ``NOT_REGENERATED``.
    - A ranking row exists but carries no position (ineligible) -> ``NOT_RANKED``.
    """
    if current_available and current_position is not None:
        if position_at_decision is not None and current_position != position_at_decision:
            return Drift(
                CHANGED,
                f"Ranking changed — shortlisted at #{position_at_decision}, "
                f"now #{current_position}",
                True,
            )
        return Drift(CURRENT, f"Currently #{current_position}", False)
    if not current_available:
        return Drift(NOT_REGENERATED, NOT_REGENERATED_TEXT, False)
    return Drift(NOT_RANKED, NOT_RANKED_TEXT, False)


def drift_note(drift: Drift) -> str:
    """The short text for a table cell: the three drift messages, and nothing for the
    unchanged case (the rank column already says it)."""
    return "" if drift.kind == CURRENT else drift.text


def rubric_label(version_number: int | None, status: str | None = None) -> str:
    """``Rubric v2``; ``Rubric v1 — Superseded`` when the version is no longer the
    approved one; ``Rubric (unknown version)`` when the version cannot be found."""
    if version_number is None:
        return UNKNOWN_VERSION_LABEL
    label = f"Rubric v{version_number}"
    if status and status != "APPROVED":
        label += " — " + status.replace("_", " ").capitalize()
    return label


def has_multiple_versions(version_numbers: Iterable[int | None]) -> bool:
    """More than one distinct rubric version among the rows (an unknown version counts
    as its own)."""
    return len(set(version_numbers)) > 1


def version_note(version_numbers: Iterable[int | None]) -> str | None:
    """The one-line note for the Shortlist stage, or ``None`` with a single version."""
    return VERSION_NOTE if has_multiple_versions(version_numbers) else None
