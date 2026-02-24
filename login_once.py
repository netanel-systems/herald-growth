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


def main() -> None:
    config = load_config()

    # Force visible browser for manual login
    config.browser_headless = False

    print("Opening dev.to login page in a visible browser...")
    print("Log in with Google. The script will detect when you're logged in.")
    print()

    browser = DevToBrowser(config)
    browser.start()

    try:
        browser._page.goto("https://dev.to/enter", wait_until="domcontentloaded")

        print("Waiting for you to log in...")
        print("(checking every 3 seconds)")
        print()

        for attempt in range(120):  # 6 minutes max
            time.sleep(3)
            if browser._is_logged_in():
                browser._save_session()
                print()
                print("Login successful! Session saved.")
                print(f"Cookies saved to: {browser._storage_path}")
                print()
                print("You can now close this and run the reactor:")
                print("  python -m growth.reactor")
                print()
                print("All future headless runs will use these cookies.")
                return

            if attempt % 10 == 0 and attempt > 0:
                print(f"  Still waiting... ({attempt * 3}s elapsed)")

        print()
        print("Timed out after 6 minutes. Try again: python login_once.py")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        browser.stop()


if __name__ == "__main__":
    main()
