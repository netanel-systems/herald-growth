"""Tests for DevToBrowser.like_comment().

Covers:
1. Empty/invalid article_url guard
2. Invalid comment_id_code guard
3. Comment container not found
4. Like button not found within container
5. Already liked (idempotent - returns True)
6. Successful like returns True
7. BrowserLoginRequired returns False
8. PlaywrightTimeoutError during navigation returns False
"""

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


def _locator(*, visible: bool = True, timeout: bool = False, cls: str = "") -> MagicMock:
    """Build a mock Locator. .first returns self for chaining."""
    loc = MagicMock()
    loc.first = loc
    if timeout:
        loc.is_visible.side_effect = PlaywrightTimeoutError("timeout")
        loc.wait_for.side_effect = PlaywrightTimeoutError("timeout")
    else:
        loc.is_visible.return_value = visible
        loc.wait_for.return_value = None
    loc.get_attribute.return_value = cls
    return loc


# -- Test 1: Empty article_url returns False immediately --

def test_no_article_url_returns_false(browser: DevToBrowser) -> None:
    """Empty article_url returns False without touching the page."""
    result = browser.like_comment("abc12", "")
    assert result is False
    browser._page.goto.assert_not_called()


# -- Test 2: Invalid comment_id_code returns False --

def test_invalid_comment_id_code_returns_false(browser: DevToBrowser) -> None:
    """Empty, non-string, or unsafe comment_id_code returns False immediately."""
    assert browser.like_comment("", "https://dev.to/a/b") is False
    assert browser.like_comment(123, "https://dev.to/a/b") is False  # type: ignore[arg-type]
    assert browser.like_comment('abc"def', "https://dev.to/a/b") is False
    assert browser.like_comment("abc def", "https://dev.to/a/b") is False
    browser._page.goto.assert_not_called()


# -- Test 3: Comment container not found --

def test_comment_container_not_found_returns_false(browser: DevToBrowser) -> None:
    """Missing comment container returns False."""
    with patch.object(browser, "ensure_logged_in"):
        container = _locator(timeout=True)
        browser._page.locator.return_value = container

        result = browser.like_comment("abc12", "https://dev.to/a/b")

    assert result is False


# -- Test 4: Like button not found within container --

def test_like_button_not_found_returns_false(browser: DevToBrowser) -> None:
    """If all like button selectors fail, returns False."""
    with patch.object(browser, "ensure_logged_in"):
        container = _locator(visible=True)
        container.locator.return_value = _locator(visible=False)
        browser._page.locator.return_value = container

        result = browser.like_comment("abc12", "https://dev.to/a/b")

    assert result is False


# -- Test 5: Already liked returns True (idempotent) --

def test_already_liked_returns_true(browser: DevToBrowser) -> None:
    """Comment already liked returns True without clicking again."""
    with patch.object(browser, "ensure_logged_in"):
        container = _locator(visible=True)
        like_btn = _locator(visible=True, cls="comment__like-button reacted")
        container.locator.return_value = like_btn
        browser._page.locator.return_value = container

        result = browser.like_comment("abc12", "https://dev.to/a/b")

    assert result is True
    like_btn.click.assert_not_called()


# -- Test 6: Successful like returns True --

def test_successful_like_returns_true(browser: DevToBrowser) -> None:
    """Full success path: click like button, activation confirmed."""
    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_save_session"):
            container = _locator(visible=True)
            like_btn = _locator(visible=True, cls="comment__like-button")

            # After click, class should include "reacted"
            like_btn.get_attribute.side_effect = [
                "comment__like-button",  # first check: not yet liked
                "comment__like-button reacted",  # after click: liked
            ]

            container.locator.return_value = like_btn
            browser._page.locator.return_value = container

            result = browser.like_comment("abc12", "https://dev.to/a/b")

    assert result is True
    like_btn.click.assert_called_once()


# -- Test 7: BrowserLoginRequired returns False --

def test_login_required_returns_false(browser: DevToBrowser) -> None:
    """BrowserLoginRequired from ensure_logged_in() returns False."""
    with patch.object(
        browser, "ensure_logged_in", side_effect=BrowserLoginRequired("expired")
    ):
        result = browser.like_comment("abc12", "https://dev.to/a/b")

    assert result is False


# -- Test 8: PlaywrightTimeoutError during navigation returns False --

def test_playwright_timeout_returns_false(browser: DevToBrowser) -> None:
    """PlaywrightTimeoutError during page.goto() returns False gracefully."""
    with patch.object(browser, "ensure_logged_in"):
        browser._page.goto.side_effect = PlaywrightTimeoutError("nav timeout")

        result = browser.like_comment("abc12", "https://dev.to/a/b")

    assert result is False


# -- Test 9: Click dispatched but activation class not confirmed --

def test_like_click_without_activation_class_still_returns_true(
    browser: DevToBrowser,
) -> None:
    """If click dispatched but Forem uses a different indicator, still True."""
    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_save_session"):
            container = _locator(visible=True)
            like_btn = _locator(visible=True, cls="comment__like-button")

            # Class never changes to include "reacted"
            like_btn.get_attribute.return_value = "comment__like-button"

            container.locator.return_value = like_btn
            browser._page.locator.return_value = container

            result = browser.like_comment("abc12", "https://dev.to/a/b")

    assert result is True
    like_btn.click.assert_called_once()


# -- Test 10: Valid alphanumeric id_codes with dashes/underscores --

def test_valid_id_code_formats_accepted(browser: DevToBrowser) -> None:
    """id_codes with dashes and underscores are valid."""
    with patch.object(browser, "ensure_logged_in"):
        # Container not found is fine -- we just want to verify id_code validation passes
        container = _locator(timeout=True)
        browser._page.locator.return_value = container

        # These should NOT be rejected by the id_code guard
        browser.like_comment("abc-def", "https://dev.to/a/b")
        browser.like_comment("abc_def", "https://dev.to/a/b")
        browser.like_comment("34ohb", "https://dev.to/a/b")

        # page.goto was called for each valid id_code
        assert browser._page.goto.call_count == 3


# -- Test 11: Page not initialized returns False --

def test_page_not_initialized_returns_false(browser: DevToBrowser) -> None:
    """If _page is None after ensure_logged_in, returns False."""
    browser._page = None
    with patch.object(browser, "ensure_logged_in"):
        result = browser.like_comment("abc12", "https://dev.to/a/b")
    assert result is False
