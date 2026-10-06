"""The HR app's one static stylesheet (Streamlit-native widgets + this CSS).

THE RULE — read before touching this file
-----------------------------------------
:func:`inject_theme` is the ONLY place in ``app/`` that may pass
``unsafe_allow_html=True`` to Streamlit. It sends :data:`THEME_CSS` and nothing
else.

    NEVER put a variable, a name, a note, a candidate or interviewer field, a
    database value, or any user-supplied text into this call — not through an
    f-string, ``%``, ``.format``, concatenation or a default argument.

``unsafe_allow_html`` turns Markdown into raw HTML. The moment any dynamic value
reaches it, that value becomes an HTML/script-injection hole on a page that shows
candidate data. That is why :data:`THEME_CSS` is a single plain string literal and
why ``inject_theme`` takes no arguments: there is nothing to pass through. A test
(``tests/test_ui_theme.py``) enforces all of this by AST scan, and also that
``unsafe_allow_html`` appears in no other file under ``app/`` and ``st.html``
nowhere.

WHAT THE STYLESHEET DOES
------------------------
It restyles NATIVE Streamlit elements only (no custom markup, no new widgets, no
JavaScript) to the approved enterprise look in
``docs/design/ui_mockup_v3_enterprise.html``: neutral greys, ONE accent
(``#2E6E4E``), status colours only for status, thin borders, 8-10px radii, white
cards on a ``#F9FAFB`` page, a system font stack, 14px base text.

Hooks are ``data-testid`` attributes and ARIA roles, which Streamlit keeps stable
across builds, never the generated ``st-emotion-cache-*`` class names. (Streamlit
1.62 exposes no ``data-baseweb`` attributes at all.) Bordered ``st.container``\\s
have no testid of their own; the only distinguishing mark is the
``data-test-scroll-behavior`` attribute on their ``stVerticalBlock``, which the
stylesheet uses — if a future Streamlit drops it, cards fall back to Streamlit's
own border rather than breaking.

It is loaded only by ``app/main.py`` (login screen included). The public candidate
app does not import this module and ``.streamlit/config.toml`` is untouched, so
the candidate-facing look is unchanged.

Colour contrast (WCAG 2.x, sRGB; ``tests/test_ui_theme.py`` recomputes every
pair listed here from the CSS itself and fails below 4.5:1): body text #101828 on
#F9FAFB 17.0:1; secondary text #475467 on #FFFFFF 7.7:1; muted/caption text
#667085 on #F9FAFB 4.8:1 and on #FFFFFF 5.0:1; accent #2E6E4E on white 6.1:1;
white on accent 6.1:1; active nav #245A3F on #EAF3EE 7.1:1; alert text on its tint
about 9:1; focus ring #175CD3 on the page 5.7:1 (non-text needs 3:1). Streamlit's
own badge text colours for green/orange/red/gray fell below 4.5:1 on their tint
(4.3 / 3.1 / 4.4 / ~3.5) and are darkened here; disabled controls are exempt.
"""

from __future__ import annotations

import streamlit as st

