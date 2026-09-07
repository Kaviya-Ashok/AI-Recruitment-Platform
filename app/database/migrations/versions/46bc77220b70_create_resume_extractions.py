"""create resume_extractions

Revision ID: 46bc77220b70
Revises: 0b273d2a41f8
Create Date: 2026-09-02 06:19:49.747347

Notes
-----
The ``resume_extractions`` table — one row per successful ``resume_parsing`` AI
run against one ``documents`` row (CLAUDE.md §§3, 18, 23). It stores the
schema-validated JSON *evidence inventory* (``extracted_data``) plus provenance
(``ai_model``).

``document_id`` FK is ``ON DELETE RESTRICT`` — same non-cascading convention as
``documents`` (``0b273d2a41f8``) and ``applications`` (``4f6f59712443``): an AI
extraction is a business/audit record and must not vanish as a side effect of
deleting its document.

No status / version column: re-extraction is not versioned in this phase (see
the model docstring). No native PG ENUM is introduced. ``downgrade`` drops the
table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '46bc77220b70'
down_revision: Union[str, Sequence[str], None] = '0b273d2a41f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'resume_extractions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column(
            'extracted_data',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column('ai_model', sa.String(length=100), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['document_id'], ['documents.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_resume_extractions_document_id'), 'resume_extractions',
        ['document_id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_resume_extractions_document_id'),
        table_name='resume_extractions',
    )
    op.drop_table('resume_extractions')
