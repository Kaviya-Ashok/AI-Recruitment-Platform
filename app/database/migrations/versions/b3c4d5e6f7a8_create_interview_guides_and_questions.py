"""create interview_guides and interview_questions

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-03 21:40:00.000000

Notes
-----
Phase 4 Step 7: the personalized human-interview guide (CLAUDE.md §6).

Both tables are created together (same pattern as
screening_questions/screening_answers in Step 3).

``interview_guides``: one row per application (``UNIQUE(application_id)``),
replaced in place on regeneration — history lives in ``INTERVIEW_GUIDE_GENERATED``
audit events, not accumulated rows. ``rubric_version_id`` is a FROZEN COPY of the
shortlist entry's captured version at generation time; it is never re-derived
from the job's current approved rubric, and it stays valid even when that
version is later ``SUPERSEDED`` (SUPERSEDED versions + criteria are never
deleted).

``interview_questions``: children of one guide. ``category`` is a validated
string (five values: REQUIREMENTS / EXPERIENCE / BEHAVIORAL / RESUME_VALIDATION /
PROBING — no "TECHNICAL"). ``rubric_criterion_id`` is nullable (behavioral /
probing questions may map to no single criterion, like
``screening_questions.rubric_criterion_id``).

All FKs are ``ON DELETE RESTRICT``.

``downgrade`` drops both tables (questions first).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'interview_guides',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('job_id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('shortlist_entry_id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column('ai_model', sa.String(length=100), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['shortlist_entry_id'], ['candidate_shortlist_entries.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'application_id', name='uq_interview_guides_application',
        ),
    )
    op.create_index(
        op.f('ix_interview_guides_job_id'),
        'interview_guides', ['job_id'], unique=False,
    )
    op.create_index(
        op.f('ix_interview_guides_application_id'),
        'interview_guides', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_interview_guides_rubric_version_id'),
        'interview_guides', ['rubric_version_id'], unique=False,
    )

    op.create_table(
        'interview_questions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('interview_guide_id', sa.UUID(), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('rubric_criterion_id', sa.UUID(), nullable=True),
        sa.Column('sequence_index', sa.Integer(), nullable=False),
        sa.Column('question_text', sa.Text(), nullable=False),
        sa.Column('evaluates', sa.Text(), nullable=False),
        sa.Column('generated_reason', sa.Text(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['interview_guide_id'], ['interview_guides.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rubric_criterion_id'], ['rubric_criteria.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_interview_questions_interview_guide_id'),
        'interview_questions', ['interview_guide_id'], unique=False,
    )
    op.create_index(
        op.f('ix_interview_questions_rubric_criterion_id'),
        'interview_questions', ['rubric_criterion_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_interview_questions_rubric_criterion_id'),
        table_name='interview_questions',
    )
    op.drop_index(
        op.f('ix_interview_questions_interview_guide_id'),
        table_name='interview_questions',
    )
    op.drop_table('interview_questions')
    op.drop_index(
        op.f('ix_interview_guides_rubric_version_id'),
        table_name='interview_guides',
    )
    op.drop_index(
        op.f('ix_interview_guides_application_id'),
        table_name='interview_guides',
    )
    op.drop_index(
        op.f('ix_interview_guides_job_id'),
        table_name='interview_guides',
    )
    op.drop_table('interview_guides')
