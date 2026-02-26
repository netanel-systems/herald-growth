"""Attribution join logic — dev.to platform.

Matches new followers against engagement log entries to attribute
follower growth to specific engagement actions.

Calculates Follow-Back Rate (FBR): of all unique users we engaged with,
what percentage followed us back within the lookback window?

Schema version: X1-attribution (GitLab #14)
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_engagement_log(data_dir: Path, lookback_days: int) -> list[dict]:
    """Load engagement log entries from the last N days.

    Args:
        data_dir: Absolute path to data directory.
        lookback_days: Number of days to look back.

    Returns:
        List of engagement log entry dicts within the lookback window.
    """
    path = data_dir / "engagement_log.jsonl"
    if not path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    entries: list[dict] = []

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", "")
                    if ts:
                        entry_time = datetime.fromisoformat(ts)
                        if entry_time >= cutoff:
                            entries.append(entry)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError as e:
        logger.warning("Failed to read engagement_log.jsonl: %s", e)

    return entries


def _load_follower_usernames(data_dir: Path) -> set[str]:
    """Load current follower usernames from latest snapshot.

    dev.to follower snapshots store usernames in the 'usernames' field
    (set by tracker.py _save_snapshot).

    Args:
        data_dir: Absolute path to data directory.

    Returns:
        Set of follower username strings. Empty set if no snapshot.
    """
    path = data_dir / "follower_snapshots.jsonl"
    if not path.exists():
        return set()

    try:
        last_line = ""
        with open(path) as f:
            for line in f:
                if line.strip():
                    last_line = line.strip()
        if last_line:
            snapshot = json.loads(last_line)
            return set(snapshot.get("usernames", []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load follower snapshot: %s", e)

    return set()


def attribute_follow(
    follower_username: str,
    data_dir: Path,
    lookback_days: int = 7,
) -> dict:
    """Attribute a new follower to engagement actions.

    Searches the engagement log for entries where we engaged with this
    follower's content within the lookback window. Returns first-touch
    attribution and all contributing touches.

    Args:
        follower_username: Username of the new follower to attribute.
        data_dir: Absolute path to data directory.
        lookback_days: Number of days to look back for engagement entries.

    Returns:
        Dict with attribution data:
        - attributed: bool (True if engagement entries found)
        - first_touch: dict | None (earliest engagement entry)
        - all_touches: list[dict] (all matching engagement entries)
        - touch_count: int
    """
    entries = _load_engagement_log(data_dir, lookback_days)

    # Filter entries where author_username matches the new follower
    matches = [
        e for e in entries
        if e.get("author_username", "").lower() == follower_username.lower()
    ]

    if not matches:
        return {
            "attributed": False,
            "follower_username": follower_username,
            "first_touch": None,
            "all_touches": [],
            "touch_count": 0,
        }

    # Sort by timestamp (oldest first) for first-touch attribution
    matches.sort(key=lambda e: e.get("timestamp", ""))

    return {
        "attributed": True,
        "follower_username": follower_username,
        "first_touch": matches[0],
        "all_touches": matches,
        "touch_count": len(matches),
    }


def calculate_fbr(data_dir: Path, lookback_days: int = 7) -> dict:
    """Calculate Follow-Back Rate (FBR).

    FBR = (attributed followers / total unique users engaged) * 100

    Args:
        data_dir: Absolute path to data directory.
        lookback_days: Number of days to look back.

    Returns:
        Dict with:
        - fbr_percent: float (0-100)
        - attributed_followers: int
        - total_engaged_users: int
        - current_followers: int
    """
    entries = _load_engagement_log(data_dir, lookback_days)
    follower_usernames = _load_follower_usernames(data_dir)

    # Get unique usernames we engaged with
    engaged_users: set[str] = set()
    for entry in entries:
        username = entry.get("author_username", "")
        if username:
            engaged_users.add(username.lower())

    if not engaged_users:
        return {
            "fbr_percent": 0.0,
            "attributed_followers": 0,
            "total_engaged_users": 0,
            "current_followers": len(follower_usernames),
        }

    # Count how many engaged users are now our followers
    follower_usernames_lower = {u.lower() for u in follower_usernames}
    attributed = engaged_users & follower_usernames_lower

    fbr = (len(attributed) / len(engaged_users)) * 100

    logger.info(
        "FBR: %.1f%% (%d/%d engaged users followed back). Total followers: %d.",
        fbr, len(attributed), len(engaged_users), len(follower_usernames),
    )

    return {
        "fbr_percent": round(fbr, 2),
        "attributed_followers": len(attributed),
        "total_engaged_users": len(engaged_users),
        "current_followers": len(follower_usernames),
    }
