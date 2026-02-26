"""Tests for DevToBrowser.post_comment().

Covers:
1. Empty article_url guard
2. Comment form not found
3. Comment textarea not found
4. Submit button not found
5. Successful comment post (verified via text on page)
6. Successful comment post (verified via textarea cleared)
7. BrowserLoginRequired returns None
8. PlaywrightTimeoutError during navigation returns None
9. Page not initialized returns None
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


# -- Test 1: Empty article_url returns None --

def test_no_article_url_returns_none(browser: DevToBrowser) -> None:
    """Empty article_url returns None without touching the page."""
    result = browser.post_comment(123, "A comment.", "")
    assert result is None
    browser._page.goto.assert_not_called()


# -- Test 2: Comment form not found --

def test_comment_form_not_found_returns_none(browser: DevToBrowser) -> None:
    """Missing comment form on page returns None."""
    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_find_element", return_value=None):
            result = browser.post_comment(
                123, "A comment.", "https://dev.to/a/b",
            )
    assert result is None


# -- Test 3: Textarea not found --

def test_textarea_not_found_returns_none(browser: DevToBrowser) -> None:
    """Comment textarea not found after form found returns None."""
    form_loc = _locator(visible=True)
    none_loc = None

    call_count = [0]

    def _find_element_side_effect(selectors, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return form_loc  # comment form found
        return none_loc  # textarea not found

    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_find_element", side_effect=_find_element_side_effect):
            result = browser.post_comment(
                123, "A comment.", "https://dev.to/a/b",
            )
    assert result is None


# -- Test 4: Submit button not found --

def test_submit_not_found_returns_none(browser: DevToBrowser) -> None:
    """Submit button not found returns None."""
    form_loc = _locator(visible=True)
    textarea_loc = _locator(visible=True)
    none_loc = None

    call_count = [0]

    def _find_element_side_effect(selectors, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return form_loc
        elif call_count[0] == 2:
            return textarea_loc
        return none_loc  # submit button not found

    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_find_element", side_effect=_find_element_side_effect):
            result = browser.post_comment(
                123, "A comment.", "https://dev.to/a/b",
            )
    assert result is None


# -- Test 5: Successful post verified via text on page --

def test_successful_comment_post_text_verified(browser: DevToBrowser) -> None:
    """Full success path: comment posted and text verified on page."""
    form_loc = _locator(visible=True)
    textarea_loc = _locator(visible=True, value="")
    submit_loc = _locator(visible=True)
    verified_text = _locator(visible=True)

    call_count = [0]

    def _find_element_side_effect(selectors, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return form_loc
        elif call_count[0] == 2:
            return textarea_loc
        elif call_count[0] == 3:
            return submit_loc
        return None

    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_save_session"):
            with patch.object(browser, "_find_element", side_effect=_find_element_side_effect):
                browser._page.get_by_text.return_value = verified_text

                result = browser.post_comment(
                    123, "Solid approach to async patterns.",
                    "https://dev.to/a/b",
                )

    assert result is not None
    assert result["status"] == "posted"
    assert result["article_id"] == 123
    assert result["source"] == "browser"


# -- Test 6: Successful post verified via textarea cleared --

def test_successful_comment_post_textarea_cleared(browser: DevToBrowser) -> None:
    """Fallback verification: textarea was cleared after submit."""
    form_loc = _locator(visible=True)
    textarea_loc = _locator(visible=True, value="")
    submit_loc = _locator(visible=True)
    # Text verification times out, but textarea is empty = success
    timeout_text = _locator(timeout=True)

    call_count = [0]

    def _find_element_side_effect(selectors, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return form_loc
        elif call_count[0] == 2:
            return textarea_loc
        elif call_count[0] == 3:
            return submit_loc
        return None

    with patch.object(browser, "ensure_logged_in"):
        with patch.object(browser, "_save_session"):
            with patch.object(browser, "_find_element", side_effect=_find_element_side_effect):
                browser._page.get_by_text.return_value = timeout_text

                result = browser.post_comment(
                    123, "Solid approach to async patterns.",
                    "https://dev.to/a/b",
                )

    assert result is not None
    assert result["status"] == "posted"


# -- Test 7: BrowserLoginRequired returns None --

def test_login_required_returns_none(browser: DevToBrowser) -> None:
    """BrowserLoginRequired from ensure_logged_in() returns None."""
    with patch.object(
        browser, "ensure_logged_in", side_effect=BrowserLoginRequired("expired")
    ):
        result = browser.post_comment(
            123, "A comment.", "https://dev.to/a/b",
        )
    assert result is None


# -- Test 8: PlaywrightTimeoutError during navigation returns None --

def test_playwright_timeout_returns_none(browser: DevToBrowser) -> None:
    """PlaywrightTimeoutError during page.goto() returns None gracefully."""
    with patch.object(browser, "ensure_logged_in"):
        browser._page.goto.side_effect = PlaywrightTimeoutError("nav timeout")

        result = browser.post_comment(
            123, "A comment.", "https://dev.to/a/b",
        )
    assert result is None


# -- Test 9: Page not initialized returns None --

def test_page_not_initialized_returns_none(browser: DevToBrowser) -> None:
    """If _page is None after ensure_logged_in, returns None."""
    browser._page = None
    with patch.object(browser, "ensure_logged_in"):
        result = browser.post_comment(
            123, "A comment.", "https://dev.to/a/b",
        )
    assert result is None
