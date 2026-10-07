"""Structural guarantees for the candidate page work (HR UI, Increment 3).

* the new HR modules make no AI call, no write and emit no HTML;
* the public candidate app can never reach any of them;
* the extracted renderers/loaders keep their old contract (the Interviews and
  Candidates pages call them and must behave exactly as before).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import app.pages.candidates as C

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "app"

_NEW_HR_MODULES = {
    "app.pages.candidate_page": _APP / "pages" / "candidate_page.py",
    "app.utils.candidate_progress": _APP / "utils" / "candidate_progress.py",
}
_WRITE_CALLS = {"add", "add_all", "delete", "merge", "commit", "flush", "rollback",
                "record_event", "execute"}
_HTML_TAG = re.compile(r"</?\s*(div|span|style|script|br|p|table|tr|td|b|i|a|img|h[1-6])\b",
                       re.IGNORECASE)


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


@pytest.mark.parametrize("name, path", sorted(_NEW_HR_MODULES.items()))
def test_new_modules_have_no_ai_import_and_no_audit_write(name, path):
    for imported in _imports(path):
        low = imported.lower()
        assert not low.startswith("app.ai"), (name, imported)
        assert "claude" not in low and "anthropic" not in low, (name, imported)
        assert "audit_service" not in low, (name, imported)
        assert "storage_service" not in low, (name, imported)


@pytest.mark.parametrize("name, path", sorted(_NEW_HR_MODULES.items()))
def test_new_modules_make_no_database_write(name, path):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in _WRITE_CALLS, (name, node.func.attr)


@pytest.mark.parametrize("name, path", sorted(_NEW_HR_MODULES.items()))
def test_new_modules_emit_no_html_and_never_use_the_escape_hatch(name, path):
    source = path.read_text(encoding="utf-8")
    assert "unsafe_allow_html" not in source, name
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) > 120:
                continue                       # docstrings may mention tags in prose
            assert not _HTML_TAG.search(node.value), (name, node.value)


def test_new_modules_contain_no_emoji():
    for name, path in _NEW_HR_MODULES.items():
        for ch in path.read_text(encoding="utf-8"):
            # the plain-text mark U+2713 is the only symbol the HR UI allows
            assert ord(ch) < 0x2190 or ch in "—–·…✓", (name, hex(ord(ch)))


def test_the_pure_helpers_module_has_no_streamlit_or_database_import():
    imported = _imports(_NEW_HR_MODULES["app.utils.candidate_progress"])
    assert not {m for m in imported if m.startswith(("streamlit", "sqlalchemy", "app.database"))}
    assert not {m for m in imported if m.startswith("app.services")}


def test_the_candidate_page_reads_only_through_services_and_reused_renderers():
    imported = _imports(_NEW_HR_MODULES["app.pages.candidate_page"])
    assert not {m for m in imported if m.startswith("app.database.models")}


# --- the public candidate app can never reach the HR-only modules -----------------------------

_HR_ONLY = {
    "app.pages.candidate_page", "app.utils.candidate_progress",
    "app.services.job_workspace_service", "app.utils.workspace_nav",
    "app.pages.job_workspace",
}


def _transitive_app_imports(start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        mod = todo.pop()
        if mod in seen or not mod.startswith("app"):
            continue
        seen.add(mod)
        path = _ROOT / (mod.replace(".", "/") + ".py")
        if not path.exists():
            path = _ROOT / mod.replace(".", "/") / "__init__.py"
        if not path.exists():
            continue
        todo.extend(n for n in _imports(path) if n.startswith("app"))
    return seen


def test_the_public_candidate_app_never_imports_any_hr_candidate_page_module():
    reached = _transitive_app_imports("app.public_main")
    assert not (reached & _HR_ONLY), reached & _HR_ONLY


@pytest.mark.parametrize("rel", ["app/public_main.py", "app/utils/candidate_ui.py",
                                 "app/services/candidate_portal_service.py"])
def test_no_candidate_facing_module_mentions_the_new_readers(rel):
    source = (_ROOT / rel).read_text(encoding="utf-8")
    for needle in ("get_candidate_header", "list_candidate_activity",
                   "list_interview_overview", "candidate_progress", "candidate_page"):
        assert needle not in source, (rel, needle)


# --- extracted helpers keep their contract -------------------------------------------------------


def test_summary_row_matches_what_the_application_list_always_built():
    app = NS(id="app-1", status="SCREENING_EVALUATED")
    cand = NS(full_name="Ada", email="a@x.test")
    base = {"application_id": "app-1", "status": "SCREENING_EVALUATED",
            "candidate_name": "Ada", "candidate_email": "a@x.test"}
    assert C._summary_row(app, cand, None) == {**base, "screening_stall_kind": None}
    assert C._summary_row(app, cand, NS(is_pre_round_1=True)) == {
        **base, "screening_stall_kind": "PRE_ROUND_1"}
    assert C._summary_row(app, cand, NS(is_pre_round_1=False)) == {
        **base, "screening_stall_kind": "MID_SCREENING"}
    assert C._summary_row(app, None, None)["candidate_name"] == "—"
    assert C._summary_row(app, None, None)["candidate_email"] == "—"


def test_the_extracted_pieces_are_still_used_by_their_original_callers():
    # (Increment 5: the Interviews page's row and view are gone; what remains of this
    # contract is the decision glue and the two original callers below.)
    import inspect

    import app.pages.interviews as I

    section_src = inspect.getsource(I._render_final_decision_section)
    assert "_render_final_decision_record" in section_src and "_render_decision_form" in section_src
    assert "_render_screening_transcript" in inspect.getsource(I._render_final_scorecard)
    assert "_summary_row" in inspect.getsource(C._load_application_summaries)
