"""create screening_questions and screening_answers

Revision ID: d4c81f6a2e50
Revises: b3d9f0a12c47
Create Date: 2026-09-03 11:40:18.552104

Notes
-----
Phase 4 Step 3: batch-generated screening questions (4-8 for round 1, 0-3
follow-ups for round 2) and one candidate answer per question.

* ``screening_questions.screening_session_id`` -> ``screening_sessions.id``
  ``ON DELETE RESTRICT`` (the non-cascading convention shared by every AI-output
  table). Unique ``(screening_session_id, round, sequence_index)`` — a stable
  per-round ordering key.
* ``screening_questions.rubric_criterion_id`` -> ``rubric_criteria.id``
  ``ON DELETE RESTRICT``, NULLABLE (a behavioural / broad question maps to no
  single criterion). NB the referenced ``rubric_criteria`` row is a criterion of
  an APPROVED rubric version, which this codebase treats as immutable and never
  deletes — so this RESTRICT never actually blocks anything in practice; it is
  the same protective posture as ``prequalification_results.rubric_version_id``.
* ``screening_answers.screening_question_id`` -> ``screening_questions.id``
  ``ON DELETE RESTRICT``, with a UNIQUE INDEX
  (``ix_screening_answers_screening_question_id``) — one answer per question,
  no threads. Same "unique index, not a separate named constraint" shape as
  ``screening_sessions.access_token``.

``round`` is a plain ``SmallInteger`` (1 or 2, validated in code — a fixed
two-round design, not a growing vocabulary). ``category`` is a validated
``String(50)`` (JD / CV / BEHAVIORAL / GAP) — no new PG enum, so this migration
has no ``ENUM.create`` / ``ENUM.drop`` lifecycle.

``downgrade`` drops ``screening_answers`` before ``screening_questions``
(FK-safe order).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4c81f6a2e50'
down_revision: Union[str, Sequence[str], None] = 'b3d9f0a12c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'screening_questions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('screening_session_id', sa.UUID(), nullable=False),
        sa.Column('round', sa.SmallInteger(), nullable=False),
        sa.Column('sequence_index', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=50), nullable=False),
        sa.Column('rubric_criterion_id', sa.UUID(), nullable=True),
        sa.Column('question_text', sa.Text(), nullable=False),
        sa.Column('generated_reason', sa.Text(), nullable=False),
        sa.Column('ai_model', sa.String(length=100), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['screening_session_id'], ['screening_sessions.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rubric_criterion_id'], ['rubric_criteria.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'screening_session_id', 'round', 'sequence_index',
            name='uq_screening_questions_session_round_seq',
        ),
    )
    op.create_index(
        op.f('ix_screening_questions_screening_session_id'),
        'screening_questions', ['screening_session_id'], unique=False,
    )
    op.create_index(
        op.f('ix_screening_questions_rubric_criterion_id'),
        'screening_questions', ['rubric_criterion_id'], unique=False,
    )

    op.create_table(
        'screening_answers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('screening_question_id', sa.UUID(), nullable=False),
        sa.Column('answer_text', sa.Text(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['screening_question_id'], ['screening_questions.id'],
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # ``unique=True`` on the model column -> a UNIQUE INDEX (not a separate
    # named constraint), matching how ``screening_sessions.access_token`` is
    # declared. This index IS the "one answer per question" guarantee.
    op.create_index(
        op.f('ix_screening_answers_screening_question_id'),
        'screening_answers', ['screening_question_id'], unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_screening_answers_screening_question_id'),
        table_name='screening_answers',
    )
    op.drop_table('screening_answers')

    op.drop_index(
        op.f('ix_screening_questions_rubric_criterion_id'),
        table_name='screening_questions',
    )
    op.drop_index(
        op.f('ix_screening_questions_screening_session_id'),
        table_name='screening_questions',
    )
    op.drop_table('screening_questions')
