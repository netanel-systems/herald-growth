"""ReactionEngine — react to articles on dev.to.

Standalone cron entry point. Runs every 30 minutes via Python (no LLM needed).
Finds rising + fresh articles across target tags, reacts with varied categories,
logs everything to engagement_log.jsonl.

Usage:
    python -m growth.reactor
"""

import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from growth.client import DevToClient, DevToError
from growth.config import GrowthConfig, load_config
from growth.scout import ArticleScout
from growth.storage import load_json_ids, save_json_ids

logger = logging.getLogger(__name__)

# Weighted reaction categories (like the plan: like 50%, fire 25%, etc.)
REACTION_WEIGHTS: list[tuple[str, int]] = [
    ("like", 50),
    ("fire", 25),
    ("raised_hands", 15),
    ("exploding_head", 10),
]


def pick_reaction_category() -> str:
    """Weighted random pick of reaction category."""
    categories, weights = zip(*REACTION_WEIGHTS)
    return random.choices(categories, weights=weights, k=1)[0]


class ReactionEngine:
    """Reacts to trending dev.to articles. Runs as standalone cron job."""

    def __init__(self, config: GrowthConfig) -> None:
        self.config = config
        self.client = DevToClient(config)
        self.scout = ArticleScout(self.client, config)
        self.data_dir = config.abs_data_dir

    def load_reacted_ids(self) -> set[int]:
        """Load article IDs we already reacted to."""
        return load_json_ids(self.data_dir / "reacted.json")

    def save_reacted_ids(self, reacted_ids: set[int]) -> None:
        """Save reacted IDs, bounded to max_reacted_history."""
        save_json_ids(
            self.data_dir / "reacted.json", reacted_ids,
            max_count=self.config.max_reacted_history,
        )

    def load_commented_ids(self) -> set[int]:
        """Load article IDs we already commented on (for filtering)."""
        return load_json_ids(self.data_dir / "commented.json")

    def log_engagement(self, action: str, article: dict, details: dict) -> None:
        """Append to engagement_log.jsonl — full audit trail."""
        path = self.data_dir / "engagement_log.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "article_id": article.get("id"),
            "article_title": article.get("title", "")[:100],
            "author": article.get("user", {}).get("username", ""),
            "tags": [t.get("name", t) if isinstance(t, dict) else t for t in article.get("tag_list", article.get("tags", []))],
            **details,
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def trim_engagement_log(self) -> None:
        """Trim engagement log to max_engagement_log entries."""
        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return
        lines = path.read_text().strip().split("\n")
        if len(lines) > self.config.max_engagement_log:
            trimmed = lines[-self.config.max_engagement_log:]
            path.write_text("\n".join(trimmed) + "\n")
            logger.info(
                "Trimmed engagement log: %d -> %d entries.",
                len(lines), len(trimmed),
            )

    def run(self) -> dict:
        """Main entry point for cron. Finds articles, reacts, logs.

        Returns summary dict with counts for monitoring.
        """
        logger.info("=== Reaction cycle starting ===")
        start = time.time()

        reacted_ids = self.load_reacted_ids()
        commented_ids = self.load_commented_ids()

        # Find articles: mix of rising + fresh
        rising = self.scout.find_rising_articles(count=self.config.max_reactions_per_run)
        fresh = self.scout.find_fresh_articles(count=self.config.max_reactions_per_run)

        # Merge, dedupe, filter
        seen_ids: set[int] = set()
        candidates: list[dict] = []
        for article in rising + fresh:
            aid = article.get("id")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                candidates.append(article)

        candidates = self.scout.filter_own_articles(candidates)
        candidates = self.scout.filter_already_engaged(
            candidates, reacted_ids, commented_ids,
        )

        # React to top N
        reacted_count = 0
        skipped_count = 0
        failed_count = 0
        new_reacted: set[int] = set()

        for article in candidates[:self.config.max_reactions_per_run]:
            aid = article.get("id")
            if not aid:
                skipped_count += 1
                continue

            category = pick_reaction_category()
            success, rate_limited = self.client.react_to_article(aid, category=category)

            if success:
                reacted_count += 1
                new_reacted.add(aid)
                self.log_engagement("reaction", article, {"category": category})
            else:
                failed_count += 1
                if rate_limited:
                    logger.info("Rate limited on article %d. Stopping early.", aid)
                    break
                logger.info("Reaction failed on article %d. Continuing.", aid)
                continue

            # Respect rate limit delay
            if reacted_count < self.config.max_reactions_per_run:
                time.sleep(self.config.reaction_delay)

        # Save updated reacted IDs
        reacted_ids.update(new_reacted)
        self.save_reacted_ids(reacted_ids)

        # Periodic log trimming
        self.trim_engagement_log()

        elapsed = time.time() - start
        summary = {
            "reacted": reacted_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "candidates": len(candidates),
            "elapsed_seconds": round(elapsed, 1),
        }
        logger.info(
            "=== Reaction cycle complete: %d reacted, %d failed, %.1fs ===",
            reacted_count, failed_count, elapsed,
        )
        return summary


def main() -> None:
    """CLI entry point for cron."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config = load_config()
        engine = ReactionEngine(config)
        summary = engine.run()
        print(json.dumps(summary, indent=2))
    except DevToError as e:
        logger.error("Reaction engine failed: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
