"""create_candidates_and_applications

Revision ID: e7617aa2220b
Revises: 3f70f447c1a6
Create Date: 2026-09-02 01:09:39.293322

Notes
-----
Phase 2 foundational schema: ``candidates`` (person identity, one row per email)
and ``applications`` (candidate x job x status, one row per application attempt).

``applications.status`` is a validated ``String`` (constants class
``ApplicationStatus``), not a native Postgres ENUM — same growing-vocabulary rule
as job / rubric / application-link status. No new PG ENUM type is introduced by
this migration, so there is no ``ENUM.create`` / ``ENUM.drop`` lifecycle to
manage (contrast the jd_input_method migration).

``candidates.email`` has a UNIQUE index — the DB-level backstop for the
service-layer dedup on the normalised email. ``(candidate_id, job_id)`` is UNIQUE
on ``applications`` — one application per person per job.

``downgrade`` drops ``applications`` before ``candidates`` (FK-safe order).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7617aa2220b'
down_revision: Union[str, Sequence[str], None] = '3f70f447c1a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'candidates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('full_name', sa.String(length=255), nullable=False),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_candidates_email'), 'candidates', ['email'], unique=True,
    )

    op.create_table(
        'applications',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('candidate_id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('application_link_id', sa.UUID(), nullable=False),
        sa.Column(
            'status', sa.String(length=50),
            server_default=sa.text("'APPLIED'"), nullable=False,
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
            ['candidate_id'], ['candidates.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['job_id'], ['jobs.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['application_link_id'], ['application_links.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'candidate_id', 'job_id', name='uq_applications_candidate_job',
        ),
    )
    op.create_index(
        op.f('ix_applications_candidate_id'), 'applications',
        ['candidate_id'], unique=False,
    )
    op.create_index(
        op.f('ix_applications_job_id'), 'applications',
        ['job_id'], unique=False,
    )
    op.create_index(
        op.f('ix_applications_application_link_id'), 'applications',
        ['application_link_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_applications_application_link_id'), table_name='applications'
    )
    op.drop_index(
        op.f('ix_applications_job_id'), table_name='applications'
    )
    op.drop_index(
        op.f('ix_applications_candidate_id'), table_name='applications'
    )
    op.drop_table('applications')

    op.drop_index(op.f('ix_candidates_email'), table_name='candidates')
    op.drop_table('candidates')
