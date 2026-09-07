"""Pydantic schema for the post-interview analysis task
(CLAUDE.md §§9, 19, 20; Phase 4 Step 9).

BOUNDARY THIS SCHEMA MUST HOLD
-----------------------------
The AI's job here is **consolidation and prose only**: given the candidate's
whole evaluation record (rubric criteria, résumé evidence, prequalification,
screening evaluation, screening transcript, and the interviewer's own written
feedback), write a single joined-up read of the candidate — what holds up
across the sources, what does not, and what is still not known.

The AI does **not** decide, and this schema deliberately has **no field for**:
* any score — per-criterion, per-bucket, or overall,
* confidence (HIGH/MEDIUM/LOW) — Python derives it,
* a recommendation (PROCEED/HOLD/REJECT) — Python computes ``ai_recommendation``
  deterministically, and REJECT is unreachable on this path by construction
  (§11: rejection is a human decision),
* any disagreement flag, severity, or AI-vs-human comparison — §8 is a separate
  concern that this step does not implement,
* anything about the final hiring decision.

Same discipline as :mod:`app.ai.schemas.screening_evaluation` and
:mod:`app.ai.schemas.interview_guide`: the AI supplies only what only the AI can
supply; every number, verdict and boundary is Python's.

WHY ``evidence_consistency_notes`` IS A SINGLE STRING
----------------------------------------------------
It is one continuous piece of reasoning about how the sources line up ("the
résumé claim, the screening answer and the interviewer's note agree on X; on Y
the interviewer observed something the screening answer did not show"). Slicing
it into a list would invite the model to emit disconnected fragments and would
misrepresent it as an enumerable set of findings. ``strengths`` / ``gaps`` /
``unknowns`` ARE list-shaped — they are genuinely enumerable, and matching
``screening_evaluations``' shape keeps the two artefacts renderable by the same
UI idiom.

An empty ``strengths`` / ``gaps`` / ``unknowns`` list is VALID and meaningful:
"no unknowns remain after the interview" is a real finding, not a failed
generation. Blank *entries*, however, are rejected — an empty string carries no
evidence and must never reach a scorecard. ``summary`` and
``evidence_consistency_notes`` must both be non-empty: an analysis that says
nothing is a failed generation, not an analysis.

PRIVACY
-------
Every field here is synthesised prose about a real person, drawn from their
résumé, their screening answers and an interviewer's words. It is persisted on
``post_interview_analyses`` and shown to HR — and nowhere else. It must never
be written to an audit event, a log line, or an exception message.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class PostInterviewAnalysisAssessment(BaseModel):
    """The AI's consolidated post-interview read of ONE candidate.

    Prose only. No score, no confidence, no recommendation, no disagreement —
    see the module docstring.
    """

    # The consolidated narrative (CLAUDE.md §9 "consolidated summary"): who this
    # candidate is against THIS rubric, drawing the résumé, screening and
    # interview together. Non-empty.
    summary: str

    # What the candidate demonstrated, each entry grounded in actual evidence
    # from the record. May be empty. No blank entries.
    strengths: list[str] = []

    # Where the candidate fell short against the rubric. Distinct from
    # ``unknowns``: a gap is evidence of absence, an unknown is absence of
    # evidence. May be empty. No blank entries.
    gaps: list[str] = []

    # What remains UNSUPPORTED by any source, even after the interview. CLAUDE.md
    # §37 — "Unknown stays unknown": these must NOT be downgraded into gaps, and
    # must never be phrased as failures. May be empty. No blank entries.
    unknowns: list[str] = []

    # How the sources line up with one another — agreement, divergence, or a
    # claim only one source supports. Reasoning, not a verdict; it must not
    # declare a winner between the AI's read and the interviewer's. Non-empty.
    evidence_consistency_notes: str

    @field_validator("summary", "evidence_consistency_notes")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned

    @field_validator("strengths", "gaps", "unknowns")
    @classmethod
    def _entries_not_blank(cls, value: list[str]) -> list[str]:
        # An empty LIST is fine ("no unknowns remain" is a real finding). A blank
        # ENTRY is not — it carries no evidence and must never reach a scorecard.
        cleaned: list[str] = []
        for entry in value or []:
            text = (entry or "").strip()
            if not text:
                raise ValueError("list entries must be non-empty strings")
            cleaned.append(text)
        return cleaned
