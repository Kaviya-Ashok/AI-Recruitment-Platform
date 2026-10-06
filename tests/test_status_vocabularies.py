"""Vocabulary tests for the Phase 4 status / audit-event additions
(CLAUDE.md §2A items 3, 4, 5).

These classes are validated-string vocabularies, not DB enums, so nothing in
Postgres stops a typo — these tests are the guard. They pin the exact member
set so an accidental rename or deletion fails loudly.
"""

from __future__ import annotations

import pytest

from app.database.models.application import ApplicationStatus
from app.database.models.audit_event import AuditEventType
from app.database.models.screening_question import (
    ScreeningQuestionCategory,
    ScreeningQuestionRound,
)
from app.database.models.post_interview_analysis import (
    PostInterviewAnalysisStatus,
)
from app.database.models.screening_session import ScreeningSessionStatus
from app.database.models.user import (
    SYSTEM_USER_EMAIL,
    SYSTEM_USER_ID,
    UserRole,
)

# --- ApplicationStatus -------------------------------------------------

_EXPECTED_APPLICATION_STATUSES = {
    "APPLIED",
    "RESUME_PROCESSING",
    "RESUME_PROCESSED",
    "RESUME_FAILED",
    "PREQUALIFICATION_COMPLETED",
    "SCREENING_IN_PROGRESS",
    "SCREENING_COMPLETED",
    "SCREENING_INCOMPLETE",
    "SCREENING_EVALUATED",
}


def test_application_status_all_matches_the_declared_members():
    assert ApplicationStatus.ALL == _EXPECTED_APPLICATION_STATUSES


@pytest.mark.parametrize(
    "value",
    [
        "SCREENING_IN_PROGRESS", "SCREENING_COMPLETED", "SCREENING_INCOMPLETE",
        "SCREENING_EVALUATED",
    ],
)
def test_new_screening_statuses_are_present_and_valid(value):
    assert value in ApplicationStatus.ALL
    assert ApplicationStatus.is_valid(value)


def test_screening_evaluated_is_the_step_4_terminal_marker():
    """Step 4 adds SCREENING_EVALUATED after SCREENING_COMPLETED. Still no
    SCREENING_EVALUATED == a hire/no-hire judgment (§11); it's a stage marker."""
    assert ApplicationStatus.SCREENING_EVALUATED == "SCREENING_EVALUATED"
    assert not ApplicationStatus.is_valid("SCORECARD_GENERATED")
    assert not ApplicationStatus.is_valid("SCORED")
    assert not ApplicationStatus.is_valid("RANKED")


def test_pre_phase4_application_statuses_are_untouched():
    """The Phase 4 addition must not have disturbed the existing vocabulary."""
    for value in (
        ApplicationStatus.APPLIED,
        ApplicationStatus.RESUME_PROCESSING,
        ApplicationStatus.RESUME_PROCESSED,
        ApplicationStatus.RESUME_FAILED,
        ApplicationStatus.PREQUALIFICATION_COMPLETED,
    ):
        assert ApplicationStatus.is_valid(value)


@pytest.mark.parametrize(
    "value",
    ["", "screening_in_progress", "SCREENING", "REJECTED", "UNKNOWN", None],
)
def test_application_status_still_rejects_invalid_values(value):
    assert not ApplicationStatus.is_valid(value)


def test_no_rejected_application_status_exists():
    """CLAUDE.md §2A item 3 / §11: rejection is a human decision recorded
    separately — it is never an application pipeline status."""
    assert "REJECTED" not in ApplicationStatus.ALL


# --- AuditEventType ----------------------------------------------------


def test_screening_audit_event_types_exist():
    values = {e.value for e in AuditEventType}
    assert "AI_SCREENING_STARTED" in values
    assert "AI_SCREENING_COMPLETED" in values
    assert "AI_SCREENING_INCOMPLETE" in values
    assert "SCREENING_QUESTIONS_GENERATED" in values  # Step 3


def test_screening_audit_event_members_map_to_their_own_names():
    assert AuditEventType.AI_SCREENING_STARTED.value == "AI_SCREENING_STARTED"
    assert AuditEventType.AI_SCREENING_COMPLETED.value == "AI_SCREENING_COMPLETED"
    assert (
        AuditEventType.AI_SCREENING_INCOMPLETE.value == "AI_SCREENING_INCOMPLETE"
    )
    assert (
        AuditEventType.SCREENING_QUESTIONS_GENERATED.value
        == "SCREENING_QUESTIONS_GENERATED"
    )


def test_audit_event_type_has_no_duplicate_values():
    values = [e.value for e in AuditEventType]
    assert len(values) == len(set(values))


