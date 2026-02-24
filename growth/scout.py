"""ArticleScout — find hot articles by tag and state.

Discovers engagement opportunities: rising articles (gaining traction),
fresh articles (brand new, author still online), and hot articles
(trending over past N days).

Filters out articles we already engaged with and our own articles.
"""

import logging
import random

from growth.client import DevToClient, DevToError
from growth.config import GrowthConfig

logger = logging.getLogger(__name__)

# Number of random tags to sample from dev.to each cycle
TAGS_PER_CYCLE = 10


def fetch_random_tags(client: DevToClient, sample_size: int = TAGS_PER_CYCLE) -> list[str]:
    """Fetch all tags from dev.to API and randomly sample.

    Each cycle gets a different random slice of the full tag pool,
    maximizing article diversity across runs.
    Falls back to config defaults if API fails.
    """
    all_tags: list[str] = []
    try:
        for page in range(1, 8):  # ~700 tags max
            tags = client.get_tags(page=page, per_page=100)
            if not tags:
                break
            all_tags.extend(t.get("name", "") for t in tags if t.get("name"))
    except DevToError as e:
        logger.warning("Failed to fetch tags from API: %s. Using defaults.", e)
        return []

    if not all_tags:
        return []

    sample = random.sample(all_tags, min(sample_size, len(all_tags)))
    logger.info("Sampled %d random tags from %d available.", len(sample), len(all_tags))
    return sample


class ArticleScout:
    """Finds articles worth engaging with on dev.to.

    Prioritizes:
    1. Rising articles — gaining traction NOW
    2. Fresh articles — brand new, author likely online
    3. Hot articles — trending, high visibility
    """

    def __init__(self, client: DevToClient, config: GrowthConfig) -> None:
        self.client = client
        self.config = config
        self._cycle_tags: list[str] | None = None

    @property
    def cycle_tags(self) -> list[str]:
        """Random tags for this cycle. Fetched once, cached per instance."""
        if self._cycle_tags is None:
            self._cycle_tags = fetch_random_tags(self.client)
            if not self._cycle_tags:
                self._cycle_tags = self.config.target_tags
                logger.info("Using %d fallback tags from config.", len(self._cycle_tags))
        return self._cycle_tags

    def find_rising_articles(
        self, tags: list[str] | None = None, count: int = 10,
    ) -> list[dict]:
        """Find articles in 'rising' state across target tags.

        Rising = gaining reactions fast. Authors are engaged.
        Dedupes across tags (same article can appear in multiple tags).
        """
        tags = tags or self.cycle_tags
        seen_ids: set[int] = set()
        results: list[dict] = []

        for tag in tags:
            if len(results) >= count:
                break
            try:
                articles = self.client.get_articles(
                    tag=tag, state="rising", per_page=5,
                )
                for article in articles:
                    aid = article.get("id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        results.append(article)
            except DevToError as e:
                logger.warning("Failed to fetch rising articles for tag '%s': %s", tag, e)
                continue

        # Sort by reaction count (highest first)
        results.sort(
            key=lambda a: a.get("positive_reactions_count", 0), reverse=True,
        )
        logger.info("Found %d rising articles across %d tags.", len(results), len(tags))
        return results[:count]

    def find_fresh_articles(
        self, tags: list[str] | None = None, count: int = 10,
    ) -> list[dict]:
        """Find newest articles — react while author is still online.

        Fresh articles have the highest chance of the author seeing
        our reaction notification immediately.
        """
        tags = tags or self.cycle_tags
        seen_ids: set[int] = set()
        results: list[dict] = []

        for tag in tags:
            if len(results) >= count:
                break
            try:
                articles = self.client.get_articles(
                    tag=tag, state="fresh", per_page=5,
                )
                for article in articles:
                    aid = article.get("id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        results.append(article)
            except DevToError as e:
                logger.warning("Failed to fetch fresh articles for tag '%s': %s", tag, e)
                continue

        logger.info("Found %d fresh articles across %d tags.", len(results), len(tags))
        return results[:count]

    def find_hot_articles(
        self, tags: list[str] | None = None, top: int = 1, count: int = 10,
    ) -> list[dict]:
        """Find trending articles from the last N days.

        Good for commenting — these articles have high visibility.
        """
        tags = tags or self.cycle_tags
        seen_ids: set[int] = set()
        results: list[dict] = []

        for tag in tags:
            if len(results) >= count:
                break
            try:
                articles = self.client.get_articles(
                    tag=tag, top=top, per_page=10,
                )
                for article in articles:
                    aid = article.get("id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        results.append(article)
            except DevToError as e:
                logger.warning("Failed to fetch hot articles for tag '%s': %s", tag, e)
                continue

        results.sort(
            key=lambda a: a.get("positive_reactions_count", 0), reverse=True,
        )
        logger.info("Found %d hot articles (top=%d) across %d tags.", len(results), top, len(tags))
        return results[:count]

    def get_article_content(self, article_id: int) -> dict:
        """Fetch full article for Nathan to read before commenting."""
        return self.client.get_article(article_id)

    def filter_already_engaged(
        self,
        articles: list[dict],
        reacted_ids: set[int],
        commented_ids: set[int],
    ) -> list[dict]:
        """Remove articles we already reacted to or commented on."""
        filtered = [
            a for a in articles
            if a.get("id") not in reacted_ids and a.get("id") not in commented_ids
        ]
        skipped = len(articles) - len(filtered)
        if skipped > 0:
            logger.info("Filtered %d already-engaged articles.", skipped)
        return filtered

    def filter_own_articles(self, articles: list[dict]) -> list[dict]:
        """Remove our own articles — don't engage with ourselves."""
        if not self.config.devto_username:
            return articles
        filtered = [
            a for a in articles
            if a.get("user", {}).get("username") != self.config.devto_username
        ]
        skipped = len(articles) - len(filtered)
        if skipped > 0:
            logger.info("Filtered %d own articles.", skipped)
        return filtered

    def filter_quality(
        self, articles: list[dict], min_reactions: int = 0,
    ) -> list[dict]:
        """Filter articles by minimum quality (reaction count)."""
        return [
            a for a in articles
            if a.get("positive_reactions_count", 0) >= min_reactions
        ]

    def find_commentable_articles(
        self,
        commented_ids: set[int],
        reacted_ids: set[int],
        count: int = 5,
    ) -> list[dict]:
        """Find best articles to comment on right now.

        Criteria: rising/hot, our tags, not yet engaged, min reactions.
        Returns articles sorted by engagement potential.
        """
        # Combine rising + hot for best comment targets
        rising = self.find_rising_articles(count=count * 2)
        hot = self.find_hot_articles(count=count * 2)

        # Merge and dedupe
        seen_ids: set[int] = set()
        combined: list[dict] = []
        for article in rising + hot:
            aid = article.get("id")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                combined.append(article)

        # Apply all filters
        combined = self.filter_own_articles(combined)
        combined = self.filter_already_engaged(combined, reacted_ids, commented_ids)
        combined = self.filter_quality(
            combined, min_reactions=self.config.min_reactions_to_comment,
        )

        # Sort by reactions (best engagement potential first)
        combined.sort(
            key=lambda a: a.get("positive_reactions_count", 0), reverse=True,
        )
        logger.info(
            "Found %d commentable articles (after filters).", len(combined[:count]),
        )
        return combined[:count]
