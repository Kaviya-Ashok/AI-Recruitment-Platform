"""add SYSTEM user_role and seed the system user

Revision ID: c9a4f1d7b208
Revises: e3c212e580f7
Create Date: 2026-09-02 16:02:44.910233

Notes
-----
Phase 4, CLAUDE.md §2A item 2: the automatic screening pipeline runs from the
unauthenticated public candidate app, where no HR user is present, but
``parse_resume`` / ``prequalify_application`` hard-enforce
``require_internal_user``. This migration creates the legitimate, always-valid
actor that guard accepts.

WHY THE ENUM IS RECREATED RATHER THAN ``ALTER TYPE ... ADD VALUE``
-----------------------------------------------------------------
``ALTER TYPE user_role ADD VALUE 'SYSTEM'`` would be the obvious one-liner, but
Postgres forbids *using* a newly added enum label in the same transaction that
added it, and this project's Alembic runs the whole upgrade in a single
transaction (``transaction_per_migration`` is not enabled). The seed INSERT
below needs the label immediately, so the one-liner cannot work here.

The rename/create/cast/drop dance is fully transactional and makes the label
usable at once:

    ALTER TYPE user_role RENAME TO user_role_old;
    CREATE TYPE user_role AS ENUM (..., 'SYSTEM');
    ALTER TABLE users ALTER COLUMN role TYPE user_role USING role::text::user_role;
    DROP TYPE user_role_old;

``users.role`` is the only column using the type (verified), and it has no
server default, so there is no default to drop and re-add around the cast.

This still honours the explicit-DROP-TYPE lesson from ``df02835dc658`` /
``e46018d1bb97``: the type's lifecycle is managed by hand in both directions —
autogenerate never emits ``DROP TYPE``, and leaving one orphaned breaks the
next upgrade with "type user_role already exists".

SEEDED ROW — IDEMPOTENT
-----------------------
Fixed id ``00000000-...-0001`` and fixed email ``system@internal.local`` (both
mirrored as constants in ``app/database/models/user.py``; a test asserts they
match). The insert is ``INSERT ... SELECT ... WHERE NOT EXISTS`` keyed on
**either** the id or the email, so re-running it can never produce a second
row, whichever identity a pre-existing row was created under.

``hashed_password`` is the sentinel ``'!'`` — deliberately NOT a bcrypt string.
``auth_service.verify_password`` returns ``False`` for a malformed hash, so this
account can never be logged into. It is an actor identity, not a login.

``downgrade`` deletes that seeded row *before* narrowing the type (the cast
would otherwise fail on it). ``audit_events.user_id`` is ``ON DELETE SET NULL``,
so audit history survives the delete with a null actor. If any OTHER row still
holds ``SYSTEM``, the narrowing cast fails loudly — which is correct: silently
rewriting somebody's role would be worse than refusing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c9a4f1d7b208'
down_revision: Union[str, Sequence[str], None] = 'e3c212e580f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept as literals (not imported from app code): a migration is a historical
# artifact and must keep working even if the application constants move.
# tests/test_system_user_service.py asserts these agree with app/database/
# models/user.py, so the duplication can never silently drift.
SYSTEM_USER_ID = '00000000-0000-0000-0000-000000000001'
SYSTEM_USER_EMAIL = 'system@internal.local'
SYSTEM_USER_FULL_NAME = 'Automated Screening Pipeline'
# Not a bcrypt hash -> verify_password() can never match it.
UNUSABLE_PASSWORD_HASH = '!'

_OLD_ROLES = ('HR', 'HIRING_MANAGER', 'ADMIN')
_NEW_ROLES = ('HR', 'HIRING_MANAGER', 'ADMIN', 'SYSTEM')


def _recreate_user_role_enum(values: tuple[str, ...]) -> None:
    """Swap ``user_role`` for a new type with exactly ``values``.

    Transactional, and the new labels are usable immediately afterwards.
    """
    op.execute('ALTER TYPE user_role RENAME TO user_role_old')
    postgresql.ENUM(*values, name='user_role', create_type=False).create(
        op.get_bind(), checkfirst=False
    )
    op.execute(
        'ALTER TABLE users ALTER COLUMN role TYPE user_role '
        'USING role::text::user_role'
    )
    op.execute('DROP TYPE user_role_old')


def upgrade() -> None:
    """Upgrade schema."""
    _recreate_user_role_enum(_NEW_ROLES)

    # Idempotent seed: guarded on id OR email so a re-run under either identity
    # is a no-op. Written as one statement so there is no check-then-insert race.
    op.execute(
        sa.text(
            """
            INSERT INTO users (
                id, email, hashed_password, full_name, role, is_active
            )
            SELECT
                CAST(:id AS uuid), :email, :pw, :full_name,
                CAST('SYSTEM' AS user_role), true
            WHERE NOT EXISTS (
                SELECT 1 FROM users
                WHERE id = CAST(:id AS uuid) OR email = :email
            )
            """
        ).bindparams(
            id=SYSTEM_USER_ID,
            email=SYSTEM_USER_EMAIL,
            pw=UNUSABLE_PASSWORD_HASH,
            full_name=SYSTEM_USER_FULL_NAME,
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Must precede the narrowing cast: a surviving SYSTEM row would break it.
    # audit_events.user_id is ON DELETE SET NULL, so audit rows are preserved.
    op.execute(
        sa.text('DELETE FROM users WHERE id = CAST(:id AS uuid)').bindparams(
            id=SYSTEM_USER_ID
        )
    )
    _recreate_user_role_enum(_OLD_ROLES)
