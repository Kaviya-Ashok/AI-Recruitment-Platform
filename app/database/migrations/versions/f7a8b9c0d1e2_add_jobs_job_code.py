"""add jobs.job_code

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-05 12:00:00.000000

Notes
-----
A short, human-readable, globally unique job identifier — ``V_001``, ``V_002``,
... ``V_999``, ``V_1000`` — used first to name Drive folders
(``"V_001 - Junior Data Engineer"``) instead of the job's UUID.

CONCURRENCY-SAFE BY CONSTRUCTION: a Postgres SEQUENCE
-----------------------------------------------------
Codes come from ``job_code_seq`` via ``nextval``, which is atomic across
connections, so two jobs created at the same instant can never receive the same
number (the UNIQUE constraint below is the backstop, not the mechanism).
``MAX(code)+1`` was rejected: it needs a table lock to be safe.

A sequence is non-transactional, so a rolled-back insert burns a number. Gaps
are normal and harmless; uniqueness is what matters.

WHY A COLUMN DEFAULT (``next_job_code()``) AND NOT PYTHON-SIDE ASSIGNMENT
-------------------------------------------------------------------------
With a DEFAULT, *every* insert path gets a code atomically in the same
statement — ``create_job``, and any code or test that constructs a ``Job``
directly. A Python-only assignment would leave those paths violating NOT NULL.

The default is a tiny SQL function rather than an inline expression because the
formatting needs the number twice (``lpad`` truncates anything longer than its
width, so 4+ digits must widen the pad), and a DEFAULT cannot call ``nextval``
twice without skipping values, nor contain a sub-select.

FORMAT: ``'V_'`` + the number zero-padded to AT LEAST 3 digits, widening
(never truncating) once it overflows: 1 -> V_001, 999 -> V_999, 1000 -> V_1000.

BACKFILL: existing jobs are numbered 1..N in ``(created_at, id)`` order — the
``id`` tiebreak makes the result deterministic for equal timestamps — and the
sequence is advanced to N so the next new job gets N+1.

Order of operations (the column cannot be NOT NULL until it is populated):
sequence -> nullable column -> backfill -> advance sequence -> function ->
NOT NULL + DEFAULT -> UNIQUE.

``downgrade`` reverses it exactly: constraint, default, column, function,
sequence — leaving nothing behind, so a downgrade-to-base then re-upgrade works.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SEQUENCE job_code_seq START WITH 1 MINVALUE 1")

    op.add_column(
        'jobs', sa.Column('job_code', sa.String(length=20), nullable=True)
    )

    op.execute(
        """
        UPDATE jobs
        SET job_code = 'V_' || lpad(
            CAST(s.n AS text), greatest(3, length(CAST(s.n AS text))), '0'
        )
        FROM (
            SELECT id, row_number() OVER (ORDER BY created_at, id) AS n
            FROM jobs
        ) AS s
        WHERE jobs.id = s.id
        """
    )

    # Next new job must get N+1. setval(seq, 0) is out of range, so only
    # advance when there is something to advance past.
    op.execute(
        """
        SELECT CASE WHEN count(*) > 0
                    THEN setval('job_code_seq', count(*), true)
               END
        FROM jobs
        """
    )

    op.execute(
        """
        CREATE FUNCTION next_job_code() RETURNS text
        LANGUAGE sql VOLATILE AS $$
            SELECT 'V_' || lpad(
                CAST(n AS text), greatest(3, length(CAST(n AS text))), '0'
            )
            FROM (SELECT nextval('job_code_seq') AS n) AS s
        $$
        """
    )

    op.alter_column('jobs', 'job_code', nullable=False)
    op.alter_column(
        'jobs', 'job_code', server_default=sa.text('next_job_code()')
    )
    op.create_unique_constraint('uq_jobs_job_code', 'jobs', ['job_code'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_jobs_job_code', 'jobs', type_='unique')
    op.alter_column('jobs', 'job_code', server_default=None)
    op.drop_column('jobs', 'job_code')
    op.execute("DROP FUNCTION next_job_code()")
    op.execute("DROP SEQUENCE job_code_seq")
