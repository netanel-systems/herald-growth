"""Enhanced engagement log schema — dev.to platform.

Defines the canonical engagement log entry format with all fields
required for attribution, A/B testing, and weekly reporting.

All new fields default to None for backward compatibility with
existing log entries. Existing fields are preserved as-is.

Schema version: X1 (GitLab #14)
"""

import uuid
from datetime import datetime, timezone


def generate_cycle_id() -> str:
    """Generate a globally unique cycle identifier.

    Format: YYYY-MM-DD-<8-char-uuid-fragment>

    Uses a UUID fragment instead of a process-local counter so that two
    cron runs on the same UTC date never produce the same cycle_id.  The
    old YYYY-MM-DD-cycle-N scheme reset its counter on every invocation,
    causing collisions in attribution and A/B grouping whenever more than
    one run appended to the same engagement log on the same day.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    uid = uuid.uuid4().hex[:8]
    return f"{today}-{uid}"


# Reserved keys that callers must not overwrite via **extras.
# These fields define the canonical schema shape; silently clobbering them
# would corrupt attribution, A/B grouping, and weekly reporting.
_RESERVED_KEYS = frozenset({
    "timestamp",
    "platform",
    "action",
    "target_username",
    "target_post_id",
    "target_followers_at_engagement",
    "target_post_reactions_at_engagement",
    "target_post_age_hours",
    "comment_template_category",
    "comment_has_question",
    "cycle_id",
})


def build_engagement_entry(
    *,
    action: str,
    platform: str = "devto",
    target_username: str | None = None,
    target_post_id: str | None = None,
    target_followers_at_engagement: int | None = None,
    target_post_reactions_at_engagement: int | None = None,
    target_post_age_hours: float | None = None,
    comment_template_category: str | None = None,
    comment_has_question: bool | None = None,
    cycle_id: str | None = None,
    **extras: object,
) -> dict:
    """Build a canonical engagement log entry with enhanced schema fields.

    Merges the enhanced X1 fields with any platform-specific extras.
    Timestamp is always generated fresh (UTC ISO-8601).

    Args:
        action: Engagement action type (reaction, comment, follow).
        platform: Platform identifier. Defaults to "devto".
        target_username: Username of the engagement target author.
        target_post_id: Article ID of the engagement target.
        target_followers_at_engagement: Target author's follower count
            at the time of engagement. None until scout targeting ships.
        target_post_reactions_at_engagement: Target article's reaction count
            at the time of engagement. None until scout targeting ships.
        target_post_age_hours: Hours since the target article was published.
            None until scout targeting ships.
        comment_template_category: Template category used for comment
            generation. None for non-comment actions.
        comment_has_question: Whether the comment contains a question.
            None for non-comment actions.
        cycle_id: Cycle identifier (YYYY-MM-DD-<uuid-fragment>).
        **extras: Any additional platform-specific fields to include.
            Reserved canonical keys are rejected to prevent silent corruption.

    Returns:
        Dict ready for JSON serialization and appending to engagement_log.jsonl.

    Raises:
        ValueError: If any key in extras collides with a reserved canonical field.
    """
    collisions = _RESERVED_KEYS & extras.keys()
    if collisions:
        raise ValueError(
            f"build_engagement_entry: extras must not override reserved fields: "
            f"{sorted(collisions)}"
        )

    entry: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "action": action,
        "target_username": target_username,
        "target_post_id": target_post_id,
        "target_followers_at_engagement": target_followers_at_engagement,
        "target_post_reactions_at_engagement": target_post_reactions_at_engagement,
        "target_post_age_hours": target_post_age_hours,
        "comment_template_category": comment_template_category,
        "comment_has_question": comment_has_question,
        "cycle_id": cycle_id,
    }
    entry.update(extras)
    return entry
