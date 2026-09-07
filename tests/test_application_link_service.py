"""Tests for app.services.application_link_service (Phase 1.4).

No AI in this step, so no mocking — real Postgres via the savepoint-rollback
``db`` fixture. The link service only inspects ``job.status`` (never the rubric
rows), so tests set the status directly instead of driving the full rubric flow.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import func, select

from app.database.models.application_link import (
    ApplicationLink,
    ApplicationLinkStatus,
)
from app.database.models.audit_event import AuditEvent, AuditEventType
from app.database.models.job import JdInputMethod, JobStatus
from app.database.models.user import UserRole
from app.services import application_link_service as svc
from app.services.application_link_service import (
    ApplicationLinkNotFoundError,
    LinkJobNotFoundError,
    LinkResolutionOutcome,
    LinkStateError,
    close_job,
    generate_link,
    get_active_link,
    resolve_link,
    revoke_link,
)
from app.services.auth_service import create_user
from app.services.job_service import create_job

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,}$")


def _hr_user(db):
    return create_user(
        db=db,
        email=f"hr-{uuid.uuid4().hex}@example.test",
        plain_password="pw-for-tests-only",
        full_name="HR Tester",
        role=UserRole.HR,
    )


def _job(db, user, status: str = JobStatus.RUBRIC_APPROVED):
    job = create_job(
        db,
        title="Linkable Job",
        department="Ops",
        jd_input_method=JdInputMethod.TEXT_PASTE,
        jd_source_text="A JD.",
        created_by_user_id=user.id,
    )
    job.status = status
    db.flush()
    return job


def _all_audit_for_job(db, job_id, link_ids):
    ids = [job_id] + list(link_ids)
    return db.execute(
        select(AuditEvent).where(AuditEvent.entity_id.in_(ids))
    ).scalars().all()


def _audit_text(e: AuditEvent) -> str:
    return f"{e.action} || {e.previous_state} || {e.new_state} || {e.event_metadata}"


# --- generate_link: state gating ------------------------------------


@pytest.mark.parametrize(
    "status",
    [JobStatus.DRAFT, JobStatus.JD_ANALYZED, JobStatus.RUBRIC_PENDING],
)
def test_generate_link_rejected_before_rubric_approved(db, status):
    user = _hr_user(db)
    job = _job(db, user, status)
    before = db.execute(
        select(func.count()).select_from(ApplicationLink)
    ).scalar_one()

    with pytest.raises(LinkStateError):
        generate_link(db, job_id=job.id, requested_by_user_id=user.id)

    db.refresh(job)
    assert job.status == status
    assert db.execute(
        select(func.count()).select_from(ApplicationLink)
    ).scalar_one() == before


def test_generate_link_rejected_when_closed(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.CLOSED)
    with pytest.raises(LinkStateError):
        generate_link(db, job_id=job.id, requested_by_user_id=user.id)


def test_generate_link_unknown_job(db):
    user = _hr_user(db)
    with pytest.raises(LinkJobNotFoundError):
        generate_link(db, job_id=uuid.uuid4(), requested_by_user_id=user.id)


# --- generate_link: happy path ------------------------------------


def test_generate_link_first_time_opens_job(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)

    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)

    assert link.status == ApplicationLinkStatus.ACTIVE
    assert link.sequence_number == 1
    assert link.created_by == user.id
    assert _TOKEN_RE.match(link.token)
    db.refresh(job)
    assert job.status == JobStatus.OPEN


def test_generate_link_first_time_writes_two_events(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)

    events = _all_audit_for_job(db, job.id, [link.id])
    types = sorted(e.event_type for e in events)
    assert AuditEventType.APPLICATION_LINK_GENERATED.value in types
    assert AuditEventType.JOB_OPENED.value in types
    gen = next(
        e for e in events
        if e.event_type == AuditEventType.APPLICATION_LINK_GENERATED.value
    )
    assert gen.new_state["is_first_link"] is True
    assert gen.new_state["sequence_number"] == 1


def test_regeneration_supersedes_and_keeps_job_open(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)

    v1 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    old_token = v1.token
    v2 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)

    db.refresh(v1)
    db.refresh(job)
    assert v1.status == ApplicationLinkStatus.SUPERSEDED
    assert v2.status == ApplicationLinkStatus.ACTIVE
    assert v2.sequence_number == 2
    assert job.status == JobStatus.OPEN  # unchanged

    # old token no longer resolves as valid; new one does
    assert resolve_link(db, old_token).outcome == LinkResolutionOutcome.INACTIVE
    assert resolve_link(db, v2.token).outcome == LinkResolutionOutcome.VALID

    # regeneration -> one more APPLICATION_LINK_GENERATED, NO second JOB_OPENED
    events = _all_audit_for_job(db, job.id, [v1.id, v2.id])
    assert sum(
        e.event_type == AuditEventType.APPLICATION_LINK_GENERATED.value
        for e in events
    ) == 2
    assert sum(
        e.event_type == AuditEventType.JOB_OPENED.value for e in events
    ) == 1
    regen = next(
        e for e in events
        if e.event_type == AuditEventType.APPLICATION_LINK_GENERATED.value
        and e.new_state["sequence_number"] == 2
    )
    assert regen.new_state["is_first_link"] is False
    assert regen.previous_state["superseded_prior_link_sequence"] == 1


def test_generate_link_regeneration_allowed_from_open(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    generate_link(db, job_id=job.id, requested_by_user_id=user.id)  # -> OPEN
    db.refresh(job)
    assert job.status == JobStatus.OPEN
    # a second call with job already OPEN must succeed
    v2 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    assert v2.sequence_number == 2


def test_token_collision_retry_then_error(db, mocker):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    existing = generate_link(db, job_id=job.id, requested_by_user_id=user.id)

    # Force _generate_token to always return an already-used token.
    mocker.patch.object(svc, "_generate_token", return_value=existing.token)
    with pytest.raises(svc.LinkGenerationError):
        generate_link(db, job_id=job.id, requested_by_user_id=user.id)


# --- revoke_link -------------------------------------------------


def test_revoke_link(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)

    revoked = revoke_link(db, link_id=link.id, requested_by_user_id=user.id)

    assert revoked.status == ApplicationLinkStatus.REVOKED
    assert revoked.revoked_by == user.id
    assert revoked.revoked_at is not None
    db.refresh(job)
    assert job.status == JobStatus.OPEN  # NOT changed by revoke
    assert get_active_link(db, job.id) is None
    assert resolve_link(db, link.token).outcome == LinkResolutionOutcome.INACTIVE

    ev = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == link.id,
            AuditEvent.event_type == AuditEventType.APPLICATION_LINK_REVOKED.value,
        )
    ).scalars().one()
    assert ev.new_state["sequence_number"] == link.sequence_number


def test_revoke_already_revoked_raises(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    revoke_link(db, link_id=link.id, requested_by_user_id=user.id)
    with pytest.raises(LinkStateError):
        revoke_link(db, link_id=link.id, requested_by_user_id=user.id)


def test_revoke_superseded_raises(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    v1 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    generate_link(db, job_id=job.id, requested_by_user_id=user.id)  # supersedes v1
    db.refresh(v1)
    assert v1.status == ApplicationLinkStatus.SUPERSEDED
    with pytest.raises(LinkStateError):
        revoke_link(db, link_id=v1.id, requested_by_user_id=user.id)


def test_revoke_unknown_link_raises(db):
    user = _hr_user(db)
    with pytest.raises(ApplicationLinkNotFoundError):
        revoke_link(db, link_id=uuid.uuid4(), requested_by_user_id=user.id)


def test_regenerate_after_revoke(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    revoke_link(db, link_id=link.id, requested_by_user_id=user.id)
    # job still OPEN, no active link -> generate is allowed
    v2 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    assert v2.status == ApplicationLinkStatus.ACTIVE
    assert v2.sequence_number == 2


# --- close_job -------------------------------------------------


def test_close_job_requires_open(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    with pytest.raises(LinkStateError):
        close_job(db, job_id=job.id, requested_by_user_id=user.id)


def test_close_job_sets_closed_and_does_not_touch_links(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    link_snapshot = (link.status, link.revoked_by, link.revoked_at)

    closed = close_job(db, job_id=job.id, requested_by_user_id=user.id)

    assert closed.status == JobStatus.CLOSED
    db.refresh(link)
    assert (link.status, link.revoked_by, link.revoked_at) == link_snapshot
    assert link.status == ApplicationLinkStatus.ACTIVE  # untouched

    ev = db.execute(
        select(AuditEvent).where(
            AuditEvent.entity_id == job.id,
            AuditEvent.event_type == AuditEventType.JOB_CLOSED.value,
        )
    ).scalars().one()
    assert ev.new_state["status"] == JobStatus.CLOSED


def test_close_job_stops_link_resolution(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    assert resolve_link(db, link.token).outcome == LinkResolutionOutcome.VALID

    close_job(db, job_id=job.id, requested_by_user_id=user.id)

    result = resolve_link(db, link.token)
    assert result.outcome == LinkResolutionOutcome.JOB_CLOSED
    assert result.outcome != LinkResolutionOutcome.NOT_FOUND  # distinct
    assert result.link is not None and result.job is not None


# --- resolve_link: all four outcomes -------------------------------


def test_resolve_link_four_cases(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)

    # 1. VALID
    assert resolve_link(db, link.token).outcome == LinkResolutionOutcome.VALID

    # 2. NOT_FOUND
    nf = resolve_link(db, "definitely-not-a-real-token-xxxxxxxxxxxxxxxxxxx")
    assert nf.outcome == LinkResolutionOutcome.NOT_FOUND
    assert nf.link is None and nf.job is None

    # 3. INACTIVE (revoked)
    revoke_link(db, link_id=link.id, requested_by_user_id=user.id)
    assert resolve_link(db, link.token).outcome == LinkResolutionOutcome.INACTIVE

    # 3b. INACTIVE (superseded) — fresh job
    job2 = _job(db, user, JobStatus.RUBRIC_APPROVED)
    s1 = generate_link(db, job_id=job2.id, requested_by_user_id=user.id)
    generate_link(db, job_id=job2.id, requested_by_user_id=user.id)
    assert resolve_link(db, s1.token).outcome == LinkResolutionOutcome.INACTIVE

    # 4. JOB_CLOSED — fresh job
    job3 = _job(db, user, JobStatus.RUBRIC_APPROVED)
    l3 = generate_link(db, job_id=job3.id, requested_by_user_id=user.id)
    close_job(db, job_id=job3.id, requested_by_user_id=user.id)
    assert resolve_link(db, l3.token).outcome == LinkResolutionOutcome.JOB_CLOSED

    assert resolve_link(db, "").outcome == LinkResolutionOutcome.NOT_FOUND


# --- get_active_link -------------------------------------------


def test_get_active_link(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)
    assert get_active_link(db, job.id) is None

    link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    got = get_active_link(db, job.id)
    assert got is not None and got.id == link.id

    revoke_link(db, link_id=link.id, requested_by_user_id=user.id)
    assert get_active_link(db, job.id) is None


# --- token uniqueness -----------------------------------------


def test_tokens_are_unique_and_url_safe(db):
    user = _hr_user(db)
    tokens = set()
    for _ in range(3):
        job = _job(db, user, JobStatus.RUBRIC_APPROVED)
        link = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
        # regenerate once too
        link2 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
        for t in (link.token, link2.token):
            assert _TOKEN_RE.match(t), t
            assert t not in tokens
            tokens.add(t)
    assert len(tokens) == 6


# --- security: token never in audit metadata -----------------


def test_token_never_appears_in_any_audit_field(db):
    user = _hr_user(db)
    job = _job(db, user, JobStatus.RUBRIC_APPROVED)

    l1 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    l2 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)  # supersedes l1
    revoke_link(db, link_id=l2.id, requested_by_user_id=user.id)
    l3 = generate_link(db, job_id=job.id, requested_by_user_id=user.id)
    close_job(db, job_id=job.id, requested_by_user_id=user.id)

    tokens = {l1.token, l2.token, l3.token}
    events = _all_audit_for_job(db, job.id, [l1.id, l2.id, l3.id])
    assert events  # sanity

    for e in events:
        blob = _audit_text(e)
        for tok in tokens:
            assert tok not in blob, (
                f"token leaked into audit event {e.event_type}: {blob!r}"
            )
        # also: the 'token_present' marker is a bool, not the value
        if e.new_state and "token_present" in e.new_state:
            assert e.new_state["token_present"] is True
