# CLAUDE.md — Architecture Notes for AI Sessions

This file describes the architecture of the D2L → OneDrive auto-sync system for future AI sessions.

## System Purpose

Automated daily sync of course files from UWaterloo D2L Brightspace (learn.uwaterloo.ca) to a University Microsoft 365 OneDrive account. Runs on GitHub Actions free tier via cron.

## Component Responsibilities

```
scripts/
  cookie_helper.py     LOCAL ONLY — captures Duo MFA session cookie via headed browser
  d2l_scraper.py       D2LScraper class — Playwright login + course discovery + content traversal + download
  onedrive_uploader.py OneDriveUploader class — MSAL auth + Graph API upload (simple + chunked)
  sync.py              Orchestrator — loads config/state, calls scraper + uploader, saves state

config/
  settings.json        User-editable config (heavily commented via _comment_* keys)
  state.json           Auto-generated, gitignored — tracks uploaded files by MD5 key

.github/workflows/
  daily_sync.yml       Cron 0 13 * * * (8am EST), workflow_dispatch with dry_run input
```

## Data Flow

```
[local] cookie_helper.py
    → session_cookie.json (user copies to GitHub Secret D2L_SESSION_COOKIE)

[CI] daily_sync.yml
    → actions/cache/restore → config/state.json
    → sync.py
        → D2LScraper.login()          inject D2L_SESSION_COOKIE into Playwright context
        → D2LScraper.get_courses()    Valence API /d2l/api/le/1.0/enrollments/myenrollments/
        → D2LScraper.get_content_tree(course)  /d2l/api/le/1.0/{courseId}/content/toc
        → D2LScraper.download_file()  requests.Session with Playwright cookies
        → OneDriveUploader.upload()   Microsoft Graph API
        → save_state()                config/state.json (written after EACH upload)
    → actions/cache/save (if: always()) → config/state.json
```

## State File Format

`config/state.json` tracks every successfully uploaded file:

```json
{
  "version": 1,
  "files": {
    "<md5_hex>": {
      "course": "CS341 - Algorithm Design",
      "path": "Week 1 - Intro/lecture1.pdf",
      "uploaded_at": "2024-01-15T13:00:00+00:00",
      "size_bytes": 2048000
    }
  }
}
```

**MD5 key computation** (`sync.py:compute_file_key`):
```python
md5(f"{course_name}|{folder_path}|{filename}")
```
Uses `|` as delimiter to avoid collisions. Uses `course_name` (not `course_id`) so the key is human-readable in state.json.

## GitHub Secrets (all 7 required)

| Secret | Used by | Purpose |
|---|---|---|
| `D2L_EMAIL` | d2l_scraper.py | Credential fallback login + cookie_helper.py |
| `D2L_PASSWORD` | d2l_scraper.py | Credential fallback login + cookie_helper.py |
| `D2L_SESSION_COOKIE` | d2l_scraper.py | JSON list of Playwright cookies injected at startup |
| `AZURE_CLIENT_ID` | onedrive_uploader.py | MSAL ConfidentialClientApplication |
| `AZURE_CLIENT_SECRET` | onedrive_uploader.py | MSAL ConfidentialClientApplication |
| `AZURE_TENANT_ID` | onedrive_uploader.py | MSAL authority URL |
| `ONEDRIVE_USER_EMAIL` | onedrive_uploader.py | Graph API path `/users/{email}/drive/root:/...` |

## D2L API Endpoints Used

All under `https://learn.uwaterloo.ca`:

- `GET /d2l/api/le/1.0/enrollments/myenrollments/?classlistRoleId=3` — active student enrollments
- `GET /d2l/api/le/1.0/{courseId}/content/toc` — full content table of contents (modules + topics)
- Topic download URLs come from `toc["Modules"][*]["Topics"][*]["Url"]` — routed through D2L's CDN

API version is `1.0` (stable). The `unstable` version has different schemas.

`TopicType` values in the content tree:
- `1` = File (download these)
- `2` = Link (skip)
- `3` = Video (skip unless extension filter allows it)

## Microsoft Graph API Endpoints Used

All under `https://graph.microsoft.com/v1.0`:

- `PUT /users/{email}/drive/root:/{path}:/content` — simple upload (< 4MB)
- `POST /users/{email}/drive/root:/{path}:/createUploadSession` — initiate chunked upload
- Chunk PUTs go to the `uploadUrl` from the session response (no Authorization header needed on chunks)

Graph API auto-creates intermediate folders when using the `root:/{path}:/content` endpoint — no need to pre-create folder hierarchy.

## Auth Flow

**D2L:** Cookie injection → Playwright → requests.Session (cookie transfer)
```
D2L_SESSION_COOKIE (env) → context.add_cookies() → navigate to D2L → extract cookies → requests.Session
```

**Microsoft Graph:** Client credentials flow (Application permissions, not delegated)
```
MSAL.ConfidentialClientApplication → acquire_token_for_client(["https://graph.microsoft.com/.default"])
```
Token is cached in memory and refreshed 5 minutes before expiry.

## Known Limitations

1. **Duo MFA cookie expires every ~30 days.** User must re-run `cookie_helper.py` and update `D2L_SESSION_COOKIE`. The sync logs a clear error with instructions when this happens.

2. **`Files.ReadWrite.All` requires admin consent.** Application permissions (vs. delegated) need org-level approval. UWaterloo students may need to contact IST.

3. **Content-condition locked topics are invisible.** D2L doesn't include locked/unreleased topics in the `/content/toc` response. These are silently skipped — correct behavior.

4. **No support for D2L Discussion posts, Dropbox submissions, or Grades.** Only `TopicType=1` (file attachments) are synced.

5. **GitHub Actions cache has a 10GB total limit and 7-day expiry.** State.json is tiny (kilobytes), so this is not a concern.

## How to Extend

**Add a new file type to sync:**
Edit `config/settings.json` → `allowed_extensions` array. No code changes needed.

**Sync only specific courses:**
Edit `config/settings.json` → `courses_filter` array. Partial case-insensitive match.

**Skip certain D2L modules:**
Edit `config/settings.json` → `exclude_folders` array.

**Change OneDrive destination folder:**
Edit `config/settings.json` → `onedrive_root_folder`.

**Add support for D2L link topics (TopicType=2):**
In `d2l_scraper.py:_walk_module`, change `TopicType != 1` to handle type 2 as well.

**Test locally without uploading:**
Set `"dry_run": true` in `config/settings.json`, then `python scripts/sync.py`.

## Testing

Local dry run:
```bash
pip install -r requirements.txt
playwright install chromium
export D2L_SESSION_COOKIE='[...json from cookie_helper.py...]'
export AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=... AZURE_TENANT_ID=... ONEDRIVE_USER_EMAIL=...
# Edit config/settings.json: set "dry_run": true
python scripts/sync.py
```

CI dry run: Actions → D2L to OneDrive Sync → Run workflow → check "Dry run mode"
