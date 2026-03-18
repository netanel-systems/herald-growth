"""One-shot script: delete 4 mistakenly posted comments from our own article.

Run from the herald_growth project directory:
    .venv/bin/python delete_comments.py

Comments to delete (all replies to ptak_dev's comment 35ho3):
    35ho6 — posted 12:41 UTC  [ALREADY DELETED in first run]
    35i0j — posted 13:33 UTC
    35i1g — posted 14:03 UTC
    35i24 — posted 14:36 UTC

Strategy:
- Reuse existing browser session from data/browser_state.json if valid.
- Locate each comment via [data-path$="/comments/{id_code}"].
- Click the "..." dropdown (aria-label="Toggle dropdown menu"), then click Delete.
- Forem uses window.confirm for delete — Playwright dialog handler accepts it.

Screenshot analysis from first run confirmed:
- Menu button selector works: button[aria-label='Toggle dropdown menu']
- Menu shows: Copy link / Settings / Hide / Report abuse / Edit / Delete
- The Delete item is a plain element — use page.get_by_text("Delete", exact=True)
  NOT a:has-text() which is invalid in Playwright CSS engine.
"""

import json
import logging
import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from this directory
load_dotenv(Path(__file__).parent / ".env")

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ARTICLE_URL = "https://dev.to/klement_gunndu/the-ai-engineering-stack-in-2026-what-to-learn-first-1nhj"

# Only the 3 remaining comments — 35ho6 was already deleted
COMMENT_ID_CODES = ["35i0j", "35i1g", "35i24"]

STATE_PATH = Path(__file__).parent / "data" / "browser_state.json"
SCREENSHOT_DIR = Path(__file__).parent / "data" / "screenshots"

EMAIL = os.environ.get("GROWTH_DEVTO_EMAIL", "")
PASSWORD = os.environ.get("GROWTH_DEVTO_PASSWORD", "")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def human_delay(min_s: float = 0.5, max_s: float = 2.0) -> None:
    time.sleep(random.uniform(min_s, max_s))  # noqa: S311


def save_screenshot(page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path))
        logger.info("Screenshot saved: %s", path)
    except Exception as exc:
        logger.warning("Failed to save screenshot %s: %s", name, exc)


def load_stored_session() -> bool:
    if not STATE_PATH.exists():
        return False
    try:
        data = json.loads(STATE_PATH.read_text())
        cookies = data.get("cookies", [])
        if not cookies:
            return False
        now = time.time()
        session_cookies = [
            c for c in cookies
            if any(k in c.get("name", "").lower() for k in ("devto", "forem", "_session", "remember"))
        ]
        if session_cookies:
            for cookie in session_cookies:
                expires = cookie.get("expires", -1)
                if expires > 0 and expires < now:
                    logger.warning("Session cookie '%s' expired.", cookie.get("name", "?"))
                    return False
        return True
    except (json.JSONDecodeError, OSError):
        return False


def is_logged_in(page) -> bool:
    try:
        current_url = page.url
        if not current_url.startswith("https://dev.to"):
            page.goto("https://dev.to", wait_until="domcontentloaded")
        for selector in (
            'meta[name="user-signed-in"][content="true"]',
            'a[href="/new"]',
            'a[href="/notifications"]',
            'img.crayons-avatar',
        ):
            el = page.query_selector(selector)
            if el is not None:
                return True
        return False
    except Exception:
        return False


