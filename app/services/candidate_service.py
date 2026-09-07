"""Candidate service — resolve or create the person behind an application.

Phase 2 scope: candidate identity only. No resume, no Drive, no AI here.

Transaction model
-----------------
These functions are **building blocks**, not top-level business actions. They
take the caller's ``Session`` and do NOT ``commit`` — the caller
(:func:`app.services.application_service.create_application`) owns the single
transaction that must contain the candidate row, the application row, and the
``CANDIDATE_APPLIED`` audit row all-or-nothing (CLAUDE.md §20; project rule:
composed helpers never open their own transaction).

Not committing here also means a candidate row created for an application that
then fails to insert is rolled back with it — no orphan person record.

These functions assume **no authenticated session**: they are reached from the
public, unauthenticated application flow. Nothing here reads ``st.session_state``
or a current user.

PRIVACY
-------
``email`` and ``phone`` are personal data (CLAUDE.md §§23, 24). This module never
writes them to logs or to audit metadata, and never puts them in exceptions that
reach a UI. A candidate is referenced by ``id`` in every such path.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models.candidate import Candidate

logger = logging.getLogger(__name__)


class CandidateValidationError(Exception):
    """Raised for invalid candidate input (missing email or name)."""


def normalise_email(email: str | None) -> str:
    """Canonical form for the dedup key: trimmed + lower-cased.

    Only case/whitespace is normalised — nothing else about the address is
    rewritten. Matches ``auth_service._normalise_email``.
    """
    return (email or "").strip().lower()


def get_candidate_by_email(db: Session, email: str) -> Candidate | None:
    """Return the candidate with this (normalised) email, or ``None``."""
    email_norm = normalise_email(email)
    if not email_norm:
        return None
    return db.execute(
        select(Candidate).where(Candidate.email == email_norm)
    ).scalar_one_or_none()


def get_or_create_candidate(
    db: Session,
    *,
    email: str,
    full_name: str,
    phone: str | None,
) -> Candidate:
    """Return the existing candidate for ``email`` or create a new one.

    Dedup is by normalised email. On a repeat email the existing row is returned
    unchanged — ``full_name`` / ``phone`` from this call are **not** merged in
    (an application must not silently rewrite a person's stored identity; a
    dedicated update path can do that later if needed).

    Does NOT commit — the caller commits. The DB unique constraint on
    ``candidates.email`` is the backstop against a lost race between two
    near-simultaneous first-time applications with the same email; if that race
    is lost the losing call re-reads and returns the winner's row.

    Raises
    ------
    CandidateValidationError
        ``email`` or ``full_name`` is empty/whitespace.
    """
    email_norm = normalise_email(email)
    if not email_norm:
        raise CandidateValidationError("Email is required.")
    if not full_name or not full_name.strip():
        raise CandidateValidationError("Full name is required.")

    existing = get_candidate_by_email(db, email_norm)
    if existing is not None:
        return existing

    phone_clean = phone.strip() if phone and phone.strip() else None
    candidate = Candidate(
        email=email_norm,
        full_name=full_name.strip(),
        phone=phone_clean,
    )
    db.add(candidate)
    try:
        db.flush()  # assign candidate.id and trigger the unique constraint
    except IntegrityError:
        # Lost the race: another transaction inserted this email first. Recover
        # the winning row rather than surfacing a raw IntegrityError.
        db.rollback()
        winner = get_candidate_by_email(db, email_norm)
        if winner is None:  # pragma: no cover - the constraint says it exists
            raise
        logger.info("candidate get_or_create: race lost, reusing existing row")
        return winner

    logger.info("candidate created id=%s", candidate.id)
    return candidate
