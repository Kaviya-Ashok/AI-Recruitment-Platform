"""Database connection layer for the AI Recruitment Platform.

This module is the single place where the SQLAlchemy engine, session factory and
declarative ``Base`` are constructed. All persistence code (repositories and
services) must import :data:`SessionLocal`, :data:`Base` or :func:`get_db` from
here rather than creating its own engine.

Configuration is read exclusively from environment variables (loaded from the
repo-root ``.env`` via python-dotenv). Nothing is hard-coded and there is no
default fallback for ``DATABASE_URL`` — a missing value is a hard error.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# --- Configuration -----------------------------------------------------------

# Load .env from the repository root (three levels up: app/database/database.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

DATABASE_URL: str | None = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL not found in environment — check that a .env file exists "
        f"at {_REPO_ROOT / '.env'} and defines DATABASE_URL="
        "postgresql://<user>:<password>@<host>:<port>/<database>"
    )

try:
    _URL: URL = make_url(DATABASE_URL)
except ArgumentError as exc:  # pragma: no cover - defensive
    raise RuntimeError(
        f"DATABASE_URL is malformed and could not be parsed: {exc}. "
        "Expected form: postgresql://<user>:<password>@<host>:<port>/<database>. "
        "If your password contains special characters such as '@', ':' or '/', "
        "they must be percent-encoded (e.g. '@' -> '%40')."
    ) from exc


def _safe_url() -> str:
    """Return the connection URL with the password masked, safe for logging."""
    return _URL.render_as_string(hide_password=True)


# --- Engine / Session / Base ------------------------------------------------

engine: Engine = create_engine(
    _URL,
    pool_pre_ping=True,  # transparently recover from dropped connections
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


class Base(DeclarativeBase):
    """Declarative base class for all ORM models.

    No models inherit from this yet — model definitions arrive in a later
    Phase 0 step.
    """


def get_db() -> Iterator[Session]:
    """Yield a database session and guarantee it is closed.

    Standard SQLAlchemy dependency-style generator, intended for use by service
    and repository code (not by Streamlit UI directly)::

        db_gen = get_db()
        db = next(db_gen)
        try:
            ...
        finally:
            db_gen.close()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short-lived transactional session for a single unit of work.

    Preferred entry point for the Streamlit UI, which re-runs the whole script
    on every interaction: open a session inside the handler, use it, commit (or
    roll back on error), close — all within that one rerun. A session is
    deliberately never held across reruns, where it would be shared mutable
    state that can go stale or leak connections.

        with session_scope() as db:
            ...
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --- Connectivity check ----------------------------------------------------


def check_connection() -> bool:
    """Execute ``SELECT 1`` against the configured database.

    Returns ``True`` on success. Raises :class:`RuntimeError` with an actionable,
    secret-free message on failure.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - re-raised with context below
        raise RuntimeError(
            "Failed to connect to the database using "
            f"{_safe_url()!r}.\n"
            f"Underlying error: {type(exc).__name__}: {exc}\n"
            "Check that PostgreSQL is running, the database exists, and the "
            "credentials in .env are correct (percent-encode special characters "
            "in the password, e.g. '@' -> '%40')."
        ) from exc


if __name__ == "__main__":
    print(f"Connecting to {_safe_url()} ...")
    check_connection()
    print("OK - database connection succeeded (SELECT 1).")
