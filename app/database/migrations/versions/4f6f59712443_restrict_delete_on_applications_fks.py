"""restrict_delete_on_applications_fks

Revision ID: 4f6f59712443
Revises: e7617aa2220b
Create Date: 2026-09-02 01:40:00.000000

Why
---
``e7617aa2220b`` created all three FKs on ``applications`` with
``ON DELETE CASCADE`` (candidate_id, job_id, application_link_id). That means a
hard-delete of a ``candidate`` / ``job`` / ``application_link`` row would
silently delete every dependent ``applications`` row — destroying a
business-critical record (potentially a candidate's only trace, and the anchor
for all downstream assessments) with no audit trail and no human decision point.
That violates CLAUDE.md §§12, 20, 23.

This migration switches all three FKs to ``ON DELETE RESTRICT``: the database now
refuses to delete a parent row while an application references it. Removing a
candidate's data (CLAUDE.md §24) will be an explicit, audited orchestration that
deletes the applications first — never a cascade.

Postgres has no ``ALTER CONSTRAINT`` for changing ``ON DELETE`` behaviour, so
each FK is dropped and recreated. Constraint names are the Postgres defaults
assigned by the create-table in ``e7617aa2220b`` (``<table>_<column>_fkey``).

``downgrade`` restores the CASCADE variant so the chain round-trips cleanly,
even though CASCADE is the worse state.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '4f6f59712443'
down_revision: Union[str, Sequence[str], None] = 'e7617aa2220b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (constraint name, referenced table, local column, referenced column)
_FKS = [
    ("applications_candidate_id_fkey", "candidates", "candidate_id", "id"),
    ("applications_job_id_fkey", "jobs", "job_id", "id"),
    (
        "applications_application_link_id_fkey",
        "application_links",
        "application_link_id",
        "id",
    ),
]


def _recreate_all(ondelete: str) -> None:
    for name, ref_table, local_col, ref_col in _FKS:
        op.drop_constraint(name, "applications", type_="foreignkey")
        op.create_foreign_key(
            name,
            "applications",
            ref_table,
            [local_col],
            [ref_col],
            ondelete=ondelete,
        )


def upgrade() -> None:
    """Upgrade schema: CASCADE -> RESTRICT on all three applications FKs."""
    _recreate_all("RESTRICT")


def downgrade() -> None:
    """Downgrade schema: RESTRICT -> CASCADE (restores the prior, worse state)."""
    _recreate_all("CASCADE")
