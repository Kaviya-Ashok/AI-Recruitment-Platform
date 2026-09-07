"""add applications.resume_retry_token

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-04 15:05:00.000000

Notes
-----
Phase D Fix 3: a durable, per-application credential for resuming a FAILED
résumé upload (CLAUDE.md §27 — a résumé/Drive failure must "allow retry").

Before this, the only route back to an application whose résumé upload failed
was ``st.session_state``, which a browser refresh clears. That stranded the
candidate with an application row carrying no document and no way to attach
one. None of the three identifiers already in play could serve as the recovery
credential: ``application_links.token`` is shared by every candidate for the
job, ``applications.id`` is a non-secret surrogate PK (CLAUDE.md §2A item 5),
and ``screening_sessions.access_token`` does not exist at that point — the
automatic pipeline that mints it never starts when the upload fails.

``resume_retry_token`` — ``String(128)``, **NULLABLE**, UNIQUE, indexed. Same
generation method and column width as ``application_links.token`` and
``screening_sessions.access_token`` (``secrets.token_urlsafe(32)`` -> 43 chars,
256 bits), and the same UNIQUE + index shape. It differs from both only in
being nullable: it is minted **lazily**, on the first ``RESUME_UPLOAD_FAILED``
outcome and never on a successful path, so almost every row keeps it NULL. In
Postgres a UNIQUE constraint permits unlimited NULLs, so this costs nothing on
rows that never failed.

Column only — **no** data migration, no backfill, no ``ApplicationStatus``
change, no new audit event, no other table touched. Existing rows get NULL,
which is exactly right: an application that never failed has no retry to
resume.

``downgrade`` drops the index then the column (this repo's migration tests
exercise a full upgrade -> downgrade -> upgrade round-trip, so the downgrade is
real, not decorative).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'applications',
        sa.Column('resume_retry_token', sa.String(length=128), nullable=True),
    )
    op.create_index(
        op.f('ix_applications_resume_retry_token'),
        'applications',
        ['resume_retry_token'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_applications_resume_retry_token'),
        table_name='applications',
    )
    op.drop_column('applications', 'resume_retry_token')
