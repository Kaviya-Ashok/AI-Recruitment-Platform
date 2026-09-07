"""create_documents

Revision ID: 0b273d2a41f8
Revises: 4f6f59712443
Create Date: 2026-09-02 02:05:00.000000

Notes
-----
Phase 2: the ``documents`` table — Postgres's record of files stored in the
document provider (Google Drive), per CLAUDE.md §17. The bytes live in Drive
(behind ``storage_service``); this row holds the provider file id + metadata +
the application relationship.

``application_id`` FK is ``ON DELETE RESTRICT`` (not CASCADE) — same
non-cascading convention established in ``4f6f59712443`` for ``applications``'
own FKs: a document must not silently vanish because its application row was
deleted.

``drive_file_id`` has a UNIQUE index (one DB row per stored file).

No native PG ENUM is introduced. ``downgrade`` drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0b273d2a41f8'
down_revision: Union[str, Sequence[str], None] = '4f6f59712443'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'documents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('application_id', sa.UUID(), nullable=False),
        sa.Column('drive_file_id', sa.String(length=255), nullable=False),
        sa.Column('drive_folder_id', sa.String(length=255), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('mime_type', sa.String(length=255), nullable=False),
        sa.Column('file_size_bytes', sa.Integer(), nullable=False),
        sa.Column(
            'uploaded_at', sa.DateTime(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['application_id'], ['applications.id'], ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_documents_application_id'), 'documents',
        ['application_id'], unique=False,
    )
    op.create_index(
        op.f('ix_documents_drive_file_id'), 'documents',
        ['drive_file_id'], unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_documents_drive_file_id'), table_name='documents')
    op.drop_index(op.f('ix_documents_application_id'), table_name='documents')
    op.drop_table('documents')
