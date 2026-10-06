"""AuditEvent model — the append-only audit trail (CLAUDE.md section 20).

Every meaningful state change in the platform (job created, JD analyzed, rubric
approved, candidate applied, screening completed, decision submitted, ...) is
recorded here as one immutable row.

APPEND-ONLY CONTRACT
--------------------
Application code MUST only ever INSERT into this table. Rows must never be
UPDATEd or DELETEd. This is a design contract for now — DB-level enforcement
(revoking UPDATE/DELETE, triggers, partitioning) is a later hardening concern
and is deliberately NOT built in this step.

Design decisions
----------------
ID type — **UUID**, consistent with :class:`~app.database.models.user.User`.

``event_type`` — **validated string, not a DB enum** (recommended approach):
    CLAUDE.md sections 12/20 already list ~20 event types and every later phase
    will add more. A Postgres ``ENUM`` would then require an ``ALTER TYPE ...
    ADD VALUE`` migration for every new event (values also can't be renamed or
    removed, and the operation has historically had transaction caveats).
    A plain ``String`` column with application-layer validation against a
    single canonical list (:class:`AuditEventType` below) keeps the schema
    stable while the vocabulary grows, and still gives one authoritative place
    to see every valid value. The column is indexed for "show me all events of
    type X" queries. Trade-off: the DB will accept an unknown string if a bug
    bypasses the service layer — acceptable for an append-only log where a
    stray value is visible but harmless, and far cheaper than constant enum
    migrations.

``entity_id`` — **generic UUID, not a typed foreign key**:
    An audit event can point at a job, candidate, rubric, interview, etc.
    Because ``entity_type`` varies row to row, ``entity_id`` cannot be a single
    typed FK. It is stored as a bare UUID alongside the ``entity_type``
    discriminator. This may be revisited (e.g. per-entity audit link tables, or
    polymorphic associations) once those tables exist; for Phase 0 the generic
    pair is sufficient and keeps the audit table decoupled from a schema that
    does not exist yet.

``user_id`` — nullable FK to ``users.id`` with ``ON DELETE SET NULL`` so that
    an audit row outlives the actor if a user is ever hard-deleted (normally
    users are only deactivated). For Phase 0 the actor is typically an HR user.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class AuditEventType(str, enum.Enum):
    """Canonical list of audit event types (CLAUDE.md sections 12 & 20).

    This is the application-layer source of truth used to validate
    ``AuditEvent.event_type`` before insert. It is intentionally NOT bound to a
    database enum — see the module docstring. Later phases append new members
    here without a schema migration.
    """

    # Account / user lifecycle (security-relevant, CLAUDE.md §§16, 20, 23).
    USER_CREATED = "USER_CREATED"

    JOB_CREATED = "JOB_CREATED"
    JOB_OPENED = "JOB_OPENED"
    JOB_CLOSED = "JOB_CLOSED"
    JD_ANALYZED = "JD_ANALYZED"
    RUBRIC_GENERATED = "RUBRIC_GENERATED"
    RUBRIC_EDITED = "RUBRIC_EDITED"
    RUBRIC_APPROVED = "RUBRIC_APPROVED"
    RUBRIC_VERSION_CREATED = "RUBRIC_VERSION_CREATED"
    # Application-link lifecycle (Phase 1.4 — not named individually in
    # CLAUDE.md §12, which predates the link design; a necessary extension).
    APPLICATION_LINK_GENERATED = "APPLICATION_LINK_GENERATED"
    APPLICATION_LINK_REVOKED = "APPLICATION_LINK_REVOKED"
    # A candidate successfully opened a valid application link (Phase 1.5).
    APPLICATION_LINK_VIEWED = "APPLICATION_LINK_VIEWED"
    CANDIDATE_APPLIED = "CANDIDATE_APPLIED"
    RESUME_UPLOADED = "RESUME_UPLOADED"
    RESUME_PROCESSED = "RESUME_PROCESSED"
    PREQUALIFICATION_COMPLETED = "PREQUALIFICATION_COMPLETED"
    AI_SCREENING_STARTED = "AI_SCREENING_STARTED"
    # Phase 4 Step 3: questions for one screening round were generated + persisted.
    # Emitted once per round. Safe metadata only — counts / criterion ids /
    # model, never question or answer text.
    SCREENING_QUESTIONS_GENERATED = "SCREENING_QUESTIONS_GENERATED"
    AI_SCREENING_COMPLETED = "AI_SCREENING_COMPLETED"
    # Phase 4 (CLAUDE.md §2A item 4): the candidate abandoned screening. A
    # distinct fact from COMPLETED — and never a rejection (CLAUDE.md §3, §11).
    AI_SCREENING_INCOMPLETE = "AI_SCREENING_INCOMPLETE"
    SCORE_GENERATED = "SCORE_GENERATED"
    CANDIDATE_SHORTLISTED = "CANDIDATE_SHORTLISTED"
    # Phase 4 Step 6: HR reversed a shortlist decision. A distinct fact from
    # CANDIDATE_SHORTLISTED — one enum member per fact, matching the
    # RUBRIC_APPROVED / RUBRIC_VERSION_CREATED precedent. Emitted only on a real
    # True -> False transition; never for a no-op click.
    CANDIDATE_UNSHORTLISTED = "CANDIDATE_UNSHORTLISTED"
    INTERVIEW_GUIDE_GENERATED = "INTERVIEW_GUIDE_GENERATED"
    HUMAN_INTERVIEW_COMPLETED = "HUMAN_INTERVIEW_COMPLETED"
    HUMAN_FEEDBACK_SUBMITTED = "HUMAN_FEEDBACK_SUBMITTED"
    # Increment B: a transcript file was attached to (or replaced for) one
    # interview round. Structural metadata only — never a file or candidate name.
    INTERVIEW_TRANSCRIPT_UPLOADED = "INTERVIEW_TRANSCRIPT_UPLOADED"
    HUMAN_RECOMMENDATION_SUBMITTED = "HUMAN_RECOMMENDATION_SUBMITTED"
    AI_HUMAN_DISAGREEMENT_DETECTED = "AI_HUMAN_DISAGREEMENT_DETECTED"
    POST_INTERVIEW_ANALYSIS_COMPLETED = "POST_INTERVIEW_ANALYSIS_COMPLETED"
    RANKING_GENERATED = "RANKING_GENERATED"
    # Step 10b: HR generated the post-interview FINAL ranking for one job /
    # rubric-version partition. Structural metadata only (ids, weights, counts).
    FINAL_RANKING_GENERATED = "FINAL_RANKING_GENERATED"
    FINAL_DECISION_SUBMITTED = "FINAL_DECISION_SUBMITTED"


class AuditEvent(Base):
    """One immutable audit-trail entry. Insert-only (see module docstring)."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Nullable: some events may have no authenticated actor (e.g. a future
    # candidate-initiated action). SET NULL keeps the audit row if the user
    # is ever hard-deleted.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Stored as a string; validated at the service layer against
    # ``AuditEventType``. Indexed for filtering by event type.
    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    # What kind of entity this event concerns ("job", "candidate", "rubric"...).
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Generic reference to the entity; not a typed FK (entity_type varies).
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )

    # Short human-readable description of what happened.
    action: Mapped[str] = mapped_column(String(500), nullable=False)

    previous_state: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    new_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Any other contextual data (request id, AI task id, model name, ...).
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return (
            f"<AuditEvent id={self.id!r} type={self.event_type!r} "
            f"entity={self.entity_type!r}:{self.entity_id!r}>"
        )
