"""
d2l_scraper.py — D2L Brightspace login, course discovery, and content tree traversal.

Handles:
- Cookie-first login via Playwright (Duo MFA session cookie injected from env)
- Credential fallback login if cookie is expired
- Course discovery via Valence API with HTML scraping fallback
- Content tree traversal via Valence content/toc API
- File download via authenticated requests session
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests not installed. Run: pip install -r requirements.txt")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sys.exit("ERROR: Playwright not installed. Run: pip install -r requirements.txt && playwright install chromium")

log = logging.getLogger(__name__)

D2L_BASE_URL = "https://learn.uwaterloo.ca"
CONTENT_TOC_TIMEOUT_MS = 30_000
DUO_WAIT_SECONDS = 180


class D2LScraperError(Exception):
    pass


class DuoExpiredError(D2LScraperError):
    pass


class D2LScraper:
    def __init__(self, settings: dict, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.temp_dir = Path(settings.get("temp_dir", "/tmp/d2l_sync"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # These are initialized in login()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._session = None  # requests.Session with D2L cookies

    # -------------------------------------------------------------------------
    # Login
    # -------------------------------------------------------------------------

    def login(self):
        """Log into D2L using stored session cookie. Falls back to credentials if expired."""
        log.info("Starting D2L login...")

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()

        # Try cookie-based login first
        cookie_json = os.environ.get("D2L_SESSION_COOKIE")
        if cookie_json:
            try:
                cookies = json.loads(cookie_json)
                self._context.add_cookies(cookies)
                log.info(f"Injected {len(cookies)} cookies from D2L_SESSION_COOKIE.")
            except json.JSONDecodeError as e:
                raise D2LScraperError(
                    f"D2L_SESSION_COOKIE is not valid JSON: {e}\n"
                    "Re-run scripts/cookie_helper.py locally to generate a fresh cookie."
                )

        log.info(f"Navigating to {D2L_BASE_URL}...")
        self._page.goto(D2L_BASE_URL, wait_until="networkidle", timeout=30_000)

        if self._is_authenticated():
            log.info("Cookie-based login successful.")
            self._build_requests_session()
            return

        log.warning("Cookie-based login failed (cookie may be expired). Trying credentials...")
        self._credential_login()
        self._build_requests_session()

    def _is_authenticated(self) -> bool:
        """Return True if the current page is D2L's home (not a login redirect)."""
        current_url = self._page.url
        if "login.microsoftonline.com" in current_url:
            return False
        if "/d2l/login" in current_url:
            return False
        # Look for D2L navigation element as confirmation
        try:
            self._page.locator(".d2l-navigation, [data-region='header']").wait_for(timeout=5_000)
            return True
        except PlaywrightTimeoutError:
            return False

    def _credential_login(self):
        """Fall back to email + password login via Microsoft SSO."""
        email = os.environ.get("D2L_EMAIL")
        password = os.environ.get("D2L_PASSWORD")

        if not email or not password:
            raise DuoExpiredError(
                "D2L session cookie is expired and no credentials are available.\n"
                "To fix this:\n"
                "  1. Run scripts/cookie_helper.py locally to capture a fresh Duo session cookie.\n"
                "  2. Update the D2L_SESSION_COOKIE GitHub Secret with the new cookie JSON.\n"
                "See the README Troubleshooting section for detailed steps."
            )

        log.info("Filling in Microsoft SSO credentials...")

        # Fill email
        try:
            email_input = self._page.locator("input[type='email'], input[name='loginfmt']")
            email_input.wait_for(timeout=10_000)
            email_input.fill(email)
            self._page.locator("input[type='submit'][value='Next'], button:has-text('Next')").click()
            self._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception as e:
            raise D2LScraperError(f"Failed to fill email on SSO page: {e}")

        # Fill password
        try:
            password_input = self._page.locator("input[type='password'], input[name='passwd']")
            password_input.wait_for(timeout=10_000)
            password_input.fill(password)
            self._page.locator("input[type='submit'][value='Sign in'], button:has-text('Sign in')").click()
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception as e:
            raise D2LScraperError(f"Failed to fill password on SSO page: {e}")

        # Handle Duo MFA if present
        self._handle_duo()

        # Handle 'Stay signed in?' prompt
        try:
            no_button = self._page.locator("input[value='No'], button:has-text('No')")
            no_button.wait_for(timeout=3_000)
            no_button.click()
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        if not self._is_authenticated():
            raise D2LScraperError(
                "Credential-based login failed. Check D2L_EMAIL and D2L_PASSWORD secrets."
            )

        log.info("Credential-based login successful.")

    def _handle_duo(self):
        """Wait for Duo MFA push notification approval."""
        try:
            self._page.locator("iframe[src*='duosecurity.com']").wait_for(timeout=5_000)
        except PlaywrightTimeoutError:
            log.debug("No Duo iframe detected, skipping Duo wait.")
            return

        log.info(f"Duo MFA detected. Waiting up to {DUO_WAIT_SECONDS}s for push approval...")
        start = time.time()
        while time.time() - start < DUO_WAIT_SECONDS:
            current_url = self._page.url
            if "login.microsoftonline.com" not in current_url and "duosecurity.com" not in current_url:
                log.info(f"Duo approved after {int(time.time() - start)}s.")
                return
            elapsed = int(time.time() - start)
            log.debug(f"Waiting for Duo... {DUO_WAIT_SECONDS - elapsed}s remaining")
            time.sleep(3)

        raise DuoExpiredError(
            f"Duo MFA timed out after {DUO_WAIT_SECONDS}s.\n"
            "The stored D2L session cookie has likely expired.\n"
            "Re-run scripts/cookie_helper.py locally to capture a fresh cookie."
        )

    def _build_requests_session(self):
        """Build a requests.Session using the authenticated Playwright cookies."""
        self._session = requests.Session()
        cookies = self._context.cookies()
        for cookie in cookies:
            self._session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        log.debug(f"Built requests session with {len(cookies)} cookies.")

    def close(self):
        """Clean up Playwright resources."""
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Course discovery
    # -------------------------------------------------------------------------

    def get_courses(self) -> list[dict]:
        """
        Return list of active enrolled courses: [{id, name}]
        Tries Valence API first, falls back to HTML scraping.
        """
        courses = self._get_courses_via_api()
        if courses is None:
            log.warning("Valence API unavailable. Falling back to HTML scraping.")
            courses = self._get_courses_via_html()

        if not courses:
            log.warning("No active courses found.")
            return []

        # Apply courses_filter (partial case-insensitive match)
        courses_filter = self.settings.get("courses_filter", [])
        if courses_filter:
            filtered = []
            for course in courses:
                for f in courses_filter:
                    if f.lower() in course["name"].lower():
                        filtered.append(course)
                        break
            log.info(
                f"courses_filter applied: {len(filtered)} of {len(courses)} courses selected."
            )
            courses = filtered
        else:
            log.info(f"Found {len(courses)} active courses (no filter applied).")

        return courses

    def _get_courses_via_api(self) -> Optional[list[dict]]:
        """Use the D2L Valence API to discover enrolled courses. Returns None on API failure."""
        url = f"{D2L_BASE_URL}/d2l/api/le/1.0/enrollments/myenrollments/"
        params = {"classlistRoleId": 3}  # 3 = Student role

        try:
            resp = self._session.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            log.warning(f"Valence API request failed: {e}")
            return None

        if resp.status_code in (403, 404):
            log.warning(f"Valence API returned {resp.status_code}.")
            return None
        if resp.status_code != 200:
            log.warning(f"Valence API unexpected status {resp.status_code}.")
            return None

        try:
            data = resp.json()
        except ValueError as e:
            log.warning(f"Valence API returned invalid JSON: {e}")
            return None

        courses = []
        for item in data.get("Items", []):
            org_unit = item.get("OrgUnit", {})
            access = item.get("Access", {})
            if access.get("IsActive") and org_unit.get("Id") and org_unit.get("Name"):
                courses.append({"id": org_unit["Id"], "name": org_unit["Name"]})

        log.info(f"Valence API: found {len(courses)} active courses.")
        return courses

    def _get_courses_via_html(self) -> list[dict]:
        """Scrape the D2L home page for enrolled courses."""
        log.info("Scraping D2L home page for courses...")
        self._page.goto(f"{D2L_BASE_URL}/d2l/home", wait_until="networkidle", timeout=20_000)

        courses = []
        # Look for course card links with course IDs in their hrefs
        links = self._page.locator("a[href*='/d2l/home/']").all()
        seen_ids = set()

        for link in links:
            href = link.get_attribute("href") or ""
            match = re.search(r"/d2l/home/(\d+)", href)
            if match:
                course_id = int(match.group(1))
                if course_id in seen_ids:
                    continue
                seen_ids.add(course_id)
                name = (link.get_attribute("title") or link.inner_text() or "").strip()
                name = re.sub(r"\s+", " ", name)
                if course_id and name:
                    courses.append({"id": course_id, "name": name})

        log.info(f"HTML scraping: found {len(courses)} courses.")
        return courses

    # -------------------------------------------------------------------------
    # Content tree traversal
    # -------------------------------------------------------------------------

    def get_content_tree(self, course: dict) -> list[dict]:
        """
        Return a flat list of downloadable files in a course.
        Each entry: {course_name, course_id, folder_path, filename, download_url}
        """
        course_id = course["id"]
        course_name = course["name"]
        url = f"{D2L_BASE_URL}/d2l/api/le/1.0/{course_id}/content/toc"

        log.info(f"Fetching content tree for: {course_name} (id={course_id})")

        try:
            resp = self._session.get(url, timeout=CONTENT_TOC_TIMEOUT_MS / 1000)
        except requests.Timeout:
            log.warning(f"Content tree request timed out for course {course_name}. Skipping.")
            return []
        except requests.RequestException as e:
            log.warning(f"Content tree request failed for {course_name}: {e}. Skipping.")
            return []

        if resp.status_code != 200:
            log.warning(f"Content tree returned {resp.status_code} for {course_name}. Skipping.")
            return []

        try:
            toc = resp.json()
        except ValueError as e:
            log.warning(f"Content tree invalid JSON for {course_name}: {e}. Skipping.")
            return []

        results = []
        for module in toc.get("Modules", []):
            self._walk_module(module, course_name, course_id, folder_path="", results=results)

        log.info(f"Found {len(results)} files in {course_name}.")
        return results

    def _walk_module(self, module: dict, course_name: str, course_id: int, folder_path: str, results: list):
        """Recursively walk a D2L content module, collecting downloadable file topics."""
        title = (module.get("Title") or "").strip()
        if not title:
            return

        current_path = f"{folder_path}/{title}" if folder_path else title

        # Apply exclude_folders filter
        exclude_folders = self.settings.get("exclude_folders", [])
        for excl in exclude_folders:
            if excl.lower() in current_path.lower():
                log.debug(f"Skipping excluded folder: {current_path}")
                return

        # Process topics in this module
        for topic in module.get("Topics", []):
            # TopicType 1 = File attachment
            if topic.get("TopicType") != 1:
                continue
            topic_title = (topic.get("Title") or "").strip()
            topic_url = topic.get("Url") or topic.get("url")
            if not topic_title or not topic_url:
                continue

            results.append({
                "course_name": course_name,
                "course_id": course_id,
                "folder_path": current_path,
                "filename": topic_title,
                "download_url": topic_url,
            })

        # Recurse into sub-modules
        for sub_module in module.get("Modules", []):
            self._walk_module(sub_module, course_name, course_id, current_path, results)

    # -------------------------------------------------------------------------
    # File download
    # -------------------------------------------------------------------------

    def download_file(self, file_info: dict) -> Optional[Path]:
        """
        Download a file to the temp directory.
        Returns the local Path on success, or None if the file should be skipped.
        """
        filename = file_info["filename"]
        download_url = file_info["download_url"]

        # Build absolute URL
        if download_url.startswith("http"):
            url = download_url
        else:
            url = urljoin(D2L_BASE_URL, download_url)

        # Extension filter
        allowed_exts = self.settings.get("allowed_extensions", [])
        if allowed_exts:
            suffix = Path(filename).suffix.lower()
            if suffix not in [e.lower() for e in allowed_exts]:
                log.debug(f"Skipping {filename} (extension {suffix} not in allowed list).")
                return None

        if self.dry_run:
            log.info(f"[DRY RUN] Would download: {file_info['course_name']}/{file_info['folder_path']}/{filename}")
            return None

        log.info(f"Downloading: {filename}")
        try:
            resp = self._session.get(url, stream=True, timeout=60, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"Failed to download {filename}: {e}")
            return None

        # Try to get real filename from Content-Disposition header
        content_disposition = resp.headers.get("Content-Disposition", "")
        if content_disposition:
            match = re.search(r'filename[^;=\n]*=(["\'])?(.*?)\1', content_disposition)
            if match:
                real_name = match.group(2).strip()
                if real_name:
                    filename = real_name

        # Check file size limit
        content_length = resp.headers.get("Content-Length")
        max_file_mb = self.settings.get("max_file_mb", 500)
        if max_file_mb > 0 and content_length:
            size_mb = int(content_length) / (1024 * 1024)
            if size_mb > max_file_mb:
                log.warning(
                    f"Skipping {filename}: {size_mb:.1f}MB exceeds max_file_mb={max_file_mb}."
                )
                return None

        # Sanitize filename for filesystem
        safe_filename = _sanitize_filename(filename)
        local_path = self.temp_dir / safe_filename

        # Write streamed content to temp file
        downloaded_bytes = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

        # Post-download size check (for responses without Content-Length)
        if max_file_mb > 0:
            size_mb = downloaded_bytes / (1024 * 1024)
            if size_mb > max_file_mb:
                local_path.unlink(missing_ok=True)
                log.warning(
                    f"Skipping {filename}: downloaded {size_mb:.1f}MB exceeds max_file_mb={max_file_mb}."
                )
                return None

        log.info(f"Downloaded {filename} ({downloaded_bytes / 1024:.1f} KB) → {local_path}")
        return local_path


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Remove characters illegal on common filesystems."""
    # Replace illegal characters with underscore
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    # Collapse multiple spaces/underscores
    name = re.sub(r"[ _]{2,}", "_", name)
    # Truncate to 200 chars (keep extension)
    if len(name) > 200:
        suffix = Path(name).suffix
        stem = name[: 200 - len(suffix)]
        name = stem + suffix
    return name.strip()
