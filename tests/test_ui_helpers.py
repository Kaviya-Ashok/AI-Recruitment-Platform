"""Unit tests for the shared HR-page presentation helpers (app/utils/ui.py).

These are pure functions (no ``st.*``, no DB) — the label registry, the badge
helper and its palette, and the AI-provenance caption. This is the Phase A
safety net for the new helpers, and the H9 regression guard (LOW confidence
must not share a colour with "not applicable").
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.database.models.application import ApplicationStatus
from app.database.models.interview_guide import InterviewQuestionCategory
from app.database.models.job import JobStatus
from app.database.models.rubric import RubricVersionStatus
from app.database.models.screening_session import ScreeningSessionStatus
from app.database.models.user import UserRole
from app.utils import ui


# --- label_for ----------------------------------------------------------

_ALL_RENDERED_VALUES = (
    sorted(JobStatus.ALL)
    + sorted(ApplicationStatus.ALL)
    + sorted(ScreeningSessionStatus.ALL)
    + sorted(RubricVersionStatus.ALL)
    + [r.value for r in UserRole]
    + sorted(InterviewQuestionCategory.ALL)
)


@pytest.mark.parametrize("value", _ALL_RENDERED_VALUES)
def test_every_current_enum_value_has_a_non_empty_human_label(value):
    label = ui.label_for(value)
    assert label and label.strip()
    # A mapped label is never just the raw token echoed back.
    assert label != value or value in {"HR"}  # "HR" is intentionally kept as-is


@pytest.mark.parametrize("value", list(JobStatus.ALL) + list(ApplicationStatus.ALL))
def test_mapped_labels_are_not_screaming_snake_case(value):
    assert "_" not in ui.label_for(value)


def test_label_for_unmapped_value_falls_back_safely():
    assert ui.label_for("SOME_FUTURE_STATUS") == "Some Future Status"
    assert ui.label_for("wibble") == "Wibble"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_label_for_blank_is_em_dash_not_error(blank):
    assert ui.label_for(blank) == "—"


# --- badge + palette --------------------------------------------------

@pytest.mark.parametrize(
    "kind,colour",
    [
        ("positive", "green"),
        ("caution", "orange"),
        ("negative", "red"),
        ("neutral", "gray"),
        ("info", "blue"),
    ],
)
def test_badge_kind_maps_to_its_fixed_colour(kind, colour):
    assert ui.badge(kind, "x") == f":{colour}-badge[x]"


def test_badge_unknown_kind_falls_back_to_neutral_without_raising():
    assert ui.badge("not-a-kind", "x") == ":gray-badge[x]"


def test_badge_collapses_whitespace_and_never_empty():
    assert ui.badge("info", "  a   b\n") == ":blue-badge[a b]"
    assert ui.badge("info", "   ") == ":blue-badge[—]"


def test_badge_kinds_constant_matches_palette():
    assert ui.BADGE_KINDS == frozenset(
        {"positive", "caution", "negative", "neutral", "info"}
    )


# --- H9 regression: LOW confidence vs "not applicable" ---------------

def test_low_confidence_and_not_applicable_render_different_colours():
    """H9: a real caution signal (LOW confidence) must be visually distinct
    from genuinely-absent data (gray / neutral)."""
    low_conf_badge = ui.badge(ui.confidence_kind("LOW"), "confidence: LOW")
    not_applicable_badge = ui.badge("neutral", "Not assessed for this role")

    assert "gray" not in low_conf_badge
    assert "orange" in low_conf_badge
    assert "gray" in not_applicable_badge
    assert ui.confidence_kind("LOW") != ui.status_kind(None)


def test_low_and_medium_confidence_are_both_caution():
    assert ui.confidence_kind("LOW") == "caution"
    assert ui.confidence_kind("MEDIUM") == "caution"
    assert ui.confidence_kind("HIGH") == "positive"


@pytest.mark.parametrize(
    "result,kind",
    [("PASS", "positive"), ("FAIL", "negative"), ("UNKNOWN", "neutral")],
)
def test_result_kind(result, kind):
    assert ui.result_kind(result) == kind


@pytest.mark.parametrize(
    "rec,kind",
    [("PROCEED", "positive"), ("HOLD", "caution"), ("REJECT", "negative")],
)
def test_recommendation_kind(rec, kind):
    assert ui.recommendation_kind(rec) == kind


def test_unknown_result_and_confidence_do_not_raise():
    assert ui.result_kind(None) == "neutral"
    assert ui.result_kind("weird") == "neutral"
    assert ui.confidence_kind(None) == "caution"


# --- status_badge -----------------------------------------------------

def test_status_badge_translates_and_colours():
    assert ui.status_badge("RUBRIC_APPROVED") == ":blue-badge[Rubric approved]"
    assert ui.status_badge("OPEN") == ":green-badge[Open]"
    assert ui.status_badge("SCREENING_EVALUATED") == ":green-badge[Evaluation complete]"
    assert ui.status_badge("RESUME_FAILED") == ":red-badge[Résumé parsing failed]"


def test_status_badge_none_is_neutral_dash():
    assert ui.status_badge(None) == ":gray-badge[—]"


def test_no_status_badge_contains_a_raw_enum_token():
    for value in list(JobStatus.ALL) + list(ApplicationStatus.ALL) + list(
        ScreeningSessionStatus.ALL
    ):
        assert value not in ui.status_badge(value)


# --- ai_provenance --------------------------------------------------

_MODELY_INPUTS = [
    datetime(2026, 9, 4, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 9, 4, 12, 30),  # naive -> treated as UTC
    None,
    "2026-09-04",
]


@pytest.mark.parametrize("value", _MODELY_INPUTS)
def test_ai_provenance_never_contains_a_model_id(value):
    out = ui.ai_provenance(value)
    lowered = out.lower()
    for needle in ("claude", "sonnet", "opus", "haiku", "gpt", "model", "-5", "-4"):
        assert needle not in lowered
    assert out.startswith("AI-generated")


def test_ai_provenance_shapes():
    assert ui.ai_provenance() == "AI-generated"
    assert (
        ui.ai_provenance(datetime(2026, 9, 4, tzinfo=timezone.utc))
        == "AI-generated · 2026-09-04"
    )
    assert ui.ai_provenance("") == "AI-generated"


def test_ai_provenance_is_verb_free_single_phrasing():
    """No 'Reconciled by' / 'Extracted by' / 'Judged by' / 'Generated by'."""
    out = ui.ai_provenance(datetime(2026, 1, 1, tzinfo=timezone.utc))
    for verb in ("reconciled", "extracted", "judged by", "generated by"):
        assert verb not in out.lower()


# --- the module's defining invariant --------------------------------

def test_ui_module_never_calls_streamlit():
    """``ui.py`` is pure string helpers by design — that is what makes it
    testable here without Streamlit's harness. Widget-rendering shared code
    belongs in ``ui_widgets.py``; this guard stops it drifting back."""
    import ast
    import inspect

    source = inspect.getsource(ui)
    tree = ast.parse(source)

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "streamlit" not in imported, "ui.py must not import streamlit"

    calls = {
        node.func.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }
    assert "st" not in calls, "ui.py must contain no st.* calls"


# --- entity icons / entity_badge (H8) ------------------------------

def test_entity_icons_are_the_five_agreed_types():
    assert set(ui.ENTITY_ICONS) == {
        "job", "application", "screening", "shortlist", "ranking",
    }


def test_every_entity_icon_is_distinct_and_non_empty():
    icons = list(ui.ENTITY_ICONS.values())
    assert all(i.strip() for i in icons)
    assert len(icons) == len(set(icons)), "each entity type needs its own icon"


@pytest.mark.parametrize("entity", sorted(ui.ENTITY_ICONS))
def test_entity_icon_round_trips_and_is_case_insensitive(entity):
    assert ui.entity_icon(entity) == ui.ENTITY_ICONS[entity]
    assert ui.entity_icon(entity.upper()) == ui.ENTITY_ICONS[entity]


@pytest.mark.parametrize("unknown", [None, "", "   ", "nope"])
def test_entity_icon_unknown_is_blank_not_an_error(unknown):
    assert ui.entity_icon(unknown) == ""


def test_entity_badge_prefixes_the_icon_and_keeps_the_palette():
    out = ui.entity_badge("job", "positive", "Open")
    assert out == f":green-badge[{ui.ENTITY_ICONS['job']} Open]"


def test_entity_badge_unknown_entity_degrades_to_a_plain_badge():
    assert ui.entity_badge("nope", "caution", "Stalled") == ui.badge(
        "caution", "Stalled"
    )


def test_entity_badge_always_keeps_the_text_label():
    """H8/§26: the icon is a wayfinding cue — never the only signal."""
    for entity in ui.ENTITY_ICONS:
        assert "Screening complete" in ui.entity_badge(
            entity, "positive", "Screening complete"
        )


def test_different_entities_same_status_render_differently():
    """The whole point of H8: a job and an application at the same sentiment
    must no longer be visually interchangeable."""
    job = ui.entity_badge("job", "info", "Applied")
    application = ui.entity_badge("application", "info", "Applied")
    assert job != application


def test_entity_status_badge_translates_and_colours():
    assert ui.entity_status_badge("job", "OPEN") == (
        f":green-badge[{ui.ENTITY_ICONS['job']} Open]"
    )
    assert ui.entity_status_badge("application", "SCREENING_EVALUATED") == (
        f":green-badge[{ui.ENTITY_ICONS['application']} Evaluation complete]"
    )


def test_entity_status_badge_never_leaks_a_raw_enum():
    for value in ("RUBRIC_APPROVED", "SCREENING_INCOMPLETE", "READY_FOR_ROUND_1"):
        assert value not in ui.entity_status_badge("application", value)


# --- score_out_of_ten ----------------------------------------------

def test_score_out_of_ten_whole_numbers_by_default():
    """Bucket scores are ints and render with no decimals, as they always have."""
    assert ui.score_out_of_ten(8) == "**8 / 10**"
    assert ui.score_out_of_ten(0) == "**0 / 10**"
    assert ui.score_out_of_ten(10) == "**10 / 10**"


def test_score_out_of_ten_places_controls_decimals():
    """The ranking's weighted mean shows two decimals."""
    assert ui.score_out_of_ten(7.333, places=2) == "**7.33 / 10**"
    assert ui.score_out_of_ten(7, places=2) == "**7.00 / 10**"
    assert ui.score_out_of_ten(7.999, places=1) == "**8.0 / 10**"


