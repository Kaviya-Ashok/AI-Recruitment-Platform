"""Candidate model — a person who has applied through an application link.

Phase 2 scope: candidate identity only. A candidate is **not** a
:class:`~app.database.models.user.User` (users are internal staff); candidates
never authenticate and are reached only through a unique application link.

Design decisions
----------------
ID type — **UUID v4**, Python-side ``default=uuid.uuid4`` (project precedent).

``email`` — **unique + indexed**. This is the dedup key: a person is one row
regardless of how many jobs they apply to. Normalisation (lower-case + trim) is
the service layer's job — the same canonical-form approach as ``users.email`` —
so the DB unique constraint actually blocks ``Foo@x.com`` / ``foo@x.com``.

``email`` and ``phone`` are **personal data** (CLAUDE.md §§23, 24). They must
never appear in audit metadata, logs, or UI-facing error messages — reference a
candidate by ``id`` only in those paths.

No ``updated_at``: a Phase 2 candidate row is write-once (identity captured at
application time). Add one if/when candidate self-service editing arrives.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class Candidate(Base):
    """A person who has applied to at least one job."""

    __tablename__ = "candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Dedup key. Stored in a single canonical (lower-cased, trimmed) form by the
    # service layer; the unique index is the DB-level backstop.
    email: Mapped[str] = mapped_column(
        String(320),  # RFC 5321 max, matches users.email
        nullable=False,
        unique=True,
        index=True,
    )

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        # Deliberately NOT including email / phone / name (personal data).
        return f"<Candidate id={self.id!r}>"
