"""Navigation and structural guarantees for the overview pages (HR UI, Increment 4).

* the new Candidates list replaces the old Candidates sidebar entry; every internal
  role sees Dashboard / Jobs / Candidates / Interviews, and only ADMIN also sees Users;
* none of the new modules emits HTML, makes an AI call or writes to the database;
* the public candidate app can never reach any of them.
"""

from __future__ import annotations

import ast
import re
import sys
import uuid
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "app"

_NEW_MODULES = {
    "app.services.overview_service": _APP / "services" / "overview_service.py",
    "app.utils.overview_helpers": _APP / "utils" / "overview_helpers.py",
    "app.pages.dashboard": _APP / "pages" / "dashboard.py",
    "app.pages.candidates_list": _APP / "pages" / "candidates_list.py",
    "app.pages.quick_find": _APP / "pages" / "quick_find.py",
}
_WRITE_CALLS = {"add", "add_all", "delete", "merge", "commit", "flush", "rollback",
                "record_event", "insert", "update"}
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


# --- navigation ----------------------------------------------------------------------------

_NAV = '''
import sys
import streamlit as st
from app.utils.session import SESSION_USER_KEY

_orig = st.navigation


def _recording(pages, **kw):
    st.session_state["_nav"] = [(p.title, p.url_path) for p in pages]
    return _orig(pages, **kw)


st.navigation = _recording
sys.modules.pop("app.main", None)
st.session_state[SESSION_USER_KEY] = {
    "id": "%s", "email": "p@x.test", "full_name": "P", "role": "__ROLE__",
}
import app.main  # noqa: F401  (runs main())
''' % uuid.uuid4()


@pytest.fixture(autouse=True)
def _forget_main():
    sys.modules.pop("app.main", None)
    try:
        yield
    finally:
        sys.modules.pop("app.main", None)


def _nav(role: str) -> list[tuple[str, str]]:
    at = AppTest.from_string(_NAV.replace("__ROLE__", role), default_timeout=60).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at.session_state["_nav"]


@pytest.mark.parametrize("role", ["HR", "HIRING_MANAGER", "ADMIN"])
def test_every_internal_role_sees_the_four_main_pages_in_order(role):
    titles = [t for t, _ in _nav(role) if t != "Users"]
    assert titles == ["Dashboard", "Jobs", "Candidates", "Interviews"]


def test_users_stays_admin_only():
    assert ("Users", "users") in _nav("ADMIN")
    for role in ("HR", "HIRING_MANAGER"):
        assert "Users" not in [t for t, _ in _nav(role)]


def test_the_candidates_entry_is_the_new_cross_job_list_not_the_old_page():
    tree = _tree(_APP / "main.py")
    imports = _imports(_APP / "main.py")
    assert "app.pages.candidates_list" in imports and "app.pages.dashboard" in imports
    assert "app.pages.candidates" not in imports                    # the old entry is gone
    assert not any(n.endswith("render_candidates_page") for n in imports)

    pages = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "_PAGES")
    by_key = {k.value: v for k, v in zip(pages.value.keys, pages.value.values)}
    assert list(by_key) == ["dashboard", "jobs", "candidates", "interviews"]
    assert ast.unparse(by_key["candidates"].args[0]) == "_candidates_page"
    assert ast.unparse(by_key["dashboard"].args[0]) == "_dashboard_page"

    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "render_candidates_list_page(_PAGES)" in ast.unparse(fns["_candidates_page"])
    assert "render_dashboard_page(_PAGES)" in ast.unparse(fns["_dashboard_page"])


def test_the_old_candidates_module_and_its_renderers_remain_for_the_applicants_stage():
    import app.pages.candidates as old
    import app.pages.job_workspace as workspace

    for name in ("render_candidates_page", "_render_ranking_section",
                 "_render_applications_tab", "_render_application_body"):
        assert hasattr(old, name), name
    # Existence only, not identity: other tests leave stubs on the old module (the
    # known stub leak), so `is` comparisons are order-dependent.
    assert callable(workspace._render_ranking_section)
    assert callable(workspace._render_applications_tab)


def test_the_interviews_entry_is_still_registered_until_increment_5():
    assert ("Interviews", "interviews") in _nav("HR")


def test_the_dashboard_logic_no_longer_lives_in_main():
    source = (_APP / "main.py").read_text(encoding="utf-8")
    for gone in ("_gather_landing_data", "list_applications_for_job",
                 "get_shortlist_status_for_job", "deep_link_target"):
        assert gone not in source, gone


# --- the new modules ---------------------------------------------------------------------------


@pytest.mark.parametrize("name, path", sorted(_NEW_MODULES.items()))
def test_no_html_no_emoji_no_ai_no_audit_write(name, path):
    source = path.read_text(encoding="utf-8")
    assert "unsafe_allow_html" not in source, name
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) <= 120:
            assert not _HTML_TAG.search(node.value), (name, node.value)
    for ch in source:
        assert ord(ch) < 0x2190 or ch in "—–·…✓é", (name, hex(ord(ch)))
    for imported in _imports(path):
        low = imported.lower()
        assert not low.startswith("app.ai"), (name, imported)
        assert "claude" not in low and "anthropic" not in low, (name, imported)
        assert "audit_service" not in low and "storage_service" not in low, (name, imported)


@pytest.mark.parametrize("name", ["app.services.overview_service", "app.utils.overview_helpers"])
def test_the_read_modules_make_no_write_call(name):
    for node in ast.walk(_tree(_NEW_MODULES[name])):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in _WRITE_CALLS, (name, node.func.attr)


def test_the_pages_never_touch_the_database_models_directly():
    for name in ("app.pages.dashboard", "app.pages.candidates_list", "app.pages.quick_find"):
        imported = _imports(_NEW_MODULES[name])
        assert not {m for m in imported if m.startswith("app.database.models")}, name


def test_the_theme_stylesheet_is_the_only_css_and_is_untouched_by_the_new_modules():
    for name, path in _NEW_MODULES.items():
        source = path.read_text(encoding="utf-8")
        assert "THEME_CSS" not in source and "inject_theme" not in source, name


# --- candidate-app isolation ---------------------------------------------------------------------


def _transitive(start: str) -> set[str]:
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
        if path.exists():
            todo.extend(n for n in _imports(path) if n.startswith("app"))
    return seen


def test_the_public_candidate_app_reaches_none_of_the_new_modules():
    reached = _transitive("app.public_main")
    assert not reached & (set(_NEW_MODULES) | {"app.pages.jobs", "app.pages.users"}), reached


@pytest.mark.parametrize("rel", ["app/public_main.py", "app/utils/candidate_ui.py",
                                 "app/services/candidate_portal_service.py"])
def test_no_candidate_facing_module_mentions_the_overview_readers(rel):
    source = (_ROOT / rel).read_text(encoding="utf-8")
    for needle in ("overview_service", "overview_helpers", "get_dashboard_summary",
                   "list_candidates_overview", "quick_find", "list_jobs_overview"):
        assert needle not in source, (rel, needle)
