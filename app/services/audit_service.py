"""Audit-trail service.

Phase 0 wrote :class:`AuditEvent` rows inline in ``auth_service``. Phase 1.1 is
the first non-auth action that needs to audit, so the write is factored into a
single reusable helper here. It only ever INSERTs (CLAUDE.md §20: the audit
table is append-only).

The caller owns the transaction: ``record_event`` adds the row and ``flush``es
it, but does not ``commit``. This lets a service commit the business row and its
audit row together (all-or-nothing).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.database.models.audit_event import AuditEvent, AuditEventType


def record_event(
    db: Session,
    *,
    event_type: AuditEventType | str,
    action: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    previous_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Insert one audit event and flush it. Does not commit.

    ``event_type`` accepts an :class:`AuditEventType` member or a raw string;
    it is normalised and validated against the canonical list.

    Raises
    ------
    ValueError
        If ``event_type`` is not a known :class:`AuditEventType` value, or
        ``action`` is empty.
    """
    type_value = getattr(event_type, "value", event_type)
    if type_value not in {e.value for e in AuditEventType}:
        raise ValueError(f"unknown audit event_type: {type_value!r}")
    if not action or not action.strip():
        raise ValueError("audit action must not be empty")

    event = AuditEvent(
        event_type=type_value,
        action=action.strip(),
        entity_type=entity_type,
        entity_id=entity_id,
        user_id=user_id,
        previous_state=previous_state,
        new_state=new_state,
        event_metadata=metadata,
    )
    db.add(event)
    db.flush()
    return event
