"""Entry point for autonomous follow cycle on dev.to.

Cron-friendly: loads config, scouts articles, runs follow cycle via Playwright, exits 0/1.
No LLM calls -- following is mechanical (reciprocity check + browser click).

The dev.to Forem API does not support follows for regular users (admin-only).
This uses Playwright browser automation via DevToBrowser.

Usage:
    python -m growth.follower_main
"""

import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from growth.browser import DevToBrowser
from growth.client import DevToClient
from growth.config import load_config
from growth.follower import FollowEngine
from growth.scout import ArticleScout

logger = logging.getLogger(__name__)

STATE_PATH = os.environ.get(
    "GROWTH_STATE_PATH",
    str(Path.home() / "netanel" / ".nathan" / "teams" / "herald_growth" / "state.json"),
)


def _load_atomic_state():
    """Load atomic_state module at runtime.

    Deferred to avoid ImportError if atomic_state.py does not exist at
    import time (e.g. in CI/test environments).

    Returns:
        Tuple of (atomic_write_state, read_state_safe) callables.
    """
    # ---------------------------------------------------------------------------
    # atomic_state: internal shared utility (not an installable package).
    # Derive the scripts directory from NATHAN_SCRIPTS_DIR env var so this works
    # in CI/containers without coupling the code to a specific machine layout.
    # Falls back to the conventional home-directory path if the env var is unset.
    # ---------------------------------------------------------------------------
    scripts_dir = Path(
        os.environ.get(
            "NATHAN_SCRIPTS_DIR",
            str(Path.home() / "netanel" / ".nathan" / "scripts"),
        )
    )
    atomic_state_path = scripts_dir / "atomic_state.py"

    if not atomic_state_path.exists():
        raise ImportError(
            f"atomic_state module not found at {atomic_state_path}. "
            "Set NATHAN_SCRIPTS_DIR to the directory containing atomic_state.py."
        )

    spec = importlib.util.spec_from_file_location("atomic_state", atomic_state_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load atomic_state from {atomic_state_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["atomic_state"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.atomic_write_state, mod.read_state_safe


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        atomic_write_state, read_state_safe = _load_atomic_state()
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
    except Exception:
        logger.exception("Follow cycle failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
