"""One-time manual login to dev.to via Google OAuth.

Opens a VISIBLE browser window. You log in manually with Google.
Session cookies are saved to data/browser_state.json.
All future headless cron runs use these cookies.

Usage:
    python login_once.py

After logging in, the script verifies the session and saves it.
Press Ctrl+C to cancel at any time.
"""

import sys
import time

from growth.browser import DevToBrowser
from growth.config import load_config


def _check_logged_in_no_redirect(browser: DevToBrowser) -> bool:
    """Check login state WITHOUT navigating away from current page.

    During OAuth flow, the browser is on accounts.google.com.
    We must NOT redirect it back to dev.to — that breaks the flow.
    Only check login meta tag when already on dev.to.
    """
    if browser._page is None:
        return False
    try:
        current_url = browser._page.url
        # Still on Google/other OAuth provider — don't touch it
        if not current_url.startswith("https://dev.to"):
            return False
        # Back on dev.to — check the logged-in meta tag
        meta = browser._page.query_selector(browser.SEL_LOGGED_IN)
        return meta is not None
    except Exception:
        return False


def main() -> None:
    config = load_config()

    # Force visible browser for manual login
    config.browser_headless = False

    print("=" * 50)
    print("  dev.to — One-Time Login")
    print("=" * 50)
    print()
    print("A browser window will open to dev.to/enter.")
    print("Log in with Google (or email). The script will")
    print("detect when you're done and save the session.")
    print()
    print("DO NOT close the browser — the script will close it.")
    print("Press Ctrl+C to cancel at any time.")
    print()

    browser = DevToBrowser(config)
    browser.start()

    try:
        browser._page.goto("https://dev.to/enter", wait_until="domcontentloaded")

        print("Waiting for you to log in...")
        print("(checking every 3 seconds, won't interrupt OAuth flow)")
        print()

        for attempt in range(120):  # 6 minutes max
            time.sleep(3)

            if _check_logged_in_no_redirect(browser):
                browser._save_session()
                print()
                print("Login successful! Session saved.")
                print(f"Cookies saved to: {browser._storage_path}")
                print()
                print("You can now run the reactor:")
                print("  python -m growth.reactor")
                print()
                print("All future headless runs will use these cookies.")
                return

            if attempt % 10 == 0 and attempt > 0:
                url = browser._page.url if browser._page else "unknown"
                domain = url.split("/")[2] if url.startswith("http") else url
                print(f"  Still waiting... ({attempt * 3}s, on {domain})")

        print()
        print("Timed out after 6 minutes. Try again: python login_once.py")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        browser.stop()


if __name__ == "__main__":
    main()
