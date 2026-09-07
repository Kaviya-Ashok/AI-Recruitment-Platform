"""ScreeningSession model — one AI screening conversation for one application
(CLAUDE.md §2A item 5, §§3, 15, 23, 24).

Phase 4 Step 2 scope: the session *row* only. It is created by
``screening_pipeline_service`` **at the very start of the automatic pipeline**
(right after application submission), NOT after resume parsing +
prequalification finish — so that a candidate who closes the tab, or an HR user
recovering a stalled pipeline, always has a durable row and an ``access_token``
to resume with (CLAUDE.md §2A items 5, 6). Question generation, answers, and
evaluation are later steps and have no columns here yet.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``application_id`` — FK to ``applications.id``, **``ON DELETE RESTRICT``** and
**UNIQUE**.

* ``RESTRICT`` matches the non-cascading convention of ``documents`` /
  ``resume_extractions`` / ``prequalification_results`` / ``applications``: a
  screening session is a business/audit record and must never vanish as a side
  effect of deleting a parent row (CLAUDE.md §§12, 23).
* ``UNIQUE`` encodes "one screening session per application in this MVP".
  It is also the concurrency backstop: two overlapping Streamlit reruns can
  both pass the service's check-first guard, and this constraint — not the
  check — is what actually guarantees a single row. The service catches the
  resulting ``IntegrityError`` and resolves it to the existing session.

``access_token`` — **credential-equivalent**, generated with
``secrets.token_urlsafe(32)`` (256 bits), the same method and column width as
``application_links.token``. It is the candidate's **per-session** secure
re-entry credential, and it exists precisely because neither existing
identifier can do that job (CLAUDE.md §2A item 5):

* ``application_links.token`` is shared by every candidate who applies to a
  job — useless as a per-candidate credential;
* ``applications.id`` is a plain surrogate PK that this codebase already
  treats as non-secret (it is stashed in public ``st.session_state``).

SECURITY: like ``application_links.token``, this value must never appear in
audit metadata, log lines, error messages, or ``__repr__``. Phase 4 Step 1 only
*stores* it — the returning-candidate flow that consumes it is a later step.

``status`` — **validated String, NOT a native Postgres ENUM**, same
growing-vocabulary rule as ``ApplicationStatus`` / ``ApplicationLinkStatus``:
the screening vocabulary grows every step of this phase. Validated at the
service layer against :class:`ScreeningSessionStatus`. Step 3 adds the round
lifecycle (``ROUND_1_IN_PROGRESS`` … ``SCREENING_COMPLETE``). The values are
**ordered** (:data:`ScreeningSessionStatus.ORDER`) so services can ask "is this
session at or past stage X?" without hard-coding every combination — the
pipeline stepper and the ``?screening=`` resolver both rely on that.

Step 1's single ``CREATED`` value is **retired**: it conflated "row exists" with
"ready for round 1" precisely because Step 1 only created the row once
everything was done. Step 2 splits those. No ``CREATED`` rows exist (Step 1
shipped no screening data), so there is nothing to migrate beyond realigning the
column's ``server_default`` (see migration ``b3d9f0a12c47``).

``created_at`` / ``updated_at`` — timezone-aware, server-defaulted, with
``onupdate`` on ``updated_at`` (same as ``applications`` / ``application_links``).
Unlike the write-once ``resume_extractions`` / ``prequalification_results``,
this row *is* mutated as the conversation progresses, so it carries
``updated_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ScreeningSessionStatus:
    """Lifecycle states for a screening session (validated string column).

    Later Phase 4 steps append members here with no schema migration.

    * ``PENDING`` — the session row exists (created at pipeline start) but the
      automatic pre-screening pipeline has not finished: resume parsing and/or
      prequalification are still outstanding. The ``access_token`` is already
      usable for resume-by-link; round-1 questions are NOT available yet.
    * ``READY_FOR_ROUND_1`` — resume parsing AND prequalification have both
      completed. Round-1 question generation is now eligible to run (it fires
      automatically as the last pipeline stage).
    * ``ROUND_1_IN_PROGRESS`` — round-1 questions have been generated and
      persisted; the candidate is answering them.
    * ``ROUND_1_COMPLETE`` — every round-1 question has an answer. Round-2
      generation is now eligible (triggered by that last answer, not by a
      timer). A brief state — normally superseded within the same request by
      ``ROUND_2_IN_PROGRESS`` or ``SCREENING_COMPLETE``; it persists only if
      round-2 generation failed and needs a retry.
    * ``ROUND_2_IN_PROGRESS`` — round-2 (follow-up) questions were generated
      (1–3 of them); the candidate is answering them.
    * ``SCREENING_COMPLETE`` — terminal. Reached either because round 2
      produced zero follow-up questions, or because every round-2 question has
      an answer. ``AI_SCREENING_COMPLETED`` is emitted exactly once on entry.
      Evaluation/scoring of the collected answers is a separate future step.

    ``ORDER`` makes the progression explicit so code can compare positions.
    ``CREATED`` (Step 1) is retired — see the module docstring.
    """

    PENDING = "PENDING"
    READY_FOR_ROUND_1 = "READY_FOR_ROUND_1"
    ROUND_1_IN_PROGRESS = "ROUND_1_IN_PROGRESS"
    ROUND_1_COMPLETE = "ROUND_1_COMPLETE"
    ROUND_2_IN_PROGRESS = "ROUND_2_IN_PROGRESS"
    SCREENING_COMPLETE = "SCREENING_COMPLETE"

    #: Monotonic lifecycle order. A session only ever moves forward through this.
    ORDER: tuple[str, ...] = (
        PENDING,
        READY_FOR_ROUND_1,
        ROUND_1_IN_PROGRESS,
        ROUND_1_COMPLETE,
        ROUND_2_IN_PROGRESS,
        SCREENING_COMPLETE,
    )

    ALL: frozenset[str] = frozenset(ORDER)

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL

    @classmethod
    def rank(cls, value: str) -> int:
        """Position of ``value`` in :data:`ORDER`. Raises ``ValueError`` for an
        unknown value (callers pass validated statuses)."""
        return cls.ORDER.index(value)

    @classmethod
    def at_least(cls, value: str, target: str) -> bool:
        """True iff ``value`` is ``target`` or a later lifecycle state."""
        return cls.rank(value) >= cls.rank(target)


class ScreeningSession(Base):
    """One AI screening conversation for one application."""

    __tablename__ = "screening_sessions"

    __table_args__ = (
        UniqueConstraint(
            "application_id", name="uq_screening_sessions_application"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Opaque per-candidate re-entry credential. Set only by the service, never
    # a column default. NEVER log, audit, or render this value.
    access_token: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )

    # Validated against ScreeningSessionStatus at the service layer. The service
    # always sets this explicitly on insert; the server_default is a backstop
    # kept consistent with the initial state (migration b3d9f0a12c47).
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default=text(f"'{ScreeningSessionStatus.PENDING}'"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # Deliberately NOT including access_token.
        return (
            f"<ScreeningSession id={self.id!r} "
            f"application_id={self.application_id!r} status={self.status!r}>"
        )
