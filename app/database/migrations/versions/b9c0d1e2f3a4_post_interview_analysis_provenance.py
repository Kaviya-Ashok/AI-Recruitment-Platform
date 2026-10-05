"""post-interview analysis provenance: all rounds + interview transcripts

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-10-06 12:00:00.000000

Notes
-----
Increment C amends Step 9. The analysis now reads EVERY interview-feedback
record of an application and the CURRENT transcript of every round that has one,
so it must record exactly which ones it used.

``post_interview_analysis_feedback`` and ``post_interview_analysis_transcripts``
are plain join tables (composite primary keys, no text). The first also carries
``recommendation_snapshot`` — the interviewer's recommendation for that round AT
GENERATION TIME, copied from the database for HR's display context. It is never
sent to the AI.

FKs are all ``ON DELETE RESTRICT``: the closest precedent is
``post_interview_analyses`` itself (every FK there is RESTRICT — an independent
business/audit record that must not vanish as a side effect of deleting a
parent), and these rows are provenance for that table.

``post_interview_analyses`` gains ``transcript_evidence_notes`` (AI prose about
what the transcripts added; ``''`` when none were used) and
``transcript_unreadable_rounds`` (round numbers whose current transcript had no
readable text and was therefore NOT sent).

BACKFILL: every pre-existing analysis read exactly one feedback record (its
``interview_feedback_id``), so each gets exactly one join row built from that
feedback row's round and the analysis's own ``human_recommendation_snapshot``.
Their ``analyzed_only_latest_feedback`` stays true, which is accurate for them.
They get no transcript rows (none were read).

``downgrade`` drops the two tables and the two columns. The per-round provenance
is lost on downgrade; the analyses themselves are untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b9c0d1e2f3a4'
down_revision: Union[str, Sequence[str], None] = 'a8b9c0d1e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'post_interview_analysis_feedback',
        sa.Column('analysis_id', sa.UUID(), nullable=False),
        sa.Column('interview_feedback_id', sa.UUID(), nullable=False),
        sa.Column('interview_round', sa.Integer(), nullable=False),
        sa.Column(
            'recommendation_snapshot', sa.String(length=20), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['analysis_id'], ['post_interview_analyses.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['interview_feedback_id'], ['interview_feedback.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('analysis_id', 'interview_feedback_id'),
    )
    op.create_index(
        op.f('ix_post_interview_analysis_feedback_interview_feedback_id'),
        'post_interview_analysis_feedback', ['interview_feedback_id'],
        unique=False,
    )

    op.create_table(
        'post_interview_analysis_transcripts',
        sa.Column('analysis_id', sa.UUID(), nullable=False),
        sa.Column('interview_transcript_id', sa.UUID(), nullable=False),
        sa.Column('interview_round', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['analysis_id'], ['post_interview_analyses.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['interview_transcript_id'], ['interview_transcripts.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('analysis_id', 'interview_transcript_id'),
    )
    op.create_index(
        op.f('ix_post_interview_analysis_transcripts_interview_transcript_id'),
        'post_interview_analysis_transcripts', ['interview_transcript_id'],
        unique=False,
    )

    op.add_column(
        'post_interview_analyses',
        sa.Column(
            'transcript_evidence_notes', sa.Text(), nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        'post_interview_analyses',
        sa.Column(
            'transcript_unreadable_rounds',
            postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Backfill: exactly one join row per existing analysis.
    op.execute(
        """
        INSERT INTO post_interview_analysis_feedback
            (analysis_id, interview_feedback_id, interview_round,
             recommendation_snapshot)
        SELECT a.id, a.interview_feedback_id, f.interview_round,
               a.human_recommendation_snapshot
        FROM post_interview_analyses a
        JOIN interview_feedback f ON f.id = a.interview_feedback_id
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('post_interview_analyses', 'transcript_unreadable_rounds')
    op.drop_column('post_interview_analyses', 'transcript_evidence_notes')
    op.drop_index(
        op.f('ix_post_interview_analysis_transcripts_interview_transcript_id'),
        table_name='post_interview_analysis_transcripts',
    )
    op.drop_table('post_interview_analysis_transcripts')
    op.drop_index(
        op.f('ix_post_interview_analysis_feedback_interview_feedback_id'),
        table_name='post_interview_analysis_feedback',
    )
    op.drop_table('post_interview_analysis_feedback')
