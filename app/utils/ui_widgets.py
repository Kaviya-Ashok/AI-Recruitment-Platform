"""Shared HR-app UI *components* — the widget-rendering sibling of ``ui.py``.

WHY THIS IS A SEPARATE MODULE
-----------------------------
``app/utils/ui.py`` holds a deliberate invariant: **zero ``st.*`` calls**. Every
function there takes plain values and returns a plain string, which is exactly
why it is fully unit-testable with ordinary pytest and no Streamlit harness.

Phase C-2 needed genuinely *stateful* shared pieces — a confirmation gate, a
job picker with cross-page memory, one error renderer, one toast renderer.
Those must call ``st.*``. Putting them in ``ui.py`` would have destroyed its
invariant and its testability, so they live here instead:

* ``ui.py``          — pure strings (labels, colours, badges, provenance).
* ``ui_widgets.py``  — renders widgets; imports *from* ``ui.py``, never the
  reverse.

Everything here is presentation only. Nothing computes a score, changes a
status, or decides a business outcome.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import streamlit as st

from app.database.database import session_scope
from app.services.job_service import list_jobs
from app.utils.ui import badge_color, label_for

# ---------------------------------------------------------------------------
# Destructive-action gate
# ---------------------------------------------------------------------------


def confirmed(label: str, key: str) -> bool:
    """A one-checkbox confirmation gate for a destructive/irreversible action.

    Promoted verbatim from ``jobs.py``'s private ``_confirmed`` (audit H21) so
    any page can gate a destructive action the same way, instead of each page
    inventing its own.
    """
    return st.checkbox(label, key=key)


# ---------------------------------------------------------------------------
# Job picker (shared by Candidates and Interviews)
# ---------------------------------------------------------------------------
#
# CROSS-PAGE PERSISTENCE — why the extra key.
#
# Streamlit does NOT keep a widget's value when the widget stops being rendered
# for a run and is then rendered again (verified with AppTest on 1.62: pick the
# third job, navigate to a page that does not render the picker, come back —
# the selectbox is back on the first job). Widget state is tied to the widget's
# lifetime, not to the session.
#
# So the selection is mirrored into a SEPARATE, plain session-state key that no
# widget owns. Streamlit never garbage-collects it, and it is read back as the
# selectbox's ``index=`` on every render. Both Candidates and Interviews pass
# the same ``key``, so picking a job on one page lands you on the same job on
# the other.


#: The one picker key the HR pages share. Candidates and Interviews both pass
#: this, so "the job I'm working on" follows you between them.
HR_JOB_PICKER_KEY = "hr_selected_job"


def _memory_key(key: str) -> str:
    """Plain (non-widget) session-state key holding the remembered job id."""
    return f"{key}__remembered_id"


def load_job_options() -> list[dict]:
    """Jobs as ``{"id", "title", "status"}`` primitives, newest first.

    The single canonical copy — ``candidates.py`` and ``interviews.py`` each had
    a byte-identical private version of this (audit H19). It lives beside the
    picker that consumes it so the two can never drift apart again.
    """
    with session_scope() as db:
        return [
            {"id": str(j.id), "title": j.title, "status": j.status}
            for j in list_jobs(db)
        ]


def job_picker(
    jobs: Sequence[dict], *, key: str, label: str = "Job"
) -> str | None:
    """Render the shared job selector and return the chosen job id.

    ``jobs`` is what :func:`load_job_options` returns. ``key`` names the shared
    memory slot — pass the SAME key on every page that should agree on "the
    job I'm working on". Returns ``None`` only when ``jobs`` is empty.

    Options are the job **ids** with a ``format_func``, not pre-built label
    strings, so two jobs that happen to share a title and status stay distinct
    (a dict keyed by label would silently drop one).
    """
    if not jobs:
        return None

    ids = [j["id"] for j in jobs]
    captions = {
        j["id"]: f"{j['title']}  ·  {label_for(j['status'])}" for j in jobs
    }

    remembered = st.session_state.get(_memory_key(key))
    index = ids.index(remembered) if remembered in ids else 0

    chosen = st.selectbox(
        label,
        ids,
        index=index,
        format_func=lambda job_id: captions.get(job_id, job_id),
        key=f"{key}__widget",
    )
    st.session_state[_memory_key(key)] = chosen
    return chosen


# ---------------------------------------------------------------------------
# Outcome rendering
# ---------------------------------------------------------------------------


def load_error(message: str) -> None:
    """The one way a load failure is shown (audit H15).

    Some load failures used to be ``st.caption`` — grey, quiet, and easy to
    read straight past as if the section were simply empty. Every one of them
    is an error and now renders as one.
    """
    st.error(message)


def success_toast(message: str) -> None:
    """Confirmation for an action that is about to ``st.rerun()`` (audit H16).

    ``st.success`` writes into the page body, so the rerun that follows an
    action wipes it before anyone reads it. A toast is owned by the session,
    not the page, and survives.

    Only for the rerun case. A confirmation meant to *stay* on screen (the
    approved-rubric panel, the job-created message) is still ``st.success``.
    """
    st.toast(message, icon=":material/check_circle:")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def page_header(
    title: str,
    subtitle: str | None = None,
    code: str | None = None,
    status_kind: str | None = None,
    status_text: str | None = None,
) -> None:
    """The title block every HR page opens with, from native elements only.

    ``st.title`` for the title (so it is the page's one ``h1``), an optional row
    holding a short ``code`` (for example a job code) and a status ``st.badge``,
    then an optional ``st.caption`` subtitle. No HTML, no CSS — the look comes
    from the injected stylesheet (``app/ui/theme.py``).

    ``status_kind`` is one of the palette kinds in ``app/utils/ui.py``
    (positive / caution / negative / neutral / info); the badge always carries
    ``status_text``, so status never relies on colour alone. A badge is drawn only
    when BOTH ``status_text`` is given; ``status_kind`` defaults to neutral.
    """
    st.title(title)
    items: list[tuple[str, str]] = []
    if code:
        items.append(("code", str(code)))
    if status_text:
        items.append(("status", str(status_text)))
    if items:
        cols = st.columns([1] * len(items) + [8], vertical_alignment="center")
        for col, (what, value) in zip(cols, items):
            if what == "code":
                # backticks would end the inline-code span early
                col.markdown("`" + value.replace("`", "'") + "`")
            else:
                col.badge(value, color=badge_color(status_kind))
    if subtitle:
        st.caption(subtitle)


def detail_lines(pairs: Iterable[tuple[str, object]]) -> None:
    """Render ``(label, value)`` detail rows as an indented markdown list.

    The native replacement for the hand-written ``&nbsp;&nbsp;`` indentation
    that used to prefix every Evidence / Reasoning / Criterion line (audit
    H22). A real markdown list indents itself, so no HTML entity — and no CSS —
    is involved. Falsy values are skipped; nothing is rendered if none remain.
    """
    items = [f"- *{label}:* {value}" for label, value in pairs if value]
    if items:
        st.markdown("\n".join(items))


# ---------------------------------------------------------------------------
# Opening a job / candidate from another page
# ---------------------------------------------------------------------------


def go_to_jobs(pages, params: dict[str, str]) -> None:
    """Open the Jobs page — which renders the job workspace or the candidate page
    when the URL carries ``job`` / ``candidate`` — with ``params`` as its query
    string (the existing deep-link scheme; see ``app/utils/workspace_nav.py``).

    Call it from the body of a script run after a button or a table selection (not
    from a callback). ``st.switch_page`` replaces the query string, so the
    parameters are consumed once and cannot loop. ``pages`` is the registry of
    ``st.Page`` objects ``app/main.py`` passes in.
    """
    st.switch_page(pages["jobs"], query_params=params)
