"""create_application_links

Revision ID: 3f70f447c1a6
Revises: d0bf56dc9ba8
Create Date: 2026-09-01 22:51:18.297987

Notes
-----
``status`` is a validated ``String`` (constants class ``ApplicationLinkStatus``),
not a native Postgres ENUM — same growing-vocabulary rule as job/rubric status.
The ``ix_application_links_token`` index is UNIQUE: a DB-level backstop against
duplicate tokens on top of the service's ``secrets``-based generation.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '3f70f447c1a6'
down_revision: Union[str, Sequence[str], None] = 'd0bf56dc9ba8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'application_links',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('token', sa.String(length=128), nullable=False),
        sa.Column('sequence_number', sa.Integer(), nullable=False),
        sa.Column(
            'status', sa.String(length=50),
            server_default=sa.text("'ACTIVE'"), nullable=False,
        ),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('revoked_by', sa.UUID(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['revoked_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_application_links_created_by'), 'application_links',
        ['created_by'], unique=False,
    )
    op.create_index(
        op.f('ix_application_links_job_id'), 'application_links',
        ['job_id'], unique=False,
    )
    op.create_index(
        op.f('ix_application_links_token'), 'application_links',
        ['token'], unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_application_links_token'), table_name='application_links'
    )
    op.drop_index(
        op.f('ix_application_links_job_id'), table_name='application_links'
    )
    op.drop_index(
        op.f('ix_application_links_created_by'), table_name='application_links'
    )
    op.drop_table('application_links')
