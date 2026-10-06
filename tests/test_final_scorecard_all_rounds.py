"""Step 10b changes to the Step 10 final scorecard: the Interview score now uses
ALL rounds (the shared ``final_scoring`` function), and the scorecard carries the
candidate's entry in the latest final ranking, read-only.

Reuses the single-candidate seed from ``test_final_scorecard_service``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database.models.application import Application
from app.services.final_ranking_service import generate_final_ranking
from app.services.final_scorecard_service import get_final_scorecard
from app.services.interview_feedback_service import create_interview_feedback
from tests.test_final_scorecard_service import _seed

D = Decimal


def _card(db, s):
    return get_final_scorecard(db, s["app"].id, acting_user_id=s["hr"].id)


def _round(db, s, number, ratings, *, notes="More notes about this round."):
    return create_interview_feedback(
        db, user_id=s["hr"].id, application_id=s["app"].id,
        interview_guide_id=s["guide"].id, interview_round=number,
        recommendation="HOLD", notes=notes,
        ratings=[
            {"competency_label": f"Skill {i}", "rating": r, "comment": None}
            for i, r in enumerate(ratings)
        ],
    )


def _age_first_round(db, s):
    s["feedback"].created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.flush()


def test_interview_score_uses_every_round_equally(db):
    s = _seed(db)                      # round 1: ratings 4 and 3 -> mean 3.5
    _age_first_round(db, s)
    _round(db, s, 2, [5])              # round 2: mean 5
    v = _card(db, s)
    # (3.5 + 5) / 2 = 4.25 -> x 2 = 8.50. The OLD latest-round-only score would
    # have been 10 (round 2 alone); pooling every rating would give 4.0 -> 8.00.
    assert v.interview.score == D("8.50")
    assert v.interview_rounds_used == (1, 2)
    assert v.interview_rounds_unscored == ()
    assert "Round 1: mean 3.50/5 from 2 rating(s)" in v.interview.evidence
    assert "Round 2: mean 5.00/5 from 1 rating(s)" in v.interview.evidence


def test_a_notes_only_round_is_excluded_listed_and_never_zero(db):
    s = _seed(db)
    _age_first_round(db, s)
    _round(db, s, 2, [])
    v = _card(db, s)
    assert v.interview.score == D("7.00")               # round 1 alone
    assert v.interview_rounds_used == (1,)
    assert v.interview_rounds_unscored == (2,)
    assert any(
        "Round 2: no ratings recorded — not scored" in line
        for line in v.interview.evidence
    )


def test_all_rounds_without_ratings_gives_no_score(db):
    s = _seed(db, ratings=[])
    v = _card(db, s)
    assert v.interview.score is None
    assert v.interview_rounds_used == () and v.interview_rounds_unscored == (1,)


def test_the_scorecard_and_the_final_ranking_agree_on_the_interview_score(db):
    s = _seed(db)
    _age_first_round(db, s)
    _round(db, s, 2, [5, 4, 5])
    generate_final_ranking(
        db, job_id=s["job"].id, requested_by_user_id=s["hr"].id
    )
    v = _card(db, s)
    assert v.final_ranking is not None
    assert v.final_ranking.interview_score == v.interview.score


def test_no_final_ranking_yet_is_reported_as_none(db):
    s = _seed(db)
    assert _card(db, s).final_ranking is None


def test_the_scorecard_carries_the_final_ranking_entry_as_stored(db):
    s = _seed(db)                       # screening 7.25, interview 7.00, HIGH/HIGH
    generate_final_ranking(
        db, job_id=s["job"].id, requested_by_user_id=s["hr"].id
    )
    f = _card(db, s).final_ranking
    assert f.entry_status == "RANKED"
    assert f.final_score == D("7.10")                   # 0.4 x 7.25 + 0.6 x 7.00
    assert (f.rank, f.ranked_count, f.tied) == (1, 1, False)
    assert f.final_confidence == "HIGH"
    assert (f.screening_weight, f.interview_weight) == (D("0.4000"), D("0.6000"))
    assert f.screening_score == D("7.25") and f.interview_score == D("7.00")


def test_the_scorecard_shows_a_stale_snapshot_as_stored_not_recomputed(db):
    s = _seed(db)
    generate_final_ranking(
        db, job_id=s["job"].id, requested_by_user_id=s["hr"].id
    )
    stored = _card(db, s).final_ranking.final_score
    _age_first_round(db, s)
    _round(db, s, 2, [1, 1])            # would change a recomputed score
    again = _card(db, s)
    assert again.final_ranking.final_score == stored    # read-only snapshot
    assert again.interview.score == D("4.50")           # (3.5 + 1) / 2 x 2 -- the bucket is live


def test_reading_the_scorecard_creates_no_run_and_no_audit_event(db):
    from sqlalchemy import func, select

    from app.database.models.audit_event import AuditEvent, AuditEventType
    from app.database.models.final_ranking import FinalRanking

    s = _seed(db)
    _card(db, s)
    assert db.execute(select(func.count(FinalRanking.id)).where(
        FinalRanking.job_id == s["job"].id)).scalar_one() == 0
    assert db.execute(select(func.count(AuditEvent.id)).where(
        AuditEvent.entity_id == s["job"].id,
        AuditEvent.event_type == AuditEventType.FINAL_RANKING_GENERATED.value,
    )).scalar_one() == 0
    assert db.get(Application, s["app"].id) is not None
