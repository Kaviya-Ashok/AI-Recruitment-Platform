"""Tests for app.services.rubric_service (Phase 1.3).

Mocking boundary: ``app.services.<module>.get_structured_response`` is patched
(the claude_client function boundary) — for JD analysis in job_service and for
rubric generation in rubric_service. The real Claude API is never touched.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.ai.claude_client import AIOutputError, AIRequestError
from app.ai.schemas.jd_analysis import JdAnalysisResult
from app.ai.schemas.rubric_generation import RubricGenerationResult
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.job_requirement import JobRequirement
from app.database.models.rubric import (
    RubricCriterion,
    RubricVersion,
    RubricVersionStatus,
)
from app.database.models.user import UserRole
from app.services import job_service, rubric_service
from app.services.auth_service import create_user
from app.services.job_service import analyze_jd, create_job
from app.services.rubric_service import (
    RubricGenerationError,
    RubricNotFoundError,
    RubricStateError,
    add_criterion,
    approve_rubric,
    delete_criterion,
    generate_rubric,
    get_approved_rubric,
    get_current_draft,
    list_criteria,
    update_criterion,
)

_FORBIDDEN_TEXT_MARKERS = ("5+ years", "PostgreSQL", "Apache Spark", "mentor")


def _hr_user(db):
    return create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )


def _analyzed_job(db, mocker, user, *, jd_fixture="jd_analysis_valid.json"):
    """Create a job and run analyze_jd with a mocked AI result (6 requirements)."""
    job = create_job(
        db,
        title="Senior Data Engineer",
        department="Data",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="Senior Data Engineer. Python, PostgreSQL, Spark, mentoring.",
        created_by_user_id=user.id,
    )
    import json
    from pathlib import Path

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "ai_responses" / jd_fixture).read_text()
    )
    mocker.patch.object(
        job_service,
        "get_structured_response",
        return_value=JdAnalysisResult.model_validate(fixture),
    )
    analyze_jd(db, job_id=job.id, requested_by_user_id=user.id)
    db.refresh(job)
    return job


def _patch_rubric_ai(mocker, fixture_name, ai_response_dict):
    result = RubricGenerationResult.model_validate(ai_response_dict(fixture_name))
    return mocker.patch.object(
        rubric_service, "get_structured_response", return_value=result
    )


def _audit_blob(event: AuditEvent) -> str:
    return f"{event.action} | {event.previous_state} | {event.new_state}"


# --- generate_rubric --------------------------------------------------


def test_generate_rubric_creates_draft_with_criteria(db, mocker, ai_response_dict):
    user = _hr_user(db)
    job = _analyzed_job(db, mocker, user)
    _patch_rubric_ai(mocker, "rubric_generation_valid.json", ai_response_dict)

    version = generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)

    assert version.status == RubricVersionStatus.DRAFT
    assert version.version_number == 1
    assert version.generated_from_requirements_version == 1
    assert version.created_by == user.id

    criteria = list_criteria(db, version.id)
    assert len(criteria) == 6
    assert [c.display_order for c in criteria] == [1, 2, 3, 4, 5, 6]

    # source index resolution: 1..5 resolve to real JobRequirement ids, last is null
    reqs = job_service.get_current_requirements(db, job.id)
    req_ids = {r.id for r in reqs}
    assert criteria[0].source_job_requirement_id in req_ids
    assert criteria[-1].source_job_requirement_id is None

    db.refresh(job)
    assert job.status == JobStatus.RUBRIC_PENDING


def test_generate_rubric_resolves_bad_index_to_none(db, mocker, ai_response_dict):
    user = _hr_user(db)
    job = _analyzed_job(db, mocker, user)
    _patch_rubric_ai(mocker, "rubric_generation_bad_index.json", ai_response_dict)

    version = generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)
    criteria = list_criteria(db, version.id)

    assert criteria[0].source_job_requirement_id is not None  # index 1 ok
    assert criteria[1].source_job_requirement_id is None      # index 99 -> None
    assert criteria[2].source_job_requirement_id is None      # index null


def test_generate_rubric_requires_jd_analyzed(db, mocker):
    user = _hr_user(db)
    job = create_job(
        db,
        title="Raw Job",
        department=None,
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="Some JD text that was never analysed.",
        created_by_user_id=user.id,
    )
    spy = mocker.patch.object(rubric_service, "get_structured_response")

    with pytest.raises(RubricStateError):
        generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)
    spy.assert_not_called()


def test_generate_rubric_unknown_job_raises(db, mocker):
    mocker.patch.object(rubric_service, "get_structured_response")
    with pytest.raises(RubricNotFoundError):
        generate_rubric(db, job_id=uuid.uuid4(), requested_by_user_id=None)


@pytest.mark.parametrize(
    "exc",
    [
        AIOutputError("rubric_generation", '{"criteria": []}', "empty"),
        AIOutputError("rubric_generation", "{...}", "no mandatory"),
        AIRequestError("rubric_generation: Claude API request failed"),
    ],
)
def test_generate_rubric_ai_failure_makes_no_db_changes(db, mocker, exc):
    user = _hr_user(db)
    job = _analyzed_job(db, mocker, user)
    before_versions = db.execute(
        select(func.count()).select_from(RubricVersion)
    ).scalar_one()
    mocker.patch.object(rubric_service, "get_structured_response", side_effect=exc)

    with pytest.raises(RubricGenerationError):
        generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)

    db.refresh(job)
    assert job.status == JobStatus.JD_ANALYZED  # unchanged
    assert db.execute(
        select(func.count())
        .select_from(RubricVersion)
        .where(RubricVersion.job_id == job.id)
    ).scalar_one() == 0
    assert db.execute(
        select(func.count()).select_from(RubricVersion)
    ).scalar_one() == before_versions
    # no RUBRIC_GENERATED event referencing any version of this job
    assert db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.RUBRIC_GENERATED.value,
            AuditEvent.entity_id.in_(
                select(RubricVersion.id).where(RubricVersion.job_id == job.id)
            ),
        )
    ).scalar_one() == 0


def test_regenerate_supersedes_prior_draft(db, mocker, ai_response_dict):
    user = _hr_user(db)
    job = _analyzed_job(db, mocker, user)

    _patch_rubric_ai(mocker, "rubric_generation_valid.json", ai_response_dict)
    v1 = generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)

    _patch_rubric_ai(mocker, "rubric_generation_regen.json", ai_response_dict)
    v2 = generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)

    db.refresh(v1)
    assert v1.status == RubricVersionStatus.SUPERSEDED
    assert v2.status == RubricVersionStatus.DRAFT
    assert v2.version_number == 2
    # v1's criteria are NOT deleted
    assert len(list_criteria(db, v1.id)) == 6
    assert get_current_draft(db, job.id).id == v2.id


# --- the important one: regenerate must not touch an APPROVED version ---


def test_regenerate_leaves_approved_version_untouched(db, mocker, ai_response_dict):
    user = _hr_user(db)
    approver = _hr_user(db)
    job = _analyzed_job(db, mocker, user)

    _patch_rubric_ai(mocker, "rubric_generation_valid.json", ai_response_dict)
    v1 = generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)
    approved_v1 = approve_rubric(db, rubric_version_id=v1.id, requested_by_user_id=approver.id)
    assert approved_v1.status == RubricVersionStatus.APPROVED
    v1_approved_at = approved_v1.approved_at
    v1_criteria_ids = {c.id for c in list_criteria(db, v1.id)}

    db.refresh(job)
    assert job.status == JobStatus.RUBRIC_APPROVED

    # Now HR generates a fresh draft alongside the approved rubric.
    _patch_rubric_ai(mocker, "rubric_generation_regen.json", ai_response_dict)
    v2 = generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)

    db.refresh(v1)
    db.refresh(job)
    # APPROVED v1 is completely untouched
    assert v1.status == RubricVersionStatus.APPROVED
    assert v1.approved_by == approver.id
    assert v1.approved_at == v1_approved_at
    assert {c.id for c in list_criteria(db, v1.id)} == v1_criteria_ids
    # v2 is a separate DRAFT
    assert v2.status == RubricVersionStatus.DRAFT
    assert v2.version_number == 2
    assert get_approved_rubric(db, job.id).id == v1.id
    assert get_current_draft(db, job.id).id == v2.id
    # job.status stays RUBRIC_APPROVED — the approved rubric is still effective
    assert job.status == JobStatus.RUBRIC_APPROVED

    # Approving v2 now supersedes v1
    v2_approved = approve_rubric(db, rubric_version_id=v2.id, requested_by_user_id=approver.id)
    db.refresh(v1)
    assert v1.status == RubricVersionStatus.SUPERSEDED
    assert v2_approved.status == RubricVersionStatus.APPROVED
    assert get_approved_rubric(db, job.id).id == v2.id


# --- editing -------------------------------------------------------


def _draft_with_criteria(db, mocker, ai_response_dict):
    user = _hr_user(db)
    job = _analyzed_job(db, mocker, user)
    _patch_rubric_ai(mocker, "rubric_generation_valid.json", ai_response_dict)
    version = generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)
    return user, job, version


def test_update_criterion_on_draft(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    crit = list_criteria(db, version.id)[2]  # a PREFERRED one

    updated = update_criterion(
        db,
        criterion_id=crit.id,
        requested_by_user_id=user.id,
        requirement_type="MANDATORY",
        category="Core Skill",
        criterion_text="Reworded criterion text",
    )

    assert updated.requirement_type == "MANDATORY"
    assert updated.category == "Core Skill"
    assert updated.criterion_text == "Reworded criterion text"

    event = db.execute(
        select(AuditEvent)
        .where(
            AuditEvent.entity_type == "rubric_criterion",
            AuditEvent.entity_id == crit.id,
            AuditEvent.event_type == AuditEventType.RUBRIC_EDITED.value,
        )
        .order_by(AuditEvent.timestamp.desc())
    ).scalars().first()
    assert event is not None
    changed = event.new_state["changed_fields"]
    assert changed["requirement_type"] == {"from": "PREFERRED", "to": "MANDATORY"}
    assert changed["criterion_text"] == "changed"  # not the text itself
    assert "Reworded criterion text" not in _audit_blob(event)


def test_update_criterion_noop_writes_no_event(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    crit = list_criteria(db, version.id)[0]
    before = db.execute(select(func.count()).select_from(AuditEvent)).scalar_one()

    update_criterion(
        db,
        criterion_id=crit.id,
        requested_by_user_id=user.id,
        requirement_type=crit.requirement_type,  # same value
    )

    assert db.execute(select(func.count()).select_from(AuditEvent)).scalar_one() == before


def test_add_criterion_on_draft(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    before = list_criteria(db, version.id)

    new_c = add_criterion(
        db,
        rubric_version_id=version.id,
        requested_by_user_id=user.id,
        requirement_type="MANDATORY",
        category="Added",
        criterion_text="A manually added mandatory criterion",
    )

    assert new_c.display_order == len(before) + 1
    assert new_c.source_job_requirement_id is None
    event = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == new_c.id)
    ).scalars().one()
    assert event.event_type == AuditEventType.RUBRIC_EDITED.value
    assert event.new_state["operation"] == "criterion_added"
    assert "manually added mandatory criterion" not in _audit_blob(event)


def test_delete_criterion_on_draft(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    crit = list_criteria(db, version.id)[0]
    crit_id = crit.id

    delete_criterion(db, criterion_id=crit_id, requested_by_user_id=user.id)

    assert db.get(RubricCriterion, crit_id) is None
    assert len(list_criteria(db, version.id)) == 5
    event = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == crit_id,
            AuditEvent.event_type == AuditEventType.RUBRIC_EDITED.value,
        )
    ).scalars().one()
    assert event.previous_state["operation"] == "criterion_removed"


@pytest.mark.parametrize("op", ["update", "add", "delete"])
def test_edits_rejected_on_non_draft(db, mocker, ai_response_dict, op):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    approve_rubric(db, rubric_version_id=version.id, requested_by_user_id=user.id)
    db.refresh(version)
    assert version.status == RubricVersionStatus.APPROVED
    crit = list_criteria(db, version.id)[0]

    with pytest.raises(RubricStateError):
        if op == "update":
            update_criterion(
                db, criterion_id=crit.id, requested_by_user_id=user.id,
                criterion_text="nope",
            )
        elif op == "add":
            add_criterion(
                db, rubric_version_id=version.id, requested_by_user_id=user.id,
                requirement_type="MANDATORY", category=None, criterion_text="nope",
            )
        else:
            delete_criterion(db, criterion_id=crit.id, requested_by_user_id=user.id)


def test_edits_rejected_on_superseded(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    _patch_rubric_ai(mocker, "rubric_generation_regen.json", ai_response_dict)
    generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)  # supersedes v1
    db.refresh(version)
    assert version.status == RubricVersionStatus.SUPERSEDED
    crit = list_criteria(db, version.id)[0]

    with pytest.raises(RubricStateError):
        update_criterion(
            db, criterion_id=crit.id, requested_by_user_id=user.id,
            criterion_text="nope",
        )


# --- approval -----------------------------------------------------


def test_approve_rubric_success(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)

    approved = approve_rubric(
        db, rubric_version_id=version.id, requested_by_user_id=user.id
    )

    assert approved.status == RubricVersionStatus.APPROVED
    assert approved.approved_by == user.id
    assert approved.approved_at is not None
    db.refresh(job)
    assert job.status == JobStatus.RUBRIC_APPROVED

    events = db.execute(
        select(AuditEvent)
        .where(AuditEvent.entity_id == version.id)
        .order_by(AuditEvent.timestamp)
    ).scalars().all()
    types = {e.event_type for e in events}
    assert AuditEventType.RUBRIC_APPROVED.value in types
    assert AuditEventType.RUBRIC_VERSION_CREATED.value in types
    for e in events:
        assert not any(m.lower() in _audit_blob(e).lower() for m in _FORBIDDEN_TEXT_MARKERS)


def test_approve_rejected_without_mandatory(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    # Delete every MANDATORY criterion, then try to approve.
    for c in list_criteria(db, version.id):
        if c.requirement_type == "MANDATORY":
            delete_criterion(db, criterion_id=c.id, requested_by_user_id=user.id)

    with pytest.raises(RubricStateError):
        approve_rubric(db, rubric_version_id=version.id, requested_by_user_id=user.id)

    db.refresh(version)
    db.refresh(job)
    assert version.status == RubricVersionStatus.DRAFT  # no state change
    assert job.status == JobStatus.RUBRIC_PENDING
    assert db.execute(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.event_type == AuditEventType.RUBRIC_APPROVED.value,
            AuditEvent.entity_id == version.id,
        )
    ).scalar_one() == 0


def test_approve_rejected_on_already_approved(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    approve_rubric(db, rubric_version_id=version.id, requested_by_user_id=user.id)

    with pytest.raises(RubricStateError):
        approve_rubric(db, rubric_version_id=version.id, requested_by_user_id=user.id)


def test_approve_rejected_on_superseded(db, mocker, ai_response_dict):
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    _patch_rubric_ai(mocker, "rubric_generation_regen.json", ai_response_dict)
    generate_rubric(db, job_id=job.id, requested_by_user_id=user.id)
    db.refresh(version)
    assert version.status == RubricVersionStatus.SUPERSEDED

    with pytest.raises(RubricStateError):
        approve_rubric(db, rubric_version_id=version.id, requested_by_user_id=user.id)


def test_no_bulk_text_in_any_rubric_audit_metadata(db, mocker, ai_response_dict):
    """Sweep: across generate + edit + approve, no criterion text / JD content
    appears in any audit event's action / previous_state / new_state."""
    user, job, version = _draft_with_criteria(db, mocker, ai_response_dict)
    crit = list_criteria(db, version.id)[0]
    update_criterion(
        db, criterion_id=crit.id, requested_by_user_id=user.id,
        criterion_text="Some brand new criterion wording XYZ",
    )
    add_criterion(
        db, rubric_version_id=version.id, requested_by_user_id=user.id,
        requirement_type="MANDATORY", category=None,
        criterion_text="Another secret criterion ABC",
    )
    approve_rubric(db, rubric_version_id=version.id, requested_by_user_id=user.id)

    entity_ids = [version.id] + [c.id for c in list_criteria(db, version.id)]
    all_events = db.execute(
        select(AuditEvent).where(
            AuditEvent.event_type.in_(
                [
                    AuditEventType.RUBRIC_GENERATED.value,
                    AuditEventType.RUBRIC_EDITED.value,
                    AuditEventType.RUBRIC_APPROVED.value,
                    AuditEventType.RUBRIC_VERSION_CREATED.value,
                ]
            ),
            AuditEvent.entity_id.in_(entity_ids),
        )
    ).scalars().all()
    assert all_events
    for e in all_events:
        blob = _audit_blob(e)
        assert "Some brand new criterion wording XYZ" not in blob
        assert "Another secret criterion ABC" not in blob
        assert "5+ years" not in blob
        assert "PostgreSQL" not in blob
