"""Alembic environment.

Connection and metadata are sourced from the application itself so there is a
single source of truth:

* ``app.database.database`` loads ``DATABASE_URL`` from the repo-root ``.env``
  (via python-dotenv) and builds the SQLAlchemy ``engine``. Alembic reuses that
  engine — the URL is never hard-coded here or in ``alembic.ini``.
* ``Base.metadata`` is the autogenerate target, so every model that inherits
  from ``app.database.database.Base`` is picked up automatically once models
  exist (there are none yet).
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# --- Make the ``app`` package importable ----------------------------------
# env.py lives at app/database/migrations/env.py; the repo root is 3 levels up.
# ``prepend_sys_path = .`` in alembic.ini already covers running from the repo
# root, but this makes the import robust regardless of the current directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.database.database import Base, engine  # noqa: E402

# Importing the models package registers every model on ``Base.metadata`` so
# that ``--autogenerate`` can see them.
import app.database.models  # noqa: E402,F401

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate target. Base has no models attached yet — that is intentional
# for this step.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI needed)."""
    url = engine.url.render_as_string(hide_password=False)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode, reusing the application's engine."""
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
