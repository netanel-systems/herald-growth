"""Engagement sequence state machine -- dev.to platform.

Manages per-target engagement state for the like->comment->conditional follow
sequence. Tracks touchpoints per username and enforces:
- Like before comment (must have liked before commenting)
- Follow only after target replies to our comment
- 14-day cooldown after 3 unreciprocated touchpoints

State persists to data/engagement_targets.json between cycles.

Schema version: D5 (GitLab #14)
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Cooldown after N unreciprocated touchpoints
MAX_UNRECIPROCATED_TOUCHPOINTS: int = 3
# Cooldown duration in days
COOLDOWN_DAYS: int = 14


class EngagementState:
    """Per-target engagement state for like->comment->conditional follow flow.

    Each target username maps to a state dict:
    {
        "liked_at": "ISO-8601" | None,
        "commented_at": "ISO-8601" | None,
        "target_replied": bool,
        "followed_at": "ISO-8601" | None,
        "touchpoint_count": int,
        "cooldown_until": "ISO-8601" | None,
    }
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._path = data_dir / "engagement_targets.json"
        self._targets: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        """Load engagement state from disk."""
        if not self._path.exists():
            self._targets = {}
            return
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                self._targets = data
            else:
                logger.warning(
                    "engagement_targets.json: unexpected format %s, starting fresh.",
                    type(data).__name__,
                )
                self._targets = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load engagement_targets.json: %s", e)
            self._targets = {}

    def save(self) -> None:
        """Atomically save engagement state to disk."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self._targets, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, suffix=".tmp", prefix=".engagement_",
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _get_target(self, username: str) -> dict:
        """Get or create target state entry."""
        if username not in self._targets:
            self._targets[username] = {
                "liked_at": None,
                "commented_at": None,
                "target_replied": False,
                "followed_at": None,
                "touchpoint_count": 0,
                "cooldown_until": None,
            }
        return self._targets[username]

    def record_like(self, username: str) -> None:
        """Record that we liked a post by this target author."""
        target = self._get_target(username)
        target["liked_at"] = datetime.now(timezone.utc).isoformat()
        target["touchpoint_count"] = target.get("touchpoint_count", 0) + 1
        self._check_cooldown(username)
        self.save()

    def record_comment(self, username: str) -> None:
        """Record that we commented on a post by this target author."""
        target = self._get_target(username)
        target["commented_at"] = datetime.now(timezone.utc).isoformat()
        target["touchpoint_count"] = target.get("touchpoint_count", 0) + 1
        self._check_cooldown(username)
        self.save()

    def record_target_reply(self, username: str) -> None:
        """Record that the target author replied to our comment.

        This unlocks the follow action for this target.
        """
        target = self._get_target(username)
        target["target_replied"] = True
        # Reset cooldown since they are reciprocating
        target["cooldown_until"] = None
        self.save()

    def record_follow(self, username: str) -> None:
        """Record that we followed this target author."""
        target = self._get_target(username)
        target["followed_at"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def has_liked(self, username: str) -> bool:
        """Check if we have liked a post by this author."""
        target = self._targets.get(username, {})
        return target.get("liked_at") is not None

    def has_commented(self, username: str) -> bool:
        """Check if we have commented on a post by this author."""
        target = self._targets.get(username, {})
        return target.get("commented_at") is not None

    def should_comment(self, username: str) -> bool:
        """Check if we should comment on this author's post.

        Requires: we have liked first and target is not deprioritized.
        """
        if self.should_deprioritize(username):
            return False
        return self.has_liked(username)

    def should_follow(self, username: str) -> bool:
        """Check if we should follow this target author.

        Requires: target has replied to our comment (reciprocity signal).
        Also requires: not already followed and not deprioritized.
        """
        target = self._targets.get(username, {})
        if not target:
            return False
        if target.get("followed_at") is not None:
            return False  # Already followed
        if self.should_deprioritize(username):
            return False
        return target.get("target_replied", False)

    def should_deprioritize(self, username: str) -> bool:
        """Check if this target should be deprioritized (cooldown active).

        Returns True if target has reached MAX_UNRECIPROCATED_TOUCHPOINTS
        without reciprocating and cooldown period has not elapsed.
        """
        target = self._targets.get(username, {})
        if not target:
            return False

        # Check active cooldown
        cooldown_until = target.get("cooldown_until")
        if cooldown_until:
            try:
                cooldown_dt = datetime.fromisoformat(cooldown_until)
                if datetime.now(timezone.utc) < cooldown_dt:
                    return True
                # Cooldown expired -- reset
                target["cooldown_until"] = None
                target["touchpoint_count"] = 0
                return False
            except (ValueError, TypeError):
                pass

        return False

    def _check_cooldown(self, username: str) -> None:
        """Set cooldown if touchpoints reach threshold without reciprocity."""
        target = self._targets.get(username, {})
        if not target:
            return
        if target.get("target_replied", False):
            return  # Reciprocating -- no cooldown
        touchpoints = target.get("touchpoint_count", 0)
        if touchpoints >= MAX_UNRECIPROCATED_TOUCHPOINTS:
            from datetime import timedelta
            cooldown_end = datetime.now(timezone.utc) + timedelta(days=COOLDOWN_DAYS)
            target["cooldown_until"] = cooldown_end.isoformat()
            logger.info(
                "Cooldown set for %s: %d unreciprocated touchpoints. "
                "Deprioritized until %s.",
                username, touchpoints, cooldown_end.isoformat()[:10],
            )

    def get_target_state(self, username: str) -> dict | None:
        """Get the full state dict for a target (for debugging/reporting)."""
        return self._targets.get(username)

    def get_all_targets(self) -> dict[str, dict]:
        """Get all target states (for reporting)."""
        return dict(self._targets)
