"""create_users_and_audit_events

Revision ID: df02835dc658
Revises:
Create Date: 2026-09-01 17:43:24.776946

Notes
-----
The ``user_role`` Postgres ENUM is managed explicitly here rather than as an
implicit side effect of the ``role`` column:

* ``upgrade()``   -> ``ENUM.create(..., checkfirst=True)`` before the table, and
  ``create_type=False`` on the column so the table DDL does not try to create it
  again.
* ``downgrade()`` -> ``ENUM.drop(..., checkfirst=True)`` after the table is
  dropped. Alembic autogenerate does NOT emit this, which would otherwise leave
  an orphaned type and break a subsequent ``upgrade``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'df02835dc658'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# create_type=False: this migration manages the type lifecycle explicitly
# (create() in upgrade, drop() in downgrade) so the table DDL must not also
# try to emit CREATE TYPE.
user_role_enum = postgresql.ENUM(
    'HR', 'HIRING_MANAGER', 'ADMIN', name='user_role', create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    user_role_enum.create(bind, checkfirst=True)

    op.create_table(
        'users',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('hashed_password', sa.String(length=255), nullable=False),
        sa.Column('full_name', sa.String(length=255), nullable=False),
        sa.Column('role', user_role_enum, nullable=False),
        sa.Column(
            'is_active', sa.Boolean(),
            server_default=sa.text('true'), nullable=False,
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    op.create_table(
        'audit_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column(
            'timestamp', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column('user_id', sa.UUID(), nullable=True),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('entity_type', sa.String(length=50), nullable=True),
        sa.Column('entity_id', sa.UUID(), nullable=True),
        sa.Column('action', sa.String(length=500), nullable=False),
        sa.Column(
            'previous_state',
            postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        ),
        sa.Column(
            'new_state',
            postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        ),
        sa.Column(
            'event_metadata',
            postgresql.JSONB(astext_type=sa.Text()), nullable=True,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_audit_events_entity_id'), 'audit_events',
        ['entity_id'], unique=False,
    )
    op.create_index(
        op.f('ix_audit_events_event_type'), 'audit_events',
        ['event_type'], unique=False,
    )
    op.create_index(
        op.f('ix_audit_events_timestamp'), 'audit_events',
        ['timestamp'], unique=False,
    )
    op.create_index(
        op.f('ix_audit_events_user_id'), 'audit_events',
        ['user_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_audit_events_user_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_timestamp'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_event_type'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_entity_id'), table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')

    # Autogenerate does not emit this; without it the type is orphaned and a
    # subsequent upgrade fails with "type user_role already exists".
    user_role_enum.drop(op.get_bind(), checkfirst=True)
