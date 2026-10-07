"""Pure helpers for the overview pages (HR UI, Increment 4): filter normalisation,
pagination maths, the Needs-attention order and cap, wildcard escaping, deep links,
stage options and the stale-selection key. No database, no Streamlit app.
"""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

import app.utils.overview_helpers as h
import app.utils.workspace_nav as nav
from app.utils.candidate_progress import STAGE_LABELS, STAGE_PRECEDENCE

_MODULE = Path(h.__file__)


# --- the module is pure ----------------------------------------------------------------


def test_the_module_imports_neither_streamlit_nor_the_database_nor_the_services():
    imported: set[str] = set()
    for node in ast.walk(ast.parse(_MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {m for m in imported if m.startswith(("streamlit", "sqlalchemy", "app.database", "app.services"))}


def test_no_html_and_no_emoji():
    source = _MODULE.read_text(encoding="utf-8")
    assert "unsafe_allow_html" not in source and "<div" not in source
    for ch in source:
        assert ord(ch) < 0x2190 or ch in "—–·…✓é", hex(ord(ch))


def test_the_local_copies_match_the_workspace_and_navigation_definitions():
    from app.services.job_workspace_service import STAGE_KEYS, STAGE_NAMES

    assert h.STAGE_KEYS == STAGE_KEYS and h.STAGE_NAMES == STAGE_NAMES
    assert (h._QP_JOB, h._QP_STAGE, h._QP_CANDIDATE, h._QP_TAB) == (
        nav.QP_JOB, nav.QP_STAGE, nav.QP_CANDIDATE, nav.QP_TAB,
    )


# --- links ------------------------------------------------------------------------------


def test_build_link_includes_only_what_is_asked_for():
    j, c = uuid.uuid4(), uuid.uuid4()
    assert h.build_link(j) == {"job": str(j)}
    assert h.build_link(j, stage="applicants") == {"job": str(j), "stage": "applicants"}
    assert h.build_link(j, candidate=c, tab="decision") == {
        "job": str(j), "candidate": str(c), "tab": "decision",
    }
    assert h.build_link(str(j), candidate=str(c)) == {"job": str(j), "candidate": str(c)}


def test_a_built_link_is_accepted_by_the_redirect_helper(monkeypatch):
    """The scheme is the one ``deep_link_target`` validates and carries."""
    j, c = uuid.uuid4(), uuid.uuid4()

    class Fake:
        query_params = h.build_link(j, stage="interviews", candidate=c, tab="interview")

    monkeypatch.setattr(nav, "st", Fake)
    assert nav.deep_link_target() == h.build_link(j, stage="interviews", candidate=c, tab="interview")


# --- text normalisation ---------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("  Ada   Lovelace ", "Ada Lovelace"), ("a\tb\nc", "a b c"), ("", ""), ("   ", ""),
    (None, ""), (5, ""), ("x" * 300, "x" * 100),
])
def test_normalise_filter_text(raw, expected):
    assert h.normalise_filter_text(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("ab", "ab"), (" ab ", "ab"), ("a", None), ("  a ", None), ("", None), (None, None),
    ("a  b", "a b"), ("  ", None), ("%%", "%%"),
])
def test_normalise_query_needs_two_characters(raw, expected):
    assert h.normalise_query(raw) == expected


@pytest.mark.parametrize("term, escaped", [
    ("plain", "plain"), ("50%", "50\\%"), ("a_b", "a\\_b"), ("a\\b", "a\\\\b"),
    ("%_\\", "\\%\\_\\\\"), ("'; DROP TABLE jobs;--", "'; DROP TABLE jobs;--"),
])
def test_escape_like_makes_wildcards_literal(term, escaped):
    assert h.escape_like(term) == escaped


def test_like_pattern_wraps_the_escaped_term():
    assert h.like_pattern("50%") == "%50\\%%"
    assert h.like_pattern("ab") == "%ab%"


# --- pagination ---------------------------------------------------------------------------------


@pytest.mark.parametrize("total, size, pages", [
    (0, 25, 0), (1, 25, 1), (25, 25, 1), (26, 25, 2), (50, 25, 2), (51, 25, 3),
    (-3, 25, 0), (10, 0, 0),
])
def test_page_count(total, size, pages):
    assert h.page_count(total, size) == pages


@pytest.mark.parametrize("page, total, expected", [
    (0, 100, 0), (3, 100, 3), (4, 100, 3), (99, 100, 3), (-1, 100, 0), (5, 0, 0),
    ("2", 100, 0), (None, 100, 0), (True, 100, 0), (1.0, 100, 0),
])
def test_clamp_page(page, total, expected):
    assert h.clamp_page(page, total, 25) == expected


