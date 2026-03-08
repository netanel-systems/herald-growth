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
                        # Normalize to UTC so naive datetimes (no offset) can be
                        # compared against the UTC cutoff without a TypeError.
                        if entry_time.tzinfo is None:
                            entry_time = entry_time.replace(tzinfo=timezone.utc)
                        if entry_time >= cutoff:
                            entries.append(entry)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError as e:
        logger.warning("Failed to read engagement_log.jsonl: %s", e)

    return entries


def _load_follower_snapshots(data_dir: Path) -> list[dict]:
    """Load all follower snapshots, sorted oldest-first.

    Each snapshot has at minimum a 'timestamp' and 'usernames' field
    (set by tracker.py _save_snapshot).

    Args:
        data_dir: Absolute path to data directory.

    Returns:
        List of snapshot dicts sorted by timestamp ascending. Empty list if
        no snapshots exist or the file cannot be read.
    """
    path = data_dir / "follower_snapshots.jsonl"
    if not path.exists():
        return []

    snapshots: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snap = json.loads(line)
                    snapshots.append(snap)
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("Failed to load follower snapshots: %s", e)
        return []

    def _snap_ts(s: dict) -> datetime:
        ts = s.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    snapshots.sort(key=_snap_ts)
    return snapshots


def _load_follower_usernames(data_dir: Path) -> set[str]:
    """Load current follower usernames from latest snapshot.

    dev.to follower snapshots store usernames in the 'usernames' field
    (set by tracker.py _save_snapshot).

    Args:
        data_dir: Absolute path to data directory.

    Returns:
        Set of follower username strings. Empty set if no snapshot.
    """
    snapshots = _load_follower_snapshots(data_dir)
    if snapshots:
        return set(snapshots[-1].get("usernames", []))
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

    # Sort by timestamp (oldest first) for first-touch attribution.
    # Parse to datetime so mixed ISO-8601 forms (with/without offset) sort
    # correctly instead of relying on lexicographic string ordering.
    def _parse_ts(e: dict) -> datetime:
        ts = e.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    matches.sort(key=_parse_ts)

    return {
        "attributed": True,
        "follower_username": follower_username,
        "first_touch": matches[0],
        "all_touches": matches,
        "touch_count": len(matches),
    }


def calculate_fbr(data_dir: Path, lookback_days: int = 7) -> dict:
    """Calculate Follow-Back Rate (FBR).

    FBR = (new followers gained during lookback window who we engaged with
           / total unique users we engaged with during the window) * 100

    Uses follower *deltas* rather than the latest snapshot so that users
    who were already following before the lookback window are not counted
    as follow-backs.  A baseline snapshot is determined as the last snapshot
    recorded before the window start; any username present in the current
    snapshot but absent in the baseline is treated as a new follower.

    Args:
        data_dir: Absolute path to data directory.
        lookback_days: Number of days to look back.

    Returns:
        Dict with:
        - fbr_percent: float (0-100)
        - attributed_followers: int (engaged users who newly followed back)
        - total_engaged_users: int
        - current_followers: int (total followers right now)
    """
    entries = _load_engagement_log(data_dir, lookback_days)
    snapshots = _load_follower_snapshots(data_dir)

    window_start = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # Determine baseline: last snapshot recorded before the window start.
    # Any snapshot within or after the window is part of the "current" view.
    baseline_usernames: set[str] = set()
    current_usernames: set[str] = set()

    if snapshots:
        def _snap_ts(s: dict) -> datetime:
            ts = s.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                return datetime.min.replace(tzinfo=timezone.utc)

        pre_window = [s for s in snapshots if _snap_ts(s) < window_start]
        if pre_window:
            baseline_usernames = {u.lower() for u in pre_window[-1].get("usernames", [])}

        current_usernames = {u.lower() for u in snapshots[-1].get("usernames", [])}

    # New followers = in current snapshot but NOT in baseline
    new_followers = current_usernames - baseline_usernames

    # Get unique usernames we engaged with during the window
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
            "current_followers": len(current_usernames),
        }

    # Count engaged users who are also new followers (delta-based)
    attributed = engaged_users & new_followers

    fbr = (len(attributed) / len(engaged_users)) * 100

    logger.info(
        "FBR: %.1f%% (%d/%d engaged users followed back). "
        "New followers this window: %d. Total followers: %d.",
        fbr, len(attributed), len(engaged_users),
        len(new_followers), len(current_usernames),
    )

    return {
        "fbr_percent": round(fbr, 2),
        "attributed_followers": len(attributed),
        "total_engaged_users": len(engaged_users),
        "current_followers": len(current_usernames),
    }
