"""Tests for the HR design foundation (app/ui/theme.py, page_header).

The one rule being enforced: the stylesheet is a static constant, injected
through the ONLY ``unsafe_allow_html`` call in ``app/``, and nothing dynamic can
ever reach it.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.ui import theme
from app.utils import ui

_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "app"

#: sha256 of .streamlit/config.toml BEFORE this increment. The candidate app reads
#: that file too, so the HR redesign must not change a byte of it.
_CONFIG_SHA256 = "6e1769a4976d4b90e135aeef700787d3fbed58ecf86b23baf05ebfb7719f9bd5"


def _py_files() -> list[Path]:
    return [p for p in _APP.rglob("*.py") if "__pycache__" not in p.parts]


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


# --- the one unsafe_allow_html call --------------------------------------------


def _unsafe_calls(path: Path) -> list[ast.Call]:
    return [
        node for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and any(kw.arg == "unsafe_allow_html" for kw in node.keywords)
    ]


def test_unsafe_allow_html_is_passed_in_exactly_one_file_under_app():
    offenders = {
        str(p.relative_to(_APP)).replace("\\", "/"): len(_unsafe_calls(p))
        for p in _py_files() if _unsafe_calls(p)
    }
    assert offenders == {"ui/theme.py": 1}


def test_st_html_is_used_nowhere():
    for path in _py_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr == "html":
                assert not (
                    isinstance(node.value, ast.Name) and node.value.id == "st"
                ), path
            if isinstance(node, ast.ImportFrom) and node.module == "streamlit":
                assert "html" not in [a.name for a in node.names], path
        # and no raw-HTML escape hatches by another name
        assert "st.html(" not in path.read_text(encoding="utf-8"), path


def test_the_unsafe_call_passes_true_and_is_in_inject_theme_only():
    tree = _tree(_APP / "ui" / "theme.py")
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert [f.name for f in funcs] == ["inject_theme"]
    calls = [
        n for n in ast.walk(funcs[0])
        if isinstance(n, ast.Call)
        and any(k.arg == "unsafe_allow_html" for k in n.keywords)
    ]
    assert len(calls) == 1
    (kw,) = [k for k in calls[0].keywords if k.arg == "unsafe_allow_html"]
    assert isinstance(kw.value, ast.Constant) and kw.value.value is True


# --- THEME_CSS is a plain literal; inject_theme sends only that -------------------


def test_theme_css_is_a_module_level_plain_string_literal():
    assert isinstance(theme.THEME_CSS, str) and theme.THEME_CSS.strip()
    tree = _tree(_APP / "ui" / "theme.py")
    assigns = [
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "THEME_CSS" for t in n.targets)
    ]
    assert len(assigns) == 1
    value = assigns[0].value
    # a bare string literal: not an f-string, not a BinOp/concatenation, not a call
    assert isinstance(value, ast.Constant) and isinstance(value.value, str)
    assert not any(isinstance(n, (ast.JoinedStr, ast.BinOp, ast.Call, ast.Name))
                   for n in ast.walk(value))


def test_theme_css_is_the_only_module_level_assignment_of_data():
    tree = _tree(_APP / "ui" / "theme.py")
    names = [
        t.id for n in tree.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    ]
    assert names == ["THEME_CSS"]


def test_inject_theme_takes_no_parameters():
    sig = inspect.signature(theme.inject_theme)
    assert list(sig.parameters) == []


def test_inject_theme_markdown_call_receives_only_the_constant():
    fn = next(n for n in _tree(_APP / "ui" / "theme.py").body
              if isinstance(n, ast.FunctionDef) and n.name == "inject_theme")
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Expr)
    call = body[0].value
    assert isinstance(call, ast.Call)
    assert ast.unparse(call.func) == "st.markdown"
    assert len(call.args) == 1
    # exactly:  "<style>" + THEME_CSS + "</style>"
    arg = call.args[0]
    assert ast.unparse(arg) == "'<style>' + THEME_CSS + '</style>'"
    assert not any(isinstance(n, (ast.JoinedStr, ast.FormattedValue, ast.Call,
                                  ast.Subscript, ast.Attribute))
                   for n in ast.walk(arg))
    names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
    assert names == {"THEME_CSS"}
    consts = [n.value for n in ast.walk(arg) if isinstance(n, ast.Constant)]
    assert sorted(consts) == ["</style>", "<style>"]
    assert [k.arg for k in call.keywords] == ["unsafe_allow_html"]


def test_inject_theme_sends_exactly_the_constant_at_runtime(mocker):
    spy = mocker.patch.object(theme.st, "markdown")
    theme.inject_theme()
    spy.assert_called_once_with(
        "<style>" + theme.THEME_CSS + "</style>", unsafe_allow_html=True
    )


def test_inject_theme_docstrings_state_the_rule():
    for doc in (theme.__doc__, theme.inject_theme.__doc__):
        flat = " ".join(doc.split())
        assert "NEVER put a variable" in flat or "never put any variable" in flat.lower()
    assert "ONLY place in ``app/``" in " ".join(theme.__doc__.split())


# --- the stylesheet's content is safe ------------------------------------------------


def test_theme_css_has_no_external_resources_or_script_vectors():
    css = theme.THEME_CSS
    lowered = css.lower()
    for forbidden in (
        "@import", "url(", "<script", "</style", "<style", "javascript:",
        "expression(", "-moz-binding", "behavior:", "http://", "https://", "//cdn",
        "@font-face", "data:",
    ):
        assert forbidden not in lowered, forbidden
    assert not re.search(r"url\(\s*['\"]?(?:[a-z][a-z0-9+.-]*:)?//", lowered)
    # no HTML tags of any kind inside the stylesheet
    assert not re.search(r"<\s*/?\s*[a-z]", lowered)


def test_theme_css_is_a_sane_size():
    assert 2_000 < len(theme.THEME_CSS) < 20_000


def test_theme_css_has_balanced_braces_and_no_unclosed_comment():
    css = theme.THEME_CSS
    assert css.count("{") == css.count("}")
    assert css.count("/*") == css.count("*/")


def test_theme_css_uses_stable_hooks_not_generated_class_names():
    assert "st-emotion-cache" not in theme.THEME_CSS
    assert not re.search(r"\.css-[0-9a-z]+", theme.THEME_CSS)
    assert "data-testid" in theme.THEME_CSS


def test_theme_css_uses_the_one_accent_and_the_system_font_stack():
    css = theme.THEME_CSS
    assert "--ri-accent:#2E6E4E" in css
    assert "-apple-system" in css and "Segoe UI" in css
    assert "gradient" not in css.lower()
    assert "font-size:14px" in css


def test_theme_css_covers_every_element_family_the_design_calls_for():
    css = theme.THEME_CSS
    for hook in (
        "stSidebar", "stSidebarNavLink", "stMainBlockContainer", "stMetric",
        "stExpander", "stAlert", "stTextInputRootElement", "stTextAreaRootElement",
        "stSelectbox", "stTabs", "stButtonGroup", "stBaseButton-primary",
        "stBaseButton-secondary", "stDialog", "stCaptionContainer", "stForm",
        ":focus-visible", "data-test-scroll-behavior", "max-width:1320px",
    ):
        assert hook in css, hook


# --- contrast (WCAG 2.x), recomputed from the CSS itself -------------------------------


def _lin(v: float) -> float:
    v /= 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def _lum(rgb: tuple[float, float, float]) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = hex_.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _ratio(fg: tuple, bg: tuple) -> float:
    a, b = _lum(fg), _lum(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def _token(name: str) -> str:
    m = re.search(rf"--ri-{name}:(#[0-9A-Fa-f]{{6}})", theme.THEME_CSS)
    assert m, name
    return m.group(1)


def _tint(rgba: tuple[float, float, float, float], over: str) -> tuple:
    r, g, b, a = rgba
    br, bg_, bb = _rgb(over)
    return (r * a + br * (1 - a), g * a + bg_ * (1 - a), b * a + bb * (1 - a))


@pytest.mark.parametrize("fg, bg, label", [
    ("text", "bg", "body text on page"),
    ("text", "surface", "body text on cards"),
    ("text2", "surface", "secondary text on cards"),
    ("text2", "bg", "secondary text on page"),
    ("text3", "bg", "muted/caption text on page"),
    ("text3", "surface", "muted/caption text on cards"),
    ("accent", "surface", "accent text on white"),
    ("accent", "bg", "accent text on page"),
    ("accent-d", "accent-bg", "active nav / selected tab"),
])
def test_text_pairs_meet_wcag_aa(fg, bg, label):
    assert _ratio(_rgb(_token(fg)), _rgb(_token(bg))) >= 4.5, label


def test_white_on_accent_buttons_meet_aa():
    assert _ratio((255, 255, 255), _rgb(_token("accent"))) >= 4.5
    assert _ratio((255, 255, 255), _rgb(_token("accent-d"))) >= 4.5


def test_focus_ring_meets_non_text_contrast_on_page_and_cards():
    for bg in ("bg", "surface"):
        assert _ratio(_rgb(_token("focus")), _rgb(_token(bg))) >= 3.0


@pytest.mark.parametrize("kind", ["Info", "Success", "Warning", "Error"])
def test_alert_text_meets_aa_on_its_tint(kind):
    m = re.search(
        rf'stAlertContent{kind}"\]\)\{{background:(#[0-9A-Fa-f]{{6}});'
        rf'\s*border-color:#[0-9A-Fa-f]{{6}};\s*color:(#[0-9A-Fa-f]{{6}});',
        theme.THEME_CSS,
    )
    assert m, kind
    assert _ratio(_rgb(m.group(2)), _rgb(m.group(1))) >= 4.5


def test_alert_default_variant_meets_aa():
    m = re.search(
        r'\[data-testid="stAlert"\]\{[^}]*background:(#[0-9A-Fa-f]{6});\s*color:(#[0-9A-Fa-f]{6});',
        theme.THEME_CSS,
    )
    assert m
    assert _ratio(_rgb(m.group(2)), _rgb(m.group(1))) >= 4.5


@pytest.mark.parametrize("tint, base_alpha", [
    ("rgba(33, 195, 84", (33, 195, 84, 0.1)),
    ("rgba(255, 164, 33", (255, 164, 33, 0.1)),
    ("rgba(255, 43, 43", (255, 43, 43, 0.1)),
    ("rgba(49, 51, 63", (49, 51, 63, 0.1)),
])
def test_darkened_badge_text_meets_aa_on_the_streamlit_tint(tint, base_alpha):
    m = re.search(
        rf'stMarkdownBadge\[style\*="{re.escape(tint)}"\]\{{color:(#[0-9A-Fa-f]{{6}})',
        theme.THEME_CSS,
    )
    assert m, tint
    for page in ("#F9FAFB", "#FFFFFF"):
        assert _ratio(_rgb(m.group(1)), _tint(base_alpha, page)) >= 4.5, (tint, page)


# --- no other app reaches the stylesheet or the escape hatch ---------------------------------


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


@pytest.mark.parametrize("rel", ["public_main.py", "utils/candidate_ui.py"])
def test_the_candidate_app_never_imports_the_hr_theme(rel):
    imported = _imports(_APP / rel)
    assert not any(name == "app.ui" or name.startswith("app.ui.") for name in imported), rel
    assert "theme" not in " ".join(imported).lower().replace("app.utils.", "")


def test_nothing_the_candidate_app_imports_pulls_in_the_theme():
    """public_main's transitive app imports must not include app.ui.*."""
    seen: set[str] = set()
    todo = ["app.public_main"]
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
    assert not {m for m in seen if m == "app.ui" or m.startswith("app.ui.")}


