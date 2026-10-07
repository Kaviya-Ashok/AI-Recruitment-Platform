# HR app: navigation, URLs and theme

The HR-facing Streamlit app is `app/main.py`. This page is the map: which pages exist,
how a link reaches a job or a candidate, what the app does not do, and what the theme
depends on. (The candidate-facing app, `app/public_main.py`, is separate and shares
none of this.)

## Page map

Sidebar entries are registered in `app/main.py` (`_PAGES`, plus `_USERS_PAGE`).

| Sidebar entry | URL path | Module | Who sees it |
|---|---|---|---|
| Dashboard | `/` (default) | `app/pages/dashboard.py` | every internal role |
| Jobs | `/jobs` | `app/pages/jobs.py` | every internal role |
| Candidates | `/candidates` | `app/pages/candidates_list.py` | every internal role |
| Users | `/users` | `app/pages/users.py` | **ADMIN only** (added in `_pages_for_current_user`; other roles' navigation simply does not contain it) |

There is no separate Interviews entry. Everything it did lives in the job workspace and
the candidate page:

```
Jobs (table)  ──select a row──▶  job workspace   /jobs?job=<id>&stage=<stage>
                                   ├─ 1 Setup          job body: JD, requirements, rubric, application link
                                   ├─ 2 Applicants     Ranking & Shortlist | Applications  (candidates.py renderers)
                                   ├─ 3 Shortlist      table; rubric version + ranking note per row
                                   ├─ 4 Interviews     table of candidates in the interview stage
                                   └─ 5 Final ranking  generate / read the post-interview ranking (interviews.py renderer)
                                          │ select a row
                                          ▼
                                 candidate page       /jobs?job=<id>&candidate=<application id>&tab=<tab>
                                   Overview · Screening · Interview · AI analysis · Scorecard · Decision
Candidates (cross-job list) ──select a row──▶ the same candidate page
Dashboard "Needs attention" / Quick find ──button──▶ the workspace or the candidate page
```

`app/pages/candidates.py` and `app/pages/interviews.py` are no longer pages. They are
libraries of renderers that the workspace stages and candidate tabs call. Several of
them still carry the word "page" or "section" in their history; read it as "renderer".

## URL parameters

All four are read through `app/utils/workspace_nav.py`; the URL is the source of truth
and `st.session_state` only mirrors it.

| Parameter | Value | Meaning |
|---|---|---|
| `job` | a job UUID | open that job's workspace. A value that is not a UUID is ignored (no error, no redirect) |
| `stage` | `setup`, `applicants`, `shortlist`, `interviews`, `final_ranking` | which workspace stage. Unknown stage: a calm message and a back button. Missing: the first incomplete stage |
| `candidate` | an application UUID **of that job** | open the candidate page instead of the workspace. The `stage` is kept so closing the candidate returns to it. Not UUID, or not that job's: a calm message |
| `tab` | `overview`, `screening`, `interview`, `analysis`, `scorecard`, `decision` | which candidate tab. Only read together with `candidate`. Unknown: Overview |

Nothing user-supplied is written anywhere except the query string.

## The post-login deep-link redirect

A logged-out visit to `/jobs?job=…&stage=…` is handled by Streamlit before our code
runs: the only registered page is the login page, so the visitor lands on `/` with the
parameters kept (`/?job=…&stage=…`). After sign-in the Dashboard loads, and **the first
thing `render_dashboard_page` does** is `deep_link_target()`; if the `job` parameter is a
valid UUID it calls `st.switch_page(<Jobs page>, query_params=…)`. `switch_page` replaces
the query string, so the parameters are consumed and cannot loop. The same applies to a
candidate link. Tested in `tests/test_job_workspace_redirect.py` and
`tests/test_candidate_redirect.py`.

## Known limitations

- **Back / Forward does not rerun until the next click.** Every stage, candidate and tab
  change pushes a history entry, so URLs are correct and shareable, but Streamlit does
  not rerun a page when only the query string changes through the browser's Back or
  Forward buttons. The URL reverts while the page keeps showing the previous content
  until the user's next click, which re-reads the URL. Nothing is lost. Fixing it needs
  client-side JavaScript, which this app does not use. (See `workspace_nav.py`.)
- **The logged-out "Page not found" flash.** The deep-link redirect above is preceded by
  a short-lived "Page not found" dialog, because Streamlit does not know `/jobs` while
  logged out. Press Escape or wait; sign-in continues normally.
- **First request to a cold server can show a traceback.** Streamlit auto-discovers
  `app/pages/*.py` until the entrypoint has called `st.navigation` once in the process. If
  the very first request is a logged-out `/jobs?…` (or any `/<page name>`), it runs that
  page file on its own, without `app/main.py`'s `sys.path` setup. With the repo root not
  on the path that is `ModuleNotFoundError: No module named 'app'`; with `PYTHONPATH`
  set it is a blank page. After any request to `/` the problem is gone. **On Render set
  `PYTHONPATH` to the repository root** (the directory that contains `app/`). It changes
  no code. The auto-discovered sidebar then lists the module names of `app/pages/` to an
  unauthenticated first visitor and renders nothing else. Renaming `app/pages/` would
  remove that, but it is a broad change and has not been made.

## Theme selector inventory

All styling is one string, `THEME_CSS` in `app/ui/theme.py`, injected once by
`inject_theme()` (the only `unsafe_allow_html=True` call in `app/`). It relies on
Streamlit's **stable `data-testid` hooks**, never generated class names:

`stApp`, `stAppViewContainer`, `stMain`, `stMainBlockContainer`, `stHeader`, `stSidebar`,
`stSidebarContent`, `stSidebarNavLink`, `stVerticalBlock` (with `data-test-scroll-behavior`
for bordered containers), `stElementContainer`, `stMarkdown`, `stMarkdownContainer`,
`stCaptionContainer`, `stWidgetLabel`, `stMetric`, `stMetricLabel`, `stMetricValue`,
`stAlert`, `stAlertContainer`, `stAlertContent{Info,Success,Warning,Error}`,
`stAlertDynamicIcon`, `stToastDynamicIcon`, `stIconMaterial`, `stIconEmoji`,
`stBaseButton-{primary,secondary}` and their `FormSubmit` variants, `stDownloadButton`,
`stButtonGroup` (segmented control, `data-variant="segmented_control"`), `stTabs`,
`stExpander`, `stForm`, `stDialog`, `stSelectbox`, `stMultiSelect`, `stTextInputRootElement`,
`stTextAreaRootElement`, `stNumberInputContainer`, `stFileUploaderDropzone`,
`stFileUploaderDropzoneInstructions`, plus `.stMarkdownBadge` (badge tint classes).
Streamlit 1.62 exposes no `data-baseweb` attributes, so none are used.

Two rules to remember:

- The blanket `font-family: inherit` reset **must skip every icon element**
  (`stIconMaterial`, `stIconEmoji`, `stAlertDynamicIcon`, `stToastDynamicIcon`): Material
  icons are ligatures, so the glyph's *name* is the element's text, and without the icon
  font it shows as plain words. `tests/test_ui_theme.py` pins this.
- Status is never carried by colour alone. Every badge and alert states its meaning in
  words; icons are Material icons (`:material/…:`), not emoji.

## Streamlit stays pinned

`requirements.txt` pins `streamlit==1.62.0`. The theme, the `AppTest` page tests, the
dataframe row-selection behaviour and the `st.navigation` / `st.switch_page` semantics
above were all built and checked against that version. **Streamlit stays pinned.
Re-check the look after any upgrade:** run the full suite, then open each page at 1440,
1024 and 768 px and check the selector list above still matches the rendered DOM (a renamed
`data-testid` fails silently — the style just stops applying).
