"""Code-enforced authorization gate for HR / internal-only service functions.

WHY THIS EXISTS
---------------
Several service functions return candidate-sensitive data — raw resume bytes,
extracted resume evidence, per-criterion prequalification judgments. Until now
they were "HR-only" **by convention only**: the sole thing stopping the
unauthenticated public candidate app (``app/public_main.py``) from calling them
was that they happened to be imported only into ``app/main.py``'s authenticated
page tree. One accidental import — a copy-paste, an autocomplete slip, a future
contributor who doesn't know the convention — would have exposed all of it with
zero code-level resistance.

:func:`require_internal_user` is that missing resistance. Every HR/internal-only
service function calls it as the **first line of its body**, before any business
DB read, any AI call, any Google Drive call. A caller from the public app has no
:class:`~app.database.models.user.User` in session (candidates are not users),
so it passes ``None`` / an invalid id and this raises immediately.

THREAT MODEL — READ THIS
------------------------
This guard defends against **accidental exposure**: a wrong import, a future
mistake, a contributor unaware of the boundary. It is NOT, and is not intended
to be, a defense against a *malicious authenticated internal user* who decides
to misuse their legitimate access — that is a different threat model
(fine-grained per-record authorization, action auditing, anomaly detection) and
is deliberately out of scope here.

WHY "ANY ACTIVE INTERNAL USER" AND NOT A SPECIFIC ROLE
-----------------------------------------------------
``UserRole`` has ``HR``, ``HIRING_MANAGER`` and ``ADMIN`` — all internal staff.
All three legitimately work with candidate evidence in the recruitment workflow
(a hiring manager reviewing a shortlist needs the resume evidence and the
prequalification result). Candidates are not users at all. So "the id resolves
to an active ``User``" is a complete guard against the accidental-public-exposure
threat, and gating to a single role would only risk breaking a legitimate
internal workflow. Fine-grained RBAC, if ever needed, is a separate decision.

Phase 4 added a fourth role, ``SYSTEM`` — the single seeded actor representing
the automated screening pipeline (CLAUDE.md §2A item 2). **This module needs no
special case for it**, and deliberately has none: that row is an active
``User``, so it passes this guard by exactly the same rule as a human one. That
is the whole point — the automatic pipeline authenticates as a real, always-valid
internal actor rather than bypassing the gate. It is resolved through
:func:`app.services.system_user_service.get_system_user_id`, which refuses to
create it on the fly.

Note the boundary this preserves: passing the guard is not the same as being a
person. Anything that must distinguish "a human decided this" from "the pipeline
did this" must check the role or the actor id explicitly — the audit trail does
so by recording the actor on every event.
"""

from __future__ import annotations

import logging
import uuid
from typing import NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.user import User

logger = logging.getLogger(__name__)


class UnauthorizedError(Exception):
    """The caller has not proven an active internal-user identity.

    Carries a single generic message and never echoes the offending id back to
    the caller — same "don't leak which accounts exist / are active" discipline
    as :func:`app.services.auth_service.authenticate_user`. It is a distinct
    type, never conflated with the AI / Drive / parsing domain errors, so a
    caller can catch "not allowed" separately from "the work failed".
    """


_GENERIC_MESSAGE = "an active authenticated internal user is required"


def require_internal_user(
    db: Session, user_id: uuid.UUID | str | None
) -> User:
    """Return the active :class:`User` for ``user_id``, or raise
    :class:`UnauthorizedError`.

    This is the first authorization gate for every HR/internal-only service
    function — call it before any other work.

    Raises :class:`UnauthorizedError` (generic message, no detail to the caller)
    when ``user_id`` is ``None``, is not a well-formed UUID, does not match a
    ``users`` row, or matches a row whose ``is_active`` is false (an account
    disabled mid-session, per CLAUDE.md §23 accounts are disabled not deleted).
    """
    if user_id is None:
        _deny("missing", user_id)

    try:
        resolved = (
            user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
        )
    except (ValueError, TypeError):
        _deny("malformed", user_id)

    user = db.execute(
        select(User).where(User.id == resolved)
    ).scalar_one_or_none()

    if user is None:
        _deny("unknown", resolved)
    if not user.is_active:
        _deny("inactive", resolved)

    return user


def _deny(reason: str, user_id: object) -> NoReturn:
    """Log a coarse reason (no PII, no content — a bare id/reason) and raise."""
    logger.warning("authorization denied: reason=%s user_id=%s", reason, user_id)
    raise UnauthorizedError(_GENERIC_MESSAGE)
