"""ScreeningAnswer model — one candidate answer to one screening question
(CLAUDE.md §§3, 23, 24; Phase 4 Step 3).

One row = one candidate's answer to one :class:`~app.database.models.
screening_question.ScreeningQuestion`. **One answer per question** — this MVP
has no multi-turn threads on a single question (CLAUDE.md §3: batch-generated
rounds, not a chat loop). The candidate may edit their answer while the round is
still open; once the round is marked complete the service refuses further
writes.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``screening_question_id`` — FK to ``screening_questions.id``, **``ON DELETE
RESTRICT``** and **UNIQUE**. RESTRICT for the same reason as every other
AI/candidate record: it must not vanish as a side effect. UNIQUE encodes "one
answer per question" and is the concurrency backstop — two overlapping submit
requests both pass the service's check-first guard, and this constraint (not
the check) guarantees a single row; the service catches the ``IntegrityError``
and resolves it to an update of the existing row.

``answer_text`` — ``Text``, NOT NULL. Candidate-supplied free text. It is
**UNTRUSTED DATA** everywhere it is used — exactly as untrusted as the resume:
it is framed as untrusted in the round-2 prompt, and it is NEVER logged at more
than a redacted/length-only level, NEVER put in audit metadata.

``submitted_at`` — ``timestamptz``, **NULLABLE**. Set on the candidate's first
answer to this question and then left alone; later edits touch ``updated_at``
only. So ``submitted_at`` answers "when did the candidate first respond to
this?". It is nullable because the column outlives this step — a future
explicit draft/submit split could create a row with a draft ``answer_text`` and
a null ``submitted_at`` — but in Step 3 every row written by ``submit_answer``
has it set.

``created_at`` / ``updated_at`` — timezone-aware, server-defaulted, ``onupdate``
on ``updated_at``. Unlike write-once AI-output rows, an answer row is mutated
(edited) while its round is open.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ScreeningAnswer(Base):
    """One candidate answer to one screening question."""

    __tablename__ = "screening_answers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    screening_question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("screening_questions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )

    # UNTRUSTED candidate free text. Never logged raw, never in audit metadata.
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Set once, on the first answer; edits don't move it. Null only if a future
    # step introduces a pure-draft row (not in Step 3).
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
        # No answer_text (untrusted candidate free text).
        return (
            f"<ScreeningAnswer id={self.id!r} "
            f"question={self.screening_question_id!r} "
            f"submitted={self.submitted_at is not None}>"
        )
