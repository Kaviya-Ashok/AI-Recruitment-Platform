"""create interview_transcripts

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-10-06 09:00:00.000000

Notes
-----
Increment B: one interview transcript (PDF/DOCX, stored in Google Drive) per
interview round, linked to that round's immutable ``interview_feedback`` row.

A separate table, NOT ``documents``: ``documents`` has no type column and many
call sites treat any document on an application as the résumé.

Only metadata is stored; the transcript text never enters the database.
``file_name`` embeds the candidate's name and is therefore personal data — it is
kept on this row and out of every audit event.

Replacement is non-destructive: the old row becomes ``SUPERSEDED`` and a new
``CURRENT`` row is inserted. The PARTIAL UNIQUE INDEX
``uq_interview_transcripts_one_current_per_feedback`` makes the database itself
refuse a second CURRENT row for the same feedback.

``status`` is a validated string, not a native Postgres enum (repo convention),
so adding a status later needs no migration.

Both FKs are ``ON DELETE RESTRICT``. ``downgrade`` drops the indexes then the
table; the Drive files are not touched (this migration never could).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'interview_transcripts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('interview_feedback_id', sa.UUID(), nullable=False),
        sa.Column('uploaded_by_user_id', sa.UUID(), nullable=False),
        sa.Column('drive_file_id', sa.String(length=255), nullable=False),
        sa.Column('drive_folder_id', sa.String(length=255), nullable=False),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('mime_type', sa.String(length=255), nullable=False),
        sa.Column('file_size_bytes', sa.Integer(), nullable=False),
        sa.Column('text_extractable', sa.Boolean(), nullable=False),
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
            ['interview_feedback_id'], ['interview_feedback.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['uploaded_by_user_id'], ['users.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('drive_file_id'),
    )
    op.create_index(
        op.f('ix_interview_transcripts_interview_feedback_id'),
        'interview_transcripts', ['interview_feedback_id'], unique=False,
    )
    op.create_index(
        op.f('ix_interview_transcripts_uploaded_by_user_id'),
        'interview_transcripts', ['uploaded_by_user_id'], unique=False,
    )
    op.create_index(
        'uq_interview_transcripts_one_current_per_feedback',
        'interview_transcripts', ['interview_feedback_id'], unique=True,
        postgresql_where=sa.text("status = 'CURRENT'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'uq_interview_transcripts_one_current_per_feedback',
        table_name='interview_transcripts',
        postgresql_where=sa.text("status = 'CURRENT'"),
    )
    op.drop_index(
        op.f('ix_interview_transcripts_uploaded_by_user_id'),
        table_name='interview_transcripts',
    )
    op.drop_index(
        op.f('ix_interview_transcripts_interview_feedback_id'),
        table_name='interview_transcripts',
    )
    op.drop_table('interview_transcripts')
