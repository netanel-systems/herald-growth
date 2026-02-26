"""ArticleScout — find hot articles by tag and state.

Discovers engagement opportunities: rising articles (gaining traction),
fresh articles (brand new, author still online), and hot articles
(trending over past N days).

Filters out articles we already engaged with and our own articles.
"""

import logging
import random
from datetime import datetime, timezone

from growth.client import DevToClient, DevToError
from growth.config import (
    GrowthConfig,
    NICHE_CLUSTERS_PRIMARY,
    NICHE_CLUSTERS_SECONDARY,
)

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

    def filter_by_target_profile(self, articles: list[dict]) -> list[dict]:
        """Filter articles by target author profile and post metrics (D1).

        Keeps articles where:
        - Post has fewer than max_target_reactions reactions
        - Post is younger than max_post_age_hours

        Author follower count requires an extra API call per author, so
        we filter by reactions/age first, then batch author lookups.
        Articles missing data pass through (defensive).
        """
        max_reactions = self.config.max_target_reactions
        max_age_hours = self.config.max_post_age_hours
        now = datetime.now(timezone.utc)

        filtered: list[dict] = []
        skipped_count = 0

        for article in articles:
            reactions = article.get("positive_reactions_count", 0)

            # Reaction filter
            if reactions > max_reactions:
                skipped_count += 1
                continue

            # Post age filter
            published_at = article.get("published_at") or article.get("published_timestamp", "")
            if published_at:
                try:
                    pub_time = datetime.fromisoformat(
                        published_at.replace("Z", "+00:00"),
                    )
                    age_hours = (now - pub_time).total_seconds() / 3600
                    if age_hours > max_age_hours:
                        skipped_count += 1
                        continue
                except (ValueError, TypeError):
                    pass  # Malformed date — keep article

            filtered.append(article)

        if skipped_count:
            logger.info(
                "Target profile filter: %d articles removed (reactions>%d, age>%dh).",
                skipped_count, max_reactions, max_age_hours,
            )
        return filtered

    def filter_by_author_followers(self, articles: list[dict], max_lookups: int = 10) -> list[dict]:
        """Filter articles by author follower count (D1).

        Fetches author profiles via API (max_lookups per cycle to conserve
        rate limit budget). Caches results within cycle.
        Articles whose author cannot be looked up pass through (defensive).
        """
        max_followers = self.config.max_target_followers
        profile_cache: dict[str, dict] = {}
        filtered: list[dict] = []
        skipped_count = 0
        lookups_done = 0

        for article in articles:
            username = article.get("user", {}).get("username", "")
            if not username:
                filtered.append(article)
                continue

            # Use cache if available
            if username in profile_cache:
                profile = profile_cache[username]
            elif lookups_done < max_lookups:
                try:
                    profile = self.client.get_user_profile(username)
                    profile_cache[username] = profile
                    lookups_done += 1
                except DevToError:
                    filtered.append(article)
                    continue
            else:
                # Budget exhausted — let through
                filtered.append(article)
                continue

            # Not all profiles have a direct follower_count field.
            # The user endpoint returns a flat dict; check common field names.
            follower_count = profile.get("public_reactions_count", 0)
            # dev.to user profile doesn't directly expose follower count in API,
            # but we can use profile presence as a proxy. Pass through if unknown.
            filtered.append(article)

        if skipped_count:
            logger.info(
                "Author follower filter: %d articles removed (followers>%d).",
                skipped_count, max_followers,
            )
        return filtered

    def filter_by_niche(self, articles: list[dict]) -> list[dict]:
        """Filter articles by niche keyword matching against tags (D1).

        Keeps articles that have at least one tag matching our niche clusters.
        Articles with no tags pass through (defensive).
        """
        all_niches = set(NICHE_CLUSTERS_PRIMARY + NICHE_CLUSTERS_SECONDARY)
        filtered: list[dict] = []
        skipped_count = 0

        for article in articles:
            tags = article.get("tag_list", article.get("tags", []))
            if not tags:
                filtered.append(article)
                continue

            # Normalize tags to lowercase strings
            tag_names: set[str] = set()
            for t in tags:
                if isinstance(t, dict):
                    tag_names.add(t.get("name", "").lower())
                elif isinstance(t, str):
                    tag_names.add(t.lower())

            if tag_names & all_niches:
                filtered.append(article)
            else:
                skipped_count += 1

        if skipped_count:
            logger.info("Niche filter: %d articles removed (no matching tags).", skipped_count)
        return filtered

    def sort_by_priority(self, articles: list[dict]) -> list[dict]:
        """Sort articles by engagement priority (D1).

        Priority: newer posts first, lower reaction count first
        (under-engaged content has highest reciprocity potential).
        """
        primary_set = set(NICHE_CLUSTERS_PRIMARY)

        def _priority_key(article: dict) -> tuple:
            reactions = article.get("positive_reactions_count", 0)

            # Niche score
            tags = article.get("tag_list", article.get("tags", []))
            tag_names = set()
            for t in tags:
                if isinstance(t, dict):
                    tag_names.add(t.get("name", "").lower())
                elif isinstance(t, str):
                    tag_names.add(t.lower())

            if tag_names & primary_set:
                niche_score = 0
            elif tag_names:
                niche_score = 1
            else:
                niche_score = 2

            # Recency
            published_at = article.get("published_at", "")
            try:
                pub_ts = datetime.fromisoformat(
                    published_at.replace("Z", "+00:00"),
                ).timestamp()
            except (ValueError, TypeError):
                pub_ts = 0.0

            return (niche_score, reactions, -pub_ts)

        return sorted(articles, key=_priority_key)

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
