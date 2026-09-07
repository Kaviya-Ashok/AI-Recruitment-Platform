"""Smoke-test the real Google Drive storage integration — MANUAL DEV TOOL.

NOT production code. NOT imported by the app. NOT part of the pytest suite.

It exists to confirm the OAuth-as-owner auth path actually works end to end
against the real Drive: an earlier attempt authorized as a *service account*
and hit a hard ``403 storageQuotaExceeded`` (personal Gmail = no SA quota).
storage_service.py now authorizes as the human account that owns the folder,
so uploads count against that person's real quota.

What it does (all against the REAL configured Drive):
  1. builds the real Drive client via storage_service._get_drive()
     (OAuth user credentials from GOOGLE_OAUTH_CLIENT_ID / _CLIENT_SECRET /
     _REFRESH_TOKEN — the refresh token is auto-exchanged for an access token)
  2. creates a subfolder named  smoke-test-<timestamp>  under
     GOOGLE_DRIVE_ROOT_FOLDER_ID
  3. uploads a tiny real PDF into it
  4. reads the file's metadata back (id, name, size, owners, quotaBytesUsed,
     ownedByMe) and prints it plus a webViewLink
  5. downloads the bytes back and checks they round-trip
  6. leaves the file in place (so you can inspect it in the Drive UI) and
     prints the file id + folder id so you can delete them manually afterwards

Usage
-----
    python scripts/smoke_test_drive_upload.py            # prompts before writing
    python scripts/smoke_test_drive_upload.py --yes      # skip the prompt

Requires a real .env with:
    GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / GOOGLE_OAUTH_REFRESH_TOKEN
    GOOGLE_DRIVE_ROOT_FOLDER_ID=<the folder id>
(Run scripts/authorize_drive_oauth.py once first to get the refresh token.)

This performs a REAL write to Google Drive. It is cheap (a few KB) but it does
create a real file. Delete the smoke-test folder from Drive when you are done.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _tiny_pdf_bytes() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"Drive smoke test {time.strftime('%Y-%m-%d %H:%M:%S')}")
    data = doc.tobytes()
    doc.close()
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = parser.parse_args(argv)

    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Import the real service — this also runs its lazy config check.
    from app.services import storage_service
    from googleapiclient.http import MediaIoBaseUpload  # noqa: PLC0415

    try:
        service, root_folder_id = storage_service._get_drive()
    except storage_service.StorageConfigError as exc:
        sys.exit(f"Storage not configured: {exc}")
    except storage_service.DriveAuthError as exc:
        sys.exit(f"Drive auth failed: {exc}")

    print(f"Root folder id: {root_folder_id}")
    if not args.yes:
        if input("Upload one small real test PDF to that folder now? type 'yes': ").strip() != "yes":
            print("Aborted.")
            return 1

    folder_name = f"smoke-test-{int(time.time())}"
    folder_id = storage_service._drive_create_folder(service, root_folder_id, folder_name)
    print(f"Created subfolder {folder_name!r} -> {folder_id}")

    pdf = _tiny_pdf_bytes()
    media = MediaIoBaseUpload(io.BytesIO(pdf), mimetype="application/pdf", resumable=False)
    created = (
        service.files()
        .create(
            body={"name": "smoke-test-resume.pdf", "parents": [folder_id]},
            media_body=media,
            fields="id, name, size, webViewLink, owners(emailAddress,displayName), "
            "ownedByMe, quotaBytesUsed",
            supportsAllDrives=True,
        )
        .execute()
    )

    print("\n--- uploaded file metadata ---")
    for key in ("id", "name", "size", "quotaBytesUsed", "ownedByMe", "webViewLink"):
        print(f"  {key}: {created.get(key)}")
    owners = created.get("owners") or []
    for o in owners:
        print(f"  owner: {o.get('displayName')} <{o.get('emailAddress')}>")

    # Round-trip the bytes back.
    got = storage_service._drive_download_file(service, created["id"])
    print(f"\n  download round-trip: {'OK' if got == pdf else 'MISMATCH'} ({len(got)} bytes)")

    print("\n--- WHAT TO CHECK ---")
    print("  * 'owner' above should be the human Google account you authorized")
    print("    (kaviyaashokece@gmail.com), and 'ownedByMe' should be True.")
    print("  * The upload SUCCEEDING at all is the headline result — the old")
    print("    service-account path failed here with 403 storageQuotaExceeded.")
    print("  * 'quotaBytesUsed' being non-zero and the file visible in the")
    print("    owner's Drive confirms it counts against the owner's real quota.")
    print("  * If you instead see a 403 / RefreshError / DriveAuthError above,")
    print("    re-run scripts/authorize_drive_oauth.py and refresh")
    print("    GOOGLE_OAUTH_REFRESH_TOKEN in .env.")
    print(f"\n  Clean up when done: delete folder {folder_id} (contains file {created['id']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
