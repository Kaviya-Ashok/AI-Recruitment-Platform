"""create final_decisions

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-10-07 14:00:00.000000

Notes
-----
Step 11 (CLAUDE.md §11): the hiring manager's final human decision. The only
place a REJECT can be recorded.

Revising a decision is append-only: a new row is inserted and the previous CURRENT
row becomes ``SUPERSEDED``. The PARTIAL UNIQUE INDEX
``uq_final_decisions_one_current_per_application`` makes the database refuse a
second CURRENT decision for the same application.

``decision`` / ``status`` are validated strings, not native Postgres enums (repo
convention). ``rationale`` is the decider's own words and is NOT NULL.

The snapshot columns copy what the decider could see at that moment. Two are
RESTRICT foreign keys (``final_ranking_entries``, ``post_interview_analyses``)
because nothing deletes from either table; ``rubric_version_id`` is a plain UUID
(it is a copy, never joined through). ``created_at`` has NO server default: the
service sets it explicitly so two writes in one transaction can never tie.

``downgrade`` drops the indexes then the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c0d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'final_decisions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('decided_by_user_id', sa.UUID(), nullable=False),
        sa.Column('decision', sa.String(length=20), nullable=False),
        sa.Column('rationale', sa.Text(), nullable=False),
        sa.Column(
            'status', sa.String(length=20), nullable=False,
            server_default=sa.text("'CURRENT'"),
        ),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('final_ranking_entry_id', sa.UUID(), nullable=True),
        sa.Column('post_interview_analysis_id', sa.UUID(), nullable=True),
        sa.Column('final_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('final_rank', sa.Integer(), nullable=True),
        sa.Column('entry_status', sa.String(length=30), nullable=True),
        sa.Column('final_confidence', sa.String(length=10), nullable=True),
        sa.Column('rubric_version_id', sa.UUID(), nullable=True),
        sa.Column('ai_recommendation_snapshot', sa.String(length=20), nullable=True),
        sa.Column(
            'interview_feedback_ids', postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['decided_by_user_id'], ['users.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['final_ranking_entry_id'], ['final_ranking_entries.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['post_interview_analysis_id'], ['post_interview_analyses.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_final_decisions_application_id'), 'final_decisions',
        ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_final_decisions_decided_by_user_id'), 'final_decisions',
        ['decided_by_user_id'], unique=False,
    )
    op.create_index(
        op.f('ix_final_decisions_final_ranking_entry_id'), 'final_decisions',
        ['final_ranking_entry_id'], unique=False,
    )
    op.create_index(
        op.f('ix_final_decisions_post_interview_analysis_id'), 'final_decisions',
        ['post_interview_analysis_id'], unique=False,
    )
    op.create_index(
        'uq_final_decisions_one_current_per_application', 'final_decisions',
        ['application_id'], unique=True,
        postgresql_where=sa.text("status = 'CURRENT'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'uq_final_decisions_one_current_per_application',
        table_name='final_decisions',
        postgresql_where=sa.text("status = 'CURRENT'"),
    )
    op.drop_index(
        op.f('ix_final_decisions_post_interview_analysis_id'),
        table_name='final_decisions',
    )
    op.drop_index(
        op.f('ix_final_decisions_final_ranking_entry_id'),
        table_name='final_decisions',
    )
    op.drop_index(
        op.f('ix_final_decisions_decided_by_user_id'), table_name='final_decisions',
    )
    op.drop_index(
        op.f('ix_final_decisions_application_id'), table_name='final_decisions',
    )
    op.drop_table('final_decisions')