# A single plain string literal: no f-string, no format, no concatenation, no
# variables. No @import, no url(), no external resource of any kind.
THEME_CSS = """
:root{
  --ri-bg:#F9FAFB; --ri-surface:#FFFFFF; --ri-border:#E4E7EC; --ri-border2:#D0D5DD;
  --ri-text:#101828; --ri-text2:#475467; --ri-text3:#667085;
  --ri-accent:#2E6E4E; --ri-accent-d:#245A3F; --ri-accent-bg:#EAF3EE;
  --ri-focus:#175CD3;
}

/* ---- page, font, base size ------------------------------------------------ */
[data-testid="stApp"],
[data-testid="stAppViewContainer"],
[data-testid="stMain"]{background:var(--ri-bg);}
[data-testid="stApp"]{
  color:var(--ri-text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-size:14px; line-height:1.5; -webkit-font-smoothing:antialiased;
}
[data-testid="stApp"] :is(p,span,div,label,li,a,button,input,textarea,summary,h1,h2,h3,h4,h5,h6,small,strong,em):not([data-testid="stIconMaterial"]):not([data-testid="stIconEmoji"]){
  font-family:inherit;
}
[data-testid="stHeader"]{background:var(--ri-bg);}
/* the injected stylesheet element's own container must not add a blank row */
[data-testid="stElementContainer"]:has(> [data-testid="stMarkdown"] style){display:none;}

/* ---- page container ------------------------------------------------------- */
[data-testid="stMainBlockContainer"]{
  max-width:1320px; padding:3.25rem 2rem 4rem;
}
@media (max-width:1000px){
  [data-testid="stMainBlockContainer"]{padding:3rem 1rem 3rem;}
}
/* login screen: no navigation links (also true after an in-session logout, when
   Streamlit leaves an empty sidebar shell behind) -> a narrow centred column */
[data-testid="stApp"]:not(:has([data-testid="stSidebarNavLink"])) [data-testid="stMainBlockContainer"]{
  max-width:460px; padding-top:12vh;
}

/* ---- headings and text ------------------------------------------------------ */
[data-testid="stMain"] h1{
  font-size:22px !important; line-height:1.3 !important; font-weight:600 !important;
  letter-spacing:-.01em; padding:0 0 .25rem !important; color:var(--ri-text);
}
[data-testid="stMain"] h2{font-size:17px !important; line-height:1.35 !important; font-weight:600 !important; padding:.5rem 0 .25rem !important;}
[data-testid="stMain"] h3{font-size:15px !important; line-height:1.4 !important; font-weight:600 !important; padding:.5rem 0 .25rem !important;}
[data-testid="stMain"] h4{font-size:14px !important; line-height:1.4 !important; font-weight:600 !important; padding:.4rem 0 .2rem !important;}
[data-testid="stMain"] h5,
[data-testid="stMain"] h6{font-size:13px !important; font-weight:600 !important; padding:.3rem 0 .15rem !important;}
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li{font-size:14px; line-height:1.5;}
[data-testid="stMarkdownContainer"] a{color:var(--ri-accent-d);}
[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] p{color:var(--ri-text3); font-size:12.5px; line-height:1.45;}
[data-testid="stWidgetLabel"] p,
[data-testid="stWidgetLabel"] label{color:var(--ri-text); font-size:13px; font-weight:600;}
[data-testid="stMain"] hr,
[data-testid="stSidebar"] hr{border:0; border-top:1px solid var(--ri-border); margin:1rem 0;}

/* inline code (e.g. a job code): a quiet chip, not a syntax-highlighted span */
[data-testid="stMarkdownContainer"] code{
  font-size:12px; color:var(--ri-text2); background:var(--ri-bg);
  border:1px solid var(--ri-border); border-radius:6px; padding:0 6px;
}

/* ---- status badges: AA text colour ---------------------------------------------
   Streamlit's own badge text colours miss 4.5:1 on their tint for green, orange,
   red and gray. Only the TEXT COLOUR is darkened; the label and the tint are
   untouched, so status never relies on colour alone. Keyed on each badge's tint
   (stable Streamlit constants); if Streamlit changes them the default shows. */
.stMarkdownBadge[style*="rgba(33, 195, 84"]{color:#0B5D2E !important;}
.stMarkdownBadge[style*="rgba(255, 164, 33"]{color:#8A3A0B !important;}
.stMarkdownBadge[style*="rgba(255, 43, 43"]{color:#912018 !important;}
.stMarkdownBadge[style*="rgba(49, 51, 63"]{color:#344054 !important;}

/* ---- sidebar ---------------------------------------------------------------- */
[data-testid="stSidebar"],
[data-testid="stSidebarContent"]{background:var(--ri-surface);}
[data-testid="stSidebar"]{border-right:1px solid var(--ri-border);}
[data-testid="stSidebarNavLink"]{
  border-radius:8px; padding:.45rem .65rem; color:var(--ri-text2); font-weight:500; font-size:14px;
}
[data-testid="stSidebarNavLink"]:hover{background:var(--ri-bg); color:var(--ri-text);}
[data-testid="stSidebarNavLink"][aria-current="page"]{
  background:var(--ri-accent-bg); color:var(--ri-accent-d); font-weight:600;
}
[data-testid="stSidebarNavLink"] span{color:inherit;}

/* ---- buttons ----------------------------------------------------------------- */
[data-testid="stBaseButton-secondary"],
[data-testid="stBaseButton-secondaryFormSubmit"],
[data-testid="stDownloadButton"] button{
  background:var(--ri-surface); color:var(--ri-text); border:1px solid var(--ri-border2);
  border-radius:8px; min-height:34px; padding:0 13px; font-size:13.5px; font-weight:600;
  box-shadow:none;
}
[data-testid="stBaseButton-secondary"]:hover,
[data-testid="stBaseButton-secondaryFormSubmit"]:hover,
[data-testid="stDownloadButton"] button:hover{background:var(--ri-bg); border-color:var(--ri-border2); color:var(--ri-text);}
[data-testid="stBaseButton-primary"],
[data-testid="stBaseButton-primaryFormSubmit"]{
  background:var(--ri-accent); color:#FFFFFF; border:1px solid var(--ri-accent);
  border-radius:8px; min-height:34px; padding:0 13px; font-size:13.5px; font-weight:600; box-shadow:none;
}
[data-testid="stBaseButton-primary"]:hover,
[data-testid="stBaseButton-primaryFormSubmit"]:hover{background:var(--ri-accent-d); border-color:var(--ri-accent-d); color:#FFFFFF;}
[data-testid="stBaseButton-primary"] p,
[data-testid="stBaseButton-primaryFormSubmit"] p{color:#FFFFFF;}
[data-testid^="stBaseButton-"]:disabled{opacity:.45;}

/* ---- metrics: bordered cells ----------------------------------------------------- */
[data-testid="stMetric"]{
  background:var(--ri-surface); border:1px solid var(--ri-border); border-radius:10px; padding:12px 16px;
}
[data-testid="stMetricLabel"],
[data-testid="stMetricLabel"] p{color:var(--ri-text3); font-size:12.5px;}
[data-testid="stMetricValue"]{font-size:24px; font-weight:600; letter-spacing:-.01em; font-variant-numeric:tabular-nums; color:var(--ri-text);}

/* ---- cards: bordered containers, forms ----------------------------------------------- */
[data-testid="stVerticalBlock"][data-test-scroll-behavior]:not([data-testid="stDialog"] *){
  background:var(--ri-surface); border:1px solid var(--ri-border); border-radius:10px;
}
[data-testid="stForm"]{
  background:var(--ri-surface); border:1px solid var(--ri-border); border-radius:10px;
}

/* ---- expanders ---------------------------------------------------------------------------- */
[data-testid="stExpander"] details{
  background:var(--ri-surface); border:1px solid var(--ri-border); border-radius:8px;
}
[data-testid="stExpander"] summary{font-size:13.5px; font-weight:600; color:var(--ri-text);}
[data-testid="stExpander"] summary:hover{color:var(--ri-accent-d);}

/* ---- alerts: muted tints, dark text -------------------------------------------------------------- */
[data-testid="stAlert"]{border-radius:8px; border:1px solid var(--ri-border2); background:#F2F4F7; color:#1D2939;}
[data-testid="stAlertContainer"]{background:transparent; color:inherit; border-radius:8px;}
[data-testid="stAlert"] [data-testid^="stAlertContent"],
[data-testid="stAlert"] p,
[data-testid="stAlert"] li{color:inherit; font-size:13.5px;}
[data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]){background:#EFF8FF; border-color:#B2DDFF; color:#194185;}
[data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]){background:#ECFDF3; border-color:#ABEFC6; color:#054F31;}
[data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]){background:#FFFAEB; border-color:#FEDF89; color:#7A2E0E;}
[data-testid="stAlert"]:has([data-testid="stAlertContentError"]){background:#FEF3F2; border-color:#FECDCA; color:#7A271A;}

/* ---- inputs and selects ------------------------------------------------------------------------------- */
[data-testid="stTextInputRootElement"],
[data-testid="stTextAreaRootElement"],
[data-testid="stSelectbox"] [role="group"],
[data-testid="stMultiSelect"] [role="group"],
[data-testid="stNumberInputContainer"]{
  background:var(--ri-surface); border:1px solid var(--ri-border2); border-radius:8px;
}
[data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stTextAreaRootElement"]:focus-within,
[data-testid="stSelectbox"] [role="group"]:focus-within,
[data-testid="stMultiSelect"] [role="group"]:focus-within{
  border-color:var(--ri-accent); box-shadow:0 0 0 1px var(--ri-accent);
}
[data-testid="stTextInputRootElement"] input,
[data-testid="stTextAreaRootElement"] textarea,
[data-testid="stSelectbox"] input{font-size:14px; color:var(--ri-text); background:transparent;}

[data-testid="stFileUploaderDropzone"]{
  background:var(--ri-bg); border:1px dashed var(--ri-border2); border-radius:8px;
}
[data-testid="stFileUploaderDropzoneInstructions"] span,
[data-testid="stFileUploaderDropzoneInstructions"] small{color:var(--ri-text3);}

/* ---- tabs and segmented control: underlined, accent underline ----------------------------------------------- */
[data-testid="stTabs"] [role="tablist"]{border-bottom:1px solid var(--ri-border); gap:4px;}
[data-testid="stTabs"] [role="tab"]{
  color:var(--ri-text2); font-size:14px; font-weight:500; padding:10px 12px;
  border-bottom:2px solid transparent; margin-bottom:-1px; background:transparent;
}
[data-testid="stTabs"] [role="tab"]:hover{color:var(--ri-text);}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{
  color:var(--ri-accent-d); font-weight:600; border-bottom-color:var(--ri-accent);
}
[data-testid="stTabs"] [role="tab"] p{color:inherit; font-size:14px;}
[data-testid="stButtonGroup"] [role="radiogroup"]:has(> button[data-variant="segmented_control"]){
  border-bottom:1px solid var(--ri-border); gap:4px; flex-wrap:wrap;
}
[data-testid="stButtonGroup"] button[data-variant="segmented_control"]{
  background:transparent; border:0; border-bottom:2px solid transparent; border-radius:0;
  margin-bottom:-1px; padding:10px 12px; box-shadow:none;
  color:var(--ri-text2); font-size:14px; font-weight:500;
}
[data-testid="stButtonGroup"] button[data-variant="segmented_control"] p{color:inherit; font-size:14px;}
[data-testid="stButtonGroup"] button[data-variant="segmented_control"]:hover{color:var(--ri-text); background:transparent;}
[data-testid="stButtonGroup"] button[data-variant="segmented_control"][aria-checked="true"]{
  color:var(--ri-accent-d); font-weight:600; border-bottom-color:var(--ri-accent); background:transparent;
}

/* ---- dialogs, popovers ---------------------------------------------------------------------------------------- */
[data-testid="stDialog"] [role="dialog"]{background:var(--ri-surface); border-radius:12px;}
[data-testid="stDialog"] [data-testid="stVerticalBlock"]{border:0; background:transparent;}

/* ---- focus rings (visible, AA non-text contrast) ----------------------------------------------------------------- */
[data-testid="stApp"] :focus-visible{outline:2px solid var(--ri-focus); outline-offset:2px;}
"""


def inject_theme() -> None:
    """Inject :data:`THEME_CSS` once for the current run. Takes no arguments.

    The ONLY ``unsafe_allow_html=True`` call in ``app/``. Sends the constant and
    nothing else — never put any variable, name, note or user text here (see the
    module docstring).
    """
    st.markdown("<style>" + THEME_CSS + "</style>", unsafe_allow_html=True)
