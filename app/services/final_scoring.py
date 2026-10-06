"""Final score and final ranking — deterministic, pure, no AI (CLAUDE.md §§5, 10,
20, 21; Phase 4 Step 10b).

Pure functions only: no database, no Streamlit, no Claude call, no I/O — the same
discipline as :mod:`app.services.screening_scoring`. Same inputs, same outputs.
:mod:`app.services.final_ranking_service` gathers the inputs and persists the
result; this module only does arithmetic and ordering.

THE FORMULA — verbatim (CLAUDE.md §20: "Document the scoring formula.")
=======================================================================
    interview_round_mean(r)  = mean of the competency ratings recorded in round r
    interview_score          = 2 x mean( interview_round_mean(r) for r in the
                                         rounds that have at least one rating )
                               rounded to 2 decimals, half-up        (0 - 10)
    screening_score          = the Step 5 ranking's overall_score (a 0-10 weighted
                               mean of the screening buckets) rounded to 2
                               decimals, half-up
    final_score              = round_half_up( 0.40 x screening_score
                                              + 0.60 x interview_score , 2 )

``SCREENING_WEIGHT = 0.40`` and ``INTERVIEW_WEIGHT = 0.60``; they sum to exactly
1 and are stored on every ranking run and shown to HR.

ROUNDING — Decimal, ROUND_HALF_UP
---------------------------------
All arithmetic is :class:`decimal.Decimal`; ``float`` is never used for a score.
Rounding is half-up (0.125 -> 0.13), NOT Python's ``round()`` (banker's rounding,
which turns 0.125 into 0.12). The screening input arrives as a ``float`` from the
Step 5 row and is converted with ``Decimal(str(value))`` so the stored 2-decimal
text, not the binary approximation, is what gets rounded.

The screening score and the interview score are each rounded to 2 decimals FIRST,
and the final score is computed from those ROUNDED values. Reason: HR sees three
numbers on screen — screening, interview, final — and must be able to reproduce
the third from the first two with a calculator. Computing from unrounded values
could make 6.67 x 0.4 + 8.33 x 0.6 disagree with the displayed final score in the
last digit.

WHY THE 1-5 RATING SCALE BECOMES 0-10 BY "x 2"
----------------------------------------------
Competency ratings run 1-5 (:data:`RATING_MAX`). 10 / 5 = 2, so a mean rating of
4 is a 8.00 interview score — "proportion of the maximum x 10", the same shape as
the screening buckets and Step 10's original single-round score. (A straight-1s
interview scores 2.00, not 0: the interviewer chose the lowest AVAILABLE rating,
which is not "zero merit".)

WHY THE MEAN OF ROUND MEANS (not the mean of all ratings)
---------------------------------------------------------
Every interview round counts EQUALLY. A round with eight ratings must not
outweigh a round with two just because it had more competencies. So each round
is averaged first, and the round means are averaged.

UNKNOWN STAYS UNKNOWN (CLAUDE.md §37)
-------------------------------------
* A round with no ratings (including notes-only feedback) is EXCLUDED from the
  mean — never counted as zero — and reported separately by the caller.
* If NO round has ratings the interview score is ``None``; if the screening score
  is ``None`` the final score is ``None``. The weights are NEVER redistributed to
  the part that exists and a missing part is NEVER scored as 0.

WHAT IS DELIBERATELY NOT AN INPUT
---------------------------------
No AI recommendation, no human recommendation, no disagreement, no confidence
value and no transcript text enters the score. :func:`compute_final_score` has no
recommendation parameter (a structural test pins that). Transcripts influence the
number only indirectly — through the Step 9 analysis a human reads beside it —
and the AI never assigns a numeric score.

RANKING — standard competition ranking ("1, 1, 3")
--------------------------------------------------
Candidates with an equal STORED (2-decimal) final score share a rank, and the
next rank skips: scores 9.00, 8.00, 8.00, 7.00 rank 1, 2, 2, 4. Only eligible
candidates that have a final score receive a rank; the ineligible and the
incomplete are never ranked and never placed above a ranked candidate. Within a
tied group the on-screen order is stable (screening score descending, then
application created_at ascending, then application id) but CARRIES NO MEANING —
the UI must say so.

FINAL CONFIDENCE (CLAUDE.md §21)
--------------------------------
The LOWEST of the screening confidence and the CURRENT post-interview analysis's
confidence (HIGH > MEDIUM > LOW). If there is no usable analysis the result is
capped at MEDIUM, because the evidence base has not been consolidated since the
interview — a weaker claim than the screening alone supports. Confidence
describes the evidence behind the number and is never multiplied into it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

#: Weights of the two components. Stored on every ranking run and shown to HR.
SCREENING_WEIGHT = Decimal("0.40")
INTERVIEW_WEIGHT = Decimal("0.60")

#: Top of the competency rating scale (1..RATING_MAX). Mirrors
#: ``app.database.models.interview_feedback.RATING_MAX``; kept as a local constant
#: so this module stays free of the persistence layer. A test asserts the two
#: agree.
RATING_MAX = 5

#: Top of the 0-10 score scale all three numbers are expressed on.
SCORE_MAX = Decimal(10)

_TWO_PLACES = Decimal("0.01")

#: Confidence ordering, lowest first.
CONFIDENCE_ORDER: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")

#: Reason fragments the service appends to ``status_reason``.
NO_ANALYSIS_NOTE = (
    "No post-interview analysis counted toward confidence, so it is capped at "
    "MEDIUM."
)


def round_half_up(value: Decimal | int | str, places: int = 2) -> Decimal:
    """Round to ``places`` decimals, half away from zero (0.125 -> 0.13)."""
    quantum = Decimal(1).scaleb(-places)
    return Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP)


def _is_rating(value: object) -> bool:
    """A real integer rating. ``bool`` is an ``int`` subclass and must not become
    rating 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def interview_round_means(
    ratings_by_round: Mapping[int, Sequence[int]],
) -> dict[int, Decimal]:
    """Mean rating per round, for the rounds that HAVE ratings.

    Rounds with no usable rating (empty, or notes-only feedback) are omitted —
    never present as 0. Means are exact ``Decimal`` quotients, unrounded: rounding
    happens once, on the final interview score. Rounds are returned ascending.
    """
    out: dict[int, Decimal] = {}
    for round_number in sorted(ratings_by_round):
        usable = [r for r in ratings_by_round[round_number] if _is_rating(r)]
        if usable:
            out[round_number] = Decimal(sum(usable)) / Decimal(len(usable))
    return out


