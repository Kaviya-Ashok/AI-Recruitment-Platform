"""One-time Google Drive OAuth authorization - MANUAL, RUN ONCE.

NOT production code. NOT imported by the app. NOT part of the pytest suite.
Same category as scripts/smoke_test_drive_upload.py and
scripts/compare_rubric_models.py: side-effect-gated, human-run-only.

WHY
---
Personal Gmail accounts have no service-account storage quota, so uploading a
resume as a service account fails with a hard 403 storageQuotaExceeded. The fix
is to authorize as the *human* who owns the Drive folder; uploads then count
against that person's real quota. This script runs Google's "installed app"
OAuth flow to obtain a long-lived refresh token that
app/services/storage_service.py uses from then on to mint access tokens
automatically - no service account involved.

Run this EXACTLY ONCE. Run it again only if GOOGLE_OAUTH_REFRESH_TOKEN is later
revoked (from https://myaccount.google.com/permissions) or stops working. It is
not part of any normal development or deploy flow.

PREREQUISITES (already done in the Google Cloud console - do not recreate)
  * OAuth consent screen: External, publishing status "Testing", with
    kaviyaashokece@gmail.com added as a test user (this is also the account
    that owns the "Recruitment Platform Resumes" Drive folder).
  * An OAuth client of type "Desktop app"; its client-secret JSON downloaded to
    a path OUTSIDE this repo.

USAGE
-----
  Windows (PowerShell):
      $env:GOOGLE_OAUTH_CLIENT_SECRET_FILE = "C:\\path\\to\\client_secret.json"
      venv\\Scripts\\python.exe scripts\\authorize_drive_oauth.py
  POSIX:
      export GOOGLE_OAUTH_CLIENT_SECRET_FILE=/path/to/client_secret.json
      venv/bin/python scripts/authorize_drive_oauth.py

A browser window opens on localhost. Log in as the folder-owning Google account
and approve the Drive scope. The script then prints the refresh token. Copy it
into .env yourself - the script never writes it anywhere.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Same pattern as app/services/storage_service.py, but this file lives in
# scripts/ (one level below the repo root, not two), so parents[1].
_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env")

# Full drive scope is required (not drive.file): the Drive root folder was
# created in the Drive UI, not by this app, and this headless flow has no Google
# Picker step through which drive.file access to that folder could be granted.
# See app/services/storage_service.py for the full reasoning.
_SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> int:
    client_secret_file = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET_FILE")
    if not client_secret_file:
        sys.exit(
            "GOOGLE_OAUTH_CLIENT_SECRET_FILE is not set. Point it at the "
            "client-secret JSON you downloaded from the Google Cloud console, "
            "then re-run this script."
        )
    if not os.path.isfile(client_secret_file):
        sys.exit(
            f"GOOGLE_OAUTH_CLIENT_SECRET_FILE is not a readable file: "
            f"{client_secret_file!r}"
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit(
            "google-auth-oauthlib is not installed. Run "
            "`pip install -r requirements.txt` in the venv and try again."
        )

    # Consumes the console-generated client-secret JSON as-is.
    flow = InstalledAppFlow.from_client_secrets_file(
        client_secret_file, scopes=_SCOPES
    )

    # run_local_server: opens the default browser, runs a throwaway localhost
    # HTTP server to catch the redirect, completes automatically (no code
    # copy-paste). access_type=offline + prompt=consent guarantee a refresh
    # token is returned, even on a re-authorization.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message=(
            "\nOpening your browser to authorize Google Drive access.\n"
            "Log in as the Google account that OWNS the Drive folder and "
            "approve.\nIf the browser does not open, visit this URL:\n  {url}\n"
        ),
        success_message=(
            "Authorization complete. You can close this tab and return to the "
            "terminal."
        ),
    )

    if not creds.refresh_token:
        sys.exit(
            "The flow completed but no refresh token was returned. Revoke this "
            "app's access at https://myaccount.google.com/permissions and run "
            "this script again."
        )

    sep = "=" * 72
    print(f"\n{sep}")
    print("SUCCESS - copy this line into your .env file:\n")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("\nAlso set these in .env from the same client-secret JSON:")
    print("  GOOGLE_OAUTH_CLIENT_ID=<the client_id from that JSON>")
    print("  GOOGLE_OAUTH_CLIENT_SECRET=<the client_secret from that JSON>")
    print(sep)
    print(
        "\nThe refresh token is a long-lived credential - treat it like a "
        "password.\nThis script did NOT save it to any file; only you have it "
        "now (in this terminal).\n"
        "Next: run `python scripts/smoke_test_drive_upload.py` to confirm "
        "uploads work."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
