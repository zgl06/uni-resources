"""
onedrive_uploader.py — Upload files to OneDrive via Microsoft Graph API.

Uses MSAL client credentials flow (application permissions, not delegated).
Requires Files.ReadWrite.All application permission with admin consent.

Handles:
- Simple PUT upload for files < 4MB
- Chunked upload session for files >= 4MB
- Automatic retry with exponential backoff on HTTP 429 (rate limiting)
- Path sanitization for OneDrive-safe filenames
- Token caching to avoid unnecessary re-authentication
"""

import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

try:
    import msal
    import requests
except ImportError:
    sys.exit("ERROR: msal/requests not installed. Run: pip install -r requirements.txt")

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CHUNK_SIZE = 5 * 1024 * 1024       # 5 MB per chunk
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024  # Use simple upload for files < 4MB
MAX_RETRIES = 5


class OneDriveUploaderError(Exception):
    pass


class OneDriveUploader:
    def __init__(self, settings: dict, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run

        # Validate required env vars at startup — fail fast with a clear message
        required = [
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_TENANT_ID",
            "ONEDRIVE_USER_EMAIL",
        ]
        missing = [v for v in required if not os.environ.get(v)]
        if missing:
            raise OneDriveUploaderError(
                f"Missing required Azure/OneDrive environment variables: {', '.join(missing)}\n"
                "Ensure all 7 GitHub Secrets are set. See README → Step 2: GitHub Secrets."
            )

        self._client_id = os.environ["AZURE_CLIENT_ID"]
        self._client_secret = os.environ["AZURE_CLIENT_SECRET"]
        self._tenant_id = os.environ["AZURE_TENANT_ID"]
        self._user_email = os.environ["ONEDRIVE_USER_EMAIL"]
        self._root_folder = settings.get("onedrive_root_folder", "D2L Sync")

        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a valid access token, acquiring a new one if needed."""
        # Refresh if expired or within 5 minutes of expiry
        if self._token and time.time() < self._token_expires_at - 300:
            return self._token

        log.debug("Acquiring new Microsoft Graph access token via MSAL...")
        app = msal.ConfidentialClientApplication(
            client_id=self._client_id,
            client_credential=self._client_secret,
            authority=f"https://login.microsoftonline.com/{self._tenant_id}",
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )

        if "access_token" not in result:
            error = result.get("error", "unknown_error")
            description = result.get("error_description", "No description provided.")
            raise OneDriveUploaderError(
                f"MSAL authentication failed: {error}\n{description}\n\n"
                "Check that:\n"
                "  - AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID are correct\n"
                "  - The Azure app has Files.ReadWrite.All (Application) permission\n"
                "  - Admin consent has been granted for that permission\n"
                "See README → Step 1: Azure App Registration."
            )

        self._token = result["access_token"]
        self._token_expires_at = time.time() + result.get("expires_in", 3600)
        log.debug("Access token acquired successfully.")
        return self._token

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # -------------------------------------------------------------------------
    # Public upload interface
    # -------------------------------------------------------------------------

    def upload(self, local_path: Path, course_name: str, file_info: dict):
        """
        Upload a file to OneDrive under:
            {root_folder}/{course_name}/{folder_path}/{filename}

        Uses simple PUT for small files, chunked upload for large files.
        """
        folder_path = file_info.get("folder_path", "")
        filename = file_info.get("filename", local_path.name)

        onedrive_path = self._build_path(course_name, folder_path, local_path.name)
        file_size = local_path.stat().st_size

        if self.dry_run:
            log.info(f"[DRY RUN] Would upload {local_path.name} ({file_size / 1024:.1f} KB) → {onedrive_path}")
            return

        log.info(f"Uploading: {local_path.name} ({file_size / 1024:.1f} KB) → OneDrive:{onedrive_path}")

        if file_size < SIMPLE_UPLOAD_MAX:
            self._simple_upload(local_path, onedrive_path)
        else:
            self._chunked_upload(local_path, onedrive_path, file_size)

        log.info(f"Upload complete: {local_path.name}")

    # -------------------------------------------------------------------------
    # Simple upload (< 4MB)
    # -------------------------------------------------------------------------

    def _simple_upload(self, local_path: Path, onedrive_path: str):
        encoded_path = quote(onedrive_path, safe="/")
        url = f"{GRAPH_BASE}/users/{self._user_email}/drive/root:/{encoded_path}:/content"

        with open(local_path, "rb") as f:
            data = f.read()

        headers = {
            **self._auth_headers(),
            "Content-Type": "application/octet-stream",
        }
        resp = self._request_with_backoff("PUT", url, headers=headers, data=data)

        if resp.status_code not in (200, 201):
            raise OneDriveUploaderError(
                f"Simple upload failed for {local_path.name}: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )

    # -------------------------------------------------------------------------
    # Chunked upload (>= 4MB)
    # -------------------------------------------------------------------------

    def _chunked_upload(self, local_path: Path, onedrive_path: str, file_size: int):
        """Upload large files using Microsoft Graph upload sessions."""
        upload_url = self._create_upload_session(onedrive_path)

        log.info(f"Chunked upload started: {local_path.name} ({file_size / (1024*1024):.1f} MB)")

        with open(local_path, "rb") as f:
            chunk_number = 0
            bytes_uploaded = 0

            while bytes_uploaded < file_size:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break

                chunk_start = bytes_uploaded
                chunk_end = bytes_uploaded + len(chunk) - 1
                total = file_size

                headers = {
                    "Content-Range": f"bytes {chunk_start}-{chunk_end}/{total}",
                    "Content-Length": str(len(chunk)),
                    # NOTE: Do NOT add Authorization header here — upload session URL
                    # already embeds auth credentials.
                }

                # Retry each chunk independently
                resp = self._request_with_backoff(
                    "PUT", upload_url, headers=headers, data=chunk
                )

                if resp.status_code == 202:
                    # Intermediate chunk accepted
                    bytes_uploaded += len(chunk)
                    chunk_number += 1
                    progress = bytes_uploaded / file_size * 100
                    log.debug(f"Chunk {chunk_number} uploaded ({progress:.0f}%)")
                elif resp.status_code in (200, 201):
                    # Final chunk — upload complete
                    bytes_uploaded += len(chunk)
                    log.debug("Final chunk accepted, upload complete.")
                    break
                else:
                    raise OneDriveUploaderError(
                        f"Chunked upload failed at byte {chunk_start} for {local_path.name}: "
                        f"HTTP {resp.status_code} — {resp.text[:200]}"
                    )

        if bytes_uploaded < file_size:
            raise OneDriveUploaderError(
                f"Chunked upload incomplete: uploaded {bytes_uploaded} of {file_size} bytes."
            )

    def _create_upload_session(self, onedrive_path: str) -> str:
        """Create a Graph API upload session and return the upload URL."""
        encoded_path = quote(onedrive_path, safe="/")
        url = (
            f"{GRAPH_BASE}/users/{self._user_email}/drive/root:/{encoded_path}:/createUploadSession"
        )
        payload = {
            "item": {
                "@microsoft.graph.conflictBehavior": "replace",
                "name": onedrive_path.split("/")[-1],
            }
        }
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/json",
        }
        resp = self._request_with_backoff("POST", url, headers=headers, json=payload)

        if resp.status_code != 200:
            raise OneDriveUploaderError(
                f"Failed to create upload session: HTTP {resp.status_code} — {resp.text[:200]}"
            )

        upload_url = resp.json().get("uploadUrl")
        if not upload_url:
            raise OneDriveUploaderError("Upload session response missing 'uploadUrl'.")

        return upload_url

    # -------------------------------------------------------------------------
    # HTTP helpers
    # -------------------------------------------------------------------------

    def _request_with_backoff(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make an HTTP request, retrying on 429 with Retry-After or exponential backoff."""
        for attempt in range(MAX_RETRIES):
            resp = requests.request(method, url, **kwargs)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning(
                    f"Rate limited (429). Waiting {retry_after}s before retry "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})."
                )
                time.sleep(retry_after)
                continue

            return resp

        raise OneDriveUploaderError(
            f"Request failed after {MAX_RETRIES} retries due to rate limiting: {url}"
        )

    # -------------------------------------------------------------------------
    # Path helpers
    # -------------------------------------------------------------------------

    def _build_path(self, course_name: str, folder_path: str, filename: str) -> str:
        """
        Construct the full OneDrive path:
            D2L Sync / {course_name} / {folder_path} / {filename}
        Each segment is sanitized for OneDrive compatibility.
        """
        safe_course = _sanitize_path_segment(course_name)
        safe_folder_parts = [
            _sanitize_path_segment(p) for p in folder_path.split("/") if p.strip()
        ]
        safe_filename = _sanitize_path_segment(filename)

        parts = [self._root_folder, safe_course] + safe_folder_parts + [safe_filename]
        return "/".join(p for p in parts if p)


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _sanitize_path_segment(name: str) -> str:
    """
    Sanitize a single path segment for OneDrive.
    OneDrive segment rules:
    - Cannot contain: \\ / : * ? \" < > |
    - Cannot end with a space or period
    - Maximum 255 characters per segment
    """
    # Replace illegal characters with underscore
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    # Collapse multiple spaces/underscores
    name = re.sub(r"[ _]{2,}", " ", name).strip()
    # Remove trailing periods
    name = name.rstrip(".")
    # Truncate to 200 chars (leave room for extensions)
    if len(name) > 200:
        suffix = Path(name).suffix
        stem = name[: 200 - len(suffix)]
        name = stem + suffix
    return name
