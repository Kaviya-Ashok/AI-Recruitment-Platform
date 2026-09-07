"""Deterministic per-criterion confidence scoring for prequalification.

CLAUDE.md §21 forbids "arbitrary confidence percentages without a justified
methodology" and requires the rules for each of HIGH / MEDIUM / LOW to be
defined. This module IS that methodology, expressed as code — NOT a prompt
instruction to the AI. It runs in Python AFTER the AI has returned its
per-criterion PASS / FAIL / UNKNOWN judgments (CLAUDE.md §§4, 20, B: AI reasons,
Python applies deterministic rules).

THE RULE SET
------------
Confidence is computed per criterion from the AI's own structured output only
(``result``, ``evidence_summary``, ``reasoning``). No additional AI call.

1. LOW  — if ``result == "UNKNOWN"``.
   An UNKNOWN result means the evidence was insufficient to decide; an
   insufficient-evidence judgment cannot itself be made with confidence. This is
   the one rule that carries real semantic weight and it is tautological.

2. LOW  — if ``result`` is PASS/FAIL but ``evidence_summary`` has fewer than
   ``MIN_EVIDENCE_CHARS`` non-whitespace characters.
   A verdict asserted without citing substantive evidence is not trustworthy,
   regardless of how certain the model sounded.

3. HIGH — if ``result`` is PASS/FAIL AND ``evidence_summary`` is at least
   ``STRONG_EVIDENCE_CHARS`` chars AND ``reasoning`` is at least
   ``MIN_REASONING_CHARS`` chars.

4. MEDIUM — everything else (a PASS/FAIL with real but moderate supporting
   text).

KNOWN LIMITATION (documented on purpose)
---------------------------------------
Rules 2–4 use the *length* of the evidence/reasoning text as a crude,
deterministic proxy for *specificity*. A verbose model could inflate its
confidence; a terse-but-precise one could be under-rated. The thresholds are
therefore deliberately conservative (biased toward MEDIUM/LOW). A future step
could replace the length proxy with structured evidence-citation fields (e.g.
"which extracted evidence items were cited") and score on those instead. Until
then this is the justified, reproducible methodology §21 asks for, and an HR
reviewer can read these four rules and understand any assigned value.
"""

from __future__ import annotations

from typing import Literal

from app.ai.schemas.prequalification import CriterionAssessment

ConfidenceLevel = Literal["HIGH", "MEDIUM", "LOW"]

CONFIDENCE_VALUES: tuple[str, ...] = ("HIGH", "MEDIUM", "LOW")

# Tunable in one place. Character counts, whitespace-stripped.
MIN_EVIDENCE_CHARS = 40
STRONG_EVIDENCE_CHARS = 120
MIN_REASONING_CHARS = 40


def compute_confidence(
    *,
    result: str,
    evidence_summary: str,
    reasoning: str,
) -> ConfidenceLevel:
    """Return HIGH / MEDIUM / LOW per the documented rule set above.

    Pure function: same inputs -> same output, no I/O, no AI call.
    """
    evidence_len = len((evidence_summary or "").strip())
    reasoning_len = len((reasoning or "").strip())

    # Rule 1: UNKNOWN is, by definition, not a confident assessment.
    if result == "UNKNOWN":
        return "LOW"

    # Rule 2: PASS/FAIL with no real supporting evidence text.
    if evidence_len < MIN_EVIDENCE_CHARS:
        return "LOW"

    # Rule 3: PASS/FAIL with substantive evidence AND substantive reasoning.
    if evidence_len >= STRONG_EVIDENCE_CHARS and reasoning_len >= MIN_REASONING_CHARS:
        return "HIGH"

    # Rule 4: everything else.
    return "MEDIUM"


def confidence_for_assessment(assessment: CriterionAssessment) -> ConfidenceLevel:
    """Convenience wrapper for a validated :class:`CriterionAssessment`."""
    return compute_confidence(
        result=assessment.result,
        evidence_summary=assessment.evidence_summary,
        reasoning=assessment.reasoning,
    )


# ---------------------------------------------------------------------------
# AGGREGATE (whole-candidate) confidence — Phase 4 Step 4
# ---------------------------------------------------------------------------
#
# ``compute_confidence`` above answers "how sure are we of THIS one criterion?".
# The screening-evaluation scorecard also needs "how sure are we of the picture
# as a WHOLE?" — one HIGH / MEDIUM / LOW for the candidate. That is this
# function. It is **purely additive**: ``compute_confidence`` and its thresholds
# are untouched, and its per-criterion output is reused here as one input.
#
# THE RULE SET (documented per CLAUDE.md §21, same rigor as compute_confidence)
# --------------------------------------------------------------------------
# Inputs: the final reconciled per-criterion list, each item carrying at least
# ``requirement_type`` (str), ``result`` ("PASS"/"FAIL"/"UNKNOWN"), and
# ``confidence`` ("HIGH"/"MEDIUM"/"LOW", the per-criterion value from
# ``compute_confidence``).
#
#   LOW  if ANY of:
#     - any MANDATORY criterion's result is UNKNOWN
#       (a must-have we still cannot judge — the whole assessment is shaky), OR
#     - more than 30% of all criteria are UNKNOWN
#       (too much of the rubric is unresolved to be confident overall), OR
#     - more than half of the per-criterion confidences are LOW
#       (even where we have verdicts, most rest on thin evidence).
#
#   HIGH if ALL of:
#     - no MANDATORY criterion is UNKNOWN or FAIL
#       (every must-have is a confident PASS), AND
#     - at most one criterion in total is UNKNOWN, AND
#     - no per-criterion confidence is LOW.
#
#   MEDIUM otherwise.
#
# An empty criterion list -> LOW (nothing was assessed).
# UNKNOWN is never treated as FAIL here (CLAUDE.md §B): it lowers confidence
# via the rules above, it does not count as a failed must-have.

_MANDATORY = "MANDATORY"
_UNKNOWN_FRACTION_LOW_THRESHOLD = 0.30


def compute_overall_confidence(criterion_results: list[dict]) -> ConfidenceLevel:
    """Return the whole-candidate HIGH / MEDIUM / LOW per the documented rules
    above. Pure function: no I/O, no AI call.

    ``criterion_results`` items must carry ``requirement_type``, ``result``, and
    ``confidence`` (the per-criterion value from :func:`compute_confidence`).
    """
    if not criterion_results:
        return "LOW"

    total = len(criterion_results)
    unknown_count = sum(1 for r in criterion_results if r.get("result") == "UNKNOWN")
    low_conf_count = sum(1 for r in criterion_results if r.get("confidence") == "LOW")
    mandatory = [
        r for r in criterion_results if r.get("requirement_type") == _MANDATORY
    ]
    mandatory_unknown = any(r.get("result") == "UNKNOWN" for r in mandatory)
    mandatory_fail = any(r.get("result") == "FAIL" for r in mandatory)

    # --- LOW ---------------------------------------------------------------
    if mandatory_unknown:
        return "LOW"
    if unknown_count / total > _UNKNOWN_FRACTION_LOW_THRESHOLD:
        return "LOW"
    if low_conf_count * 2 > total:  # strictly more than half
        return "LOW"

    # --- HIGH --------------------------------------------------------------
    if (
        not mandatory_unknown
        and not mandatory_fail
        and unknown_count <= 1
        and low_conf_count == 0
    ):
        return "HIGH"

    # --- MEDIUM -----------------------------------------------------------
    return "MEDIUM"
