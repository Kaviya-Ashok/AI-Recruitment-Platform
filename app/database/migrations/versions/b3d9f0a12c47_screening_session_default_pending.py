"""realign screening_sessions.status server-default to PENDING

Revision ID: b3d9f0a12c47
Revises: a7e35c9d146b
Create Date: 2026-09-03 10:15:22.400188

Notes
-----
Phase 4 Step 2 retires the Step 1 ``ScreeningSessionStatus.CREATED`` value and
replaces the initial state with ``PENDING`` (row exists, pipeline not finished).
The screening vocabulary is a validated ``String(50)``, not a PG enum, so the
new *values* need no migration — but the column's ``server_default`` still
literally said ``'CREATED'``, a value that no longer exists in the vocabulary.

This migration is nothing but ``ALTER COLUMN status SET DEFAULT 'PENDING'`` and
its reverse. No enum, no type change, no data backfill: the ``screening_sessions``
table holds no rows (Step 1 shipped no screening data), and the service sets
``status`` explicitly on every insert regardless — the default is only a
backstop. Keeping it honest avoids a future raw insert silently producing an
invalid ``'CREATED'`` row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3d9f0a12c47'
down_revision: Union[str, Sequence[str], None] = 'a7e35c9d146b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'screening_sessions', 'status',
        existing_type=sa.String(length=50),
        existing_nullable=False,
        server_default=sa.text("'PENDING'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'screening_sessions', 'status',
        existing_type=sa.String(length=50),
        existing_nullable=False,
        server_default=sa.text("'CREATED'"),
    )
