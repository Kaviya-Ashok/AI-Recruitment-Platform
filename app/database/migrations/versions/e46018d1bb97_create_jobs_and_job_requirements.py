"""create_jobs_and_job_requirements

Revision ID: e46018d1bb97
Revises: df02835dc658
Create Date: 2026-09-01 18:54:35.257498

Notes
-----
``jd_input_method`` is a native Postgres ENUM. As in the Phase 0 migration, its
lifecycle is managed explicitly (``ENUM.create(checkfirst=True)`` in upgrade,
``ENUM.drop(checkfirst=True)`` in downgrade) with ``create_type=False`` on the
column, because Alembic autogenerate creates the type implicitly but never
emits ``DROP TYPE`` — without the explicit drop a downgrade/upgrade round-trip
fails with "type jd_input_method already exists".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e46018d1bb97'
down_revision: Union[str, Sequence[str], None] = 'df02835dc658'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


jd_input_method_enum = postgresql.ENUM(
    'TEXT_PASTE', 'FILE_UPLOAD', name='jd_input_method', create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    jd_input_method_enum.create(bind, checkfirst=True)

    op.create_table(
        'jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('department', sa.String(length=255), nullable=True),
        sa.Column(
            'status', sa.String(length=50),
            server_default=sa.text("'DRAFT'"), nullable=False,
        ),
        sa.Column('jd_source_text', sa.Text(), nullable=False),
        sa.Column('jd_input_method', jd_input_method_enum, nullable=False),
        sa.Column('jd_original_filename', sa.String(length=255), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_jobs_created_by'), 'jobs', ['created_by'], unique=False
    )

    op.create_table(
        'job_requirements',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('requirement_type', sa.String(length=50), nullable=False),
        sa.Column('category', sa.String(length=100), nullable=True),
        sa.Column('requirement_text', sa.Text(), nullable=False),
        sa.Column(
            'source_version', sa.Integer(),
            server_default=sa.text('1'), nullable=False,
        ),
        sa.Column(
            'is_current', sa.Boolean(),
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
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_job_requirements_job_id'),
        'job_requirements', ['job_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_job_requirements_job_id'), table_name='job_requirements'
    )
    op.drop_table('job_requirements')
    op.drop_index(op.f('ix_jobs_created_by'), table_name='jobs')
    op.drop_table('jobs')

    # Autogenerate omits this; without it the type is orphaned.
    jd_input_method_enum.drop(op.get_bind(), checkfirst=True)
