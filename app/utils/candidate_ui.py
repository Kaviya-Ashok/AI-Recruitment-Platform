"""Candidate-side presentation helpers for the public app (Phase D, Tier 2).

The candidate-facing sibling of the HR app's ``app/utils/ui.py`` /
``app/utils/ui_widgets.py`` — and **deliberately severed from both**.

WHY SEVERED, NOT SHARED
-----------------------
The two modules look tempting to reuse; both are genuinely unsafe here.

* ``ui_widgets.py`` imports ``app.services.job_service.list_jobs`` at module
  level. Importing it anywhere in ``app/public_main.py``'s graph would pull an
  HR service into the **unauthenticated** app — exactly the accidental-exposure
  failure mode ``app/utils/authorization.py`` warns about ("one accidental
  import — a copy-paste, an autocomplete slip...").
* ``ui.py`` is pure and ``st.*``-free, so it *is* importable — but its
  ``label_for()`` maps internal HR vocabulary to prose (``SCREENING_EVALUATED``
  -> "Evaluation complete", ``SCREENING_INCOMPLETE`` -> "Screening incomplete",
  ``RUBRIC_APPROVED`` -> "Rubric approved"). Making that function reachable from
  a candidate surface puts a one-line-away path to leaking internal pipeline
  state to candidates — the precise thing
  ``resolve_screening_access_token``'s anti-enumeration discipline exists to
  prevent.

Only ``badge()`` and ``truncate()`` are domain-neutral, so the shared surface
was two small functions against those two risks. Full severance was judged
safer than any import path, however narrow. **This module must never import
``app.utils.ui``, ``app.utils.ui_widgets``, or any HR service** — an AST test
in ``tests/test_candidate_ui_helpers.py`` enforces it.

STRUCTURE
---------
Kept as one file: this increment's scope is two consumers, which is not enough
content to justify the ``ui.py`` / ``ui_widgets.py`` two-file split. The
convention that split encodes is still respected internally — the pure,
Streamlit-free logic is separated from the one rendering function below, and
is unit-tested without a Streamlit runtime.

Nothing here is speculative: it contains exactly what C8 (the save-link) and
C2 (the round-1 follow-up framing) need, and nothing else.
"""

from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------------------
# Pure helpers — no ``st.*``, unit-testable without a Streamlit runtime
# ---------------------------------------------------------------------------

#: Shown under round 1's "X of Y answered" caption. Deliberately says nothing
#: about *how many* follow-ups there might be: round 2 is generated only after
#: round 1 closes and can legitimately produce **zero** questions, so any count
#: — even a vague "a few more" — would be a claim the system cannot stand
#: behind. "may" carries the uncertainty honestly.
FOLLOW_UP_MAY_FOLLOW = (
    "Depending on your answers, a short follow-up round may come after this one."
)


def follow_up_notice(round_number: int) -> str | None:
    """The follow-up framing for a screening round, or ``None`` for no notice.

    Truthful only while round 2's existence is genuinely unknown, which is
    exactly "the candidate is answering round 1":

    * round 1 — round 2 has not been generated yet (it is generated only once
      round 1 closes), so "may come next" is the honest state -> notice;
    * round 2 — it already exists and its own "X of Y answered" caption is
      accurate and sufficient; a "may come next" line would be stale -> ``None``;
    * anything else — no notice.

    A fully completed screening never reaches this function at all: the public
    page dispatches ``SCREENING_COMPLETE`` to its completion message and only
    calls the round form for the two in-progress states.
    """
    return FOLLOW_UP_MAY_FOLLOW if round_number == 1 else None


# ---------------------------------------------------------------------------
# Widgets — the ``st.*`` half
# ---------------------------------------------------------------------------


def resume_link_block(url: str) -> None:
    """Render a resume-later link so it wraps AND stays one-click copyable.

    ``st.code(url, language=None)`` alone put the link in a non-wrapping block:
    a base URL plus a 43-character token overflows a phone-width container and
    scrolls sideways, so the candidate could not see the whole link they were
    being told to keep. Its native copy button, though, is a real asset —
    especially on mobile, where selecting long text by hand is miserable.

    ``wrap_lines=True`` (Streamlit >= 1.40; this project runs 1.62) keeps that
    copy button and makes the block wrap, so both properties hold at once. No
    custom CSS and no ``unsafe_allow_html`` — the standing constraint for both
    apps.

    ``url`` embeds the candidate's own credential. It is rendered here and
    nowhere else: never logged, never put in an error message, never passed to
    any other call from this function.
    """
    st.code(url, language=None, wrap_lines=True)
