"""ReactionEngine — react to articles on dev.to.

Standalone cron entry point. Runs every 10 minutes via Python (no LLM needed).
Finds rising + fresh articles across target tags, reacts with varied categories,
logs everything to engagement_log.jsonl.

Write operations use Playwright headless browser (Forem API doesn't support
reactions/comments for regular users — admin-only endpoints).

Usage:
    python -m growth.reactor
"""

import json
import logging
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from growth.browser import BrowserError, BrowserLoginRequired, DevToBrowser
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


def _notify_session_expired(data_dir: Path) -> None:
    """Alert Klement that browser session expired and needs re-login.

    Creates an alert file + sends desktop notification.
    """
    alert_path = data_dir / "SESSION_EXPIRED"
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    alert_path.write_text(
        f"Session expired at {datetime.now(timezone.utc).isoformat()}\n"
        "Run: cd ~/netanel/teams/herald_growth && python login_once.py\n"
    )
    logger.error(
        "SESSION EXPIRED — Run 'python login_once.py' to re-authenticate."
    )
    # Desktop notification (non-blocking, best-effort)
    try:
        subprocess.Popen(
            [
                "notify-send",
                "--urgency=critical",
                "Herald Growth: Session Expired",
                "Run 'python login_once.py' to re-login to dev.to",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # notify-send not available


def pick_reaction_category() -> str:
    """Weighted random pick of reaction category."""
    categories, weights = zip(*REACTION_WEIGHTS)
    return random.choices(categories, weights=weights, k=1)[0]


class ReactionEngine:
    """Reacts to trending dev.to articles. Runs as standalone cron job.

    Uses Playwright browser for reactions when config.use_browser is True.
    Falls back to API client (admin-only, will fail for regular users).
    """

    def __init__(self, config: GrowthConfig) -> None:
        self.config = config
        self.client = DevToClient(config)
        self.scout = ArticleScout(self.client, config)
        self.data_dir = config.abs_data_dir
        self._browser: DevToBrowser | None = None

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
        """Trim engagement log to max_engagement_log entries. Atomic write."""
        import os
        import tempfile

        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return
        lines = [l for l in path.read_text().strip().split("\n") if l.strip()]
        if len(lines) > self.config.max_engagement_log:
            trimmed = lines[-self.config.max_engagement_log:]
            content = "\n".join(trimmed) + "\n"
            fd, tmp_path = tempfile.mkstemp(
                dir=path.parent, suffix=".tmp", prefix=".engagement_",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            logger.info(
                "Trimmed engagement log: %d -> %d entries.",
                len(lines), len(trimmed),
            )

    def _start_browser(self) -> DevToBrowser:
        """Lazy-init and start the browser if not already running."""
        if self._browser is None:
            self._browser = DevToBrowser(self.config)
            self._browser.start()
        return self._browser

    def _stop_browser(self) -> None:
        """Stop browser if running."""
        if self._browser is not None:
            self._browser.stop()
            self._browser = None

    def _react_via_browser(
        self, article_id: int, category: str, article_url: str,
    ) -> tuple[bool, bool]:
        """React to an article using Playwright browser."""
        browser = self._start_browser()
        return browser.react_to_article(article_id, category, article_url)

    def _react_via_api(
        self, article_id: int, category: str,
    ) -> tuple[bool, bool]:
        """React to an article using the API (admin-only — will fail for regular users)."""
        return self.client.react_to_article(article_id, category=category)

    def run(self) -> dict:
        """Main entry point for cron. Finds articles, reacts, logs.

        Uses Playwright browser for reactions when config.use_browser is True.
        Returns summary dict with counts for monitoring.
        """
        logger.info("=== Reaction cycle starting (browser=%s) ===", self.config.use_browser)
        start = time.time()

        try:
            reacted_ids = self.load_reacted_ids()
            commented_ids = self.load_commented_ids()
            max_reactions = min(self.config.max_reactions_per_run, 20)

            # Keep sampling random tags until we have enough new articles
            seen_ids: set[int] = set()
            candidates: list[dict] = []
            max_attempts = 5  # cap retries to avoid infinite loop

            for attempt in range(max_attempts):
                # Each attempt gets fresh random tags via scout.cycle_tags
                if attempt > 0:
                    self.scout._cycle_tags = None  # force new random sample

                rising = self.scout.find_rising_articles(count=max_reactions)
                fresh = self.scout.find_fresh_articles(count=max_reactions)

                for article in rising + fresh:
                    aid = article.get("id")
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        candidates.append(article)

                candidates = self.scout.filter_own_articles(candidates)
                candidates = self.scout.filter_already_engaged(
                    candidates, reacted_ids, commented_ids,
                )

                if len(candidates) >= max_reactions:
                    break

                logger.info(
                    "Attempt %d: %d candidates (need %d). Sampling new tags...",
                    attempt + 1, len(candidates), max_reactions,
                )
            reacted_count = 0
            skipped_count = 0
            failed_count = 0
            new_reacted: set[int] = set()

            for idx, article in enumerate(candidates[:max_reactions]):
                aid = article.get("id")
                if not aid:
                    skipped_count += 1
                    continue

                category = pick_reaction_category()
                article_url = article.get("url", "")

                if self.config.use_browser:
                    if not article_url:
                        logger.warning("No URL for article %d. Skipping.", aid)
                        skipped_count += 1
                        continue
                    success, rate_limited = self._react_via_browser(
                        aid, category, article_url,
                    )
                else:
                    success, rate_limited = self._react_via_api(aid, category)

                if success:
                    reacted_count += 1
                    new_reacted.add(aid)
                    self.log_engagement("reaction", article, {
                        "category": category,
                        "method": "browser" if self.config.use_browser else "api",
                    })
                else:
                    failed_count += 1
                    if rate_limited:
                        remaining = len(candidates[:max_reactions]) - idx - 1
                        logger.warning(
                            "Rate limited on article %d. Skipping to next (%d articles remaining).",
                            aid, remaining,
                        )
                        continue
                    logger.info("Reaction failed on article %d. Continuing.", aid)

                # Delay after every attempt (success or fail) for rate-limit safety
                if idx < max_reactions - 1:
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
                "method": "browser" if self.config.use_browser else "api",
                "elapsed_seconds": round(elapsed, 1),
            }
            logger.info(
                "=== Reaction cycle complete: %d reacted, %d failed, %.1fs ===",
                reacted_count, failed_count, elapsed,
            )
            return summary

        except BrowserLoginRequired as exc:
            logger.exception("Browser login required — cycle aborted: %s", exc)
            _notify_session_expired(self.data_dir)
            return {"error": str(exc), "elapsed_seconds": round(time.time() - start, 1)}
        except BrowserError as exc:
            logger.exception("Browser error — cycle aborted: %s", exc)
            return {"error": str(exc), "elapsed_seconds": round(time.time() - start, 1)}
        finally:
            self._stop_browser()


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
        # Exit non-zero if run() returned an error (browser failures)
        if "error" in summary:
            logger.error("Cycle completed with error. Exiting non-zero for cron/monitoring.")
            sys.exit(1)
    except (DevToError, BrowserError) as e:
        logger.error("Reaction engine failed: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
