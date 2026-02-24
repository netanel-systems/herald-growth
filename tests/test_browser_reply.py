"""Tests for DevToBrowser.reply_to_comment()."""

from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from growth.browser import BrowserLoginRequired, DevToBrowser
from growth.config import GrowthConfig


@pytest.fixture()
def config() -> GrowthConfig:
    """Minimal GrowthConfig for tests."""
    return GrowthConfig()


@pytest.fixture()
def browser(config: GrowthConfig, tmp_path) -> DevToBrowser:
    """DevToBrowser with mocked page (no real browser launched)."""
    b = DevToBrowser(config)
    b._page = MagicMock()
    b._context = MagicMock()
    b._storage_path = tmp_path / "browser_state.json"
    b._screenshot_dir = tmp_path / "screenshots"
    return b


def _locator(*, visible: bool = True, timeout: bool = False, value: str = "") -> MagicMock:
    """Build a mock Locator. .first returns self for chaining."""
    loc = MagicMock()
    loc.first = loc
    if timeout:
        loc.is_visible.side_effect = PlaywrightTimeoutError("timeout")
        loc.wait_for.side_effect = PlaywrightTimeoutError("timeout")
    else:
        loc.is_visible.return_value = visible
        loc.wait_for.return_value = None
    loc.input_value.return_value = value
    return loc


# ── Test 1: Empty article_url guards early ────────────────────────

def test_no_article_url_returns_none(browser: DevToBrowser) -> None:
    """Empty article_url returns None without touching the page."""
    result = browser.reply_to_comment(123, "Nice post!", "")
    assert result is None
    browser._page.goto.assert_not_called()


# ── Test 2: Comment node not found ────────────────────────────────

def test_comment_node_not_found_returns_none(browser: DevToBrowser) -> None:
    """Missing #comment-node-{id} returns None."""
    with patch.object(browser, "ensure_logged_in"):
        container = _locator(timeout=True)  # wait_for raises timeout
        browser._page.locator.return_value = container

        result = browser.reply_to_comment(123, "Nice!", "https://dev.to/a/b")

    assert result is None


# ── Test 3: Reply button not found ───────────────────────────────

def test_reply_button_not_found_returns_none(browser: DevToBrowser) -> None:
    """If all reply button selectors fail, returns None."""
    with patch.object(browser, "ensure_logged_in"):
        container = _locator(visible=True)
        # All container.locator() calls return invisible locator
        container.locator.return_value = _locator(visible=False)
        browser._page.locator.return_value = container

        result = browser.reply_to_comment(123, "Nice!", "https://dev.to/a/b")

    assert result is None


# ── Test 4: Textarea not found ───────────────────────────────────

def test_textarea_not_found_returns_none(browser: DevToBrowser) -> None:
    """If textarea is not found after clicking reply, returns None."""
    with patch.object(browser, "ensure_logged_in"):
        container = _locator(visible=True)
        reply_btn = _locator(visible=True)
        invisible = _locator(visible=False)

        def container_loc(sel: str) -> MagicMock:
            if "toggle-reply-form" in sel or "reply_button" in sel:
                return reply_btn
            return invisible  # textarea and submit not found

        container.locator.side_effect = container_loc

        def page_loc(sel: str) -> MagicMock:
            if "comment-node" in sel:
                return container
            return invisible  # textarea#textarea-for-123 not found

        browser._page.locator.side_effect = page_loc

        result = browser.reply_to_comment(123, "Nice!", "https://dev.to/a/b")

    assert result is None


# ── Test 5: Successful reply returns result dict ──────────────────

def test_successful_reply_returns_dict(browser: DevToBrowser) -> None:
    """Full success path returns dict with status='replied'."""
    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_save_session"):
            container = _locator(visible=True)
            reply_btn = _locator(visible=True)
            textarea = _locator(visible=True, value="")
            submit_btn = _locator(visible=True)
            verified_text = _locator(visible=True)

            def container_loc(sel: str) -> MagicMock:
                if "toggle-reply-form" in sel or "reply_button" in sel:
                    return reply_btn
                if "textarea" in sel:
                    return textarea
                if "submit" in sel or "comment-action-button" in sel:
                    return submit_btn
                return _locator(visible=False)

            container.locator.side_effect = container_loc

            def page_loc(sel: str) -> MagicMock:
                if "comment-node" in sel:
                    return container
                if "textarea-for" in sel:
                    return textarea
                return _locator(visible=False)

            browser._page.locator.side_effect = page_loc
            browser._page.get_by_text.return_value = verified_text

            result = browser.reply_to_comment(123, "Nice post!", "https://dev.to/a/b")

    assert result is not None
    assert result["status"] == "replied"
    assert result["comment_id"] == 123
    assert result["source"] == "browser"


# ── Test 6: Invalid comment_id guard ────────────────────────────

def test_invalid_comment_id_returns_none(browser: DevToBrowser) -> None:
    """Non-positive or non-integer comment_id returns None immediately."""
    assert browser.reply_to_comment(0, "Nice!", "https://dev.to/a/b") is None
    assert browser.reply_to_comment(-1, "Nice!", "https://dev.to/a/b") is None
    # Type hint is not enforced at runtime — callers may pass strings
    assert browser.reply_to_comment("123", "Nice!", "https://dev.to/a/b") is None  # type: ignore[arg-type]
    browser._page.goto.assert_not_called()


# ── Test 7: Submit button not found ──────────────────────────────

def test_submit_button_not_found_returns_none(browser: DevToBrowser) -> None:
    """If submit button not found after filling textarea, returns None."""
    with patch.object(browser, "ensure_logged_in"):
        container = _locator(visible=True)
        reply_btn = _locator(visible=True)
        textarea = _locator(visible=True, value="")
        no_submit = _locator(visible=False)

        def container_loc(sel: str) -> MagicMock:
            if "toggle-reply-form" in sel or "reply_button" in sel:
                return reply_btn
            if "textarea" in sel:
                return textarea
            if "submit" in sel or "comment-action-button" in sel:
                return no_submit
            return _locator(visible=False)

        container.locator.side_effect = container_loc
        browser._page.locator.return_value = container

        result = browser.reply_to_comment(123, "Nice!", "https://dev.to/a/b")

    assert result is None


# ── Test 8: BrowserLoginRequired → None ──────────────────────────

def test_login_required_returns_none(browser: DevToBrowser) -> None:
    """BrowserLoginRequired from ensure_logged_in() returns None."""
    with patch.object(
        browser, "ensure_logged_in", side_effect=BrowserLoginRequired("expired")
    ):
        result = browser.reply_to_comment(123, "Nice!", "https://dev.to/a/b")

    assert result is None


# ── Test 9: PlaywrightTimeoutError during navigation → None ───────

def test_playwright_timeout_returns_none(browser: DevToBrowser) -> None:
    """PlaywrightTimeoutError during page.goto() returns None gracefully."""
    with patch.object(browser, "ensure_logged_in"):
        browser._page.goto.side_effect = PlaywrightTimeoutError("nav timeout")

        result = browser.reply_to_comment(123, "Nice!", "https://dev.to/a/b")

    assert result is None
