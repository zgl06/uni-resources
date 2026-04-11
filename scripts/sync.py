"""
sync.py — Main orchestrator for D2L → OneDrive sync.

Usage:
    python scripts/sync.py

Environment variables (set via GitHub Secrets or .env file):
    D2L_EMAIL, D2L_PASSWORD, D2L_SESSION_COOKIE
    AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID
    ONEDRIVE_USER_EMAIL
    DRY_RUN   (optional, overrides settings.json dry_run)

Configuration: config/settings.json
State tracking: config/state.json (gitignored, persisted via GitHub Actions cache)
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env if present (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Project root is one level up from this script
PROJECT_ROOT = Path(__file__).parent.parent
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
STATE_PATH = PROJECT_ROOT / "config" / "state.json"


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        raise FileNotFoundError(
            f"Settings file not found: {SETTINGS_PATH}\n"
            "Ensure config/settings.json exists in the repository."
        )
    with open(SETTINGS_PATH) as f:
        return json.load(f)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "files": {}}
    with open(STATE_PATH) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            logging.warning("state.json is corrupt. Starting with empty state.")
            return {"version": 1, "files": {}}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def compute_file_key(course_name: str, folder_path: str, filename: str) -> str:
    """Compute a stable MD5 key for a file entry in state.json."""
    raw = f"{course_name}|{folder_path}|{filename}"
    return hashlib.md5(raw.encode()).hexdigest()


def resolve_dry_run(settings: dict) -> bool:
    """
    Dry-run can be set in three ways (in priority order):
    1. DRY_RUN env var (set by GitHub Actions workflow_dispatch input)
    2. settings.json dry_run field
    3. Default: False
    """
    env_val = os.environ.get("DRY_RUN", "").lower()
    if env_val in ("true", "1", "yes"):
        return True
    if env_val in ("false", "0", "no"):
        return False
    return settings.get("dry_run", False)


def main():
    setup_logging()
    log = logging.getLogger(__name__)

    log.info("=" * 60)
    log.info("D2L Brightspace → OneDrive Sync")
    log.info("=" * 60)

    # -------------------------------------------------------------------------
    # Load configuration and state
    # -------------------------------------------------------------------------
    try:
        settings = load_settings()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error(f"Failed to load settings: {e}")
        sys.exit(1)

    dry_run = resolve_dry_run(settings)
    if dry_run:
        log.info("DRY RUN MODE — No files will be downloaded or uploaded.")

    state = load_state()
    log.info(f"State loaded: {len(state['files'])} previously synced files.")

    # -------------------------------------------------------------------------
    # Initialize components
    # -------------------------------------------------------------------------
    # Import here so missing deps produce a clean error message
    try:
        from d2l_scraper import D2LScraper, D2LScraperError, DuoExpiredError
        from onedrive_uploader import OneDriveUploader, OneDriveUploaderError
    except ImportError as e:
        log.error(f"Import error: {e}")
        log.error("Run: pip install -r requirements.txt && playwright install chromium")
        sys.exit(1)

    # Uploader validates Azure credentials at init — fail fast before D2L login
    try:
        uploader = OneDriveUploader(settings, dry_run=dry_run)
    except OneDriveUploaderError as e:
        log.error(str(e))
        sys.exit(1)

    scraper = D2LScraper(settings, dry_run=dry_run)

    # -------------------------------------------------------------------------
    # D2L Login
    # -------------------------------------------------------------------------
    try:
        scraper.login()
    except DuoExpiredError as e:
        log.error(f"\n{'='*60}")
        log.error("DUO SESSION EXPIRED")
        log.error("=" * 60)
        log.error(str(e))
        log.error("=" * 60)
        sys.exit(1)
    except Exception as e:
        log.error(f"D2L login failed: {e}")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # Course discovery
    # -------------------------------------------------------------------------
    try:
        courses = scraper.get_courses()
    except Exception as e:
        log.error(f"Failed to discover courses: {e}")
        scraper.close()
        sys.exit(1)

    if not courses:
        log.info("No courses to sync. Exiting.")
        scraper.close()
        sys.exit(0)

    log.info(f"Syncing {len(courses)} course(s): {[c['name'] for c in courses]}")

    # -------------------------------------------------------------------------
    # Sync loop
    # -------------------------------------------------------------------------
    stats = {"uploaded": 0, "skipped": 0, "failed": 0, "dry_run": 0}
    failed_files = []
    overwrite = settings.get("overwrite", False)

    for course in courses:
        log.info(f"\n--- Course: {course['name']} ---")

        # Get content tree
        try:
            files = scraper.get_content_tree(course)
        except Exception as e:
            log.error(f"Failed to get content tree for {course['name']}: {e}")
            continue

        if not files:
            log.info(f"No files found in {course['name']}.")
            continue

        log.info(f"Found {len(files)} file(s) in {course['name']}.")

        for file_info in files:
            course_name = file_info["course_name"]
            folder_path = file_info["folder_path"]
            filename = file_info["filename"]

            file_key = compute_file_key(course_name, folder_path, filename)

            # Skip already-synced files (unless overwrite mode)
            if file_key in state["files"] and not overwrite:
                log.debug(f"Skipping (already synced): {course_name}/{folder_path}/{filename}")
                stats["skipped"] += 1
                continue

            # Download file
            local_path = None
            try:
                local_path = scraper.download_file(file_info)
            except Exception as e:
                log.error(f"Download failed for {filename}: {e}")
                stats["failed"] += 1
                failed_files.append(file_info)
                continue

            if local_path is None:
                # Skipped by extension filter, size limit, or dry_run
                if dry_run:
                    stats["dry_run"] += 1
                else:
                    stats["skipped"] += 1
                continue

            # Upload to OneDrive
            try:
                uploader.upload(local_path, course_name, file_info)

                # Record success in state — write immediately (crash-safe)
                state["files"][file_key] = {
                    "course": course_name,
                    "path": f"{folder_path}/{filename}",
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    "size_bytes": local_path.stat().st_size,
                }
                save_state(state)
                stats["uploaded"] += 1

            except Exception as e:
                log.error(f"Upload failed for {filename}: {e}")
                stats["failed"] += 1
                failed_files.append(file_info)

            finally:
                # Always clean up temp file
                if local_path and local_path.exists():
                    local_path.unlink(missing_ok=True)

    # -------------------------------------------------------------------------
    # Cleanup and summary
    # -------------------------------------------------------------------------
    scraper.close()

    log.info("\n" + "=" * 60)
    log.info("SYNC SUMMARY")
    log.info("=" * 60)
    if dry_run:
        log.info(f"  Dry-run items:  {stats['dry_run']}")
    else:
        log.info(f"  Uploaded:       {stats['uploaded']}")
    log.info(f"  Skipped:        {stats['skipped']}")
    log.info(f"  Failed:         {stats['failed']}")
    log.info("=" * 60)

    if failed_files:
        log.error(f"\nFailed to sync {len(failed_files)} file(s):")
        for f in failed_files:
            log.error(f"  - {f['course_name']}/{f['folder_path']}/{f['filename']}")
        log.error("\nThese files will be retried on the next run.")
        sys.exit(1)  # Non-zero exit → GitHub Actions marks run as failed

    log.info("Sync completed successfully.")


if __name__ == "__main__":
    # Add scripts/ directory to path so relative imports work
    sys.path.insert(0, str(Path(__file__).parent))
    main()