@pytest.mark.parametrize("page, offset", [(0, 0), (1, 25), (3, 75), (-2, 0)])
def test_page_offset(page, offset):
    assert h.page_offset(page, 25) == offset


@pytest.mark.parametrize("total, page, text", [
    (0, 0, "0 of 0"), (1, 0, "1-1 of 1"), (25, 0, "1-25 of 25"), (83, 0, "1-25 of 83"),
    (83, 1, "26-50 of 83"), (83, 3, "76-83 of 83"), (26, 1, "26-26 of 26"),
])
def test_page_range_text(total, page, text):
    assert h.page_range_text(total, page, 25) == text


@pytest.mark.parametrize("total, page, prev, nxt", [
    (0, 0, False, False), (10, 0, False, False), (25, 0, False, False),
    (26, 0, False, True), (26, 1, True, False), (83, 1, True, True), (83, 3, True, False),
])
def test_previous_and_next_availability(total, page, prev, nxt):
    assert (h.has_previous(page), h.has_next(total, page, 25)) == (prev, nxt)


def test_paging_through_every_row_exactly_once():
    total, size, seen = 83, 25, []
    for page in range(h.page_count(total, size)):
        start = h.page_offset(page, size)
        seen += list(range(total))[start:start + size]
    assert seen == list(range(total))


# --- stale-selection key -----------------------------------------------------------------------


def test_the_table_key_changes_with_any_part_of_the_state():
    base = h.table_key("t", "ada", None, "Decided", 0)
    assert base == h.table_key("t", "ada", None, "Decided", 0)             # stable
    assert base.startswith("t_") and " " not in base
    for other in (("ada", None, "Decided", 1), ("adb", None, "Decided", 0),
                  ("ada", "job", "Decided", 0), ("ada", None, "Applied", 0)):
        assert h.table_key("t", *other) != base
    assert h.table_key("u", "ada", None, "Decided", 0) != base


def test_the_table_key_is_a_fixed_content_hash():
    """A content hash, not Python's per-process randomised ``hash``: this exact value
    must come out in every process, run after run."""
    assert h.table_key("t", "ada", None, "Decided", 0) == "t_1d4a62704c75"


@pytest.mark.parametrize("rows, ids, expected", [
    ([], ["a", "b"], None), (None, ["a", "b"], None), ([0], ["a", "b"], "a"),
    ([1], ["a", "b"], "b"), ([1, 0], ["a", "b"], "b"), ([2], ["a", "b"], None),
    ([-1], ["a", "b"], None), (["x"], ["a", "b"], None), ([True], ["a", "b"], None),
    ([0], [], None),
])
def test_selected_row_id(rows, ids, expected):
    assert h.selected_row_id(rows, ids) == expected


def test_selected_row_id_stringifies_uuids():
    u = uuid.uuid4()
    assert h.selected_row_id([0], [u]) == str(u)


# --- Needs attention ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    kind: str
    job_code: str
    candidate_name: str
    application_id: str = "0"


def test_there_are_exactly_three_attention_types_in_the_specified_order():
    assert h.ATTENTION_ORDER == ("RATINGS_MISSING", "RESUME_MISSING", "DECISION_PENDING")
    assert set(h.ATTENTION_LABELS) == set(h.ATTENTION_ORDER) == set(h.ATTENTION_ACTIONS)
    assert [h.ATTENTION_LABELS[k] for k in h.ATTENTION_ORDER] == [
        "Ratings missing", "Résumé missing", "Decision pending",
    ]


def test_attention_order_is_type_then_job_code_then_name():
    items = [
        _Item("DECISION_PENDING", "V_001", "Ann"), _Item("RESUME_MISSING", "V_002", "Zed"),
        _Item("RATINGS_MISSING", "V_009", "Zoe"), _Item("RESUME_MISSING", "V_001", "bob"),
        _Item("RESUME_MISSING", "V_001", "Amy"), _Item("RATINGS_MISSING", "V_001", "Yan"),
    ]
    got = [(i.kind, i.job_code, i.candidate_name) for i in h.order_attention(items)]
    assert got == [
        ("RATINGS_MISSING", "V_001", "Yan"), ("RATINGS_MISSING", "V_009", "Zoe"),
        ("RESUME_MISSING", "V_001", "Amy"), ("RESUME_MISSING", "V_001", "bob"),
        ("RESUME_MISSING", "V_002", "Zed"), ("DECISION_PENDING", "V_001", "Ann"),
    ]


def test_ordering_is_case_insensitive_and_has_a_stable_tie_break():
    a = _Item("RESUME_MISSING", "V_001", "ann", "2")
    b = _Item("RESUME_MISSING", "V_001", "Ann", "1")
    assert h.order_attention([a, b]) == [b, a]


