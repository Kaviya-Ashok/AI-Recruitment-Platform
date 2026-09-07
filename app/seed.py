"""Seed the first internal user (one-off operational script).

The database ships empty and there is no UI yet to create the first account,
so this script bootstraps one HR/ADMIN user. It lives next to ``app/main.py``
as an operational entrypoint (run via ``python -m app.seed``) rather than in
the ``database`` or ``services`` package, to keep those layers free of CLI code.

Usage::

    python -m app.seed --email admin@company.com --full-name "Admin User" --role ADMIN

The password is taken from the ``SEED_ADMIN_PASSWORD`` environment variable if
set, otherwise prompted interactively (never echoed to the terminal). The
password is never printed back out.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from app.database.database import SessionLocal
from app.database.models.user import UserRole
from app.services.auth_service import EmailAlreadyExistsError, create_user


def _resolve_password() -> str:
    """Read the new user's password from env or an interactive prompt."""
    env_pw = os.environ.get("SEED_ADMIN_PASSWORD")
    if env_pw:
        return env_pw

    first = getpass.getpass("Password for new user: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        print("Passwords do not match.", file=sys.stderr)
        raise SystemExit(1)
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create the first internal user for the platform."
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", required=True, dest="full_name")
    parser.add_argument(
        "--role",
        default=UserRole.ADMIN.value,
        choices=[r.value for r in UserRole],
    )
    args = parser.parse_args(argv)

    password = _resolve_password()

    db = SessionLocal()
    try:
        user = create_user(
            db=db,
            email=args.email,
            plain_password=password,
            full_name=args.full_name,
            role=UserRole(args.role),
        )
    except (ValueError, EmailAlreadyExistsError) as exc:
        print(f"Could not create user: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(
        f"Created user {user.email} "
        f"(id={user.id}, role={user.role.value}, active={user.is_active})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
