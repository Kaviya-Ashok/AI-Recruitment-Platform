"""create candidate_rankings

Revision ID: f1a2b3c4d5e6
Revises: e5b2a9c31f74
Create Date: 2026-09-03 19:10:44.000000

Notes
-----
Phase 4 Step 5: cross-candidate ranking (CLAUDE.md §§5, 12, 34).

One row per candidate per ``(job_id, rubric_version_id)`` partition per
generation run. ``rubric_version_id`` partitions the table because candidates
scored against different rubric versions are not comparable and must never share
a ranked list.

This table is a **full replace**, not an append-only log:
``ranking_service.generate_ranking`` DELETEs every row for a partition and
inserts a fresh set in one transaction each time HR triggers a ranking. The
``UNIQUE (job_id, rubric_version_id, application_id)`` constraint enforces
"one row per candidate per partition" after that replace. ``generation_batch_id``
groups a run's rows and correlates them with the ``RANKING_GENERATED`` audit
event.

All three FKs are ``ON DELETE RESTRICT`` — the same "independent business
record, never a silent cascade" convention as ``applications`` /
``prequalification_results`` / ``screening_evaluations``.

``rank_position`` is NULL for ineligible candidates (a reconciled MANDATORY
criterion is FAIL); ``overall_score`` is NULL only when every screening-
evaluation bucket score was NULL for that candidate.

``downgrade`` drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e5b2a9c31f74'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'candidate_rankings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('rank_position', sa.Integer(), nullable=True),
        sa.Column('overall_score', sa.Float(), nullable=True),
        sa.Column('eligible', sa.Boolean(), nullable=False),
        sa.Column(
            'mandatory_unknown_flag', sa.Boolean(),
            server_default=sa.text('false'), nullable=False,
        ),
        sa.Column(
            'generated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column('generation_batch_id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ['job_id'], ['jobs.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'job_id', 'rubric_version_id', 'application_id',
            name='uq_candidate_rankings_partition_application',
        ),
    )
    op.create_index(
        op.f('ix_candidate_rankings_job_id'),
        'candidate_rankings', ['job_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_rankings_rubric_version_id'),
        'candidate_rankings', ['rubric_version_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_rankings_application_id'),
        'candidate_rankings', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_rankings_generation_batch_id'),
        'candidate_rankings', ['generation_batch_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_candidate_rankings_generation_batch_id'),
        table_name='candidate_rankings',
    )
    op.drop_index(
        op.f('ix_candidate_rankings_application_id'),
        table_name='candidate_rankings',
    )
    op.drop_index(
        op.f('ix_candidate_rankings_rubric_version_id'),
        table_name='candidate_rankings',
    )
    op.drop_index(
        op.f('ix_candidate_rankings_job_id'),
        table_name='candidate_rankings',
    )
    op.drop_table('candidate_rankings')
