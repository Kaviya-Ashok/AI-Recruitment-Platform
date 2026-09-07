"""ApplicationLink model — the unique, opaque candidate application link.

Phase 1.4 scope: one row per generated link for a job. The ``token`` is a
cryptographically random, URL-safe opaque credential (``secrets.token_urlsafe``)
with **nothing** guessable encoded in it — no job id, no timestamp, no counter.

Lifecycle (mirrors ``RubricVersion`` supersession):
* ``ACTIVE``      — the current link; at most one per job at a time.
* ``REVOKED``     — HR explicitly disabled it; no active link until regenerate.
* ``SUPERSEDED``  — replaced by a later regeneration. Kept for history.

Whether a link resolves for a candidate is a combination of *this* status **and**
``jobs.status`` — closing a job stops applications without any write here.

SECURITY: ``token`` is credential-equivalent. It must never appear in audit
metadata, logs, or error messages — only in the one HR-facing UI display of
their own job's current active link.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class ApplicationLinkStatus:
    """Lifecycle states for an application link (validated string column)."""

    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    SUPERSEDED = "SUPERSEDED"

    ALL: frozenset[str] = frozenset({ACTIVE, REVOKED, SUPERSEDED})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls.ALL


class ApplicationLink(Base):
    """One generated application link for a job."""

    __tablename__ = "application_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Opaque random credential. UNIQUE index is a DB-level backstop on top of
    # service-layer randomness. Set only by the service, never a column default.
    token: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )

    # 1, 2, 3... per job — increments on each regeneration (traceability).
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Validated against ApplicationLinkStatus at the service layer.
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default=text(f"'{ApplicationLinkStatus.ACTIVE}'"),
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
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
        # Deliberately NOT including the token.
        return (
            f"<ApplicationLink id={self.id!r} job_id={self.job_id!r} "
            f"seq={self.sequence_number} status={self.status!r}>"
        )
