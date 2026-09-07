"""create_rubric_versions_and_rubric_criteria

Revision ID: d0bf56dc9ba8
Revises: e46018d1bb97
Create Date: 2026-09-01 21:50:44.209558

Notes
-----
``rubric_versions.status`` is a validated ``String`` (constants class
``RubricVersionStatus``), not a native Postgres ENUM — per the project's
growing-vocabulary rule (a future ``REJECTED`` state is plausible). So there is
no ENUM lifecycle to manage here, unlike the Phase 0/1.1 migrations.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd0bf56dc9ba8'
down_revision: Union[str, Sequence[str], None] = 'e46018d1bb97'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'rubric_versions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column(
            'status', sa.String(length=50),
            server_default=sa.text("'DRAFT'"), nullable=False,
        ),
        sa.Column('generated_from_requirements_version', sa.Integer(), nullable=False),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('approved_by', sa.UUID(), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['approved_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_rubric_versions_created_by'), 'rubric_versions',
        ['created_by'], unique=False,
    )
    op.create_index(
        op.f('ix_rubric_versions_job_id'), 'rubric_versions',
        ['job_id'], unique=False,
    )

    op.create_table(
        'rubric_criteria',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column('requirement_type', sa.String(length=50), nullable=False),
        sa.Column('category', sa.String(length=100), nullable=True),
        sa.Column('criterion_text', sa.Text(), nullable=False),
        sa.Column('display_order', sa.Integer(), nullable=False),
        sa.Column('source_job_requirement_id', sa.UUID(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['source_job_requirement_id'], ['job_requirements.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_rubric_criteria_rubric_version_id'), 'rubric_criteria',
        ['rubric_version_id'], unique=False,
    )
    op.create_index(
        op.f('ix_rubric_criteria_source_job_requirement_id'), 'rubric_criteria',
        ['source_job_requirement_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_rubric_criteria_source_job_requirement_id'),
        table_name='rubric_criteria',
    )
    op.drop_index(
        op.f('ix_rubric_criteria_rubric_version_id'), table_name='rubric_criteria'
    )
    op.drop_table('rubric_criteria')
    op.drop_index(
        op.f('ix_rubric_versions_job_id'), table_name='rubric_versions'
    )
    op.drop_index(
        op.f('ix_rubric_versions_created_by'), table_name='rubric_versions'
    )
    op.drop_table('rubric_versions')
