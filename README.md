# uni-resources — D2L Brightspace → OneDrive Auto-Sync

Automatically downloads all course files from UWaterloo's D2L Brightspace and syncs them to your University OneDrive, organized by course and folder. Runs daily at 8am EST via GitHub Actions (free tier).

**OneDrive folder structure:**
```
D2L Sync/
  └── CS341 - Algorithm Design/
      └── Week 1 - Introduction/
          └── lecture1.pdf
```

---

## How It Works

1. A GitHub Actions cron job runs daily at 8am EST
2. It logs into D2L using a saved Duo MFA session cookie (no phone interaction needed after initial setup)
3. It discovers all your active enrolled courses via the D2L API
4. It traverses each course's content tree and downloads new files
5. It uploads them to your OneDrive under `D2L Sync/`
6. A state file tracks what's been uploaded so nothing is re-synced unnecessarily

---

## Prerequisites

- A GitHub account (free tier is sufficient)
- Python 3.11+ installed locally (for one-time setup steps)
- Access to an Azure app registration (see Step 1)
- Your UWaterloo email and password

---

## Step 1: Azure App Registration

You need an Azure app with permission to write to your OneDrive.

> **Note for UWaterloo students:** The required permission (`Files.ReadWrite.All` as an Application permission) needs admin consent. You have two options:
> - **Option A (Recommended):** Create the app registration in the UWaterloo tenant and contact IST to grant admin consent for your specific app.
> - **Option B:** Create the app in a personal Azure tenant (free Microsoft account) and configure it to access your UWaterloo account. This may work depending on UWaterloo's tenant policies.

### Steps:

1. Go to [portal.azure.com](https://portal.azure.com) and sign in
2. Search for **"App registrations"** and click **New registration**
3. Fill in:
   - **Name:** `D2L OneDrive Sync` (or anything you like)
   - **Supported account types:** "Accounts in this organizational directory only (UWaterloo)"
   - **Redirect URI:** Leave blank
4. Click **Register**
5. On the app's overview page, copy:
   - **Application (client) ID** → this is `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → this is `AZURE_TENANT_ID`
6. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**
7. Search for and add: `Files.ReadWrite.All`
8. Click **Grant admin consent for [your organization]** (requires admin — see note above)
9. Go to **Certificates & secrets** → **New client secret**
   - Description: `github-actions`
   - Expiry: 24 months (or your preference)
10. Copy the **Value** immediately (it won't be shown again) → this is `AZURE_CLIENT_SECRET`

---

## Step 2: GitHub Secrets

In your GitHub repository: **Settings → Secrets and variables → Actions → New repository secret**

Add all 7 of the following secrets:

| Secret Name | Description | Where to find it |
|---|---|---|
| `D2L_EMAIL` | Your UWaterloo email (e.g. `j3doe@uwaterloo.ca`) | Your own email |
| `D2L_PASSWORD` | Your UWaterloo password | Your own password |
| `D2L_SESSION_COOKIE` | JSON from `cookie_helper.py` | See Step 3 below |
| `AZURE_CLIENT_ID` | Azure app Application (client) ID | Azure portal (Step 1) |
| `AZURE_CLIENT_SECRET` | Azure app client secret value | Azure portal (Step 1) |
| `AZURE_TENANT_ID` | Azure Directory (tenant) ID | Azure portal (Step 1) |
| `ONEDRIVE_USER_EMAIL` | Your UWaterloo email for OneDrive | Usually same as `D2L_EMAIL` |

---

## Step 3: Capture Duo MFA Session Cookie (Run Locally Once)

The sync uses a saved Duo session cookie so it doesn't need to prompt your phone every day. You capture this cookie once by running a local script.

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/uni-resources.git
cd uni-resources

# 2. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 3. Set your credentials (optional — the script will prompt if not set)
export D2L_EMAIL="j3doe@uwaterloo.ca"
export D2L_PASSWORD="your_password_here"

# 4. Run the helper — a browser window will open
python scripts/cookie_helper.py
```

The script will:
- Open a visible browser and navigate to learn.uwaterloo.ca
- Auto-fill your credentials and trigger the Duo push
- Wait up to 3 minutes for you to approve the push on your phone
- Save the session cookie to `session_cookie.json`
- Print the cookie JSON in the terminal

**After the script finishes:**
- Copy the entire JSON output (everything between the dashes)
- Add it as the `D2L_SESSION_COOKIE` GitHub Secret (Settings → Secrets → New)

> **Cookie expiry:** The Duo session cookie expires after approximately 30 days. When the sync starts failing with authentication errors, re-run `cookie_helper.py` and update the `D2L_SESSION_COOKIE` secret.

---

## Step 4: Customize Settings (Optional)

Edit `config/settings.json` to customize what gets synced:

```json
{
  "courses_filter": ["CS341", "MATH237"],  
  "allowed_extensions": [".pdf", ".pptx"],
  "exclude_folders": ["Archive"],          
  "dry_run": false,                        
  "overwrite": false,                      
  "onedrive_root_folder": "D2L Sync"      
}
```

Each setting has a `_comment_*` sibling key explaining it in detail.

---

## Step 5: Test with a Dry Run

Before the first real sync, do a dry run to verify everything is configured correctly.

1. Go to your repository on GitHub
2. Click **Actions** → **D2L to OneDrive Sync**
3. Click **Run workflow** (top right)
4. Check **"Dry run mode"** ✓
5. Click **Run workflow**

Check the logs to verify:
- D2L login succeeded
- Courses were discovered
- Files were listed (no actual uploads happen in dry-run mode)

---

## Step 6: Run a Live Sync

Once the dry run looks correct:

1. Go to **Actions** → **D2L to OneDrive Sync** → **Run workflow**
2. Leave "Dry run mode" unchecked
3. Click **Run workflow**

After it completes (~5–15 minutes depending on number of files), check your OneDrive for:
```
D2L Sync/
  └── [Your Course Name]/
      └── [Module Name]/
          └── file.pdf
```

The workflow also runs automatically every day at 8am EST.

---

## Troubleshooting

### "Duo session cookie is expired"
Re-run `python scripts/cookie_helper.py` locally and update the `D2L_SESSION_COOKIE` secret.

### "Missing Azure credentials: AZURE_CLIENT_ID, ..."
One or more GitHub Secrets are missing or misspelled. Double-check all 7 secrets in your repo settings.

### "MSAL authentication failed"
- Verify `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, and `AZURE_TENANT_ID` are correct
- Ensure **Files.ReadWrite.All** (Application permission) has admin consent in the Azure portal
- The client secret may have expired — generate a new one and update the secret

### "Content tree request timed out"
D2L is slow on some courses. The course is skipped with a warning and retried on the next run.

### Files appear with `_` replacing special characters
OneDrive doesn't allow `/ \ : * ? " < > |` in filenames. The sync replaces these automatically.

### "Rate limited (429)"
The Microsoft Graph API has rate limits. The sync automatically retries with backoff.

### Cron runs at 9am instead of 8am
`0 13 * * *` = 8am EST (UTC-5) but 9am EDT (UTC-4) during daylight saving time. Adjust `.github/workflows/daily_sync.yml` if needed.

---

## Privacy & Security

- Your credentials are stored only in GitHub Secrets (encrypted at rest, never logged)
- The session cookie is rotated every ~30 days when you re-run `cookie_helper.py`
- `session_cookie.json` and `.env` are gitignored and will never be committed
- `state.json` is gitignored and persisted only in GitHub Actions cache (not pushed to the repo)
