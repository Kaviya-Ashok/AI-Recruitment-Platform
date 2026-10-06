"""create final_rankings and final_ranking_entries

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-10-07 09:00:00.000000

Notes
-----
Step 10b: the post-interview final score and final ranking.

``final_rankings`` is one HR-triggered run per ``(job_id, rubric_version_id)``.
Regeneration is NON-DESTRUCTIVE: the previous CURRENT run becomes ``SUPERSEDED``
and a new CURRENT one is inserted. The PARTIAL UNIQUE INDEX
``uq_final_rankings_one_current_per_partition`` makes the database refuse a second
CURRENT run for the same partition.

``final_ranking_entries`` is the per-candidate census of a run. It deliberately
has NO foreign key into ``candidate_rankings`` (Step 5 deletes and re-inserts its
rows, so an inbound RESTRICT FK would break Step 5's regeneration) and none into
``screening_evaluations``: screening provenance is stored as plain columns
(``screening_generation_batch_id``, ``screening_generated_at``,
``screening_rank``). Every FK that does exist points at a row that is never
deleted, and is ``ON DELETE RESTRICT``.

``status`` / ``entry_status`` are validated strings, not native Postgres enums
(repo convention). No names, notes, evidence or transcript text is stored.

``downgrade`` drops the entries table, then the runs table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c0d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = 'b9c0d1e2f3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'final_rankings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column('requested_by_user_id', sa.UUID(), nullable=False),
        sa.Column('screening_weight', sa.Numeric(5, 4), nullable=False),
        sa.Column('interview_weight', sa.Numeric(5, 4), nullable=False),
        sa.Column(
            'status', sa.String(length=20), nullable=False,
            server_default=sa.text("'CURRENT'"),
        ),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['requested_by_user_id'], ['users.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_final_rankings_job_id'), 'final_rankings', ['job_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_final_rankings_rubric_version_id'), 'final_rankings',
        ['rubric_version_id'], unique=False,
    )
    op.create_index(
        op.f('ix_final_rankings_requested_by_user_id'), 'final_rankings',
        ['requested_by_user_id'], unique=False,
    )
    op.create_index(
        'uq_final_rankings_one_current_per_partition', 'final_rankings',
        ['job_id', 'rubric_version_id'], unique=True,
        postgresql_where=sa.text("status = 'CURRENT'"),
    )

    op.create_table(
        'final_ranking_entries',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('final_ranking_id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('screening_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('interview_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('final_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('rank', sa.Integer(), nullable=True),
        sa.Column('eligible', sa.Boolean(), nullable=False),
        sa.Column(
            'mandatory_unknown', sa.Boolean(), nullable=False,
            server_default=sa.text('false'),
        ),
        sa.Column('entry_status', sa.String(length=30), nullable=False),
        sa.Column(
            'status_reason', sa.Text(), nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column('final_confidence', sa.String(length=10), nullable=True),
        sa.Column('screening_confidence', sa.String(length=10), nullable=True),
        sa.Column('screening_generation_batch_id', sa.UUID(), nullable=True),
        sa.Column(
            'screening_generated_at', sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column('screening_rank', sa.Integer(), nullable=True),
        sa.Column('post_interview_analysis_id', sa.UUID(), nullable=True),
        sa.Column(
            'rounds_used', postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            'round_means', postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            'feedback_ids', postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ['final_ranking_id'], ['final_rankings.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['post_interview_analysis_id'], ['post_interview_analyses.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'final_ranking_id', 'application_id',
            name='uq_final_ranking_entries_run_application',
        ),
    )
    op.create_index(
        op.f('ix_final_ranking_entries_final_ranking_id'),
        'final_ranking_entries', ['final_ranking_id'], unique=False,
    )
    op.create_index(
        op.f('ix_final_ranking_entries_application_id'),
        'final_ranking_entries', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_final_ranking_entries_post_interview_analysis_id'),
        'final_ranking_entries', ['post_interview_analysis_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_final_ranking_entries_post_interview_analysis_id'),
        table_name='final_ranking_entries',
    )
    op.drop_index(
        op.f('ix_final_ranking_entries_application_id'),
        table_name='final_ranking_entries',
    )
    op.drop_index(
        op.f('ix_final_ranking_entries_final_ranking_id'),
        table_name='final_ranking_entries',
    )
    op.drop_table('final_ranking_entries')
    op.drop_index(
        'uq_final_rankings_one_current_per_partition',
        table_name='final_rankings',
        postgresql_where=sa.text("status = 'CURRENT'"),
    )
    op.drop_index(
        op.f('ix_final_rankings_requested_by_user_id'), table_name='final_rankings',
    )
    op.drop_index(
        op.f('ix_final_rankings_rubric_version_id'), table_name='final_rankings',
    )
    op.drop_index(op.f('ix_final_rankings_job_id'), table_name='final_rankings')
    op.drop_table('final_rankings')
