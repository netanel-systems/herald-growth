"""GrowthLearner — tracks what works and adapts strategy.

Not a dumb reaction bot. Analyzes:
- Which comment styles get likes/replies
- Which tags yield reciprocity (authors following back)
- Which time slots get most engagement
- Stores patterns as learnings for future cycles

Data sources:
- comment_history.jsonl — our posted comments
- engagement_log.jsonl — all engagement actions
- learnings.json — accumulated insights
"""

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from growth.config import GrowthConfig

logger = logging.getLogger(__name__)


class GrowthLearner:
    """Intelligence layer for the growth engine.

    Tracks performance and generates actionable insights.
    """

    def __init__(self, config: GrowthConfig) -> None:
        self.config = config
        self.data_dir = config.abs_data_dir

    def load_learnings(self) -> list[dict]:
        """Load accumulated learnings from disk."""
        path = self.data_dir / "learnings.json"
        if not path.exists():
            return []
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load learnings.json: %s", e)
            return []

    def save_learnings(self, learnings: list[dict]) -> None:
        """Save learnings to disk, bounded to max_learnings."""
        path = self.data_dir / "learnings.json"
        # Keep most recent if over limit
        if len(learnings) > self.config.max_learnings:
            learnings = learnings[-self.config.max_learnings:]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(learnings, f, indent=2)
        logger.info("Saved %d learnings.", len(learnings))

    def store_learning(
        self, pattern: str, confidence: float, evidence: str,
    ) -> None:
        """Persist a single learning insight."""
        learnings = self.load_learnings()
        learnings.append({
            "pattern": pattern,
            "confidence": round(confidence, 2),
            "evidence": evidence,
            "discovered": datetime.now(timezone.utc).isoformat(),
        })
        self.save_learnings(learnings)
        logger.info("Stored learning: %s (confidence=%.2f)", pattern, confidence)

    def get_insights_for_prompt(self, max_insights: int = 5) -> list[str]:
        """Get top learnings as bullet points for Nathan's comment prompt.

        Returns the most recent high-confidence learnings.
        """
        learnings = self.load_learnings()
        # Sort by confidence (highest first), then recency
        learnings.sort(
            key=lambda item: (
                item.get("confidence", 0),
                item.get("discovered", ""),
            ),
            reverse=True,
        )
        insights: list[str] = []
        for learning in learnings[:max_insights]:
            pattern = learning.get("pattern", "")
            confidence = learning.get("confidence", 0)
            if pattern and confidence >= 0.5:
                insights.append(f"- {pattern} (confidence: {confidence})")
        return insights

    def get_engagement_by_tag(self) -> dict[str, dict]:
        """Analyze engagement metrics grouped by tag.

        Returns: {tag: {reactions: N, comments: N, total: N}}
        """
        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return {}

        tag_stats: dict[str, dict] = defaultdict(
            lambda: {"reactions": 0, "comments": 0, "total": 0}
        )

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping invalid JSONL line in engagement_log.jsonl")
                        continue
                    action = entry.get("action", "")
                    tags = entry.get("tags", [])
                    for tag in tags:
                        if isinstance(tag, str):
                            if action == "reaction":
                                tag_stats[tag]["reactions"] += 1
                            elif action == "comment":
                                tag_stats[tag]["comments"] += 1
                            tag_stats[tag]["total"] += 1
        except OSError as e:
            logger.warning("Failed to read engagement_log.jsonl: %s", e)

        return dict(tag_stats)

    def get_engagement_by_day(self) -> dict[str, int]:
        """Analyze engagement counts by day of week.

        Returns: {Monday: N, Tuesday: N, ...}
        """
        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return {}

        day_counts: Counter = Counter()

        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping invalid JSONL line in engagement_log.jsonl")
                        continue
                    ts = entry.get("timestamp", "")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts)
                            day_name = dt.strftime("%A")
                            day_counts[day_name] += 1
                        except ValueError:
                            continue
        except OSError as e:
            logger.warning("Failed to read engagement_log.jsonl: %s", e)

        return dict(day_counts)

    def get_comment_count(self) -> int:
        """Total comments we've posted."""
        path = self.data_dir / "comment_history.jsonl"
        if not path.exists():
            return 0
        try:
            with open(path) as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def get_reaction_count(self) -> int:
        """Total reactions we've made."""
        path = self.data_dir / "reacted.json"
        if not path.exists():
            return 0
        try:
            with open(path) as f:
                data = json.load(f)
            return data.get("count", 0)
        except (json.JSONDecodeError, OSError):
            return 0

    def get_unique_authors_engaged(self) -> set[str]:
        """Get all unique author usernames we've engaged with."""
        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return set()

        authors: set[str] = set()
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    author = entry.get("author", "")
                    if author:
                        authors.add(author)
        except OSError as e:
            logger.warning("Failed to read engagement_log.jsonl: %s", e)

        return authors

    def should_skip_tag(self, tag: str) -> bool:
        """Check if a tag should be deprioritized based on learnings.

        Returns True if the tag consistently yields zero reciprocity.
        """
        learnings = self.load_learnings()
        for learning in learnings:
            pattern = learning.get("pattern", "").lower()
            if tag.lower() in pattern and "skip" in pattern:
                return learning.get("confidence", 0) >= 0.7
        return False

    def generate_weekly_summary(self) -> dict:
        """Generate a comprehensive weekly summary.

        Returns dict with all key metrics for the weekly report.
        """
        return {
            "total_comments": self.get_comment_count(),
            "total_reactions": self.get_reaction_count(),
            "unique_authors": len(self.get_unique_authors_engaged()),
            "engagement_by_tag": self.get_engagement_by_tag(),
            "engagement_by_day": self.get_engagement_by_day(),
            "learnings_count": len(self.load_learnings()),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
