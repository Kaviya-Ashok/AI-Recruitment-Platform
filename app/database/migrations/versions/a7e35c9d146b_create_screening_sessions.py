"""create screening_sessions

Revision ID: a7e35c9d146b
Revises: c9a4f1d7b208
Create Date: 2026-09-02 16:11:07.442019

Notes
-----
Phase 4, CLAUDE.md §2A item 5 (and §15, which already listed
``screening_sessions`` as a planned entity). One row per application: the AI
screening conversation, created automatically by
``screening_pipeline_service`` after resume parsing and prequalification.

* ``application_id`` -> ``applications.id`` is ``ON DELETE RESTRICT``, matching
  ``documents`` / ``resume_extractions`` / ``prequalification_results``, and is
  **UNIQUE** — one screening session per application in this MVP. The unique
  constraint is also the concurrency backstop for overlapping Streamlit reruns.
* ``access_token`` is unique + indexed: the candidate's per-session re-entry
  credential (``secrets.token_urlsafe(32)``, same as ``application_links.token``).
  Credential-equivalent — never logged or audited.
* ``status`` is a validated ``String(50)``, not a native PG ENUM (growing
  vocabulary), so this migration introduces no new enum type and has no
  ``ENUM.create`` / ``ENUM.drop`` lifecycle to manage.

``downgrade`` drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7e35c9d146b'
down_revision: Union[str, Sequence[str], None] = 'c9a4f1d7b208'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'screening_sessions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('access_token', sa.String(length=128), nullable=False),
        sa.Column(
            'status', sa.String(length=50),
            server_default=sa.text("'CREATED'"), nullable=False,
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'application_id', name='uq_screening_sessions_application',
        ),
    )
    op.create_index(
        op.f('ix_screening_sessions_access_token'),
        'screening_sessions', ['access_token'], unique=True,
    )
    op.create_index(
        op.f('ix_screening_sessions_application_id'),
        'screening_sessions', ['application_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_screening_sessions_application_id'),
        table_name='screening_sessions',
    )
    op.drop_index(
        op.f('ix_screening_sessions_access_token'),
        table_name='screening_sessions',
    )
    op.drop_table('screening_sessions')
