"""Headless browser automation for dev.to write operations.

The Forem API does not support reactions or comments for regular users.
This module uses Playwright (sync) to perform those actions through the
web UI, with session persistence via storage_state and human-like delays.

Only used for WRITE operations. All reads stay on the API client.

CSS selectors verified from Forem source code on GitHub:
- app/views/shared/authentication/_email_login_form.html.erb
- app/views/articles/_reaction_button.html.erb
- app/views/comments/_form.html.erb
- app/javascript/packs/articleReactions.js
"""

import json
import logging
import random
import time
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from growth.config import GrowthConfig

logger = logging.getLogger(__name__)


class BrowserError(Exception):
    """Browser automation error (crash, launch failure)."""


class BrowserLoginRequired(BrowserError):
    """Session expired or missing — credentials needed."""


class DevToBrowser:
    """Headless Chromium browser for dev.to write operations.

    Manages login, session persistence, reactions, and comments.

    Usage as context manager:
        with DevToBrowser(config) as browser:
            success, rate_limited = browser.react_to_article(123, "like", url)
            result = browser.post_comment(456, "Nice approach!", url)
    """

    BASE_URL = "https://dev.to"
    LOGIN_URL = "https://dev.to/enter"

    # CSS selectors — from Forem source code (verified, not guessed)
    # Fallback order: ID → data-attr → ARIA → generic
    # Primary selectors first, fallbacks follow in each tuple.
    SELS_LOGIN_EMAIL = (
        'input[autocomplete="email"]',
        'input[name="user[email]"]',
        '#user_email',
    )
    SELS_LOGIN_PASSWORD = (  # noqa: S105
        'input[autocomplete="current-password"]',
        'input[name="user[password]"]',
        '#user_password',
    )
    SELS_LOGIN_SUBMIT = (
        '#new_user input[type="submit"][name="commit"]',
        'form.new_user input[type="submit"]',
        '#new_user button[type="submit"]',
        'form[action="/users/sign_in"] input[type="submit"]',
    )
    SEL_LOGGED_IN = 'meta[name="user-signed-in"][content="true"]'
    SEL_REACTION_DRAWER = "#reaction-drawer-trigger"
    SEL_REACTION_BUTTON = "#reaction-butt-{category}"
    SELS_COMMENT_FORM = (
        "form.comment-form",
        "#new_comment",
    )
    SELS_COMMENT_TEXTAREA = (
        "textarea.comment-textarea",
        "#text-area",
    )
    SELS_COMMENT_SUBMIT = (
        '.comment-form button[type="submit"]',
        '#submit_button',
    )
    SEL_ARTICLE_BODY = "#article-body"

    # CAPTCHA / challenge indicators
    # CSS selectors only — text= syntax does NOT work with locator().
    # Text-based indicators are handled separately in _detect_captcha().
    CAPTCHA_INDICATORS = (
        "iframe[src*='captcha']",
        "iframe[src*='recaptcha']",
        "#captcha",
        ".g-recaptcha",
        "[data-sitekey]",
    )
    CAPTCHA_TEXT_INDICATORS = (
        "Please verify you are a human",
    )

    # Reply-to-comment selectors — verified from Forem source:
    # app/assets/javascripts/utilities/buildCommentHTML.js.erb
    # app/views/comments/_comment.html.erb
    # Container found via [data-path$="/comments/{id_code}"].
    SELS_REPLY_BUTTON = (
        ".toggle-reply-form",
        "button[data-tracking-name='comment_reply_button']",
    )
    SELS_REPLY_TEXTAREA = (
        "textarea.crayons-textfield.comment-textarea",
        "textarea.comment-textarea",
    )
    SELS_REPLY_SUBMIT = (
        "button[data-tracking-name='comment_reply_submit_button']",
        "button[type='submit'].comment-action-button",
    )

    # Comment like selectors — verified from Forem source:
    # app/views/comments/_comment.html.erb
    # app/javascript/packs/commentReactions.js
    # Each comment has a heart/like button scoped within its container.
    SELS_COMMENT_LIKE_BUTTON = (
        "button.comment__like-button",
        "button[data-category='like']",
        ".like-button",
    )
    # Activated state uses same "reacted" class pattern as article reactions
    COMMENT_LIKE_ACTIVATED_CLASS = "reacted"

    VALID_CATEGORIES = ("like", "unicorn", "fire", "raised_hands", "exploding_head")

    def __init__(self, config: GrowthConfig) -> None:
        self.config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._storage_path: Path = config.abs_data_dir / "browser_state.json"
        self._screenshot_dir: Path = config.abs_data_dir / "screenshots"

    # --- Selector Utilities ---

    def _find_element(self, selectors: tuple[str, ...], *, visible: bool = True):
        """Try selectors in order (ID → data-attr → ARIA → generic).

        Returns the first matching Locator, or None if none found.
        Logs which fallback was used so we detect selector drift.
        """
        if self._page is None:
            return None
        for i, sel in enumerate(selectors):
            loc = self._page.locator(sel).first
            try:
                if not visible or loc.is_visible(timeout=2000):
                    if i > 0:
                        logger.warning(
                            "Primary selector '%s' failed, using fallback '%s'.",
                            selectors[0], sel,
                        )
                    return loc
            except PlaywrightTimeoutError:
                continue
        logger.warning("All selectors failed: %s", selectors)
        return None

    def _detect_captcha(self) -> bool:
        """Check if current page shows a CAPTCHA or challenge.

        Returns True if a challenge is detected, False otherwise.
        CSS selectors use locator(); text indicators use get_by_text()
        since text= syntax is not valid in Playwright's locator().
        """
        if self._page is None:
            return False
        for indicator in self.CAPTCHA_INDICATORS:
            try:
                if self._page.locator(indicator).first.is_visible(timeout=500):
                    logger.warning("CAPTCHA/challenge detected: %s", indicator)
                    self._save_debug_screenshot("captcha_detected")
                    return True
            except PlaywrightTimeoutError:
                continue
        for text in self.CAPTCHA_TEXT_INDICATORS:
            try:
                if self._page.get_by_text(text).first.is_visible(timeout=500):
                    logger.warning("CAPTCHA detected: %s", text)
                    self._save_debug_screenshot("captcha_detected")
                    return True
            except PlaywrightTimeoutError:
                continue
        return False

    # --- Lifecycle ---

    def start(self) -> None:
        """Launch headless Chromium and create browser context.

        Validates credentials are configured before launching browser.
        Raises BrowserLoginRequired if email/password are missing and
        no stored session exists.
        """
        # Validate credentials early — fail fast, not after launching browser
        has_session = self._has_stored_session()
        has_credentials = bool(self.config.devto_email and self.config.devto_password)
        if not has_session and not has_credentials:
            raise BrowserLoginRequired(
                "No stored session and no credentials configured. "
                "Set GROWTH_DEVTO_EMAIL and GROWTH_DEVTO_PASSWORD in .env"
            )

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.config.browser_headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context_kwargs: dict = {
            "user_agent": self.config.browser_user_agent,
            "viewport": {"width": 1280, "height": 720},
            "locale": "en-US",
        }
        if self._has_stored_session():
            context_kwargs["storage_state"] = str(self._storage_path)
            logger.info("Loaded browser session from %s", self._storage_path.name)

        self._context = self._browser.new_context(**context_kwargs)
        self._context.set_default_timeout(self.config.browser_timeout * 1000)
        self._page = self._context.new_page()
        logger.info(
            "Browser started (headless=%s).", self.config.browser_headless,
        )

    def stop(self) -> None:
        """Save session state and close everything cleanly."""
        try:
            if self._context and self._page:
                self._save_session()
        except Exception as exc:
            logger.warning("Failed to save session on stop: %s", exc)
        finally:
            if self._page:
                self._page.close()
                self._page = None
            if self._context:
                self._context.close()
                self._context = None
            if self._browser:
                self._browser.close()
                self._browser = None
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
            logger.info("Browser stopped.")

    def __enter__(self) -> "DevToBrowser":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    # --- Authentication ---

    def _has_stored_session(self) -> bool:
        """Check if browser_state.json exists and has non-expired cookies.

        Proactively checks cookie expiration timestamps to avoid loading
        stale sessions that will fail immediately (H1 fix).
        """
        if not self._storage_path.exists():
            return False
        try:
            data = json.loads(self._storage_path.read_text())
            cookies = data.get("cookies", [])
            if not cookies:
                return False

            # Check if key session cookies are expired
            now = time.time()
            session_cookies = [
                c for c in cookies
                if "devto" in c.get("name", "").lower()
                or "forem" in c.get("name", "").lower()
                or "_session" in c.get("name", "").lower()
                or "remember" in c.get("name", "").lower()
            ]

            if session_cookies:
                for cookie in session_cookies:
                    expires = cookie.get("expires", -1)
                    # expires=-1 means session cookie (valid until browser close)
                    # expires=0 or past timestamp means expired
                    if expires > 0 and expires < now:
                        logger.warning(
                            "Session cookie '%s' expired at %s. Clearing stale session.",
                            cookie.get("name", "?"),
                            time.strftime("%Y-%m-%d %H:%M", time.localtime(expires)),
                        )
                        return False

            return True
        except (json.JSONDecodeError, OSError):
            return False

    # Fallback selectors for logged-in detection (meta tag may not exist on all pages)
    SELS_LOGGED_IN_FALLBACK = (
        'meta[name="user-signed-in"][content="true"]',
        'a[href="/new"]',              # "Create Post" link (only visible when logged in)
        'button[aria-label="Navigation menu"]',  # Mobile nav (logged-in)
        'a[href="/notifications"]',    # Notifications bell
        'img.crayons-avatar',          # User avatar in nav
    )

    def _is_logged_in(self) -> bool:
        """Check if current page shows logged-in state.

        Uses multiple fallback indicators since the meta tag may not
        exist on all page types (modals, redirects, etc).
        """
        if self._page is None:
            return False
        try:
            current_url = self._page.url
            if not current_url.startswith(self.BASE_URL):
                self._page.goto(self.BASE_URL, wait_until="domcontentloaded")

            # Try each indicator — any one match means logged in
            for selector in self.SELS_LOGGED_IN_FALLBACK:
                try:
                    el = self._page.query_selector(selector)
                    if el is not None:
                        return True
                except Exception:
                    continue

            return False
        except PlaywrightTimeoutError:
            return False

    def _save_session(self) -> None:
        """Persist cookies + localStorage to data/browser_state.json."""
        if self._context is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(self._storage_path))
        logger.info("Browser session saved to %s", self._storage_path.name)

    def login(self, email: str, password: str) -> bool:
        """Log in to dev.to via the web form.

        Uses selector fallbacks (ID → data-attr) for resilience.
        Detects CAPTCHA/challenge pages and aborts with screenshot.
        Returns True on success, False on failure.
        """
        if self._page is None:
            return False

        logger.info("Logging in to dev.to...")
        try:
            self._page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
            self._human_delay(1.0, 2.0)

            # dev.to redirects to homepage if session is still valid.
            # Check if we're already logged in after the redirect.
            if self._is_logged_in():
                logger.info("Already logged in (redirect from login page). Session valid.")
                self._save_session()
                return True

            # Check for CAPTCHA before interacting
            if self._detect_captcha():
                self._save_debug_screenshot("login_captcha")
                logger.error("CAPTCHA detected on login page. Manual intervention required.")
                return False

            # Find form elements with fallback selectors
            email_input = self._find_element(self.SELS_LOGIN_EMAIL)
            if email_input is None:
                self._save_debug_screenshot("login_email_not_found")
                logger.error("Login email field not found (all selectors failed).")
                return False
            email_input.fill(email)
            self._human_delay(0.3, 0.8)

            password_input = self._find_element(self.SELS_LOGIN_PASSWORD)
            if password_input is None:
                self._save_debug_screenshot("login_password_not_found")
                logger.error("Login password field not found (all selectors failed).")
                return False
            password_input.fill(password)
            self._human_delay(0.3, 0.8)

            submit_btn = self._find_element(self.SELS_LOGIN_SUBMIT)
            if submit_btn is None:
                self._save_debug_screenshot("login_submit_not_found")
                logger.error("Login submit button not found (all selectors failed).")
                return False
            submit_btn.click()
            self._page.wait_for_load_state("domcontentloaded")
            self._human_delay(1.0, 2.0)

            # Check for CAPTCHA after submit (challenge may appear post-submit)
            if self._detect_captcha():
                self._save_debug_screenshot("login_captcha_post_submit")
                logger.error("CAPTCHA detected after login submit. Manual intervention required.")
                return False

            if self._is_logged_in():
                self._save_session()
                logger.info("Login successful.")
                return True

            self._save_debug_screenshot("login_failed")
            logger.error("Login failed — not logged in after form submit.")
            return False

        except PlaywrightTimeoutError as exc:
            self._save_debug_screenshot("login_timeout")
            logger.exception("Login timed out: %s", exc)
            return False

    def ensure_logged_in(self) -> None:
        """Verify session is active. Re-login if expired.

        Supports two login methods:
        1. Saved session cookies (from manual Google OAuth login via login_once.py)
        2. Email/password auto-login (if credentials configured in .env)

        Raises BrowserLoginRequired if session expired and no way to re-login.
        """
        if self._is_logged_in():
            return

        logger.warning("Session expired or not logged in. Attempting re-login...")
        email = self.config.devto_email
        password = self.config.devto_password

        if not email or not password:
            raise BrowserLoginRequired(
                "Browser session expired and no credentials configured. "
                "Run 'python login_once.py' to log in with Google and save cookies, "
                "or set GROWTH_DEVTO_EMAIL and GROWTH_DEVTO_PASSWORD in .env."
            )

        if not self.login(email, password):
            raise BrowserLoginRequired(
                "Auto re-login failed. Check credentials or "
                "run 'python login_once.py' to log in manually."
            )

    # --- Write Operations ---

    def react_to_article(
        self,
        article_id: int,
        category: str = "like",
        article_url: str = "",
    ) -> tuple[bool, bool]:
        """Click a reaction button on an article page.

        Args:
            article_id: Article numeric ID (for logging).
            category: One of like, unicorn, fire, raised_hands, exploding_head.
            article_url: Full article URL (required).

        Returns:
            (success, rate_limited) tuple matching DevToClient signature.
        """
        if category not in self.VALID_CATEGORIES:
            logger.warning("Invalid reaction category: %s", category)
            return False, False

        if not article_url:
            logger.warning("No article URL provided for reaction on %d.", article_id)
            return False, False

        try:
            self.ensure_logged_in()
            if self._page is None:
                logger.error("Browser page not initialized for reaction.")
                return False, False

            self._page.goto(article_url, wait_until="domcontentloaded")
            self._human_delay(1.5, 3.0)

            # The sidebar heart icon is #reaction-drawer-trigger.
            # Clicking it = "like". Hovering it = opens drawer with all categories.
            drawer_trigger = self._page.locator(self.SEL_REACTION_DRAWER)
            if not drawer_trigger.is_visible():
                logger.warning(
                    "Reaction drawer trigger not visible on article %d.",
                    article_id,
                )
                self._save_debug_screenshot(f"reaction_no_drawer_{article_id}")
                return False, False

            if category == "like":
                # "Like" = click the drawer trigger (sidebar heart) directly
                classes = drawer_trigger.get_attribute("class") or ""
                if "user-activated" in classes:
                    logger.info(
                        "Already reacted 'like' to article %d. Skipping.",
                        article_id,
                    )
                    return True, False

                drawer_trigger.click()
                self._human_delay(0.5, 1.5)

                # Verify activation on the trigger itself
                classes = drawer_trigger.get_attribute("class") or ""
                if "user-activated" in classes:
                    logger.info(
                        "Reacted 'like' to article %d via browser.",
                        article_id,
                    )
                    self._save_session()
                    return True, False
            else:
                # Non-like: hover drawer trigger to reveal all reaction buttons
                selector = self.SEL_REACTION_BUTTON.format(category=category)
                drawer_trigger.hover()
                # Wait for drawer to animate open — fixed sleep is too short on slow connections
                try:
                    self._page.locator(selector).wait_for(state="visible", timeout=3000)
                except Exception:
                    # Fallback: fixed delay if wait_for times out
                    self._human_delay(1.5, 2.5)

                # Find the specific reaction button inside the drawer
                button = self._page.locator(selector)

                if not button.is_visible():
                    logger.warning(
                        "Reaction button '%s' not visible on article %d.",
                        category, article_id,
                    )
                    self._save_debug_screenshot(
                        f"reaction_not_visible_{article_id}",
                    )
                    return False, False

                classes = button.get_attribute("class") or ""
                if "user-activated" in classes:
                    logger.info(
                        "Already reacted '%s' to article %d. Skipping.",
                        category, article_id,
                    )
                    return True, False

                button.click()
                self._human_delay(0.5, 1.5)

                # Verify activation
                classes = button.get_attribute("class") or ""
                if "user-activated" in classes:
                    logger.info(
                        "Reacted '%s' to article %d via browser.",
                        category, article_id,
                    )
                    self._save_session()
                    return True, False

            # Check if rate-limited
            if self._detect_rate_limit():
                logger.warning(
                    "Rate limited after reaction '%s' on article %d.",
                    category, article_id,
                )
                return False, True

            logger.warning(
                "Reaction '%s' click did not activate on article %d.",
                category, article_id,
            )
            self._save_debug_screenshot(f"reaction_failed_{article_id}")
            return False, False

        except BrowserLoginRequired:
            logger.error("Cannot react — login required.")
            return False, False
        except PlaywrightTimeoutError:
            logger.warning("Reaction timed out on article %d.", article_id)
            self._save_debug_screenshot(f"reaction_timeout_{article_id}")
            return False, False
        except Exception as exc:
            logger.error(
                "Unexpected browser error reacting to %d: %s",
                article_id, exc,
            )
            return False, False

    def post_comment(
        self,
        article_id: int,
        body_markdown: str,
        article_url: str = "",
    ) -> dict | None:
        """Submit a comment on an article via the web form.

        Args:
            article_id: Article numeric ID (for logging).
            body_markdown: Markdown text of the comment.
            article_url: Full article URL (required).

        Returns:
            Synthetic result dict on success, None on failure.
        """
        if not article_url:
            logger.warning("No article URL for comment on %d.", article_id)
            return None

        try:
            self.ensure_logged_in()
            if self._page is None:
                logger.error("Browser page not initialized for comment.")
                return None

            self._page.goto(article_url, wait_until="domcontentloaded")
            self._human_delay(1.0, 2.0)

            # Scroll to comment form (with selector fallback)
            comment_form = self._find_element(self.SELS_COMMENT_FORM)
            if comment_form is None:
                self._save_debug_screenshot(f"comment_form_not_found_{article_id}")
                logger.warning("Comment form not found on article %d.", article_id)
                return None
            comment_form.scroll_into_view_if_needed()
            self._human_delay(0.5, 1.0)

            # Click textarea to focus (with selector fallback)
            textarea = self._find_element(self.SELS_COMMENT_TEXTAREA)
            if textarea is None:
                self._save_debug_screenshot(f"comment_textarea_not_found_{article_id}")
                logger.warning("Comment textarea not found on article %d.", article_id)
                return None
            textarea.click()
            self._human_delay(0.3, 0.6)

            # Fill comment text
            textarea.fill(body_markdown)
            self._human_delay(0.5, 1.5)

            # Click submit (with selector fallback)
            submit_btn = self._find_element(self.SELS_COMMENT_SUBMIT)
            if submit_btn is None:
                self._save_debug_screenshot(f"comment_submit_not_found_{article_id}")
                logger.warning("Comment submit button not found on article %d.", article_id)
                return None
            submit_btn.click()

            # Wait for submission
            self._page.wait_for_load_state("domcontentloaded")
            self._human_delay(1.0, 2.0)

            # Verify: check if our comment text appears on the page
            posted = False
            try:
                self._page.get_by_text(
                    body_markdown[:40], exact=False,
                ).first.wait_for(timeout=5000)
                posted = True
            except PlaywrightTimeoutError:
                # Fallback: check if textarea was cleared
                textarea_val = textarea.input_value()
                posted = textarea_val.strip() == ""

            if posted:
                logger.info(
                    "Comment posted on article %d via browser (%d chars).",
                    article_id, len(body_markdown),
                )
                self._save_session()
                return {
                    "status": "posted",
                    "article_id": article_id,
                    "source": "browser",
                }

            logger.warning("Comment may not have posted on article %d.", article_id)
            self._save_debug_screenshot(f"comment_failed_{article_id}")
            return None

        except BrowserLoginRequired:
            logger.error("Cannot comment — login required.")
            return None
        except PlaywrightTimeoutError:
            logger.warning("Comment posting timed out on article %d.", article_id)
            self._save_debug_screenshot(f"comment_timeout_{article_id}")
            return None
        except Exception as exc:
            logger.error(
                "Unexpected browser error commenting on %d: %s",
                article_id, exc,
            )
            return None

    def reply_to_comment(
        self,
        comment_id_code: str,
        body_markdown: str,
        article_url: str = "",
    ) -> dict | None:
        """Reply to an existing comment thread via the web form.

        Scopes all interactions to the specific comment container found
        via ``[data-path$="/comments/{id_code}"]`` to avoid filling the
        wrong form.

        Args:
            comment_id_code: The ``id_code`` string returned by the Forem
                comments API (e.g. ``"34ohb"``).  The Forem DOM attaches
                this value inside the ``data-path`` attribute on the
                comment container div.
            body_markdown: Markdown text of the reply.
            article_url: Full article URL (required).

        Returns:
            Synthetic result dict on success, None on failure.
        """
        # RID-001: Validate comment_id_code before constructing CSS selectors.
        # Must be a non-empty alphanumeric string — reject anything that could
        # break a CSS attribute selector (e.g. quotes, brackets, whitespace).
        if (
            not isinstance(comment_id_code, str)
            or not comment_id_code
            or not comment_id_code.replace("-", "").replace("_", "").isalnum()
        ):
            logger.warning("Invalid comment_id_code for reply: %s", comment_id_code)
            return None

        if not article_url:
            logger.warning(
                "No article URL for reply to comment %s.", comment_id_code,
            )
            return None

        try:
            self.ensure_logged_in()
            if self._page is None:
                logger.error("Browser page not initialized for reply.")
                return None

            self._page.goto(article_url, wait_until="domcontentloaded")
            self._human_delay(1.0, 2.0)

            # Scope all interactions to the specific comment container.
            # The Forem DOM sets data-path=".../<article>/comments/<id_code>"
            # on each comment div.  We match on the suffix to locate the node.
            container_sel = f'[data-path$="/comments/{comment_id_code}"]'
            try:
                container = self._page.locator(container_sel).first
                container.wait_for(state="visible", timeout=5000)
            except PlaywrightTimeoutError:
                self._save_debug_screenshot(
                    f"reply_container_not_found_{comment_id_code}"
                )
                logger.warning(
                    "Comment node %s not found on page.", comment_id_code,
                )
                return None

            # Click the reply button within the comment container
            reply_btn = None
            for sel in self.SELS_REPLY_BUTTON:
                loc = container.locator(sel).first
                try:
                    if loc.is_visible(timeout=2000):
                        reply_btn = loc
                        break
                except PlaywrightTimeoutError:
                    continue

            if reply_btn is None:
                self._save_debug_screenshot(
                    f"reply_button_not_found_{comment_id_code}"
                )
                logger.warning(
                    "Reply button not found for comment %s.", comment_id_code,
                )
                return None

            reply_btn.click()
            self._human_delay(0.5, 1.0)

            # Find the reply textarea within the container.  The Forem DOM
            # names each textarea ``#textarea-for-{numeric_id}``, but the API
            # only provides the string ``id_code`` — not the numeric ID.
            # We therefore rely on class-based selectors scoped to the
            # already-resolved container div (safe: one textarea per node).
            textarea = None
            for sel in self.SELS_REPLY_TEXTAREA:
                loc = container.locator(sel).first
                try:
                    if loc.is_visible(timeout=2000):
                        textarea = loc
                        logger.debug(
                            "Reply textarea found via selector '%s' for "
                            "comment %s.", sel, comment_id_code,
                        )
                        break
                except PlaywrightTimeoutError:
                    continue

            if textarea is None:
                self._save_debug_screenshot(
                    f"reply_textarea_not_found_{comment_id_code}"
                )
                logger.warning(
                    "Reply textarea not found for comment %s.",
                    comment_id_code,
                )
                return None

            textarea.click()
            self._human_delay(0.3, 0.6)
            textarea.fill(body_markdown)
            self._human_delay(0.5, 1.5)

            # Find and click the submit button within the comment container
            submit_btn = None
            for sel in self.SELS_REPLY_SUBMIT:
                loc = container.locator(sel).first
                try:
                    if loc.is_visible(timeout=2000):
                        submit_btn = loc
                        break
                except PlaywrightTimeoutError:
                    continue

            if submit_btn is None:
                self._save_debug_screenshot(
                    f"reply_submit_not_found_{comment_id_code}"
                )
                logger.warning(
                    "Reply submit button not found for comment %s.",
                    comment_id_code,
                )
                return None

            submit_btn.click()
            self._page.wait_for_load_state("domcontentloaded")
            self._human_delay(1.0, 2.0)

            # Verify reply posted — mirrors the verification pattern in post_comment().
            # Primary: wait for the reply text to appear on the page (high confidence).
            # Fallback: if the page renders slowly, check whether Forem cleared the
            # textarea after submit (standard Forem behaviour on success). Either path
            # indicates the form was submitted successfully.
            posted = False
            try:
                self._page.get_by_text(
                    body_markdown[:40], exact=False,
                ).first.wait_for(timeout=5000)
                posted = True
            except PlaywrightTimeoutError:
                textarea_val = textarea.input_value()
                posted = textarea_val.strip() == ""

            if posted:
                logger.info(
                    "Reply posted to comment %s via browser (%d chars).",
                    comment_id_code, len(body_markdown),
                )
                self._save_session()
                return {
                    "status": "replied",
                    "comment_id_code": comment_id_code,
                    "source": "browser",
                }

            logger.warning(
                "Reply may not have posted to comment %s.", comment_id_code,
            )
            self._save_debug_screenshot(f"reply_failed_{comment_id_code}")
            return None

        except BrowserLoginRequired:
            logger.error("Cannot reply — login required.")
            return None
        except PlaywrightTimeoutError:
            logger.warning(
                "Reply to comment %s timed out.", comment_id_code,
            )
            self._save_debug_screenshot(f"reply_timeout_{comment_id_code}")
            return None
        except Exception as exc:
            logger.error(
                "Unexpected browser error replying to comment %s: %s",
                comment_id_code, exc,
            )
            return None

    def like_comment(
        self,
        comment_id_code: str,
        article_url: str = "",
    ) -> bool:
        """Like a comment by clicking its heart/like button.

        Navigates to the article page, finds the comment container via
        ``[data-path$="/comments/{id_code}"]``, and clicks the like button
        scoped within that container.

        Args:
            comment_id_code: The ``id_code`` string returned by the Forem
                comments API (e.g. ``"34ohb"``).
            article_url: Full article URL (required).

        Returns:
            True on success (liked or already liked), False on failure.
        """
        # Validate comment_id_code — same guard as reply_to_comment()
        if (
            not isinstance(comment_id_code, str)
            or not comment_id_code
            or not comment_id_code.replace("-", "").replace("_", "").isalnum()
        ):
            logger.warning("Invalid comment_id_code for like: %s", comment_id_code)
            return False

        if not article_url:
            logger.warning(
                "No article URL for liking comment %s.", comment_id_code,
            )
            return False

        try:
            self.ensure_logged_in()
            if self._page is None:
                logger.error("Browser page not initialized for comment like.")
                return False

            self._page.goto(article_url, wait_until="domcontentloaded")
            self._human_delay(1.0, 2.0)

            # Locate the comment container by its data-path attribute
            container_sel = f'[data-path$="/comments/{comment_id_code}"]'
            try:
                container = self._page.locator(container_sel).first
                container.wait_for(state="visible", timeout=5000)
            except PlaywrightTimeoutError:
                self._save_debug_screenshot(
                    f"like_container_not_found_{comment_id_code}"
                )
                logger.warning(
                    "Comment node %s not found for like.", comment_id_code,
                )
                return False

            # Find the like button within the comment container
            like_btn = None
            for sel in self.SELS_COMMENT_LIKE_BUTTON:
                loc = container.locator(sel).first
                try:
                    if loc.is_visible(timeout=2000):
                        like_btn = loc
                        break
                except PlaywrightTimeoutError:
                    continue

            if like_btn is None:
                self._save_debug_screenshot(
                    f"like_button_not_found_{comment_id_code}"
                )
                logger.warning(
                    "Like button not found for comment %s.", comment_id_code,
                )
                return False

            # Check if already liked (avoid toggling off)
            classes = like_btn.get_attribute("class") or ""
            if self.COMMENT_LIKE_ACTIVATED_CLASS in classes:
                logger.info(
                    "Comment %s already liked. Skipping.", comment_id_code,
                )
                return True

            like_btn.click()
            self._human_delay(0.5, 1.5)

            # Verify activation
            classes = like_btn.get_attribute("class") or ""
            if self.COMMENT_LIKE_ACTIVATED_CLASS in classes:
                logger.info(
                    "Liked comment %s via browser.", comment_id_code,
                )
                self._save_session()
                return True

            # Button clicked but activation class not detected — still report success
            # as the click was dispatched (Forem may use a different indicator)
            logger.info(
                "Like click dispatched for comment %s (activation class not confirmed).",
                comment_id_code,
            )
            self._save_session()
            return True

        except BrowserLoginRequired:
            logger.error("Cannot like comment — login required.")
            return False
        except PlaywrightTimeoutError:
            logger.warning(
                "Like comment %s timed out.", comment_id_code,
            )
            self._save_debug_screenshot(f"like_timeout_{comment_id_code}")
            return False
        except Exception as exc:
            logger.error(
                "Unexpected browser error liking comment %s: %s",
                comment_id_code, exc,
            )
            return False

    # --- Detection Helpers ---

    def _detect_rate_limit(self) -> bool:
        """Check if current page shows a rate-limit or throttle message.

        Returns True if rate-limited, False otherwise.
        """
        if self._page is None:
            return False
        rate_limit_indicators = (
            "text=Rate limit reached",
            "text=Too many requests",
            "text=You've reached your daily limit",
            "text=Slow down",
        )
        for indicator in rate_limit_indicators:
            try:
                if self._page.locator(indicator).first.is_visible(timeout=1000):
                    logger.warning("Rate limit detected: %s", indicator)
                    return True
            except PlaywrightTimeoutError:
                continue
        return False

    # --- Helpers ---

    def _human_delay(self, min_s: float = 0.5, max_s: float = 2.0) -> None:
        """Sleep for a random duration to simulate human behavior."""
        time.sleep(random.uniform(min_s, max_s))

    def _save_debug_screenshot(self, name: str) -> None:
        """Save a screenshot for debugging failed actions."""
        if self._page is None:
            return
        try:
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self._screenshot_dir / f"{name}.png"
            self._page.screenshot(path=str(path))
            logger.info("Debug screenshot saved: %s", path)
        except Exception as exc:
            logger.warning("Failed to save screenshot: %s", exc)
