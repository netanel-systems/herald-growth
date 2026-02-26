"""Entry point for autonomous follow cycle on dev.to.

Cron-friendly: loads config, scouts articles, runs follow cycle via Playwright, exits 0/1.
No LLM calls -- following is mechanical (reciprocity check + browser click).

The dev.to Forem API does not support follows for regular users (admin-only).
This uses Playwright browser automation via DevToBrowser.

Usage:
    python -m growth.follower_main
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from growth.browser import DevToBrowser
from growth.client import DevToClient
from growth.config import load_config
from growth.follower import FollowEngine
from growth.scout import ArticleScout

sys.path.insert(0, str(Path.home() / "netanel" / ".nathan" / "scripts"))
from atomic_state import atomic_write_state, read_state_safe  # noqa: E402

logger = logging.getLogger(__name__)

STATE_PATH = str(
    Path.home() / "netanel" / ".nathan" / "teams" / "herald_growth" / "state.json"
)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        config = load_config()
        devto_client = DevToClient(config)
        scout = ArticleScout(devto_client, config)

        # Scout articles from multiple feeds for follow candidates
        rising = scout.find_rising_articles(count=20)
        fresh = scout.find_fresh_articles(count=20)

        # Merge and deduplicate
        seen_ids: set[int] = set()
        articles: list[dict] = []
        for article in rising + fresh:
            aid = article.get("id")
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                articles.append(article)

        logger.info("Scouted %d unique articles for follow candidates.", len(articles))

        # Run the follow cycle inside a browser context
        with DevToBrowser(config) as browser:
            engine = FollowEngine(config, browser)
            summary = engine.follow_cycle(articles)

        # Update team state.json with follow stats
        state = read_state_safe(STATE_PATH)
        total_follows = (state.get("total_follows_given") or 0) + summary["followed"]
        state["total_follows_given"] = total_follows
        state["last_follow_cycle"] = datetime.now(timezone.utc).isoformat()
        state["last_follow_summary"] = summary
        atomic_write_state(STATE_PATH, state)

        print(json.dumps(summary, indent=2))
    except Exception as e:
        logger.error("Follow cycle failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