def test_streamlit_config_is_byte_identical_to_before():
    data = (_ROOT / ".streamlit" / "config.toml").read_bytes()
    assert hashlib.sha256(data).hexdigest() == _CONFIG_SHA256


# --- main.py wiring --------------------------------------------------------------------------------


def test_main_injects_the_theme_first_thing_in_main_before_either_branch():
    tree = _tree(_APP / "main.py")
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    first = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))][0]
    assert isinstance(first, ast.Expr) and ast.unparse(first.value) == "inject_theme()"
    assert [ast.unparse(n.value) for n in ast.walk(fn)
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and ast.unparse(n.value.func) == "inject_theme"] == ["inject_theme()"]


def test_inject_theme_is_called_nowhere_else():
    callers = [
        str(p.relative_to(_APP)).replace("\\", "/") for p in _py_files()
        if "inject_theme(" in p.read_text(encoding="utf-8") and p.name != "theme.py"
    ]
    assert callers == ["main.py"]


# --- ui.py stays pure -------------------------------------------------------------------------------


def test_ui_py_still_never_imports_or_calls_streamlit():
    tree = _tree(_APP / "utils" / "ui.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "streamlit" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "streamlit"
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)):
            assert node.func.value.id != "st"


@pytest.mark.parametrize("kind, color", [
    ("positive", "green"), ("caution", "orange"), ("negative", "red"),
    ("neutral", "gray"), ("info", "blue"),
])
def test_badge_color_maps_each_palette_kind(kind, color):
    assert ui.badge_color(kind) == color


