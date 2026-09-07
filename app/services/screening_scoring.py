"""Deterministic screening-evaluation scoring — Python only, no AI
(CLAUDE.md §§4, 20, B; Phase 4 Step 4).

CLAUDE.md §20 requires scoring formulas to be documented clearly. This module
IS the formula. The AI produces per-criterion PASS/FAIL/UNKNOWN verdicts
(reconciled over résumé + screening answers); everything below is applied by
Python from those verdicts.

THE FORMULA — verbatim
======================
Per-criterion point:
    PASS    -> 1.0
    FAIL    -> 0.0
    UNKNOWN -> EXCLUDED from the score sum entirely
               (NOT 0.5, NOT 0 — CLAUDE.md §B: unknown stays unknown; an
               insufficient-evidence criterion must not lower a score as if it
               had failed).

Per-criterion weight (by requirement_type — the ONLY controlled axis; the
free-text ``category`` field is never used for bucketing or inference):
    MANDATORY   -> 2
    PREFERRED   -> 1
    EXPERIENCE  -> 1
    BEHAVIORAL  -> 1
    OTHER       -> 1

Bucket assignment (by requirement_type only):
    Requirements = {MANDATORY, PREFERRED, OTHER}
    Experience   = {EXPERIENCE}
    Behavioral   = {BEHAVIORAL}
  There is deliberately NO "Technical" bucket — see
  ``screening_evaluation.py`` model docstring.

Let SCORED(bucket) = the PASS/FAIL criteria in the bucket (UNKNOWN excluded).

Bucket score:
    if SCORED(bucket) is empty  ->  score = NULL
    else                        ->  score = round( 10 *
                                        sum(weight * point) over SCORED(bucket)
                                      / sum(weight)          over SCORED(bucket) )
                                      clamped to [0, 10]

Bucket coverage:
    if score is NULL            ->  coverage = NULL
    else                        ->  coverage =
                                        sum(weight) over SCORED(bucket)
                                      / sum(weight) over ALL criteria in bucket
                                                    (including UNKNOWN)
    coverage is a float in (0.0, 1.0].

RECOMMENDATION — verbatim (see ``compute_recommendation``)
    1. any MANDATORY criterion result == FAIL        -> HOLD
    2. else any MANDATORY criterion result == UNKNOWN -> HOLD
    3. else overall_confidence == "LOW"               -> HOLD
    4. else                                            -> PROCEED
  This function can NEVER return "REJECT" — under any input combination. That
  is a human decision (CLAUDE.md §11), and a test pins it.
"""

from __future__ import annotations

from app.database.models.job_requirement import RequirementType
from app.database.models.screening_evaluation import (
    ScreeningEvaluationBucket,
    ScreeningRecommendation,
)

# --- weights / buckets ------------------------------------------------

_WEIGHTS: dict[str, int] = {
    RequirementType.MANDATORY: 2,
    RequirementType.PREFERRED: 1,
    RequirementType.EXPERIENCE: 1,
    RequirementType.BEHAVIORAL: 1,
    RequirementType.OTHER: 1,
}

_BUCKET_OF: dict[str, str] = {
    RequirementType.MANDATORY: ScreeningEvaluationBucket.REQUIREMENTS,
    RequirementType.PREFERRED: ScreeningEvaluationBucket.REQUIREMENTS,
    RequirementType.OTHER: ScreeningEvaluationBucket.REQUIREMENTS,
    RequirementType.EXPERIENCE: ScreeningEvaluationBucket.EXPERIENCE,
    RequirementType.BEHAVIORAL: ScreeningEvaluationBucket.BEHAVIORAL,
}

_POINT: dict[str, float] = {"PASS": 1.0, "FAIL": 0.0}  # UNKNOWN absent on purpose


def weight_of(requirement_type: str) -> int:
    """Per-criterion weight. Unknown types default to 1 (defensive)."""
    return _WEIGHTS.get(requirement_type, 1)


def bucket_of(requirement_type: str) -> str:
    """Which score bucket a criterion belongs to. Unknown types fall in
    Requirements (the catch-all, same as OTHER)."""
    return _BUCKET_OF.get(requirement_type, ScreeningEvaluationBucket.REQUIREMENTS)


