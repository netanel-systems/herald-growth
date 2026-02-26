"""FollowEngine — follow dev.to users via Playwright browser.

The Forem API does not support follows for regular users (admin-only).
This module uses the DevToBrowser to click the Follow button on author
profile pages.

Tracks followed accounts in data/followed.json. Enforces daily cap
(200/day) and dedup against previously followed users.

Schema version: D3 (GitLab #14)
"""

import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from growth.browser import BrowserLoginRequired, DevToBrowser
from growth.config import GrowthConfig
from growth.schema import build_engagement_entry, generate_cycle_id
from growth.storage import load_json_ids, save_json_ids

logger = logging.getLogger(__name__)


class FollowEngine:
    """Follow dev.to users via Playwright browser.

    Manages follow dedup, daily cap enforcement, and engagement logging.

    Usage:
        with DevToBrowser(config) as browser:
            engine = FollowEngine(config, browser)
            summary = engine.follow_cycle(articles)
    """

    def __init__(
        self,
        config: GrowthConfig,
        browser: DevToBrowser,
    ) -> None:
        self.config = config
        self.browser = browser
        self.data_dir = config.abs_data_dir

    def load_followed_usernames(self) -> set[str]:
        """Load usernames we already followed from followed.json.

        Returns empty set if file is missing or corrupted.
        """
        path = self.data_dir / "followed.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return set(data.get("usernames", []))
            if isinstance(data, list):
                return {str(item) for item in data}
            return set()
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load followed.json: %s", e)
            return set()

    def save_followed_usernames(
        self,
        usernames: set[str],
        max_count: int = 5000,
    ) -> None:
        """Atomically save followed usernames. Bounded to max_count entries."""
        import os
        import tempfile

        path = self.data_dir / "followed.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        bounded = sorted(usernames)[-max_count:]
        content = json.dumps(
            {"usernames": bounded, "count": len(bounded)},
            indent=2,
        ) + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, suffix=".tmp", prefix=".followed_",
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info("Saved %d followed usernames.", len(bounded))

    def _count_today_follows(self) -> int:
        """Count how many follows we did today (UTC).

        Reads from engagement_log.jsonl, counting follow actions with
        today's date.
        """
        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return 0

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        count = 0
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if (
                            entry.get("action") == "follow"
                            and entry.get("platform") == "devto"
                            and entry.get("timestamp", "").startswith(today)
                        ):
                            count += 1
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            logger.warning("Failed to read engagement_log.jsonl: %s", e)

        return count

    def follow_user(
        self,
        username: str,
        profile_url: str = "",
    ) -> bool:
        """Follow a single user via browser.

        Args:
            username: dev.to username.
            profile_url: Full profile URL (e.g. https://dev.to/username).
                If empty, constructs from username.

        Returns:
            True on success, False on failure.
        """
        if not profile_url:
            profile_url = f"https://dev.to/{username}"

        return self.browser.follow_user(profile_url)

    def _log_engagement(
        self,
        username: str,
        cycle_id: str | None = None,
    ) -> None:
        """Append follow action to engagement_log.jsonl."""
        path = self.data_dir / "engagement_log.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        entry = build_engagement_entry(
            action="follow",
            platform="devto",
            target_username=username,
            target_post_id=None,
            cycle_id=cycle_id,
        )
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def follow_cycle(self, articles: list[dict]) -> dict:
        """Run a follow cycle on authors of the given articles.

        Deduplicates against followed.json, enforces daily cap,
        and logs all actions.

        Args:
            articles: List of article dicts with 'user' containing 'username'.

        Returns:
            Summary dict with followed, skipped, failed counts.
        """
        cycle_id = generate_cycle_id()
        followed_usernames = self.load_followed_usernames()
        today_follows = self._count_today_follows()
        max_per_day = self.config.max_follows_per_day

        followed_count = 0
        skipped_count = 0
        failed_count = 0

        # Extract unique usernames from articles
        seen: set[str] = set()
        targets: list[str] = []
        for article in articles:
            username = article.get("user", {}).get("username", "")
            if username and username not in seen:
                seen.add(username)
                targets.append(username)

        for username in targets:
            # Daily cap check
            if today_follows + followed_count >= max_per_day:
                logger.info(
                    "Daily follow cap reached (%d). Stopping.",
                    max_per_day,
                )
                break

            # Dedup check
            if username in followed_usernames:
                skipped_count += 1
                continue

            # Skip our own username
            if (
                self.config.devto_username
                and username.lower() == self.config.devto_username.lower()
            ):
                skipped_count += 1
                continue

            # Attempt follow
            try:
                success = self.follow_user(username)
            except BrowserLoginRequired:
                logger.error("Login required during follow cycle. Aborting.")
                break
            except Exception as e:
                logger.warning("Follow failed for @%s: %s", username, e)
                failed_count += 1
                continue

            if success:
                followed_count += 1
                followed_usernames.add(username)
                self._log_engagement(username, cycle_id=cycle_id)
                logger.info("Followed @%s.", username)
            else:
                failed_count += 1

            # Randomized delay between follows
            delay = self.config.follow_delay * random.uniform(0.7, 1.3)
            time.sleep(delay)

        # Save updated followed set
        self.save_followed_usernames(followed_usernames)

        summary = {
            "followed": followed_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "daily_total": today_follows + followed_count,
        }
        logger.info(
            "Follow cycle complete: %d followed, %d skipped, %d failed.",
            followed_count, skipped_count, failed_count,
        )
        return summary