@pytest.mark.parametrize("bad", [None, "", "nope", "POSITIVE"])
def test_badge_color_falls_back_to_gray(bad):
    assert ui.badge_color(bad) == "gray"


# --- page_header ----------------------------------------------------------------------------------------


def _header_app(**kw) -> str:
    return (
        "from app.utils.ui_widgets import page_header\n"
        f"page_header(**{kw!r})\n"
    )


def _markdown(at) -> list[str]:
    return [m.value for m in at.markdown]


def _run(**kw) -> AppTest:
    at = AppTest.from_string(_header_app(**kw), default_timeout=60).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_page_header_renders_title_subtitle_code_and_badge():
    at = _run(title="Senior Data Engineer", subtitle="Platform team · Open",
              code="V_003", status_kind="positive", status_text="Open")
    assert [t.value for t in at.title] == ["Senior Data Engineer"]
    assert [c.value for c in at.caption] == ["Platform team · Open"]
    assert "`V_003`" in [m.value for m in at.markdown]
    # AppTest renders st.badge as a markdown element carrying the badge syntax.
    assert [v for v in _markdown(at) if "-badge[" in v] == [":green-badge[Open]"]


def test_page_header_title_only_is_just_a_title():
    at = _run(title="Jobs")
    assert [t.value for t in at.title] == ["Jobs"]
    assert list(at.caption) == []
    assert _markdown(at) == []


