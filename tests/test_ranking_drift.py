"""The ranking-drift / rubric-version helper (HR UI, Increment 5) — pure.

Every branch and every message. The wording is the contract: it is what the old
Interviews page showed per shortlisted candidate, now shared by the Shortlist stage and
the candidate page.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.utils import ranking_drift as R
from app.utils.ranking_drift import (
    CHANGED, CURRENT, NOT_RANKED, NOT_REGENERATED, Drift,
    drift_note, has_multiple_versions, ranking_drift, rubric_label, version_note,
)

_SRC = Path(__file__).resolve().parents[1] / "app" / "utils" / "ranking_drift.py"


# --- ranking_drift: every branch ---------------------------------------------------


def test_a_moved_rank_is_a_caution_with_both_ranks_in_the_exact_words():
    d = ranking_drift(True, 3, 1)
    assert d == Drift(CHANGED, "Ranking changed — shortlisted at #1, now #3", True)


def test_a_rank_that_improved_is_also_a_change():
    assert ranking_drift(True, 1, 4).text == "Ranking changed — shortlisted at #4, now #1"


def test_an_unchanged_rank_is_quiet_information_not_a_caution():
    d = ranking_drift(True, 2, 2)
    assert d == Drift(CURRENT, "Currently #2", False)


def test_no_rank_recorded_at_shortlisting_is_not_a_change():
    d = ranking_drift(True, 2, None)
    assert d.kind == CURRENT and d.text == "Currently #2" and not d.caution


def test_no_ranking_row_for_the_version_means_not_regenerated():
    d = ranking_drift(False, None, 1)
    assert d == Drift(NOT_REGENERATED, "Ranking not regenerated since shortlisting", False)


def test_not_regenerated_wins_even_if_a_stale_position_is_passed():
    assert ranking_drift(False, 5, 1).kind == NOT_REGENERATED


def test_a_row_without_a_position_is_not_currently_ranked():
    d = ranking_drift(True, None, 2)
    assert d == Drift(NOT_RANKED, "Not currently ranked", False)


def test_not_ranked_does_not_need_a_shortlisting_rank():
    assert ranking_drift(True, None, None).kind == NOT_RANKED


@pytest.mark.parametrize(
    "args", [(True, 1, 1), (True, 9, 2), (False, None, 1), (True, None, None), (True, 2, None)]
)
def test_only_a_change_is_a_caution(args):
    d = ranking_drift(*args)
    assert d.caution is (d.kind == CHANGED)
    assert d.kind in R.KINDS


# --- drift_note: what a table cell shows -----------------------------------------


def test_the_cell_note_is_empty_when_nothing_changed():
    assert drift_note(ranking_drift(True, 2, 2)) == ""


@pytest.mark.parametrize(
    "args, text",
    [
        ((True, 3, 1), "Ranking changed — shortlisted at #1, now #3"),
        ((False, None, 1), "Ranking not regenerated since shortlisting"),
        ((True, None, 1), "Not currently ranked"),
    ],
)
def test_the_cell_note_is_the_exact_message_for_the_other_three(args, text):
    assert drift_note(ranking_drift(*args)) == text


# --- rubric labels ------------------------------------------------------------------


def test_an_approved_version_is_a_plain_label():
    assert rubric_label(2, "APPROVED") == "Rubric v2"
    assert rubric_label(2) == "Rubric v2"


def test_a_version_that_is_no_longer_approved_says_so_in_words():
    assert rubric_label(1, "SUPERSEDED") == "Rubric v1 — Superseded"


def test_a_missing_version_is_labelled_unknown():
    assert rubric_label(None) == "Rubric (unknown version)"
    assert rubric_label(None, "APPROVED") == "Rubric (unknown version)"


# --- the multi-version note ---------------------------------------------------------


def test_one_version_has_no_note():
    assert version_note([2, 2, 2]) is None and not has_multiple_versions([2])
    assert version_note([]) is None


def test_two_versions_get_the_one_line_note():
    note = version_note([2, 1, 2])
    assert note == R.VERSION_NOTE
    assert "tied to their own rubric version" in note and "never merged" in note
    assert "\n" not in note


def test_an_unknown_version_counts_as_its_own():
    assert has_multiple_versions([2, None])


def test_version_note_accepts_a_generator():
    assert version_note(v for v in (1, 2)) == R.VERSION_NOTE


# --- purity -------------------------------------------------------------------------


def test_the_module_has_no_streamlit_database_or_service_imports():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "dataclasses", "typing"}, imported
