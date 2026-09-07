"""create candidate_shortlist_entries

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-03 20:30:00.000000

Notes
-----
Phase 4 Step 6: HR shortlisting (CLAUDE.md §§5, 6, 11, 12).

One row per ``(job_id, application_id)`` (``UNIQUE``), **updated in place** on
every shortlist / unshortlist action — never appended. The history of decisions
lives in the audit log (``CANDIDATE_SHORTLISTED`` / ``CANDIDATE_UNSHORTLISTED``),
not in accumulated rows here.

All four FKs are ``ON DELETE RESTRICT`` — the same "independent business/audit
record" convention as ``applications`` / ``candidate_rankings``.
``decided_by_user_id`` is RESTRICT + NOT NULL (every decision has a real HR
actor; accounts are disabled, not deleted).

``rubric_version_id`` records which ranking partition the decision was made
against, captured at decision time (the approved rubric can change later).
``reason`` is optional free text and is **never** propagated into audit-event
metadata.

``downgrade`` drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'candidate_shortlist_entries',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column('is_shortlisted', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('rank_position_at_decision', sa.Integer(), nullable=True),
        sa.Column('decided_by_user_id', sa.UUID(), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['decided_by_user_id'], ['users.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'job_id', 'application_id',
            name='uq_candidate_shortlist_entries_job_application',
        ),
    )
    op.create_index(
        op.f('ix_candidate_shortlist_entries_job_id'),
        'candidate_shortlist_entries', ['job_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_shortlist_entries_application_id'),
        'candidate_shortlist_entries', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_candidate_shortlist_entries_rubric_version_id'),
        'candidate_shortlist_entries', ['rubric_version_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_candidate_shortlist_entries_rubric_version_id'),
        table_name='candidate_shortlist_entries',
    )
    op.drop_index(
        op.f('ix_candidate_shortlist_entries_application_id'),
        table_name='candidate_shortlist_entries',
    )
    op.drop_index(
        op.f('ix_candidate_shortlist_entries_job_id'),
        table_name='candidate_shortlist_entries',
    )
    op.drop_table('candidate_shortlist_entries')