def test_an_unknown_kind_sorts_last_and_ordering_is_idempotent():
    items = [_Item("MYSTERY", "V_1", "a"), _Item("DECISION_PENDING", "V_9", "z")]
    ordered = h.order_attention(items)
    assert [i.kind for i in ordered] == ["DECISION_PENDING", "MYSTERY"]
    assert h.order_attention(ordered) == ordered


def test_capping_keeps_the_first_n_in_order_and_states_the_total():
    items = [_Item("RESUME_MISSING", "V_1", f"n{i:02d}") for i in range(40)]
    shown, total = h.cap_attention(reversed(items), 25)
    assert total == 40 and [i.candidate_name for i in shown] == [f"n{i:02d}" for i in range(25)]


def test_capping_a_short_list_and_an_explicit_total():
    items = [_Item("RESUME_MISSING", "V_1", "a")]
    assert h.cap_attention(items, 25) == (items, 1)
    assert h.cap_attention([], 25) == ([], 0)
    shown, total = h.cap_attention(items, 25, total=61)           # the DB counted more
    assert (len(shown), total) == (1, 61)
    assert h.cap_attention(items, 0) == ([], 1)


def test_the_default_cap_is_25():
    assert h.ATTENTION_LIMIT == 25


@pytest.mark.parametrize("shown, total, text", [
    (0, 0, "0 items"), (1, 1, "1 item"), (3, 3, "3 items"), (25, 61, "Showing 25 of 61 items"),
])
def test_attention_count_text_always_states_the_total(shown, total, text):
    assert h.attention_count_text(shown, total) == text


def test_attention_links_follow_the_specified_destinations():
    j, a = uuid.uuid4(), uuid.uuid4()
    assert h.attention_link("RESUME_MISSING", j, a) == {"job": str(j), "stage": "applicants"}
    assert h.attention_link("RATINGS_MISSING", j, a) == {
        "job": str(j), "candidate": str(a), "tab": "interview"}
    assert h.attention_link("DECISION_PENDING", j, a) == {
        "job": str(j), "candidate": str(a), "tab": "decision"}
    with pytest.raises(ValueError):
        h.attention_link("OTHER", j, a)


def test_every_attention_link_target_is_a_known_stage_or_tab():
    for kind in h.ATTENTION_ORDER:
        link = h.attention_link(kind, uuid.uuid4(), uuid.uuid4())
        assert link.get("stage", "applicants") in nav_stage_keys()
        assert link.get("tab", "overview") in nav.CANDIDATE_TABS


def nav_stage_keys():
    from app.services.job_workspace_service import STAGE_KEYS

    return STAGE_KEYS


# --- a job's current stage -----------------------------------------------------------------------


def _stage(**kw):
    base = dict(approved_rubric=True, has_screening_ranking=True, shortlisted=1,
                interviewed=2, unrated=0, undecided_interviewed=0)
    base.update(kw)
    return h.job_stage_key(**base)


@pytest.mark.parametrize("kw, expected", [
    (dict(approved_rubric=False, has_screening_ranking=False, shortlisted=0, interviewed=0), "setup"),
    (dict(has_screening_ranking=False, shortlisted=0, interviewed=0), "applicants"),
    (dict(shortlisted=0, interviewed=0), "shortlist"),
    (dict(interviewed=0), "interviews"),
    (dict(unrated=1), "interviews"),
    (dict(undecided_interviewed=1), "final_ranking"),
    (dict(), "final_ranking"),                  # all complete -> the last stage
    (dict(approved_rubric=False), "setup"),     # the FIRST incomplete one wins
])
def test_job_stage_key(kw, expected):
    assert _stage(**kw) == expected


def test_stage_text_uses_the_workspace_names():
    assert [h.stage_text(k) for k in h.STAGE_KEYS] == [
        "Setup", "Applicants", "Shortlist", "Interviews", "Final ranking"]
    assert h.stage_text("nonsense") == "nonsense"


# --- candidate stage filter ----------------------------------------------------------------------


def test_stage_options_start_with_all_then_the_candidate_pages_words_in_order():
    options = h.stage_filter_options()
    assert options[0] == "All stages"
    assert options[1:] == ["Applied", "Screened", "Shortlisted", "Interviewed", "Ranked", "Decided"]
    assert options[1:] == list(STAGE_LABELS.values())


@pytest.mark.parametrize("selection, expected", [
    ("All stages", None), ("Decided", "Decided"), ("Applied", "Applied"),
    ("decided", None), ("Nonsense", None), (None, None), (3, None),
])
def test_stage_filter_value(selection, expected):
    assert h.stage_filter_value(selection) == expected


def test_the_stage_precedence_is_the_one_stage_name_uses():
    assert STAGE_PRECEDENCE == ("decided", "ranked", "interviewed", "shortlisted", "screened")
    assert set(STAGE_PRECEDENCE) | {"applied"} == set(STAGE_LABELS)
