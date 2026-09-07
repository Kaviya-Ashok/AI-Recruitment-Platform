# scripts/

Manual, occasional-use developer tools. **Not** part of the application, **not**
imported by `app/`, **not** run by the pytest suite.

## compare_rubric_models.py

Runs the rubric-generation prompt through **two** Claude models
(`claude-haiku-4-5-20251001` and the current `DEFAULT_MODELS["rubric_generation"]`
Sonnet) so a human can eyeball which model is good enough for this task before
deciding whether to change the default.

- Requires a real `ANTHROPIC_API_KEY` in the environment / `.env`.
- Makes **2 real, billable Claude API calls**. It prints a rough cost estimate
  and requires confirmation (`--yes` to skip the prompt).
- Writes `rubric_haiku.json`, `rubric_sonnet.json`, and `rubric_comparison.txt`
  to `--out-dir` (default: current directory).

```bash
# Against a real analysed job:
python scripts/compare_rubric_models.py --job-id <uuid>

# Against a built-in sample requirement set (no DB needed):
python scripts/compare_rubric_models.py --sample --yes --out-dir /tmp/rubcmp
```

Changing `DEFAULT_MODELS["rubric_generation"]` is a **separate, deliberate**
edit made after reviewing this script's output — this script never changes it.

## authorize_drive_oauth.py

**Run this once** before `smoke_test_drive_upload.py` (or the app) can touch
Drive. It runs Google's "installed app" OAuth flow as the **human account that
owns the Drive folder** and prints a long-lived refresh token.

Why OAuth-as-owner and not a service account: a personal Gmail account has no
service-account storage quota, so an SA upload fails with a hard
`403 storageQuotaExceeded`. Authorizing as the folder's owner makes uploads
count against that person's real quota.

- Needs `GOOGLE_OAUTH_CLIENT_SECRET_FILE` set to the absolute path of the
  "Desktop app" OAuth client-secret JSON downloaded from the Google Cloud
  console. (This file is only needed for this script — not at app runtime.)
- Opens your browser, you log in + approve the full Drive scope, and it prints
  a `GOOGLE_OAUTH_REFRESH_TOKEN=...` line.
- It **does not write anything to disk** — you copy the refresh token into
  `.env` yourself, plus `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`
  from the same JSON.

```bash
# PowerShell
$env:GOOGLE_OAUTH_CLIENT_SECRET_FILE = "C:\path\to\client_secret.json"
venv\Scripts\python.exe scripts\authorize_drive_oauth.py
```

Re-run only if the refresh token is later revoked or stops working.

## smoke_test_drive_upload.py

Confirms the **real** Google Drive storage integration (`storage_service`)
works end to end via the OAuth-as-owner path — the earlier service-account
attempt failed here with `403 storageQuotaExceeded`, which is the whole reason
`authorize_drive_oauth.py` exists.

- Requires a real `.env` with `GOOGLE_OAUTH_CLIENT_ID`,
  `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN` and
  `GOOGLE_DRIVE_ROOT_FOLDER_ID`.
- Performs a **real write** to Drive (a few KB — one tiny PDF in a new
  `smoke-test-<ts>` subfolder). Prompts before writing (`--yes` to skip).
- Prints the uploaded file's `owners`, `ownedByMe`, `quotaBytesUsed`,
  `webViewLink`, and a download round-trip check.

```bash
python scripts/smoke_test_drive_upload.py         # prompts before writing
python scripts/smoke_test_drive_upload.py --yes
```

Expected: `owner` is the human Google account, `ownedByMe` is `True`, the
upload succeeds. If you get a `403` / `DriveAuthError` / `RefreshError`, re-run
`authorize_drive_oauth.py` and refresh `GOOGLE_OAUTH_REFRESH_TOKEN` in `.env`.
