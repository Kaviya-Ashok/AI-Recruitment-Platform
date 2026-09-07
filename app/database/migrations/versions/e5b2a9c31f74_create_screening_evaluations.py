"""create screening_evaluations

Revision ID: e5b2a9c31f74
Revises: d4c81f6a2e50
Create Date: 2026-09-03 17:45:12.008411

Notes
-----
Phase 4 Step 4: the per-candidate initial scorecard (CLAUDE.md §§4, 20).

One row per screening session (``UNIQUE screening_session_id``), write-once /
replace-on-force like ``prequalification_results``. Both FKs are
``ON DELETE RESTRICT`` — the same non-cascading, "this is a business/audit
record" convention as every other AI-output table:

* ``screening_session_id`` -> ``screening_sessions.id``
* ``rubric_version_id``    -> ``rubric_versions.id`` (which rubric version it was
  scored against — §23 traceability)

``results`` is JSONB (per-criterion reconciled list, same shape as
``prequalification_results.results``). ``strengths`` / ``gaps`` / ``unknowns``
are JSONB ``list[str]`` with a ``'[]'::jsonb`` server default. The bucket
score/coverage columns are all NULLABLE (NULL together when a bucket has no
PASS/FAIL criterion). ``overall_confidence`` / ``ai_recommendation`` are plain
validated strings (no PG enum) — same growing-vocabulary rule as elsewhere.

``downgrade`` drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5b2a9c31f74'
down_revision: Union[str, Sequence[str], None] = 'd4c81f6a2e50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'screening_evaluations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('screening_session_id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column(
            'results', postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column('requirements_score', sa.Integer(), nullable=True),
        sa.Column('requirements_coverage', sa.Float(), nullable=True),
        sa.Column('experience_score', sa.Integer(), nullable=True),
        sa.Column('experience_coverage', sa.Float(), nullable=True),
        sa.Column('behavioral_score', sa.Integer(), nullable=True),
        sa.Column('behavioral_coverage', sa.Float(), nullable=True),
        sa.Column(
            'strengths', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column(
            'gaps', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column(
            'unknowns', postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"), nullable=False,
        ),
        sa.Column('overall_confidence', sa.String(length=10), nullable=False),
        sa.Column('ai_recommendation', sa.String(length=20), nullable=False),
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
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'screening_session_id', name='uq_screening_evaluations_session',
        ),
    )
    op.create_index(
        op.f('ix_screening_evaluations_screening_session_id'),
        'screening_evaluations', ['screening_session_id'], unique=False,
    )
    op.create_index(
        op.f('ix_screening_evaluations_rubric_version_id'),
        'screening_evaluations', ['rubric_version_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_screening_evaluations_rubric_version_id'),
        table_name='screening_evaluations',
    )
    op.drop_index(
        op.f('ix_screening_evaluations_screening_session_id'),
        table_name='screening_evaluations',
    )
    op.drop_table('screening_evaluations')
