"""Enhanced engagement log schema — dev.to platform.

Defines the canonical engagement log entry format with all fields
required for attribution, A/B testing, and weekly reporting.

All new fields default to None for backward compatibility with
existing log entries. Existing fields are preserved as-is.

Schema version: X1 (GitLab #14)
"""

from datetime import datetime, timezone

# Module-level cycle counter. Resets each process invocation.
_cycle_counter: int = 0
_cycle_date: str = ""


def generate_cycle_id() -> str:
    """Generate a cycle identifier in format YYYY-MM-DD-cycle-N.

    Increments N each time this function is called within the same UTC day.
    Resets to 1 when the UTC date changes or on process restart.
    """
    global _cycle_counter, _cycle_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _cycle_date:
        _cycle_date = today
        _cycle_counter = 0
    _cycle_counter += 1
    return f"{today}-cycle-{_cycle_counter}"


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
        cycle_id: Cycle identifier (YYYY-MM-DD-cycle-N).
        **extras: Any additional platform-specific fields to include.

    Returns:
        Dict ready for JSON serialization and appending to engagement_log.jsonl.
    """
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
