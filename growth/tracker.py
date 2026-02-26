"""GrowthTracker — follower tracking, reciprocity, and weekly reports.

Tracks:
- Follower count over time
- New followers since last check
- Reciprocity rate (of authors we engaged, who followed back?)
- Weekly growth reports

Data stored in data/weekly_report.json and data/follower_snapshots.jsonl.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from growth.attribution import calculate_fbr
from growth.client import DevToClient, DevToError
from growth.config import GrowthConfig
from growth.learner import GrowthLearner

logger = logging.getLogger(__name__)


class GrowthTracker:
    """Tracks follower growth and engagement reciprocity."""

    def __init__(
        self,
        client: DevToClient,
        config: GrowthConfig,
        learner: GrowthLearner,
    ) -> None:
        self.client = client
        self.config = config
        self.learner = learner
        self.data_dir = config.abs_data_dir

    def check_followers(self) -> dict:
        """Get current follower count and detect new followers.

        Compares against last snapshot to find new followers.
        Returns: {current_count, new_followers: [...], previous_count}
        """
        try:
            followers = self.client.get_all_followers()
        except DevToError as e:
            logger.exception("Failed to fetch followers")
            previous = self._load_last_snapshot()
            return {
                "current_count": previous.get("count", 0),
                "new_followers": [],
                "previous_count": previous.get("count", 0),
                "error": str(e),
            }

        current_usernames = {f.get("username", "") for f in followers if f.get("username")}
        current_count = len(current_usernames)

        # Load previous snapshot
        previous = self._load_last_snapshot()
        previous_usernames = set(previous.get("usernames", []))
        previous_count = previous.get("count", 0)

        # Find new followers
        new_usernames = current_usernames - previous_usernames
        new_followers = [u for u in new_usernames if u]

        # Save new snapshot
        self._save_snapshot(current_usernames, current_count)

        if new_followers:
            logger.info(
                "New followers detected: %s (total: %d -> %d)",
                ", ".join(new_followers), previous_count, current_count,
            )
        else:
            logger.info("No new followers. Total: %d", current_count)

        return {
            "current_count": current_count,
            "new_followers": sorted(new_followers),
            "previous_count": previous_count,
        }

    def get_reciprocity_rate(self) -> dict:
        """Of authors we engaged with, what % followed back?

        Returns: {engaged_authors: N, followers_back: N, rate: 0.XX}
        """
        # Get authors we've engaged with
        engaged_authors = self.learner.get_unique_authors_engaged()

        # Get current followers
        snapshot = self._load_last_snapshot()
        follower_usernames = set(snapshot.get("usernames", []))

        # Calculate reciprocity
        followers_back = engaged_authors & follower_usernames
        rate = len(followers_back) / max(len(engaged_authors), 1)

        result = {
            "engaged_authors": len(engaged_authors),
            "followers_back": len(followers_back),
            "rate": round(rate, 4),
            "reciprocal_users": sorted(followers_back),
        }
        logger.info(
            "Reciprocity: %d/%d engaged authors followed back (%.1f%%)",
            len(followers_back), len(engaged_authors), rate * 100,
        )
        return result

    def _compute_engagement_stats(self) -> dict:
        """Compute enhanced engagement statistics from engagement log (X2).

        Returns volume breakdown, reply rate, template distribution,
        targeting stats, and account health metrics.
        """
        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return {
                "volume": {"reactions": 0, "comments": 0, "follows": 0},
                "reply_rate": 0.0,
                "template_distribution": {},
                "targeting": {"avg_target_followers": None, "avg_target_reactions": None, "avg_post_age_hours": None},
                "health": {"errors": 0},
            }

        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        reactions = 0
        comments = 0
        follows = 0
        questions_count = 0
        total_comments_for_question = 0
        template_counts: dict[str, int] = {}
        target_followers_list: list[int] = []
        target_reactions_list: list[int] = []
        target_age_list: list[float] = []
        error_count = 0

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        ts = entry.get("timestamp", "")
                        if ts < cutoff:
                            continue
                        action = entry.get("action", "")
                        if action in ("reaction", "like"):
                            reactions += 1
                        elif action == "comment":
                            comments += 1
                            total_comments_for_question += 1
                            if entry.get("comment_has_question"):
                                questions_count += 1
                            cat = entry.get("comment_template_category")
                            if cat:
                                template_counts[cat] = template_counts.get(cat, 0) + 1
                        elif action == "follow":
                            follows += 1
                        # Targeting stats
                        tf = entry.get("target_followers_at_engagement")
                        if tf is not None:
                            target_followers_list.append(int(tf))
                        tr = entry.get("target_post_reactions_at_engagement")
                        if tr is not None:
                            target_reactions_list.append(int(tr))
                        ta = entry.get("target_post_age_hours")
                        if ta is not None:
                            target_age_list.append(float(ta))
                        if entry.get("status") == "failed" or entry.get("error"):
                            error_count += 1
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError as e:
            logger.warning("Failed to read engagement log for stats: %s", e)

        reply_rate = (questions_count / total_comments_for_question * 100) if total_comments_for_question > 0 else 0.0

        return {
            "volume": {"reactions": reactions, "comments": comments, "follows": follows},
            "reply_rate": round(reply_rate, 1),
            "template_distribution": template_counts,
            "targeting": {
                "avg_target_followers": round(sum(target_followers_list) / len(target_followers_list), 1) if target_followers_list else None,
                "avg_target_reactions": round(sum(target_reactions_list) / len(target_reactions_list), 1) if target_reactions_list else None,
                "avg_post_age_hours": round(sum(target_age_list) / len(target_age_list), 1) if target_age_list else None,
            },
            "health": {"errors": error_count},
        }

    def get_weekly_report(self) -> dict:
        """Generate comprehensive weekly growth report.

        Combines engagement data, follower data, learner insights,
        FBR (X2), reply rate, template distribution, targeting stats,
        and account health.
        """
        follower_data = self.check_followers()
        reciprocity = self.get_reciprocity_rate()
        learner_summary = self.learner.generate_weekly_summary()

        # X2: Enhanced metrics
        try:
            fbr_data = calculate_fbr(self.data_dir, lookback_days=7)
        except Exception as e:
            logger.warning("FBR calculation failed: %s", e)
            fbr_data = {"fbr_percent": 0.0, "error": str(e)}

        engagement_stats = self._compute_engagement_stats()

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "followers": follower_data,
            "reciprocity": reciprocity,
            "engagement": learner_summary,
            "fbr": fbr_data,
            "engagement_stats": engagement_stats,
        }

        # Save report
        self._save_weekly_report(report)
        logger.info("Weekly report generated and saved.")
        return report

    def _load_last_snapshot(self) -> dict:
        """Load the most recent follower snapshot."""
        path = self.data_dir / "follower_snapshots.jsonl"
        if not path.exists():
            return {"usernames": [], "count": 0}
        try:
            last_line = ""
            with open(path) as f:
                for line in f:
                    if line.strip():
                        last_line = line.strip()
            if last_line:
                return json.loads(last_line)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load follower snapshot: %s", e)
        return {"usernames": [], "count": 0}

    def _save_snapshot(self, usernames: set[str], count: int) -> None:
        """Append a new follower snapshot."""
        path = self.data_dir / "follower_snapshots.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "count": count,
            "usernames": sorted(usernames),
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _save_weekly_report(self, report: dict) -> None:
        """Save the weekly report to disk. Uses atomic write."""
        from growth.storage import atomic_write_json

        path = self.data_dir / "weekly_report.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, report)
