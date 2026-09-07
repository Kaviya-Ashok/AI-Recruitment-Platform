"""create prequalification_results

Revision ID: e3c212e580f7
Revises: 46bc77220b70
Create Date: 2026-09-02 14:35:11.183870

Notes
-----
The ``prequalification_results`` table — one row per run of the
``prequalification`` AI task for one application (CLAUDE.md §§4, 12, 20, 23, B).
It stores the full list of per-criterion PASS/FAIL/UNKNOWN results (AI judgment
+ Python-computed confidence) in ``results`` (JSONB), plus provenance
(``ai_model``).

All three FKs are ``ON DELETE RESTRICT`` — same non-cascading convention as
``resume_extractions`` (``46bc77220b70``) / ``documents`` / ``applications``:

* ``application_id``       -> ``applications.id``
* ``rubric_version_id``    -> ``rubric_versions.id`` (which rubric version this
  was judged against — rubrics can be regenerated; §23 traceability)
* ``resume_extraction_id`` -> ``resume_extractions.id`` (which evidence snapshot
  was used). Because this is RESTRICT, force-re-parsing a resume that already
  has a prequalification is blocked until the prequalification is removed —
  a known, deliberate interaction.

No status / version column: re-runs are not versioned in this phase (see the
model docstring). No native PG ENUM. ``downgrade`` drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e3c212e580f7'
down_revision: Union[str, Sequence[str], None] = '46bc77220b70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'prequalification_results',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('rubric_version_id', sa.UUID(), nullable=False),
        sa.Column('resume_extraction_id', sa.UUID(), nullable=False),
        sa.Column(
            'results',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column('ai_model', sa.String(length=100), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['resume_extraction_id'], ['resume_extractions.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rubric_version_id'], ['rubric_versions.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_prequalification_results_application_id'),
        'prequalification_results', ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_prequalification_results_resume_extraction_id'),
        'prequalification_results', ['resume_extraction_id'], unique=False,
    )
    op.create_index(
        op.f('ix_prequalification_results_rubric_version_id'),
        'prequalification_results', ['rubric_version_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_prequalification_results_rubric_version_id'),
        table_name='prequalification_results',
    )
    op.drop_index(
        op.f('ix_prequalification_results_resume_extraction_id'),
        table_name='prequalification_results',
    )
    op.drop_index(
        op.f('ix_prequalification_results_application_id'),
        table_name='prequalification_results',
    )
    op.drop_table('prequalification_results')