# --- bucket score + coverage --------------------------------------


def _score_and_coverage(rows: list[dict]) -> tuple[int | None, float | None]:
    """(score, coverage) for the criteria of ONE bucket. See module formula.

    ``rows`` are the reconciled per-criterion dicts already filtered to this
    bucket, each carrying ``requirement_type`` and ``result``.
    """
    total_weight = sum(weight_of(r["requirement_type"]) for r in rows)
    scored = [r for r in rows if r["result"] in _POINT]
    scored_weight = sum(weight_of(r["requirement_type"]) for r in scored)

    if not scored:  # empty bucket, or every criterion UNKNOWN
        return None, None

    weighted_points = sum(
        weight_of(r["requirement_type"]) * _POINT[r["result"]] for r in scored
    )
    raw = 10.0 * weighted_points / scored_weight
    score = max(0, min(10, round(raw)))
    coverage = scored_weight / total_weight  # total_weight >= scored_weight > 0
    return score, coverage


def compute_bucket_scores(reconciled_rows: list[dict]) -> dict[str, int | float | None]:
    """Return the six bucket columns for a ``screening_evaluations`` row:
    ``requirements_score/coverage``, ``experience_score/coverage``,
    ``behavioral_score/coverage`` — each score int|None, each coverage
    float|None (NULL together).
    """
    grouped: dict[str, list[dict]] = {
        ScreeningEvaluationBucket.REQUIREMENTS: [],
        ScreeningEvaluationBucket.EXPERIENCE: [],
        ScreeningEvaluationBucket.BEHAVIORAL: [],
    }
    for r in reconciled_rows:
        grouped[bucket_of(r["requirement_type"])].append(r)

    req_s, req_c = _score_and_coverage(grouped[ScreeningEvaluationBucket.REQUIREMENTS])
    exp_s, exp_c = _score_and_coverage(grouped[ScreeningEvaluationBucket.EXPERIENCE])
    beh_s, beh_c = _score_and_coverage(grouped[ScreeningEvaluationBucket.BEHAVIORAL])
    return {
        "requirements_score": req_s,
        "requirements_coverage": req_c,
        "experience_score": exp_s,
        "experience_coverage": exp_c,
        "behavioral_score": beh_s,
        "behavioral_coverage": beh_c,
    }


# --- recommendation ---------------------------------------------


def compute_recommendation(
    reconciled_rows: list[dict], overall_confidence: str
) -> str:
    """PROCEED or HOLD — never REJECT. See the module formula.

    ``reconciled_rows`` each carry ``requirement_type`` and ``result``.
    """
    mandatory = [
        r for r in reconciled_rows
        if r["requirement_type"] == RequirementType.MANDATORY
    ]
    if any(r["result"] == "FAIL" for r in mandatory):
        return ScreeningRecommendation.HOLD          # rule 1
    if any(r["result"] == "UNKNOWN" for r in mandatory):
        return ScreeningRecommendation.HOLD          # rule 2
    if overall_confidence == "LOW":
        return ScreeningRecommendation.HOLD          # rule 3
    return ScreeningRecommendation.PROCEED           # rule 4


# --- strengths / gaps / unknowns ------------------------------


def assemble_strengths_gaps_unknowns(
    reconciled_rows: list[dict],
) -> tuple[list[str], list[str], list[str]]:
    """Python-assembled projections of the per-criterion list — never AI free
    text. Each entry is ``f"{criterion_text}: {evidence_summary}"``.
    """
    strengths: list[str] = []
    gaps: list[str] = []
    unknowns: list[str] = []
    for r in reconciled_rows:
        line = f"{r.get('criterion_text', '')}: {r.get('evidence_summary', '')}"
        if r["result"] == "PASS":
            strengths.append(line)
        elif r["result"] == "FAIL":
            gaps.append(line)
        else:  # UNKNOWN
            unknowns.append(line)
    return strengths, gaps, unknowns


# --- audit-safe counts ----------------------------------------


def result_counts(reconciled_rows: list[dict]) -> dict[str, int]:
    """Counts by result — safe for audit metadata (no text)."""
    out = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    for r in reconciled_rows:
        out[r["result"]] = out.get(r["result"], 0) + 1
    return out
