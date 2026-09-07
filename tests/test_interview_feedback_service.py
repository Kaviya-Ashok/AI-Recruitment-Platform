"""Tests for app.services.interview_feedback_service (Phase 4 Step 8).

No AI in this step — nothing to mock at the Claude boundary. Real Postgres via
the savepoint-rollback ``db`` fixture; every upstream input (job, approved
rubric, application, shortlist entry, interview guide) is built directly with
the ORM or through the Step 6 service.

Every query is scoped to rows this test created (the suite runs against the
shared development database, which carries rows from manual testing).

TIMESTAMPS IN TESTS
-------------------
Postgres ``now()`` is *transaction-start* time, so two rows written inside one
test share an identical ``created_at``. Ordering tests therefore set
``created_at`` explicitly after insert — the same technique
``test_shortlist_service`` uses for ``ScreeningEvaluation.created_at`` — and one
test deliberately leaves them identical to pin down the ``id DESC`` tie-break.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.database.models.application import Application, ApplicationStatus
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.interview_feedback import (
    RATING_MAX,
    RATING_MIN,
    InterviewFeedback,
    InterviewFeedbackRating,
)
from app.database.models.interview_guide import InterviewGuide
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.screening_evaluation import ScreeningRecommendation
from app.database.models.user import SYSTEM_USER_ID, UserRole
from app.services.application_link_service import generate_link
from app.services.application_service import create_application
from app.services.auth_service import create_user
from app.services.interview_feedback_service import (
    InterviewFeedbackActorError,
    InterviewFeedbackDuplicateRoundError,
    InterviewFeedbackError,
    InterviewFeedbackPreconditionError,
    InterviewFeedbackTargetNotFoundError,
    InterviewFeedbackValidationError,
    create_interview_feedback,
    get_feedback_context,
    get_interview_feedback_for_application,
    get_latest_interview_feedback_for_application,
    get_suggested_next_interview_round,
    list_feedback_views,
)
from app.services.job_service import create_job
from app.services.shortlist_service import shortlist_candidate, unshortlist_candidate
from app.utils.authorization import UnauthorizedError

_T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

#: Distinguishes "caller did not pass a user" from the deliberate ``None``
#: actor the authorization tests pass in.
_UNSET = object()

_SENTINEL_NOTES = "SENTINEL_NOTES_candidate rambled about their last manager"
_SENTINEL_LABEL = "SENTINEL_LABEL_System design"
_SENTINEL_COMMENT = "SENTINEL_COMMENT_hesitant on sharding, strong on caching"


# --- seed helpers -----------------------------------------------------


def _hr(db, *, full_name="Dana Interviewer"):
    return create_user(
        db=db, email=f"hr-{uuid.uuid4().hex}@x.test",
        plain_password="pw-for-tests-only", full_name=full_name,
        role=UserRole.HR,
    )


def _rubric(db, job, hr):
    rv = RubricVersion(
        job_id=job.id, version_number=1, status=RubricVersionStatus.APPROVED,
        generated_from_requirements_version=1, created_by=hr.id,
    )
    db.add(rv)
    db.flush()
    db.add(RubricCriterion(
        rubric_version_id=rv.id, requirement_type="MANDATORY", category=None,
        criterion_text="5+ years Python", display_order=1,
    ))
    db.flush()
    return rv


def _application(db, job, link, *, full_name="Casey Candidate"):
    app = create_application(
        db, job_id=job.id, application_link_id=link.id,
        email=f"c-{uuid.uuid4().hex}@x.com", full_name=full_name, phone=None,
    )
    app.status = ApplicationStatus.SCREENING_EVALUATED
    db.flush()
    return app


def _guide(db, job, app, entry, rubric):
    """An ``interview_guides`` row built directly with the ORM.

    Step 7's ``generate_interview_guide`` would need an AI call; this step only
    needs the row to exist, so it is inserted straight rather than mocked.
    """
    guide = InterviewGuide(
        job_id=job.id, application_id=app.id, shortlist_entry_id=entry.id,
        rubric_version_id=rubric.id, ai_model="claude-sonnet-5",
        generated_at=_T0,
    )
    db.add(guide)
    db.flush()
    return guide


def _seed(db, *, candidate_name="Casey Candidate", hr_name="Dana Interviewer"):
    """HR + job + approved rubric + evaluated application + shortlist entry +
    interview guide — the minimum state Step 8 requires."""
    hr = _hr(db, full_name=hr_name)
    job = create_job(
        db, title="Backend Engineer", department="Eng",
        jd_input_method=JdInputMethod.TEXT_PASTE, jd_source_text="JD.",
        created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    rubric = _rubric(db, job, hr)
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = _application(db, job, link, full_name=candidate_name)
    entry = shortlist_candidate(
        db, job_id=job.id, application_id=app.id, rubric_version_id=rubric.id,
        requested_by_user_id=hr.id,
    )
    guide = _guide(db, job, app, entry, rubric)
    return {
        "hr": hr, "job": job, "rubric": rubric, "link": link,
        "app": app, "entry": entry, "guide": guide,
    }


def _submit(db, s, *, round_=1, recommendation="PROCEED", notes="Went well.",
            ratings=None, user_id=_UNSET):
    return create_interview_feedback(
        db,
        user_id=s["hr"].id if user_id is _UNSET else user_id,
        application_id=s["app"].id,
        interview_guide_id=s["guide"].id,
        interview_round=round_,
        recommendation=recommendation,
        notes=notes,
        ratings=[{"competency_label": "Coding", "rating": 4, "comment": None}]
        if ratings is None else ratings,
    )


# --- scoped query helpers (never unfiltered) --------------------------


def _rows(db, application_id) -> list[InterviewFeedback]:
    return list(db.execute(
        select(InterviewFeedback)
        .where(InterviewFeedback.application_id == application_id)
        .order_by(InterviewFeedback.interview_round)
    ).scalars().all())


def _row_count(db, application_id) -> int:
    return db.execute(
        select(func.count(InterviewFeedback.id))
        .where(InterviewFeedback.application_id == application_id)
    ).scalar_one()


def _ratings(db, feedback_id) -> list[InterviewFeedbackRating]:
    return list(db.execute(
        select(InterviewFeedbackRating)
        .where(InterviewFeedbackRating.interview_feedback_id == feedback_id)
        .order_by(InterviewFeedbackRating.competency_label)
    ).scalars().all())


def _orphan_rating_count(db, application_id) -> int:
    """Rating rows whose parent belongs to this application."""
    return db.execute(
        select(func.count(InterviewFeedbackRating.id))
        .join(
            InterviewFeedback,
            InterviewFeedback.id == InterviewFeedbackRating.interview_feedback_id,
        )
        .where(InterviewFeedback.application_id == application_id)
    ).scalar_one()


def _events(db, application_id, event_type) -> list[AuditEvent]:
    return list(db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == application_id,
            AuditEvent.event_type == event_type.value,
        )
    ).scalars().all())


def _set_created_at(db, feedback, moment):
    feedback.created_at = moment
    db.flush()


# --- 1. model sanity ---------------------------------------------------


def test_rows_and_child_ratings_are_created_and_read_back(db):
    s = _seed(db)
    fb = _submit(db, s, ratings=[
        {"competency_label": "System design", "rating": 5, "comment": "Deep."},
        {"competency_label": "Communication", "rating": 3, "comment": None},
    ])

    stored = db.get(InterviewFeedback, fb.id)
    assert stored is not None
    assert stored.application_id == s["app"].id
    assert stored.interview_guide_id == s["guide"].id
    assert stored.interview_round == 1
    assert stored.recommendation == "PROCEED"
    assert stored.notes == "Went well."
    assert stored.created_at is not None

    children = _ratings(db, fb.id)
    assert [(c.competency_label, c.rating, c.comment) for c in children] == [
        ("Communication", 3, None),
        ("System design", 5, "Deep."),
    ]


# --- 2. happy path ------------------------------------------------------


def test_successful_submission_persists_everything(db):
    s = _seed(db)
    fb = _submit(db, s, recommendation="HOLD", ratings=[
        {"competency_label": "  Coding  ", "rating": 4, "comment": "  ok  "},
    ])
    assert _row_count(db, s["app"].id) == 1
    assert fb.recommendation == "HOLD"
    # free text is stripped, never silently dropped
    child = _ratings(db, fb.id)[0]
    assert child.competency_label == "Coding"
    assert child.comment == "ok"


def test_notes_are_optional_and_blank_becomes_null(db):
    s = _seed(db)
    fb = _submit(db, s, notes="   ", ratings=[])
    assert fb.notes is None
    assert _ratings(db, fb.id) == []


# --- 3/4. multiple rounds coexist; history is preserved -----------------


def test_multiple_rounds_coexist_as_separate_rows(db):
    s = _seed(db)
    _submit(db, s, round_=1, recommendation="PROCEED", notes="Round one.")
    _submit(db, s, round_=2, recommendation="HOLD", notes="Round two.")

    rows = _rows(db, s["app"].id)
    assert [r.interview_round for r in rows] == [1, 2]
    assert [r.recommendation for r in rows] == ["PROCEED", "HOLD"]


def test_creating_round_2_does_not_alter_or_remove_round_1(db):
    s = _seed(db)
    first = _submit(db, s, round_=1, recommendation="PROCEED", notes="Original.")
    before = (
        first.id, first.interview_round, first.recommendation, first.notes,
        first.submitted_by_user_id, first.created_at,
    )

    other_hr = _hr(db, full_name="Second Interviewer")
    _submit(db, s, round_=2, recommendation="REJECT", notes="Different view.",
            user_id=other_hr.id)

    db.expire_all()
    reread = db.get(InterviewFeedback, before[0])
    assert (
        reread.id, reread.interview_round, reread.recommendation, reread.notes,
        reread.submitted_by_user_id, reread.created_at,
    ) == before
    assert _row_count(db, s["app"].id) == 2


# --- 5. UNIQUE(application_id, interview_round) --------------------------


def test_duplicate_round_raises_a_business_error_not_a_db_exception(db):
    s = _seed(db)
    _submit(db, s, round_=1, notes="First write-up.")

    with pytest.raises(InterviewFeedbackDuplicateRoundError) as exc:
        _submit(db, s, round_=1, notes="Accidental resubmit.")

    # A readable business message, and part of this module's own error tree.
    assert isinstance(exc.value, InterviewFeedbackError)
    assert "already been recorded" in str(exc.value)
    # the original is untouched and nothing extra was written
    assert _row_count(db, s["app"].id) == 1
    assert _rows(db, s["app"].id)[0].notes == "First write-up."
    assert len(_events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED)) == 1


def test_the_same_round_number_is_allowed_on_a_different_application(db):
    """UNIQUE is per (application, round) — never per round alone."""
    s1 = _seed(db)
    s2 = _seed(db)
    _submit(db, s1, round_=1)
    _submit(db, s2, round_=1)
    assert _row_count(db, s1["app"].id) == 1
    assert _row_count(db, s2["app"].id) == 1


# --- 6/7/8/9. interview_round semantics ---------------------------------


def test_suggested_round_is_1_when_no_feedback_exists(db):
    s = _seed(db)
    assert get_suggested_next_interview_round(db, s["app"].id) == 1


def test_suggested_round_is_max_plus_one(db):
    s = _seed(db)
    _submit(db, s, round_=1)
    assert get_suggested_next_interview_round(db, s["app"].id) == 2
    _submit(db, s, round_=2)
    assert get_suggested_next_interview_round(db, s["app"].id) == 3
    # a gap is respected: MAX+1, never "count+1"
    _submit(db, s, round_=7)
    assert get_suggested_next_interview_round(db, s["app"].id) == 8


def test_submitted_round_is_stored_verbatim_and_never_replaced_by_the_suggestion(db):
    s = _seed(db)
    assert get_suggested_next_interview_round(db, s["app"].id) == 1

    # HR overrides the suggestion with a much later round.
    fb = _submit(db, s, round_=7)
    assert fb.interview_round == 7
    assert db.get(InterviewFeedback, fb.id).interview_round == 7

    # ...and the suggestion follows the stored value, not the other way round.
    assert get_suggested_next_interview_round(db, s["app"].id) == 8


def test_round_is_never_derived_from_created_at_or_insertion_order(db):
    """Round 2 is written up first; round 1 is back-filled a week later.

    Insertion order and round order deliberately diverge — both must persist
    independently and correctly.
    """
    s = _seed(db)
    later = _submit(db, s, round_=2, recommendation="HOLD", notes="Panel round.")
    _set_created_at(db, later, _T0)

    earlier = _submit(db, s, round_=1, recommendation="PROCEED",
                      notes="Back-filled phone screen.")
    _set_created_at(db, earlier, _T0 + timedelta(days=7))

    db.expire_all()
    by_round = {r.interview_round: r for r in _rows(db, s["app"].id)}
    assert set(by_round) == {1, 2}
    # round 1 was inserted second and carries the LATER timestamp
    assert by_round[1].created_at > by_round[2].created_at
    assert by_round[1].recommendation == "PROCEED"
    assert by_round[2].recommendation == "HOLD"
    # the suggestion still comes from MAX(round), not from recency
    assert get_suggested_next_interview_round(db, s["app"].id) == 3


# --- 10/11. history and "latest" ordering --------------------------------


def test_history_returns_every_round_newest_first(db):
    s = _seed(db)
    a = _submit(db, s, round_=1, notes="oldest")
    _set_created_at(db, a, _T0)
    b = _submit(db, s, round_=2, notes="middle")
    _set_created_at(db, b, _T0 + timedelta(hours=1))
    c = _submit(db, s, round_=3, notes="newest")
    _set_created_at(db, c, _T0 + timedelta(hours=2))

    db.expire_all()
    history = get_interview_feedback_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert [r.id for r in history] == [c.id, b.id, a.id]
    assert [r.interview_round for r in history] == [3, 2, 1]


def test_latest_uses_created_at_desc_not_the_highest_round(db):
    """Round 1 written up last is the *latest* record, even though round 2 has
    the higher number — 'latest' is recency, never round order."""
    s = _seed(db)
    two = _submit(db, s, round_=2, notes="written first")
    _set_created_at(db, two, _T0)
    one = _submit(db, s, round_=1, notes="written last")
    _set_created_at(db, one, _T0 + timedelta(days=1))

    db.expire_all()
    latest = get_latest_interview_feedback_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert latest.id == one.id
    assert latest.interview_round == 1


def test_latest_is_deterministic_when_timestamps_tie(db):
    """Two rows sharing a ``created_at`` (Postgres ``now()`` is transaction
    time) resolve by ``id DESC`` — deterministic, never arbitrary."""
    s = _seed(db)
    a = _submit(db, s, round_=1)
    b = _submit(db, s, round_=2)
    _set_created_at(db, a, _T0)
    _set_created_at(db, b, _T0)

    db.expire_all()
    expected_first = max(a.id, b.id)
    history = get_interview_feedback_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert [r.id for r in history] == sorted([a.id, b.id], reverse=True)
    latest = get_latest_interview_feedback_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    )
    assert latest.id == expected_first


def test_latest_is_none_when_no_feedback_exists(db):
    s = _seed(db)
    assert get_latest_interview_feedback_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    ) is None
    assert get_interview_feedback_for_application(
        db, s["app"].id, acting_user_id=s["hr"].id
    ) == []


def test_history_is_scoped_to_one_application(db):
    s1 = _seed(db)
    s2 = _seed(db)
    _submit(db, s1, round_=1)
    _submit(db, s2, round_=1)
    _submit(db, s2, round_=2)

    assert len(get_interview_feedback_for_application(
        db, s1["app"].id, acting_user_id=s1["hr"].id
    )) == 1
    assert len(get_interview_feedback_for_application(
        db, s2["app"].id, acting_user_id=s2["hr"].id
    )) == 2


# --- 12/13. guide preconditions ------------------------------------------


def test_submission_rejected_when_the_guide_does_not_exist(db):
    s = _seed(db)
    with pytest.raises(InterviewFeedbackTargetNotFoundError):
        create_interview_feedback(
            db, user_id=s["hr"].id, application_id=s["app"].id,
            interview_guide_id=uuid.uuid4(), interview_round=1,
            recommendation="PROCEED", notes="x", ratings=[],
        )
    assert _row_count(db, s["app"].id) == 0


def test_submission_rejected_when_the_application_does_not_exist(db):
    s = _seed(db)
    with pytest.raises(InterviewFeedbackTargetNotFoundError):
        create_interview_feedback(
            db, user_id=s["hr"].id, application_id=uuid.uuid4(),
            interview_guide_id=s["guide"].id, interview_round=1,
            recommendation="PROCEED", notes="x", ratings=[],
        )


def test_submission_rejected_when_the_guide_belongs_to_another_application(db):
    """A cross-application guide/application pair is a clear business error,
    never a raw FK or constraint failure."""
    mine = _seed(db)
    theirs = _seed(db)

    with pytest.raises(InterviewFeedbackPreconditionError) as exc:
        create_interview_feedback(
            db, user_id=mine["hr"].id, application_id=mine["app"].id,
            interview_guide_id=theirs["guide"].id, interview_round=1,
            recommendation="PROCEED", notes="x", ratings=[],
        )
    assert "different candidate" in str(exc.value)
    assert _row_count(db, mine["app"].id) == 0
    assert _row_count(db, theirs["app"].id) == 0


# --- 14. shortlist decoupling (regression) -------------------------------


def test_feedback_can_be_recorded_after_the_candidate_is_unshortlisted(db):
    """An interview that actually happened stays recordable. This service must
    never consult ``candidate_shortlist_entries``."""
    s = _seed(db)
    unshortlist_candidate(
        db, job_id=s["job"].id, application_id=s["app"].id,
        requested_by_user_id=s["hr"].id,
    )
    entry = db.get(type(s["entry"]), s["entry"].id)
    assert entry.is_shortlisted is False

    fb = _submit(db, s, round_=1, notes="Interview already happened.")
    assert fb.id is not None
    assert _row_count(db, s["app"].id) == 1


# --- 15/16. actor -------------------------------------------------------


def test_submitted_by_user_id_is_the_real_authenticated_user(db):
    s = _seed(db)
    fb = _submit(db, s)
    assert fb.submitted_by_user_id == s["hr"].id
    assert fb.submitted_by_user_id != SYSTEM_USER_ID
    ev = _events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED)[0]
    assert ev.user_id == s["hr"].id


def test_system_actor_is_rejected_as_a_submitter(db):
    """The SYSTEM row passes ``require_internal_user`` by design, so this
    service rejects it explicitly — an automated pipeline never sat in an
    interview."""
    s = _seed(db)
    with pytest.raises(InterviewFeedbackActorError) as exc:
        _submit(db, s, user_id=SYSTEM_USER_ID)
    assert isinstance(exc.value, InterviewFeedbackError)
    assert "automated pipeline" in str(exc.value)

    assert _row_count(db, s["app"].id) == 0
    assert _events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED) == []


def test_writes_and_sensitive_reads_are_denied_for_a_bad_user(db):
    s = _seed(db)
    for bad in (None, uuid.uuid4(), "nope"):
        with pytest.raises(UnauthorizedError):
            _submit(db, s, user_id=bad)
        with pytest.raises(UnauthorizedError):
            get_interview_feedback_for_application(
                db, s["app"].id, acting_user_id=bad
            )
        with pytest.raises(UnauthorizedError):
            get_latest_interview_feedback_for_application(
                db, s["app"].id, acting_user_id=bad
            )
        with pytest.raises(UnauthorizedError):
            list_feedback_views(db, s["app"].id, acting_user_id=bad)
        with pytest.raises(UnauthorizedError):
            get_feedback_context(db, s["app"].id, acting_user_id=bad)
    assert _row_count(db, s["app"].id) == 0


def test_an_inactive_account_cannot_submit(db):
    s = _seed(db)
    s["hr"].is_active = False
    db.flush()
    with pytest.raises(UnauthorizedError):
        _submit(db, s)
    assert _row_count(db, s["app"].id) == 0


# --- 17. recommendation validation ---------------------------------------


@pytest.mark.parametrize(
    "recommendation", sorted(ScreeningRecommendation.ALL)
)
def test_all_three_recommendations_are_accepted_including_reject(db, recommendation):
    """A human may recommend REJECT — unlike the automated screening path,
    which is restricted to ScreeningRecommendation.AUTOMATED."""
    s = _seed(db)
    fb = _submit(db, s, recommendation=recommendation)
    assert fb.recommendation == recommendation


def test_reject_is_reachable_here_although_the_automated_path_forbids_it(db):
    s = _seed(db)
    fb = _submit(db, s, recommendation=ScreeningRecommendation.REJECT)
    assert fb.recommendation == "REJECT"
    assert "REJECT" not in ScreeningRecommendation.AUTOMATED


@pytest.mark.parametrize(
    "bad", ["", "  ", "MAYBE", "proceed", "Proceed", "PASS", None, 1, ["PROCEED"]]
)
def test_invalid_recommendations_are_rejected(db, bad):
    s = _seed(db)
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, recommendation=bad)
    assert _row_count(db, s["app"].id) == 0


# --- 18/19. rating validation --------------------------------------------


@pytest.mark.parametrize("value", [RATING_MIN, 2, 3, 4, RATING_MAX])
def test_ratings_inside_the_scale_are_accepted(db, value):
    s = _seed(db)
    fb = _submit(db, s, ratings=[
        {"competency_label": "Coding", "rating": value, "comment": None},
    ])
    assert _ratings(db, fb.id)[0].rating == value


@pytest.mark.parametrize("value", [0, -1, 6, 99])
def test_ratings_outside_the_scale_are_rejected(db, value):
    s = _seed(db)
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, ratings=[
            {"competency_label": "Coding", "rating": value, "comment": None},
        ])
    assert _row_count(db, s["app"].id) == 0


@pytest.mark.parametrize("value", ["3", 3.5, None, True, False, [3]])
def test_non_integer_ratings_are_rejected(db, value):
    """``True`` is included deliberately: ``bool`` is an ``int`` subclass in
    Python and must not silently become rating 1."""
    s = _seed(db)
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, ratings=[
            {"competency_label": "Coding", "rating": value, "comment": None},
        ])
    assert _row_count(db, s["app"].id) == 0


@pytest.mark.parametrize("label", ["", "   ", "\t\n ", None, 5])
def test_blank_or_missing_competency_labels_are_rejected(db, label):
    s = _seed(db)
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, ratings=[
            {"competency_label": label, "rating": 3, "comment": None},
        ])
    assert _row_count(db, s["app"].id) == 0


@pytest.mark.parametrize("bad_round", [0, -3, "1", 1.0, None, True])
def test_invalid_interview_rounds_are_rejected(db, bad_round):
    s = _seed(db)
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, round_=bad_round)
    assert _row_count(db, s["app"].id) == 0


def test_an_empty_rating_list_is_allowed(db):
    """CLAUDE.md §7 lists notes, ratings, comments and a recommendation without
    requiring all four — notes-only feedback is legitimate."""
    s = _seed(db)
    fb = _submit(db, s, ratings=[])
    assert _ratings(db, fb.id) == []
    assert fb.notes == "Went well."


# --- 20/21. audit events --------------------------------------------------


def test_exactly_one_human_feedback_submitted_event_per_submission(db):
    s = _seed(db)
    _submit(db, s, round_=1)
    assert len(_events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED)) == 1
    _submit(db, s, round_=2)
    assert len(_events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED)) == 2


def test_audit_metadata_carries_the_structural_facts(db):
    s = _seed(db)
    fb = _submit(db, s, round_=3, recommendation="REJECT", ratings=[
        {"competency_label": "A", "rating": 2, "comment": None},
        {"competency_label": "B", "rating": 5, "comment": "good"},
    ])
    ev = _events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED)[0]
    assert ev.entity_type == "application"
    assert ev.entity_id == s["app"].id
    assert ev.new_state == {
        "application_id": str(s["app"].id),
        "interview_feedback_id": str(fb.id),
        "interview_guide_id": str(s["guide"].id),
        "interview_round": 3,
        "recommendation": "REJECT",
        "rating_count": 2,
    }


def test_no_other_human_interview_event_is_ever_emitted(db):
    """HUMAN_INTERVIEW_COMPLETED and HUMAN_RECOMMENDATION_SUBMITTED stay
    declared-but-unemitted — Step 8 owns only HUMAN_FEEDBACK_SUBMITTED."""
    s = _seed(db)
    _submit(db, s, round_=1)
    _submit(db, s, round_=2, recommendation="REJECT")
    for unexpected in (
        AuditEventType.HUMAN_INTERVIEW_COMPLETED,
        AuditEventType.HUMAN_RECOMMENDATION_SUBMITTED,
        AuditEventType.AI_HUMAN_DISAGREEMENT_DETECTED,
        AuditEventType.POST_INTERVIEW_ANALYSIS_COMPLETED,
        AuditEventType.FINAL_DECISION_SUBMITTED,
    ):
        assert _events(db, s["app"].id, unexpected) == []


# --- 22. audit safety: interviewer free text never leaks ------------------


def test_free_text_never_appears_in_audit_metadata(db):
    s = _seed(db)
    fb = _submit(db, s, notes=_SENTINEL_NOTES, ratings=[
        {"competency_label": _SENTINEL_LABEL, "rating": 4,
         "comment": _SENTINEL_COMMENT},
    ])

    for ev in db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == s["app"].id)
    ).scalars().all():
        blob = (
            f"{ev.action} || {ev.previous_state} || {ev.new_state} || "
            f"{ev.event_metadata}"
        )
        for sentinel in ("SENTINEL_NOTES", "SENTINEL_LABEL", "SENTINEL_COMMENT"):
            assert sentinel not in blob

    # ...but every sentinel IS preserved verbatim on the rows themselves.
    stored = db.get(InterviewFeedback, fb.id)
    assert stored.notes == _SENTINEL_NOTES
    child = _ratings(db, fb.id)[0]
    assert child.competency_label == _SENTINEL_LABEL
    assert child.comment == _SENTINEL_COMMENT


def test_candidate_name_and_email_never_appear_in_audit_metadata(db):
    s = _seed(db, candidate_name="Wilhelmina Uniquename")
    _submit(db, s)
    for ev in db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == s["app"].id)
    ).scalars().all():
        blob = (
            f"{ev.action} || {ev.previous_state} || {ev.new_state} || "
            f"{ev.event_metadata}"
        )
        assert "Uniquename" not in blob


# --- 23. atomicity --------------------------------------------------------


def test_a_bad_rating_midway_through_a_batch_persists_nothing(db):
    """Validation completes before the first write, so a rejected submission
    leaves no parent row, no child rows and no audit event."""
    s = _seed(db)
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, ratings=[
            {"competency_label": "Good one", "rating": 4, "comment": "fine"},
            {"competency_label": "Also good", "rating": 5, "comment": None},
            {"competency_label": "Bad one", "rating": 99, "comment": None},
            {"competency_label": "Never reached", "rating": 3, "comment": None},
        ])

    assert _row_count(db, s["app"].id) == 0
    assert _orphan_rating_count(db, s["app"].id) == 0
    assert _events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED) == []


def test_a_failed_submission_leaves_an_earlier_one_intact(db):
    s = _seed(db)
    good = _submit(db, s, round_=1, notes="Kept.", ratings=[
        {"competency_label": "Coding", "rating": 4, "comment": None},
    ])
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, round_=2, ratings=[
            {"competency_label": "", "rating": 3, "comment": None},
        ])

    assert _row_count(db, s["app"].id) == 1
    assert _orphan_rating_count(db, s["app"].id) == 1
    assert db.get(InterviewFeedback, good.id).notes == "Kept."
    assert len(_events(db, s["app"].id, AuditEventType.HUMAN_FEEDBACK_SUBMITTED)) == 1


# --- application status is never touched ----------------------------------


def test_application_status_never_changes(db):
    """Step 8 adds no lifecycle status and writes none (same rule as Step 6)."""
    s = _seed(db)
    before = db.get(Application, s["app"].id).status
    _submit(db, s, round_=1)
    _submit(db, s, round_=2, recommendation="REJECT")
    db.expire_all()
    assert db.get(Application, s["app"].id).status == before
    assert before == ApplicationStatus.SCREENING_EVALUATED
    assert not any(
        v.startswith("INTERVIEW") or "FEEDBACK" in v
        for v in ApplicationStatus.ALL
    )


# --- UI-facing projections -------------------------------------------------


def test_list_feedback_views_resolves_names_and_orders_newest_first(db):
    s = _seed(db, hr_name="Dana Interviewer")
    a = _submit(db, s, round_=1, notes="first", ratings=[
        {"competency_label": "Zeta", "rating": 2, "comment": "z"},
        {"competency_label": "Alpha", "rating": 5, "comment": None},
    ])
    _set_created_at(db, a, _T0)
    b = _submit(db, s, round_=2, notes="second", ratings=[])
    _set_created_at(db, b, _T0 + timedelta(hours=1))

    db.expire_all()
    views = list_feedback_views(db, s["app"].id, acting_user_id=s["hr"].id)
    assert [v.interview_round for v in views] == [2, 1]
    assert views[0].submitted_by_name == "Dana Interviewer"
    assert views[0].ratings == []
    # ratings come back sorted by competency label
    assert [r.competency_label for r in views[1].ratings] == ["Alpha", "Zeta"]
    assert views[1].ratings[0].rating == 5
    assert views[1].notes == "first"


def test_feedback_context_resolves_candidate_and_guide(db):
    s = _seed(db, candidate_name="Casey Candidate")
    ctx = get_feedback_context(db, s["app"].id, acting_user_id=s["hr"].id)
    assert ctx.candidate_name == "Casey Candidate"
    assert ctx.interview_guide_id == s["guide"].id
    assert ctx.suggested_round == 1
    assert ctx.feedback_count == 0

    _submit(db, s, round_=1)
    ctx = get_feedback_context(db, s["app"].id, acting_user_id=s["hr"].id)
    assert ctx.suggested_round == 2
    assert ctx.feedback_count == 1


def test_feedback_context_reports_a_missing_guide_without_failing(db):
    """No guide yet -> the page explains why the form is unavailable instead of
    letting the submit fail."""
    hr = _hr(db)
    job = create_job(
        db, title="J", department="E", jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="JD.", created_by_user_id=hr.id,
    )
    job.status = JobStatus.RUBRIC_APPROVED
    db.flush()
    link = generate_link(db, job_id=job.id, requested_by_user_id=hr.id)
    app = _application(db, job, link)

    ctx = get_feedback_context(db, app.id, acting_user_id=hr.id)
    assert ctx is not None
    assert ctx.interview_guide_id is None
    assert ctx.suggested_round == 1


def test_feedback_context_is_none_for_an_unknown_application(db):
    hr = _hr(db)
    assert get_feedback_context(db, uuid.uuid4(), acting_user_id=hr.id) is None


# --- form helper (pure, no st.*) ------------------------------------------


def test_usable_ratings_drops_only_entirely_untouched_rows():
    from app.pages.interviews import _usable_ratings

    kept = _usable_ratings([
        {"competency_label": "  Coding  ", "rating": 4, "comment": "  solid "},
        {"competency_label": "", "rating": None, "comment": ""},
        {"competency_label": "   ", "rating": None, "comment": "   "},
        {"competency_label": "Design", "rating": None, "comment": ""},
        {"competency_label": "", "rating": 3, "comment": ""},
    ])
    # the two blank rows are dropped; the two HALF-filled rows are kept so the
    # service can reject them with a readable message rather than losing input
    assert kept == [
        {"competency_label": "Coding", "rating": 4, "comment": "solid"},
        {"competency_label": "Design", "rating": None, "comment": None},
        {"competency_label": "", "rating": 3, "comment": None},
    ]


def test_usable_ratings_handles_an_empty_form():
    from app.pages.interviews import _usable_ratings

    assert _usable_ratings([]) == []
    assert _usable_ratings([
        {"competency_label": "", "rating": None, "comment": ""},
    ]) == []


# --- UI smoke test (Streamlit API surface only) ----------------------------
#
# Page ``_render_*`` functions are not unit-tested per project convention, but a
# render that raises would take the whole Interviews page down. This exercises
# every widget call in the new section against real Streamlit, with the service
# stubbed out — it asserts "the page renders", never business behaviour.

_FEEDBACK_RENDER_SCRIPT = '''
import uuid
from datetime import datetime, timezone

import streamlit as st

import app.pages.interviews as I
from app.services.interview_feedback_service import (
    InterviewFeedbackContext,
    InterviewFeedbackRatingView,
    InterviewFeedbackView,
)
from app.utils.session import SESSION_USER_KEY

st.session_state[SESSION_USER_KEY] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "email": "p@x.test",
    "full_name": "Dana Interviewer",
    "role": "HR",
}

APP_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
GUIDE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)

STATE = {
    APP_ID: {
        "context": InterviewFeedbackContext(
            application_id=APP_ID,
            candidate_name="Casey Candidate",
            candidate_email="casey@x.com",
            interview_guide_id=GUIDE_ID,
            suggested_round=2,
            feedback_count=1,
        ),
        "history": [
            InterviewFeedbackView(
                feedback_id=uuid.uuid4(), application_id=APP_ID,
                interview_guide_id=GUIDE_ID, interview_round=1,
                recommendation="HOLD", notes="Solid but unproven at scale.",
                submitted_by_user_id=uuid.uuid4(),
                submitted_by_name="Dana Interviewer", created_at=NOW,
                ratings=[
                    InterviewFeedbackRatingView("System design", 3, "Shaky on sharding."),
                    InterviewFeedbackRatingView("Communication", 5, None),
                ],
            )
        ],
    }
}

I._render_feedback_section(APP_ID, STATE, uuid.uuid4())
'''


def test_feedback_section_renders_without_raising():
    from streamlit.testing.v1 import AppTest

    from app.pages.interviews import _RATING_SLOTS as I_RATING_SLOTS

    at = AppTest.from_string(_FEEDBACK_RENDER_SCRIPT, default_timeout=30).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    body = " ".join(
        [m.value for m in at.markdown]
        + [c.value for c in at.caption]
    )
    # the round is a phrase, not a bare number; the recommendation carries its
    # text label (CLAUDE.md §26 — never colour alone); human authorship is
    # stated explicitly
    assert "Round 1" in body
    assert "Interviewer recommends: Hold" in body
    assert "Human assessment — not AI-generated" in body
    # candidate and interviewer are resolved names, shown read-only
    assert "Casey Candidate" in body
    assert "Dana Interviewer" in body
    # ...and no raw uuid reaches the screen
    assert "2222-2222" not in body
    assert "3333-3333" not in body

    # the form actually rendered: round picker, 5 competency rows, notes,
    # recommendation radio, submit button
    assert at.selectbox(key="fb_round_22222222-2222-2222-2222-222222222222")
    assert len(at.text_input) == I_RATING_SLOTS * 2  # label + comment per row
    assert at.text_area(key="fb_notes_22222222-2222-2222-2222-222222222222")
    radio = at.radio(key="fb_rec_22222222-2222-2222-2222-222222222222")
    # AppTest reports the *rendered* options, so this also pins that all three
    # human recommendations are offered (REJECT included) and that each is
    # shown through ``label_for`` rather than as a raw enum token.
    assert list(radio.options) == ["Proceed", "Hold", "Reject"]


@pytest.mark.parametrize("entry", ["Coding:4", ("Coding", 4), 4, None])
def test_a_rating_entry_that_is_not_a_mapping_is_rejected(db, entry):
    s = _seed(db)
    with pytest.raises(InterviewFeedbackValidationError):
        _submit(db, s, ratings=[entry])
    assert _row_count(db, s["app"].id) == 0