def test_page_header_subtitle_only_matches_the_old_title_and_caption_pair():
    at = _run(title="Interviews", subtitle="Interview guides.")
    assert [t.value for t in at.title] == ["Interviews"]
    assert [c.value for c in at.caption] == ["Interview guides."]


@pytest.mark.parametrize("kind, color", [
    ("positive", "green"), ("caution", "orange"), ("negative", "red"),
    ("neutral", "gray"), ("info", "blue"), (None, "gray"),
])
def test_page_header_badge_color_comes_from_the_palette_and_always_has_text(kind, color):
    at = _run(title="T", status_kind=kind, status_text="Closed")
    assert _markdown(at) == [f":{color}-badge[Closed]"]    # text, not only colour


def test_page_header_status_kind_without_text_draws_no_badge():
    assert _markdown(_run(title="T", status_kind="positive")) == []


def test_page_header_code_with_a_backtick_cannot_break_the_inline_code_span():
    at = _run(title="T", code="V`1")
    assert "`V'1`" in [m.value for m in at.markdown]


def test_page_header_uses_no_html():
    src = inspect.getsource(__import__("app.utils.ui_widgets", fromlist=["x"]).page_header)
    for needle in ("unsafe_allow_html", "<div", "<span", "<style", "st.html"):
        assert needle not in src


def test_page_header_signature_is_the_agreed_one():
    from app.utils.ui_widgets import page_header

    assert list(inspect.signature(page_header).parameters) == [
        "title", "subtitle", "code", "status_kind", "status_text",
    ]


@pytest.mark.parametrize("page", ["jobs.py", "candidates.py", "interviews.py"])
def test_the_hr_pages_open_with_page_header_not_a_bare_title(page):
    src = (_APP / "pages" / page).read_text(encoding="utf-8")
    assert "page_header(" in src
    assert "st.title(" not in src


def test_the_dashboard_opens_with_page_header():
    src = (_APP / "main.py").read_text(encoding="utf-8")
    assert 'page_header(\n        "Dashboard"' in src
    # the login screen keeps its own plain title
    assert 'st.title("Recruitment Intelligence Platform")' in src
