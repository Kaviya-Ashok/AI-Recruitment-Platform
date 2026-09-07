"""create interview_feedback and interview_feedback_ratings

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-04 11:10:00.000000

Notes
-----
Phase 4 Step 8: human interview feedback capture (CLAUDE.md §7).

Both tables are created together (same pattern as
interview_guides/interview_questions in Step 7).

``interview_feedback`` is **append-only per round**:
``UNIQUE(application_id, interview_round)``, with no ``is_current`` column and
no ``updated_at``. Unlike ``screening_evaluations`` / ``interview_guides`` /
``candidate_rankings`` — which are replace-in-place because an AI derivation can
be regenerated — a human's interview write-up is irreplaceable testimony
(CLAUDE.md §7: "The original human feedback must be preserved"). Round 2
accompanies round 1; it never overwrites it. "Latest" is derived at read time
from ``created_at DESC, id DESC``, never stored.

``interview_round`` is submitted by the interviewer and stored verbatim. It is
never inferred from ``created_at`` or insertion order — those legitimately
diverge when a round is written up out of sequence.

``recommendation`` is a validated string checked in application code against
``ScreeningRecommendation.ALL`` (PROCEED / HOLD / REJECT). No new Postgres enum
is introduced; a human may recommend REJECT (unlike the automated path, which
is restricted to PROCEED / HOLD).

``interview_feedback_ratings.competency_label`` is free text with **no** rubric
linkage and no controlled taxonomy — this codebase has no competency entity, and
mapping an interviewer's judgment onto ``rubric_criteria`` would make it read as
a rubric verdict.

FK delete behaviour: the three ``interview_feedback`` FKs are ``RESTRICT``
(independent business/audit record, matching ``candidate_shortlist_entries`` and
``interview_guides``); ``interview_feedback_ratings.interview_feedback_id`` is
the one ``CASCADE``, because a rating is a child of its parent write-up and has
no meaning without it (same child-of-aggregate rule as ``rubric_criteria``).

FIRST CHECK CONSTRAINTS IN THIS SCHEMA
--------------------------------------
The three CHECKs below are the first DB-level CHECK constraints in this
codebase; every prior bound (screening round 1|2, score ranges) is enforced in
Python only. They are a backstop, not a replacement — the service validates all
three and raises a clear business error, so a caller never sees a raw
``IntegrityError``. They exist so a future path that bypasses the service cannot
store a 0-star rating, a round 0, or a blank competency label.

``downgrade`` drops both tables (ratings first).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'interview_feedback',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('interview_guide_id', sa.UUID(), nullable=False),
        sa.Column('submitted_by_user_id', sa.UUID(), nullable=False),
        sa.Column('interview_round', sa.Integer(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('recommendation', sa.String(length=20), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['interview_guide_id'], ['interview_guides.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['submitted_by_user_id'], ['users.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'application_id', 'interview_round',
            name='uq_interview_feedback_application_round',
        ),
        sa.CheckConstraint(
            'interview_round >= 1',
            name='ck_interview_feedback_round_positive',
        ),
    )
    op.create_index(
        op.f('ix_interview_feedback_application_id'),
        'interview_feedback', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_interview_feedback_interview_guide_id'),
        'interview_feedback', ['interview_guide_id'], unique=False,
    )
    op.create_index(
        op.f('ix_interview_feedback_submitted_by_user_id'),
        'interview_feedback', ['submitted_by_user_id'], unique=False,
    )
    # Serves the "history for this application, newest first" read used by both
    # accessors and the UI.
    op.create_index(
        'ix_interview_feedback_application_created',
        'interview_feedback',
        ['application_id', sa.text('created_at DESC')],
        unique=False,
    )

    op.create_table(
        'interview_feedback_ratings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('interview_feedback_id', sa.UUID(), nullable=False),
        sa.Column('competency_label', sa.String(length=255), nullable=False),
        sa.Column('rating', sa.Integer(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ['interview_feedback_id'], ['interview_feedback.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            'rating >= 1 AND rating <= 5',
            name='ck_interview_feedback_ratings_rating_range',
        ),
        sa.CheckConstraint(
            'length(btrim(competency_label)) > 0',
            name='ck_interview_feedback_ratings_label_not_blank',
        ),
    )
    op.create_index(
        op.f('ix_interview_feedback_ratings_interview_feedback_id'),
        'interview_feedback_ratings', ['interview_feedback_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_interview_feedback_ratings_interview_feedback_id'),
        table_name='interview_feedback_ratings',
    )
    op.drop_table('interview_feedback_ratings')
    op.drop_index(
        'ix_interview_feedback_application_created',
        table_name='interview_feedback',
    )
    op.drop_index(
        op.f('ix_interview_feedback_submitted_by_user_id'),
        table_name='interview_feedback',
    )
    op.drop_index(
        op.f('ix_interview_feedback_interview_guide_id'),
        table_name='interview_feedback',
    )
    op.drop_index(
        op.f('ix_interview_feedback_application_id'),
        table_name='interview_feedback',
    )
    op.drop_table('interview_feedback')
