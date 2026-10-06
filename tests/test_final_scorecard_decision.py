"""Step 11 changes to the Step 10 final scorecard: the "Final Human Decision"
placeholder is replaced by the CURRENT decision (decision, who, when) read through
the decision service, and stays "Not decided yet" until one exists. The rationale
is never on the scorecard. The disagreement placeholder is untouched.
"""

from __future__ import annotations

import uuid

import pytest

from app.database.models.user import UserRole
from app.services.auth_service import create_user
from app.services.final_decision_service import record_final_decision
from app.services.final_scorecard_service import (
    DISAGREEMENT_NOT_ASSESSED,
    FINAL_DECISION_NOT_DECIDED,
    get_final_scorecard,
)
from tests.test_final_scorecard_service import _seed

_WHY = "SCORECARDRATIONALE_3Q a considered, job-relevant rationale."


def _manager(db, role=UserRole.HIRING_MANAGER, name="Mia Manager"):
    return create_user(
        db=db, email=f"m-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name=name, role=role,
    )


def _card(db, s):
    return get_final_scorecard(db, s["app"].id, acting_user_id=s["hr"].id)


def test_until_a_decision_exists_the_placeholder_is_unchanged(db):
    s = _seed(db)
    v = _card(db, s)
    assert v.final_decision_status == FINAL_DECISION_NOT_DECIDED == "Not decided yet"
    assert v.final_decision is None


def test_a_recorded_decision_replaces_the_placeholder(db):
    s = _seed(db)
    mgr = _manager(db)
    rec = record_final_decision(
        db, application_id=s["app"].id, decision="HOLD", rationale=_WHY,
        acting_user_id=mgr.id,
    )
    v = _card(db, s)
    assert v.final_decision_status == "HOLD"
    assert v.final_decision.decision == "HOLD"
    assert v.final_decision.decided_by_name == "Mia Manager"
    assert v.final_decision.decided_at == rec.created_at


def test_the_scorecard_always_shows_the_current_decision_after_a_revision(db):
    s = _seed(db)
    mgr = _manager(db)
    for choice in ("PROCEED", "REJECT"):
        record_final_decision(
            db, application_id=s["app"].id, decision=choice, rationale=_WHY,
            acting_user_id=mgr.id,
        )
    assert _card(db, s).final_decision.decision == "REJECT"


def test_the_rationale_is_never_on_the_scorecard(db):
    s = _seed(db)
    record_final_decision(
        db, application_id=s["app"].id, decision="PROCEED", rationale=_WHY,
        acting_user_id=_manager(db).id,
    )
    v = _card(db, s)
    assert "SCORECARDRATIONALE" not in repr(v)
    assert not any("rationale" in f for f in v.__dataclass_fields__)
    assert not any(
        "rationale" in f for f in v.final_decision.__dataclass_fields__
    )


def test_hr_can_read_a_scorecard_that_carries_a_decision(db):
    s = _seed(db)
    record_final_decision(
        db, application_id=s["app"].id, decision="PROCEED", rationale=_WHY,
        acting_user_id=_manager(db, UserRole.ADMIN, "Ada Admin").id,
    )
    assert _card(db, s).final_decision.decided_by_name == "Ada Admin"   # read as HR


def test_the_disagreement_placeholder_is_untouched_by_a_decision(db):
    s = _seed(db)
    before = _card(db, s).disagreement_status
    record_final_decision(
        db, application_id=s["app"].id, decision="REJECT", rationale=_WHY,
        acting_user_id=_manager(db).id,
    )
    after = _card(db, s).disagreement_status
    assert before == after == DISAGREEMENT_NOT_ASSESSED


def test_reading_the_scorecard_never_writes_a_decision(db):
    from sqlalchemy import func, select

    from app.database.models.final_decision import FinalDecision

    s = _seed(db)
    _card(db, s)
    _card(db, s)
    assert db.execute(select(func.count(FinalDecision.id)).where(
        FinalDecision.application_id == s["app"].id)).scalar_one() == 0


@pytest.mark.parametrize("decision", ["PROCEED", "HOLD", "REJECT"])
def test_the_status_field_is_exactly_the_recorded_value(db, decision):
    s = _seed(db)
    record_final_decision(
        db, application_id=s["app"].id, decision=decision, rationale=_WHY,
        acting_user_id=_manager(db).id,
    )
    assert _card(db, s).final_decision_status == decision
