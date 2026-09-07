"""The SYSTEM actor — read-only accessor for the seeded automated-pipeline user
(CLAUDE.md §2A item 2).

WHY THIS EXISTS
---------------
``parse_resume`` / ``prequalify_application`` (and the getters around them)
hard-enforce :func:`~app.utils.authorization.require_internal_user`. Phase 4's
automatic pipeline fires from the unauthenticated public candidate app, where
there is no HR user in session. Rather than punching a hole in that guard, the
pipeline authenticates as a real, always-valid internal ``User`` row whose role
is :attr:`~app.database.models.user.UserRole.SYSTEM`. The guard needs no
special case — this row is simply an active internal user like any other.

That choice is also what keeps the audit trail honest: every event the
automatic pipeline writes carries this actor's id, never ``user_id=None``, so
"the pipeline did it" stays distinguishable from "an HR user did it" and from
the candidate-initiated public events (``CANDIDATE_APPLIED``,
``APPLICATION_LINK_VIEWED``, ``RESUME_UPLOADED``) that legitimately have no
actor.

READ-ONLY BY DESIGN
-------------------
:func:`get_system_user_id` never creates the row. The row is owned by migration
``c9a4f1d7b208`` and must exist in every environment before the app serves
traffic. Creating it lazily at request time would mean an unmigrated database
silently grows a privileged actor from inside a public, unauthenticated code
path — exactly the wrong place for that to happen. A missing row is a
deployment error and is reported as one.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models.user import SYSTEM_USER_EMAIL, User, UserRole


class SystemUserMissingError(Exception):
    """The seeded SYSTEM user is absent, inactive, or has the wrong role.

    Always a deployment/migration problem, never a candidate-facing one. The
    message names the fix; callers in the public app must NOT surface it
    verbatim to a candidate (show a generic message and log this).
    """


_MISSING = (
    "The SYSTEM pipeline user is missing from the database. It is seeded by "
    "migration 'c9a4f1d7b208' — run `alembic upgrade head` against this "
    "environment. It is never created at request time."
)
_INACTIVE = (
    "The SYSTEM pipeline user exists but is not usable (inactive, or not "
    "role=SYSTEM). Automated screening cannot run until it is restored; "
    "re-check the database against migration 'c9a4f1d7b208'."
)


def get_system_user(db: Session) -> User:
    """Return the seeded SYSTEM :class:`User`.

    Looked up by :data:`~app.database.models.user.SYSTEM_USER_EMAIL`, which is
    ``UNIQUE`` — so the lookup is single-row by construction.

    Raises
    ------
    SystemUserMissingError
        No such row, or it is inactive / not ``role=SYSTEM``. Never creates it.
    """
    user = db.execute(
        select(User).where(User.email == SYSTEM_USER_EMAIL)
    ).scalar_one_or_none()

    if user is None:
        raise SystemUserMissingError(_MISSING)
    if not user.is_active or user.role is not UserRole.SYSTEM:
        raise SystemUserMissingError(_INACTIVE)
    return user


def get_system_user_id(db: Session) -> uuid.UUID:
    """Return the seeded SYSTEM user's id — the actor id for automated work.

    Raises
    ------
    SystemUserMissingError
        See :func:`get_system_user`.
    """
    return get_system_user(db).id
