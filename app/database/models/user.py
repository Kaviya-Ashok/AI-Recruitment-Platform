"""User model — internal authenticated staff of the recruitment platform.

Per CLAUDE.md the MVP has only *internal* authenticated users: HR/TA staff and
hiring managers. Candidates are **not** users — they interact through unique
application links and are modelled separately in a later phase.

Relationship to CLAUDE.md security / audit requirements:

* Section 20 (Audit logging): every meaningful action is attributed to a
  ``User`` via :class:`~app.database.models.audit_event.AuditEvent.user_id`.
  ``id`` is therefore a long-lived identifier that many future tables will
  reference — see the ID-type note below.
* Section 23 (Database rules): accounts are **disabled, never hard-deleted**
  (``is_active``) so that audit history and foreign-key references stay intact.
* Security: only ``hashed_password`` is stored. There is deliberately no
  plaintext password column, not even a transient one. Hashing/verification
  logic is added in a later step; this module only defines storage.

Design decisions
----------------
ID type — **UUID (v4)**:
    This is a hiring platform. ``users.id`` will be referenced across many
    tables (audit events, jobs, decisions, interview feedback, ...) and may
    appear in URLs and exported records. Sequential integers would leak row
    counts and be trivially enumerable; UUIDs are opaque, collision-safe
    without coordination, and stable across data migrations/merges. The small
    storage/index cost is acceptable for an internal-user table.

role — **enum, not free text**:
    The set of internal roles is small, closed and security-relevant
    (authorisation decisions will branch on it). A DB-backed enum gives a
    schema-level guarantee that only valid roles exist, is self-documenting,
    and fails loudly on typos. The cost — needing a migration to add a value —
    is fine here because the role set is expected to stay stable, unlike the
    fast-growing audit ``event_type`` list.

    Phase 4 widened it once, to add ``SYSTEM`` (CLAUDE.md §2A item 2). Note for
    any future widening: ``ALTER TYPE ... ADD VALUE`` cannot *use* the new value
    in the transaction that added it, and Alembic runs the whole upgrade in one
    transaction, so the migration recreates the type instead (rename -> create
    -> cast column -> drop old). See ``c9a4f1d7b208``.

THE SYSTEM ACTOR
----------------
``UserRole.SYSTEM`` + the one row identified by :data:`SYSTEM_USER_ID` /
:data:`SYSTEM_USER_EMAIL` represent "the automated pipeline" (CLAUDE.md §2A
item 2). It exists because the HR-only guard
:func:`~app.utils.authorization.require_internal_user` is hard-enforced on
``parse_resume`` / ``prequalify_application``, while the automatic screening
pipeline is triggered from the unauthenticated public candidate app where no
real HR user is present. Attributing those audit events to this actor — rather
than ``user_id=None`` — keeps automated actions distinguishable from genuine
HR-initiated ones and from candidate-initiated public ones.

It is **not a login**: the row is seeded by migration with a deliberately
unusable password hash (:data:`_UNUSABLE_PASSWORD_HASH`-style sentinel, not a
bcrypt string), so :func:`~app.services.auth_service.authenticate_user` can
never return it — ``verify_password`` yields ``False`` for a malformed hash.
Read it through :func:`~app.services.system_user_service.get_system_user_id`;
never construct it at request time.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.database.database import Base


class UserRole(str, enum.Enum):
    """Closed set of internal user roles.

    ``HR``             — HR / talent-acquisition staff (default operator role).
    ``HIRING_MANAGER`` — owns the final hiring decision for their reqs.
    ``ADMIN``          — platform administration / user management.
    ``SYSTEM``         — the automated pipeline; NOT a human, NOT a login.
                         Exactly one such row exists, seeded by migration
                         ``c9a4f1d7b208`` (CLAUDE.md §2A item 2). See the
                         module docstring.
    """

    HR = "HR"
    HIRING_MANAGER = "HIRING_MANAGER"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


#: Fixed, deterministic id of the one seeded ``SYSTEM`` user. Hard-coded (never
#: random) so the automated pipeline can reference the same actor in every
#: environment. The migration inserts this literal; a test asserts the seeded
#: row's id matches this constant, so the two can never drift apart.
SYSTEM_USER_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")

#: Fixed, well-known address of the seeded ``SYSTEM`` user. ``.local`` is
#: reserved (RFC 6762) and can never be a deliverable mailbox. ``users.email``
#: is UNIQUE, which is what makes the lookup single-row and the seed insert
#: safely re-runnable.
SYSTEM_USER_EMAIL: str = "system@internal.local"


class User(Base):
    """An internal authenticated user (HR/TA, hiring manager, or admin)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    email: Mapped[str] = mapped_column(
        String(320),  # max length of an RFC 5321 email address
        unique=True,
        index=True,
        nullable=False,
    )

    # Stores ONLY a one-way password hash. Never assign a plaintext password
    # to this column. Hashing is performed by the auth layer (added later).
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=True, validate_strings=True),
        nullable=False,
    )

    # Disable accounts instead of deleting them (CLAUDE.md §23).
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=expression.true(),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ``onupdate`` is emitted by SQLAlchemy on ORM/Core UPDATEs; the value
    # itself is computed server-side. A raw-SQL UPDATE would bypass it, but
    # that is acceptable for the MVP.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper only
        return f"<User id={self.id!r} email={self.email!r} role={self.role!r}>"