def test_score_out_of_ten_always_shows_the_scale():
    """H7: a score must never render as a bare number."""
    for value, places in ((8, 0), (7.33, 2), (0, 0), (10, 0)):
        assert "/ 10" in ui.score_out_of_ten(value, places=places)


def test_score_out_of_ten_is_bold_markdown():
    out = ui.score_out_of_ten(5)
    assert out.startswith("**") and out.endswith("**")


# --- truncate ------------------------------------------------------

def test_truncate_leaves_short_text_untouched():
    assert ui.truncate("short enough", 50) == "short enough"
    assert ui.truncate("exactly-ten", 11) == "exactly-ten"


def test_truncate_cuts_on_a_word_boundary_with_ellipsis():
    out = ui.truncate("the quick brown fox jumps over the lazy dog", 20)
    assert out.endswith("…")
    assert len(out) <= 21  # <= limit + the 1-char suffix
    assert "…" not in out[:-1]
    assert out == "the quick brown fox…"


def test_truncate_strips_trailing_punctuation_before_ellipsis():
    assert ui.truncate("alpha, beta, gamma, delta", 12) == "alpha, beta…"


def test_truncate_handles_a_single_long_word_and_blank():
    assert ui.truncate("x" * 100, 10) == "xxxxxxxxxx…"
    assert ui.truncate("", 10) == ""
    assert ui.truncate(None, 10) == ""
    assert ui.truncate("   ", 10) == ""


def test_truncate_custom_suffix():
    assert ui.truncate("one two three four", 7, suffix=" [more]") == "one two [more]"


# --- interview_round_label (Step 8) ------------------------------------


def test_interview_round_label_renders_a_one_based_round():
    assert ui.interview_round_label(1) == "Round 1"
    assert ui.interview_round_label(2) == "Round 2"
    assert ui.interview_round_label(12) == "Round 12"


def test_interview_round_label_accepts_a_numeric_string():
    assert ui.interview_round_label("3") == "Round 3"


def test_interview_round_label_degrades_instead_of_raising():
    # Rounds are 1-based; anything outside that renders the same "no value"
    # dash label_for uses, and never raises mid-render.
    for bad in (None, 0, -4, "", "  ", "not-a-round", True, False, object()):
        assert ui.interview_round_label(bad) == "—"