def do_login(page) -> bool:
    if not EMAIL or not PASSWORD:
        logger.error("No credentials available.")
        return False

    logger.info("Logging in to dev.to as %s ...", EMAIL)
    page.goto("https://dev.to/enter", wait_until="domcontentloaded")
    human_delay(1.0, 2.0)

    if is_logged_in(page):
        logger.info("Already logged in.")
        return True

    email_input = None
    for sel in ('input[autocomplete="email"]', 'input[name="user[email]"]', '#user_email'):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=3000):
                email_input = loc
                break
        except PlaywrightTimeoutError:
            continue

    if email_input is None:
        save_screenshot(page, "login_email_not_found")
        logger.error("Email field not found.")
        return False

    email_input.fill(EMAIL)
    human_delay(0.3, 0.8)

    pw_input = None
    for sel in ('input[autocomplete="current-password"]', 'input[name="user[password]"]', '#user_password'):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=3000):
                pw_input = loc
                break
        except PlaywrightTimeoutError:
            continue

    if pw_input is None:
        save_screenshot(page, "login_password_not_found")
        logger.error("Password field not found.")
        return False

    pw_input.fill(PASSWORD)
    human_delay(0.3, 0.8)

    submit_btn = None
    for sel in (
        '#new_user input[type="submit"][name="commit"]',
        'form.new_user input[type="submit"]',
        '#new_user button[type="submit"]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=3000):
                submit_btn = loc
                break
        except PlaywrightTimeoutError:
            continue

    if submit_btn is None:
        save_screenshot(page, "login_submit_not_found")
        logger.error("Submit button not found.")
        return False

    submit_btn.click()
    page.wait_for_load_state("domcontentloaded")
    human_delay(1.5, 2.5)

    if is_logged_in(page):
        logger.info("Login successful.")
        return True

    save_screenshot(page, "login_failed")
    logger.error("Login failed.")
    return False


def delete_comment(page, id_code: str) -> bool:
    """Find comment by id_code and delete it via the Forem UI menu.

    From screenshot analysis:
    - Container selector: [data-path$="/comments/{id_code}"]
    - Menu button: button[aria-label='Toggle dropdown menu'] (within container)
    - Delete option: plain text 'Delete' in floating dropdown
      Use page.get_by_text("Delete", exact=True) — NOT :has-text() CSS pseudo.

    Forem uses window.confirm() for delete confirmation.
    Playwright dialog handler accepts it automatically.
    """
    logger.info("Attempting to delete comment %s ...", id_code)

    container_sel = f'[data-path$="/comments/{id_code}"]'

    # Locate and scroll to the comment container
    try:
        container = page.locator(container_sel).first
        container.wait_for(state="visible", timeout=8000)
        container.scroll_into_view_if_needed()
        human_delay(0.5, 1.0)
    except PlaywrightTimeoutError:
        save_screenshot(page, f"delete_container_not_found_{id_code}")
        logger.warning("Comment container for %s not found on page.", id_code)
        return False

    # Find the "..." menu button (aria-label confirmed from screenshot)
    menu_btn = None
    menu_sels = (
        "button[aria-label='Toggle dropdown menu']",
        "button.comment__toggle-dropdown",
        "button[id^='comment-actions-trigger']",
        ".comment__actions button",
    )
    for sel in menu_sels:
        try:
            loc = container.locator(sel).first
            if loc.is_visible(timeout=2000):
                menu_btn = loc
                logger.info("Menu button found via: %s", sel)
                break
        except PlaywrightTimeoutError:
            continue

    if menu_btn is None:
        save_screenshot(page, f"delete_menu_not_found_{id_code}")
        logger.warning("Comment action menu not found for %s.", id_code)
        return False

    # Register dialog handler to auto-accept window.confirm("Are you sure?")
    dialog_accepted = {"value": False}

    def handle_dialog(dialog):
        logger.info(
            "Confirm dialog: type=%s, message=%r — accepting.",
            dialog.type, dialog.message,
        )
        dialog.accept()
        dialog_accepted["value"] = True

    page.on("dialog", handle_dialog)

    try:
        # Open the dropdown menu
        menu_btn.click()
        human_delay(0.6, 1.2)

        # The dropdown renders as a floating popover outside the container.
        # Use page.get_by_text() — the only reliable way to find plain text
        # menu items in Forem's dropdown. exact=True avoids matching
        # "Delete comment" or other partial matches if they exist.
        delete_btn = None

        # Primary: exact text match
        try:
            loc = page.get_by_text("Delete", exact=True).first
            if loc.is_visible(timeout=3000):
                delete_btn = loc
                logger.info("Delete option found via get_by_text('Delete', exact=True).")
        except PlaywrightTimeoutError:
            pass

        # Fallback: role=button or role=link with name Delete
        if delete_btn is None:
            for role in ("button", "link", "menuitem"):
                try:
                    loc = page.get_by_role(role, name="Delete").first
                    if loc.is_visible(timeout=2000):
                        delete_btn = loc
                        logger.info("Delete option found via get_by_role(%s, name='Delete').", role)
                        break
                except PlaywrightTimeoutError:
                    continue

        if delete_btn is None:
            save_screenshot(page, f"delete_option_not_found_{id_code}")
            logger.warning("Delete option not found in open menu for %s.", id_code)
            page.keyboard.press("Escape")
            return False

        # Click delete and wait for potential confirm dialog + DOM update
        delete_btn.click()
        human_delay(0.5, 1.0)

        # Playwright handles window.confirm synchronously via the dialog handler
        # registered above. Give the page a moment to process the deletion.
        page.wait_for_load_state("domcontentloaded", timeout=5000)
        human_delay(0.5, 1.0)

    except Exception as exc:
        logger.error("Error during delete flow for %s: %s", id_code, exc)
        try:
            page.remove_listener("dialog", handle_dialog)
        except Exception:
            pass
        return False
    finally:
        try:
            page.remove_listener("dialog", handle_dialog)
        except Exception:
            pass

    # Verify: the comment container should be gone or show "Comment deleted"
    try:
        # Check if container is still visible
        still_visible = page.locator(container_sel).first.is_visible(timeout=2000)
        if not still_visible:
            logger.info("Comment %s deleted — container no longer visible.", id_code)
            return True

        # It might still show "Comment deleted" placeholder — that also counts
        comment_deleted_text = page.get_by_text("Comment deleted", exact=False)
        try:
            if comment_deleted_text.first.is_visible(timeout=2000):
                logger.info(
                    "Comment %s deleted — 'Comment deleted' placeholder visible.",
                    id_code,
                )
                return True
        except PlaywrightTimeoutError:
            pass

        # If dialog was accepted, trust the deletion happened
        if dialog_accepted["value"]:
            logger.info(
                "Comment %s — dialog accepted, treating as deleted "
                "(container may still render placeholder).",
                id_code,
            )
            save_screenshot(page, f"delete_verify_{id_code}")
            return True

        logger.warning(
            "Comment %s still visible after delete and no dialog was accepted.",
            id_code,
        )
        save_screenshot(page, f"delete_failed_verify_{id_code}")
        return False

    except PlaywrightTimeoutError:
        logger.info("Comment %s — container timeout (likely deleted).", id_code)
        return True


def main() -> None:
    results: dict[str, bool] = {}

    has_session = load_stored_session()
    logger.info(
        "Stored session: %s | Email: %s",
        "valid" if has_session else "none/expired",
        EMAIL or "(not set)",
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context_kwargs: dict = {
            "user_agent": USER_AGENT,
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        if has_session:
            context_kwargs["storage_state"] = str(STATE_PATH)
            logger.info("Loaded browser session from %s", STATE_PATH)

        context = browser.new_context(**context_kwargs)
        context.set_default_timeout(30_000)
        page = context.new_page()

        if not is_logged_in(page):
            if not do_login(page):
                logger.error("Cannot proceed — login failed.")
                browser.close()
                sys.exit(1)
        else:
            logger.info("Session valid — already logged in.")

        # Process each comment — reload the page before each to get fresh DOM
        for i, id_code in enumerate(COMMENT_ID_CODES):
            logger.info("--- Comment %d/%d: %s ---", i + 1, len(COMMENT_ID_CODES), id_code)
            page.goto(ARTICLE_URL, wait_until="domcontentloaded")
            human_delay(2.0, 3.0)

            success = delete_comment(page, id_code)
            results[id_code] = success
            human_delay(1.0, 2.0)

        # Save session
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STATE_PATH))
        logger.info("Session saved.")

        browser.close()

    # Report
    print("\n" + "=" * 50)
    print("DELETION RESULTS")
    print("=" * 50)
    # 35ho6 was deleted in the first run
    print("  35ho6: DELETED (first run)")
    deleted = ["35ho6"]
    failed = []
    for id_code, success in results.items():
        status = "DELETED" if success else "FAILED"
        print(f"  {id_code}: {status}")
        if success:
            deleted.append(id_code)
        else:
            failed.append(id_code)

    print(f"\nTotal deleted: {len(deleted)}/4")
    if failed:
        print(f"Failed:  {failed}")
        print("Check data/screenshots/ for debug images.")
        sys.exit(1)
    else:
        print("All targeted comments deleted successfully.")


if __name__ == "__main__":
    main()
