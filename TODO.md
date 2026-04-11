# TODO — D2L → OneDrive Sync Build Checklist

## Setup
- [x] Create repository and branch
- [x] Create directory structure (`scripts/`, `config/`, `.github/workflows/`)
- [x] Create `.gitignore`
- [x] Create `requirements.txt`

## Configuration
- [x] Create `config/settings.json` with commented fields
- [x] Create skeleton `config/state.json`

## Scripts
- [x] Implement `scripts/cookie_helper.py`
  - [x] Headed Playwright browser launch
  - [x] Auto-fill credentials, handle Duo wait
  - [x] Save cookies to `session_cookie.json` + print inline
- [x] Implement `scripts/d2l_scraper.py`
  - [x] Cookie injection login
  - [x] Credential fallback login
  - [x] Duo MFA wait with timeout
  - [x] Course discovery via Valence API
  - [x] HTML scraping fallback
  - [x] Content tree traversal (recursive)
  - [x] Extension + size filtering on download
  - [x] Real filename extraction from Content-Disposition header
- [x] Implement `scripts/onedrive_uploader.py`
  - [x] MSAL client credentials auth with token caching
  - [x] Simple PUT upload (< 4MB)
  - [x] Chunked upload session (>= 4MB)
  - [x] Retry with backoff on 429 rate limits
  - [x] Path sanitization for OneDrive
- [x] Implement `scripts/sync.py`
  - [x] Load settings + state
  - [x] Dry-run mode (env var + settings.json)
  - [x] Per-file state tracking (write after each upload)
  - [x] Temp file cleanup in finally block
  - [x] Summary stats + non-zero exit on failures

## CI/CD
- [x] Create `.github/workflows/daily_sync.yml`
  - [x] Schedule cron 8am EST
  - [x] workflow_dispatch with dry_run boolean input
  - [x] All 7 secrets wired up
  - [x] Split cache restore/save (save with `if: always()`)

## Documentation
- [x] Write `README.md` — full setup guide
- [x] Write `CLAUDE.md` — architecture notes
- [x] Write `TODO.md` — this file

## First-Run Validation (manual steps after setup)
- [ ] Run `cookie_helper.py` locally — verify `session_cookie.json` created
- [ ] Add all 7 GitHub Secrets to repository
- [ ] Trigger workflow_dispatch dry run — verify logs show courses and files
- [ ] Verify `state.json` cache was saved (Actions → Caches tab)
- [ ] Trigger live run — verify files appear in OneDrive under `D2L Sync/`
- [ ] Trigger second run — verify no re-uploads (all files skipped)
- [ ] Delete `D2L_SESSION_COOKIE` temporarily — verify error message is actionable
- [ ] Restore secret and wait for next cron run

## Ongoing Maintenance
- [ ] Re-run `cookie_helper.py` every ~30 days to refresh Duo session cookie
- [ ] Update `AZURE_CLIENT_SECRET` before it expires (check Azure portal)
- [ ] Monitor GitHub Actions → Caches for state persistence
