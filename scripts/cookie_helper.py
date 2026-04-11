"""
cookie_helper.py — Run this LOCALLY (not in CI) to capture your Duo MFA session cookie.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    cp .env.example .env      # then fill in D2L_EMAIL and D2L_PASSWORD
    python scripts/cookie_helper.py

After running, copy the contents of session_cookie.json and add it as the
GitHub Secret D2L_SESSION_COOKIE in your repository settings.

You'll need to re-run this script every ~30 days when the Duo session expires.
"""

import json
import os
import sys
import time
from pathlib import Path

# Guard: never run in CI
if os.environ.get("CI"):
    sys.exit(
        "ERROR: cookie_helper.py must not run in CI.\n"
        "This script is for local use only to capture your Duo MFA session cookie.\n"
        "Use the stored D2L_SESSION_COOKIE GitHub Secret instead."
    )

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("ERROR: python-dotenv not installed. Run: pip install -r requirements.txt")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sys.exit("ERROR: Playwright not installed. Run: pip install -r requirements.txt && playwright install chromium")

# Load .env if it exists
load_dotenv()

D2L_URL = "https://learn.uwaterloo.ca"
OUTPUT_FILE = Path("session_cookie.json")
DUO_WAIT_SECONDS = 180  # 3 minutes


def get_credentials():
    email = os.environ.get("D2L_EMAIL")
    password = os.environ.get("D2L_PASSWORD")

    if not email:
        print("D2L_EMAIL not found in environment or .env file.")
        email = input("Enter your UWaterloo email: ").strip()
    if not password:
        print("D2L_PASSWORD not found in environment or .env file.")
        import getpass
        password = getpass.getpass("Enter your UWaterloo password: ")

    if not email or not password:
        sys.exit("ERROR: Email and password are required.")

    return email, password


def wait_for_duo(page, timeout_seconds=DUO_WAIT_SECONDS):
    """Poll for Duo approval. Returns True if approved, False if timed out."""
    print(f"\nDuo MFA detected. Waiting up to {timeout_seconds}s for push notification approval...")
    print("Check your phone and approve the Duo push request.\n")

    start = time.time()
    while time.time() - start < timeout_seconds:
        elapsed = int(time.time() - start)
        remaining = timeout_seconds - elapsed
        print(f"\rWaiting for Duo approval... {remaining}s remaining ", end="", flush=True)

        # Check if Duo iframe is gone (approval completed)
        try:
            duo_frame = page.frame_locator("iframe[src*='duosecurity.com']")
            # If we can still query the frame, Duo is still active
            duo_frame.locator("body").wait_for(timeout=2000)
        except Exception:
            # Iframe gone or inaccessible — Duo approved (or page navigated away)
            pass

        # More reliable: check if we've left the Microsoft login domain
        current_url = page.url
        if "login.microsoftonline.com" not in current_url and "duosecurity.com" not in current_url:
            print(f"\nDuo approved after {elapsed}s!")
            return True

        time.sleep(2)

    print(f"\nERROR: Timed out after {timeout_seconds}s waiting for Duo approval.")
    return False


def handle_stay_signed_in(page):
    """Dismiss the 'Stay signed in?' prompt if it appears."""
    try:
        no_button = page.locator("input[value='No'], button:has-text('No')")
        no_button.wait_for(timeout=3000)
        no_button.click()
        print("Dismissed 'Stay signed in?' prompt.")
    except Exception:
        pass  # Prompt didn't appear


def main():
    print("=" * 60)
    print("D2L Brightspace Session Cookie Helper")
    print("=" * 60)
    print()
    print("This script will open a browser window and log you into D2L.")
    print("Complete the Duo MFA push on your phone when prompted.")
    print()

    email, password = get_credentials()

    with sync_playwright() as p:
        print("Launching browser (this may take a few seconds)...")
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context()
        page = context.new_page()

        try:
            print(f"Navigating to {D2L_URL}...")
            page.goto(D2L_URL, wait_until="networkidle", timeout=30000)

            # Step 1: Fill in email on Microsoft SSO page
            print("Filling in email...")
            try:
                email_input = page.locator("input[type='email'], input[name='loginfmt']")
                email_input.wait_for(timeout=10000)
                email_input.fill(email)

                next_button = page.locator("input[type='submit'][value='Next'], button:has-text('Next')")
                next_button.wait_for(timeout=5000)
                next_button.click()
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception as e:
                print(f"Note: Could not fill email automatically ({e}). Please fill it manually.")

            # Step 2: Fill in password
            print("Filling in password...")
            try:
                password_input = page.locator("input[type='password'], input[name='passwd']")
                password_input.wait_for(timeout=10000)
                password_input.fill(password)

                sign_in_button = page.locator("input[type='submit'][value='Sign in'], button:has-text('Sign in')")
                sign_in_button.wait_for(timeout=5000)
                sign_in_button.click()
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                print(f"Note: Could not fill password automatically ({e}). Please fill it manually.")

            # Step 3: Handle Duo MFA
            current_url = page.url
            if "duosecurity.com" in current_url or "login.microsoftonline.com" in current_url:
                # Check for Duo iframe
                try:
                    page.locator("iframe[src*='duosecurity.com']").wait_for(timeout=5000)
                    duo_approved = wait_for_duo(page, DUO_WAIT_SECONDS)
                    if not duo_approved:
                        sys.exit(
                            "\nERROR: Duo MFA timed out.\n"
                            "Please try again and approve the push notification within 3 minutes."
                        )
                    page.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    # No Duo iframe found — might already be past Duo
                    print("Duo iframe not detected, continuing...")

            # Step 4: Handle 'Stay signed in?' prompt
            handle_stay_signed_in(page)
            page.wait_for_load_state("networkidle", timeout=15000)

            # Step 5: Verify we're logged in
            final_url = page.url
            if "login.microsoftonline.com" in final_url:
                sys.exit(
                    "\nERROR: Still on the login page. Authentication may have failed.\n"
                    "Please check your credentials or approve Duo manually."
                )

            print(f"Successfully logged in! Current URL: {final_url}")

            # Step 6: Extract all cookies
            cookies = context.cookies()
            if not cookies:
                sys.exit("ERROR: No cookies found after login. Something went wrong.")

            # Filter to cookies most relevant to D2L (include all for safety)
            d2l_cookies = [c for c in cookies if "uwaterloo.ca" in c.get("domain", "")]
            all_cookies = cookies  # Store all cookies to be safe

            print(f"Captured {len(all_cookies)} cookies ({len(d2l_cookies)} from uwaterloo.ca domain).")

            # Step 7: Write to file
            OUTPUT_FILE.write_text(json.dumps(all_cookies, indent=2))

            print()
            print("=" * 60)
            print("SUCCESS!")
            print("=" * 60)
            print(f"Session cookies saved to: {OUTPUT_FILE.resolve()}")
            print()
            print("NEXT STEPS:")
            print("1. Copy the JSON below (or open session_cookie.json)")
            print("2. In your GitHub repo: Settings → Secrets and variables → Actions")
            print("3. Create a new secret named: D2L_SESSION_COOKIE")
            print("4. Paste the entire JSON as the secret value")
            print()
            print("--- BEGIN COOKIE JSON (copy everything between the dashes) ---")
            print(OUTPUT_FILE.read_text())
            print("--- END COOKIE JSON ---")
            print()
            print("NOTE: This cookie expires in ~30 days. Re-run this script when the")
            print("      GitHub Actions sync starts failing with authentication errors.")

        except KeyboardInterrupt:
            print("\nCancelled by user.")
            sys.exit(1)
        except Exception as e:
            print(f"\nUnexpected error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
