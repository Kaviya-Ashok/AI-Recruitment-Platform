"""create post_interview_analyses

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-09-07 06:20:00.000000

Notes
-----
Phase 4 Step 9: the AI's consolidated post-interview analysis (CLAUDE.md §9).

NON-DESTRUCTIVE REGENERATION — A DELIBERATE DIVERGENCE
------------------------------------------------------
Every other regenerable AI artefact in this schema is replace-in-place:
``prequalification_results``, ``resume_extractions``, ``screening_evaluations``,
``interview_guides`` and ``candidate_rankings`` all drop the old row on a forced
re-run. This table does NOT. A post-interview analysis is the last AI artefact a
human reads before a hiring decision, and it is produced from irreplaceable
human testimony, so a regeneration inserts a new row and marks the previous one
``SUPERSEDED`` (with ``superseded_at``). Nothing is ever deleted.

Hence the explicit ``status`` column: which analysis is current is a recorded
fact, not something re-derived from ``created_at`` ordering. There is
deliberately **no** ``UNIQUE(application_id)`` — multiple historical rows per
application are the normal, expected state. The "exactly one CURRENT row per
application" invariant is enforced in ``post_interview_service`` inside the same
transaction as the insert, rather than by a partial unique index, which would
add migration and concurrency complexity this MVP does not need.

``rubric_version_id`` is frozen at creation, resolved along
``interview_feedback.interview_guide_id -> interview_guides.rubric_version_id``
(``interview_feedback`` itself carries no rubric version, and this step does not
modify it). Same freeze-never-re-derive rule as every other downstream artefact.

All six FKs are ``ON DELETE RESTRICT`` — the standard "independent
business/audit record, never a silent cascade" convention.

No native Postgres enum: ``status`` / ``confidence`` / ``ai_recommendation`` /
``human_recommendation_snapshot`` are validated strings, checked at the service
layer, matching every status vocabulary in this codebase.

There is deliberately no disagreement column and no interview-transcript column
— both are out of scope for this step.

``downgrade`` drops the index then the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'post_interview_analyses',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('interview_feedback_id', sa.UUID(), nullable=False),
        sa.Column('interview_guide_id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column('requested_by_user_id', sa.UUID(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column(
            'strengths', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            'gaps', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            'unknowns', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column('evidence_consistency_notes', sa.Text(), nullable=False),
        sa.Column('confidence', sa.String(length=10), nullable=False),
        sa.Column('ai_recommendation', sa.String(length=20), nullable=False),
        sa.Column(
            'human_recommendation_snapshot', sa.String(length=20), nullable=False,
        ),
        sa.Column(
            'analyzed_only_latest_feedback', sa.Boolean(), nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column('ai_model', sa.String(length=100), nullable=False),
        sa.Column(
            'status', sa.String(length=20), nullable=False,
            server_default=sa.text("'CURRENT'"),
        ),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['interview_feedback_id'], ['interview_feedback.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['interview_guide_id'], ['interview_guides.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['requested_by_user_id'], ['users.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_post_interview_analyses_application_id'),
        'post_interview_analyses', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_post_interview_analyses_interview_feedback_id'),
        'post_interview_analyses', ['interview_feedback_id'], unique=False,
    )
    op.create_index(
        op.f('ix_post_interview_analyses_rubric_version_id'),
        'post_interview_analyses', ['rubric_version_id'], unique=False,
    )
    op.create_index(
        op.f('ix_post_interview_analyses_requested_by_user_id'),
        'post_interview_analyses', ['requested_by_user_id'], unique=False,
    )
    # Serves "the CURRENT analysis for this application" and the history scan.
    op.create_index(
        'ix_post_interview_analyses_application_status',
        'post_interview_analyses', ['application_id', 'status'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_post_interview_analyses_application_status',
        table_name='post_interview_analyses',
    )
    op.drop_index(
        op.f('ix_post_interview_analyses_requested_by_user_id'),
        table_name='post_interview_analyses',
    )
    op.drop_index(
        op.f('ix_post_interview_analyses_rubric_version_id'),
        table_name='post_interview_analyses',
    )
    op.drop_index(
        op.f('ix_post_interview_analyses_interview_feedback_id'),
        table_name='post_interview_analyses',
    )
    op.drop_index(
        op.f('ix_post_interview_analyses_application_id'),
        table_name='post_interview_analyses',
    )
    op.drop_table('post_interview_analyses')