def test_score_generated_exists_and_maps_to_its_name():
    """Step 4 emits SCORE_GENERATED (was declared-but-unused; CLAUDE.md §12
    "Score generated"). No new member is added for the scorecard."""
    assert AuditEventType.SCORE_GENERATED.value == "SCORE_GENERATED"
    values = {e.value for e in AuditEventType}
    assert "SCORECARD_GENERATED" not in values
    assert "SCREENING_EVALUATED" not in values  # that's an ApplicationStatus


def test_shortlist_audit_event_pair_exists():
    """Step 6: CANDIDATE_SHORTLISTED (pre-existing, now emitted) plus a new
    CANDIDATE_UNSHORTLISTED — two members, one per fact. No combined
    CANDIDATE_SHORTLIST_CHANGED member."""
    values = {e.value for e in AuditEventType}
    assert AuditEventType.CANDIDATE_SHORTLISTED.value == "CANDIDATE_SHORTLISTED"
    assert AuditEventType.CANDIDATE_UNSHORTLISTED.value == "CANDIDATE_UNSHORTLISTED"
    assert "CANDIDATE_SHORTLIST_CHANGED" not in values


def test_interview_guide_generated_exists_and_maps_to_its_name():
    """Step 7 emits INTERVIEW_GUIDE_GENERATED (was declared-but-unused). No new
    member is added."""
    assert (
        AuditEventType.INTERVIEW_GUIDE_GENERATED.value
        == "INTERVIEW_GUIDE_GENERATED"
    )
    values = {e.value for e in AuditEventType}
    assert "INTERVIEW_QUESTION_GENERATED" not in values


def test_interview_question_category_vocabulary():
    from app.database.models.interview_guide import InterviewQuestionCategory

    assert InterviewQuestionCategory.ALL == {
        "REQUIREMENTS", "EXPERIENCE", "BEHAVIORAL", "RESUME_VALIDATION", "PROBING",
    }
    assert InterviewQuestionCategory.ORDER == (
        "REQUIREMENTS", "EXPERIENCE", "BEHAVIORAL", "RESUME_VALIDATION", "PROBING",
    )
    for v in InterviewQuestionCategory.ALL:
        assert InterviewQuestionCategory.is_valid(v)
    for bad in ("TECHNICAL", "requirements", "GAP", "CV", "", None):
        assert not InterviewQuestionCategory.is_valid(bad)


# --- ScreeningRecommendation (Step 4) --------------------------------


def test_screening_recommendation_vocabulary():
    from app.database.models.screening_evaluation import ScreeningRecommendation

    assert ScreeningRecommendation.ALL == {"PROCEED", "HOLD", "REJECT"}
    # the automated evaluation path is only allowed to produce two of them
    assert ScreeningRecommendation.AUTOMATED == {"PROCEED", "HOLD"}
    assert "REJECT" not in ScreeningRecommendation.AUTOMATED
    for v in ("PROCEED", "HOLD", "REJECT"):
        assert ScreeningRecommendation.is_valid(v)
    for bad in ("proceed", "MAYBE", "", "PASS"):
        assert not ScreeningRecommendation.is_valid(bad)


# --- ScreeningSessionStatus -------------------------------------------

_EXPECTED_SESSION_STATUSES = (
    "PENDING",
    "READY_FOR_ROUND_1",
    "ROUND_1_IN_PROGRESS",
    "ROUND_1_COMPLETE",
    "ROUND_2_IN_PROGRESS",
    "SCREENING_COMPLETE",
)


def test_screening_session_status_full_set_after_step_3():
    """Step 3 adds the round lifecycle. PENDING / READY_FOR_ROUND_1 unchanged.
    Evaluation/scoring states are still deliberately NOT invented (next step)."""
    assert ScreeningSessionStatus.ALL == set(_EXPECTED_SESSION_STATUSES)
    # ORDER is the monotonic lifecycle, in exactly this sequence.
    assert ScreeningSessionStatus.ORDER == _EXPECTED_SESSION_STATUSES
    for v in _EXPECTED_SESSION_STATUSES:
        assert ScreeningSessionStatus.is_valid(v)


def test_screening_session_status_ordering_helpers():
    S = ScreeningSessionStatus
    assert S.at_least(S.ROUND_1_IN_PROGRESS, S.READY_FOR_ROUND_1) is True
    assert S.at_least(S.PENDING, S.READY_FOR_ROUND_1) is False
    assert S.at_least(S.SCREENING_COMPLETE, S.PENDING) is True
    assert S.rank(S.PENDING) == 0
    assert S.rank(S.SCREENING_COMPLETE) == len(_EXPECTED_SESSION_STATUSES) - 1