def compute_interview_score_all_rounds(
    ratings_by_round: Mapping[int, Sequence[int]],
) -> Decimal | None:
    """Interview score 0-10 (2 decimals, half-up) from ALL rounds equally, or
    ``None`` if no round has a rating. See the module docstring for the formula."""
    means = interview_round_means(ratings_by_round)
    if not means:
        return None
    mean_of_means = sum(means.values()) / Decimal(len(means))
    score = mean_of_means * (SCORE_MAX / Decimal(RATING_MAX))
    return min(SCORE_MAX, max(Decimal(0), round_half_up(score, 2)))


def round_screening_score(value: float | Decimal | int | None) -> Decimal | None:
    """The Step 5 ranking's ``overall_score`` as a 2-decimal half-up Decimal.
    ``float`` input goes through ``str`` first (see the module docstring)."""
    if value is None:
        return None
    return round_half_up(Decimal(str(value)), 2)


def compute_final_score(
    *,
    screening_score: float | Decimal | int | None,
    interview_score: float | Decimal | int | None,
) -> Decimal | None:
    """``0.40 x screening + 0.60 x interview`` (2 decimals, half-up), or ``None``.

    ``None`` if EITHER part is missing: weights are never redistributed and a
    missing part is never scored as zero. Both inputs are rounded to 2 decimals
    first, so the result can be reproduced from the displayed numbers.

    There is deliberately no recommendation, confidence or transcript parameter.
    """
    if screening_score is None or interview_score is None:
        return None
    screening = round_half_up(Decimal(str(screening_score)), 2)
    interview = round_half_up(Decimal(str(interview_score)), 2)
    return round_half_up(
        SCREENING_WEIGHT * screening + INTERVIEW_WEIGHT * interview, 2
    )


# --- ranking --------------------------------------------------------------


@dataclass(frozen=True)
class RankInput:
    """What ranking needs to know about one candidate. No identity attribute
    (name, email, phone) is ever part of it."""

    application_id: uuid.UUID
    final_score: Decimal | None
    screening_score: Decimal | None
    created_at: datetime
    eligible: bool


@dataclass(frozen=True)
class RankedEntry:
    """``rank`` is ``None`` unless the candidate is eligible AND has a final
    score. ``tied`` is True when at least one other ranked candidate shares it."""

    application_id: uuid.UUID
    rank: int | None
    tied: bool


def _screening_desc(value: Decimal | None) -> tuple[int, Decimal]:
    return (0, Decimal(0)) if value is None else (1, value)


def rank_candidates(entries: Sequence[RankInput]) -> list[RankedEntry]:
    """Standard competition ranking over ONE partition, in display order.

    Ranked candidates come first, ordered by final score descending, then (a
    stable, meaningless-within-a-tie order) screening score descending, then
    ``created_at`` ascending, then application id. Unranked candidates follow in
    the same stable order. Pure; never reads an identity attribute.
    """
    rankable = [e for e in entries if e.eligible and e.final_score is not None]
    others = [e for e in entries if not (e.eligible and e.final_score is not None)]

    def key(e: RankInput):
        return (
            -(e.final_score if e.final_score is not None else Decimal(-1)),
            tuple(-x for x in _screening_desc(e.screening_score)),
            e.created_at.timestamp(),
            str(e.application_id),
        )

    rankable.sort(key=key)
    others.sort(key=key)

    scores = [e.final_score for e in rankable]
    out: list[RankedEntry] = []
    for index, entry in enumerate(rankable):
        # competition rank = 1 + the number of candidates strictly above
        rank = 1 + sum(1 for s in scores if s > entry.final_score)
        tied = scores.count(entry.final_score) > 1
        out.append(RankedEntry(entry.application_id, rank, tied))
    out.extend(RankedEntry(e.application_id, None, False) for e in others)
    return out


# --- confidence --------------------------------------------------------------


def compute_final_confidence(
    *, screening_confidence: str | None, analysis_confidence: str | None
) -> tuple[str, bool]:
    """``(confidence, analysis_was_missing)``.

    The lowest of the two confidences; with no usable analysis
    (``analysis_confidence is None``) capped at MEDIUM. An unrecognised or missing
    screening confidence is treated as LOW — unknown stays unknown, and the
    conservative reading is the safe one. Never blended into a score.
    """

    def level(value: str | None) -> int:
        return CONFIDENCE_ORDER.index(value) if value in CONFIDENCE_ORDER else 0

    screening = level(screening_confidence)
    if analysis_confidence is None:
        capped = min(screening, CONFIDENCE_ORDER.index("MEDIUM"))
        return CONFIDENCE_ORDER[capped], True
    return CONFIDENCE_ORDER[min(screening, level(analysis_confidence))], False
