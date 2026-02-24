"""dev.to (Forem) API client — growth-focused operations.

Adapted from Herald v2 client. Focused on:
- Reading articles (rising, fresh, hot)
- Reacting to articles (like, fire, raised_hands, exploding_head)
- Posting comments
- Reading comments (for performance tracking)
- Reading followers

Forem API v1 docs: https://developers.forem.com/api/v1
Auth: API key via header (dev.to/settings/extensions).
Rate limits: 30 requests per 30-second rolling window.
Reactions endpoint uses form-data, NOT JSON body.
"""

import logging
import time

import requests

from growth.config import GrowthConfig

logger = logging.getLogger(__name__)


class DevToError(Exception):
    """dev.to API error with status code and response body."""


class DevToClient:
    """Client for the dev.to Forem API v1 — growth operations.

    Handles retries, rate limiting, and dedup filtering.
    All methods return typed dicts/lists or raise DevToError.
    """

    MAX_RETRIES: int = 3
    BASE_RETRY_DELAY: float = 1.0

    def __init__(self, config: GrowthConfig) -> None:
        if not config.devto_api_key:
            raise DevToError(
                "GROWTH_DEVTO_API_KEY not set. "
                "Get one at dev.to/settings/extensions"
            )
        self.config = config
        self.base_url = config.devto_base_url
        self.headers = {
            "api-key": config.devto_api_key,
            "Accept": config.devto_api_version,
            "Content-Type": "application/json",
        }
        # Client-side throttling to proactively stay within rate limits
        self._last_read_at: float = 0.0
        self._last_write_at: float = 0.0
        logger.info("DevToClient (growth) initialized: base_url=%s", self.base_url)

    def _throttle(self, *, is_write: bool) -> None:
        """Proactive rate limiting: 1/sec writes, 3/sec reads."""
        min_interval = 1.0 if is_write else 1.0 / 3.0
        last = self._last_write_at if is_write else self._last_read_at
        now = time.monotonic()
        sleep_for = min_interval - (now - last)
        if sleep_for > 0:
            time.sleep(sleep_for)
        if is_write:
            self._last_write_at = time.monotonic()
        else:
            self._last_read_at = time.monotonic()

    def _request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list:
        """Make an API request with retry and rate limit handling.

        Retries on 429 (rate limit) with exponential backoff.
        Raises DevToError on 4xx/5xx after all retries exhausted.
        """
        url = f"{self.base_url}{endpoint}"
        is_write = method.upper() != "GET"

        for attempt in range(self.MAX_RETRIES):
            try:
                self._throttle(is_write=is_write)
                response = requests.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    json=json_data,
                    params=params,
                    timeout=self.config.request_timeout,
                )

                if response.status_code == 429:
                    wait = min(2 ** (attempt + 1), 10)
                    logger.warning(
                        "Rate limited on %s %s. Waiting %ds (attempt %d/%d).",
                        method, endpoint, wait, attempt + 1, self.MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue

                if response.status_code >= 400:
                    raise DevToError(
                        f"API error {response.status_code} on {method} {endpoint}: "
                        f"{response.text[:500]}"
                    )

                return response.json()

            except requests.RequestException as e:
                if attempt == self.MAX_RETRIES - 1:
                    raise DevToError(
                        f"Request failed after {self.MAX_RETRIES} attempts "
                        f"on {method} {endpoint}: {e}"
                    ) from e
                logger.warning(
                    "Request error on %s %s (attempt %d/%d): %s",
                    method, endpoint, attempt + 1, self.MAX_RETRIES, e,
                )
                time.sleep(self.BASE_RETRY_DELAY * (attempt + 1))

        raise DevToError(
            f"All {self.MAX_RETRIES} retries exhausted on {method} {endpoint}"
        )

    # --- Articles ---

    def get_articles(
        self,
        tag: str | None = None,
        state: str | None = None,
        top: int | None = None,
        per_page: int = 10,
        page: int = 1,
    ) -> list[dict]:
        """Fetch articles with optional filters.

        Args:
            tag: Filter by tag name.
            state: 'rising', 'fresh', or None for default.
            top: Trending over last N days (1, 7, 30, 365).
            per_page: Results per page (max 30 per Forem).
            page: Page number.

        Returns:
            List of article dicts with id, title, user, tags, etc.
        """
        params: dict = {"per_page": min(per_page, 30), "page": page}
        if tag:
            params["tag"] = tag
        if state:
            params["state"] = state
        if top is not None:
            params["top"] = top
        return self._request("GET", "/articles", params=params)

    def get_article(self, article_id: int) -> dict:
        """Fetch full article content by ID.

        Returns article dict with body_markdown, body_html, etc.
        """
        return self._request("GET", f"/articles/{article_id}")

    # --- Tags ---

    def get_tags(self, page: int = 1, per_page: int = 100) -> list[dict]:
        """Fetch available tags from dev.to."""
        params = {"page": page, "per_page": per_page}
        return self._request("GET", "/tags", params=params)

    # --- Reactions ---

    def react_to_article(
        self, article_id: int, category: str = "like"
    ) -> tuple[bool, bool]:
        """React to an article (like, fire, raised_hands, exploding_head).

        Note: reactions endpoint requires form-data, NOT JSON body.
        Returns (success, rate_limited) tuple.
        """
        url = f"{self.base_url}/reactions/toggle"
        form_data = {
            "reactable_id": article_id,
            "reactable_type": "Article",
            "category": category,
        }
        headers = {
            "api-key": self.config.devto_api_key,
            "Accept": self.config.devto_api_version,
        }
        try:
            self._throttle(is_write=True)
            response = requests.post(
                url, headers=headers, data=form_data,
                timeout=self.config.request_timeout,
            )
            if response.status_code == 429:
                logger.warning("Reactions rate limited. Will retry next cycle.")
                return False, True
            if response.status_code >= 400:
                logger.warning(
                    "React failed (%d): %s",
                    response.status_code, response.text[:200],
                )
                return False, False
            logger.info("Reacted '%s' to article %d", category, article_id)
            return True, False
        except requests.RequestException as e:
            logger.warning("React request failed: %s", e)
            return False, False

    # --- Comments ---

    def get_comments(self, article_id: int) -> list[dict]:
        """Get all comments on an article.

        Returns top-level comments with nested children.
        Each comment has: id_code, body_html, user, children, created_at.
        """
        return self._request(
            "GET", "/comments", params={"a_id": article_id},
        )

    def post_comment(
        self,
        article_id: int,
        body_markdown: str,
        parent_id: int | None = None,
    ) -> dict:
        """Post a comment on an article.

        Args:
            article_id: The article to comment on.
            body_markdown: Markdown content of the comment.
            parent_id: If replying to a comment, the parent comment's ID.
        """
        payload: dict = {
            "comment": {
                "body_markdown": body_markdown,
                "commentable_id": article_id,
                "commentable_type": "Article",
            }
        }
        if parent_id is not None:
            payload["comment"]["parent_id"] = parent_id

        logger.info(
            "Posting comment on article %d (parent=%s, length=%d)",
            article_id, parent_id, len(body_markdown),
        )
        return self._request("POST", "/comments", json_data=payload)

    # --- Followers ---

    def get_followers(self, per_page: int = 80, page: int = 1) -> list[dict]:
        """Get followers of the authenticated user.

        Returns list of follower dicts with id, username, name, etc.
        Max 80 per page per Forem docs.
        """
        return self._request(
            "GET", "/followers/users",
            params={"per_page": min(per_page, 80), "page": page},
        )

    def get_all_followers(self, max_pages: int = 20) -> list[dict]:
        """Fetch all followers with bounded pagination.

        Returns complete follower list up to max_pages * 80 followers.
        """
        all_followers: list[dict] = []
        for page in range(1, max_pages + 1):
            batch = self.get_followers(per_page=80, page=page)
            if not batch:
                break
            all_followers.extend(batch)
            if len(batch) < 80:
                break
        logger.info("Fetched %d total followers.", len(all_followers))
        return all_followers

    # --- Utilities ---

    def verify_connection(self) -> bool:
        """Verify API key works by fetching user profile."""
        try:
            self._request("GET", "/articles/me", params={"per_page": 1})
            logger.info("dev.to connection verified.")
            return True
        except DevToError as e:
            logger.exception("dev.to connection failed")
            return False