def test_retired_created_status_is_no_longer_valid():
    assert not ScreeningSessionStatus.is_valid("CREATED")


@pytest.mark.parametrize(
    "value",
    ["", "ROUND_2_COMPLETE", "EVALUATED", "SCREENING_COMPLETED", "created", "round_1"],
)
def test_screening_session_status_rejects_out_of_scope_values(value):
    """Note ``SCREENING_COMPLETED`` (with a D) is the *application* status —
    the session status is ``SCREENING_COMPLETE`` (no D)."""
    assert not ScreeningSessionStatus.is_valid(value)


# --- ScreeningQuestionCategory / Round -------------------------------


def test_screening_question_category_vocabulary():
    assert ScreeningQuestionCategory.ALL == {"JD", "CV", "BEHAVIORAL", "GAP"}
    for v in ("JD", "CV", "BEHAVIORAL", "GAP"):
        assert ScreeningQuestionCategory.is_valid(v)


@pytest.mark.parametrize("value", ["", "jd", "TECHNICAL", "GAPS", "OTHER", None])
def test_screening_question_category_rejects_invented_values(value):
    assert not ScreeningQuestionCategory.is_valid(value)


def test_screening_question_round_is_fixed_at_two():
    assert ScreeningQuestionRound.ALL == {1, 2}
    assert ScreeningQuestionRound.is_valid(1)
    assert ScreeningQuestionRound.is_valid(2)
    for bad in (0, 3, -1, "1"):
        assert not ScreeningQuestionRound.is_valid(bad)


# --- UserRole ----------------------------------------------------------


def test_user_role_gained_system_without_losing_the_existing_roles():
    assert {r.value for r in UserRole} == {
        "HR", "HIRING_MANAGER", "ADMIN", "SYSTEM",
    }


def test_system_user_constants_are_fixed_and_deterministic():
    """These are referenced by literal in migration c9a4f1d7b208 — they must
    never change, or the seeded row becomes unreachable."""
    assert str(SYSTEM_USER_ID) == "00000000-0000-0000-0000-000000000001"
    assert SYSTEM_USER_EMAIL == "system@internal.local"


# --- PostInterviewAnalysisStatus (Phase 4 Step 9) ----------------------


def test_post_interview_analysis_status_is_exactly_two_values():
    assert PostInterviewAnalysisStatus.ALL == {"CURRENT", "SUPERSEDED"}
    assert PostInterviewAnalysisStatus.CURRENT == "CURRENT"
    assert PostInterviewAnalysisStatus.SUPERSEDED == "SUPERSEDED"


@pytest.mark.parametrize(
    "bad", ["current", "DRAFT", "DELETED", "REJECTED", "", None, 1]
)
def test_post_interview_analysis_status_rejects_everything_else(bad):
    assert not PostInterviewAnalysisStatus.is_valid(bad)


def test_step_9_added_no_application_status():
    """The analysis is an artefact, not a stage: Step 9 writes no
    ``Application.status`` and adds no value to that vocabulary."""
    for absent in (
        "POST_INTERVIEW_ANALYSED", "POST_INTERVIEW_ANALYSIS_COMPLETED",
        "ANALYSED", "INTERVIEW_ANALYSED",
    ):
        assert absent not in ApplicationStatus.ALL


def test_post_interview_audit_event_exists_and_maps_to_its_own_name():
    assert (
        AuditEventType.POST_INTERVIEW_ANALYSIS_COMPLETED.value
        == "POST_INTERVIEW_ANALYSIS_COMPLETED"
    )


def test_disagreement_event_type_exists_but_step_9_does_not_use_it():
    """CLAUDE.md §8 stays a later step — the value is declared, unemitted."""
    assert (
        AuditEventType.AI_HUMAN_DISAGREEMENT_DETECTED.value
        == "AI_HUMAN_DISAGREEMENT_DETECTED"
    )


def test_interview_transcript_uploaded_audit_member_exists():
    """Increment B: one new member, string-valued, no migration needed (the
    audit column is a String validated in the service layer)."""
    assert (
        AuditEventType.INTERVIEW_TRANSCRIPT_UPLOADED.value
        == "INTERVIEW_TRANSCRIPT_UPLOADED"
    )


def test_final_ranking_generated_audit_member_exists_and_is_emitted_only_by_its_service():
    """Step 10b: one new string member, no migration (the audit column is a String
    validated in the service layer)."""
    assert (
        AuditEventType.FINAL_RANKING_GENERATED.value == "FINAL_RANKING_GENERATED"
    )
    assert "FINAL_RANKING_GENERATED" != "RANKING_GENERATED"      # distinct from Step 5
