"""Growth engine configuration — all settings from .env, never hardcoded.

Uses pydantic-settings with GROWTH_ prefix. Every value is configurable
via environment variables. Validates at startup — fail fast, fail loud.
"""

import logging
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Default tags we monitor for engagement opportunities
# Tags are fetched live from dev.to API and randomly sampled each cycle.
# This fallback is only used if the API call fails.
DEFAULT_TARGET_TAGS: list[str] = [
    "ai", "python", "webdev", "javascript", "programming",
    "beginners", "devops", "react", "tutorial", "opensource",
]

# Niche clusters for scout targeting (D1).
# Primary clusters: our core expertise. Secondary: adjacent topics we engage with.
NICHE_CLUSTERS_PRIMARY: list[str] = [
    "ai", "llm", "gpt", "machinelearning", "deeplearning",
    "python", "langchain", "rag", "claude",
    "automation", "devops", "docker", "kubernetes", "cicd",
]
NICHE_CLUSTERS_SECONDARY: list[str] = [
    "javascript", "typescript", "react", "nextjs", "nodejs",
    "opensource", "startup", "indiehacker", "saas", "career",
    "webdev", "api", "graphql", "cloud", "datascience",
]


class GrowthConfig(BaseSettings):
    """Central configuration for Herald Growth engine.

    All settings loaded from .env with GROWTH_ prefix.
    Every default is safe for local development.
    """

    # --- API Keys (required for production) ---
    devto_api_key: str = ""

    # --- Platform: dev.to (Forem API v1) ---
    devto_base_url: str = "https://dev.to/api"
    devto_api_version: str = "application/vnd.forem.api-v1+json"

    # --- Our dev.to username (to skip our own articles) ---
    devto_username: str = ""

    # --- Paths ---
    project_root: Path = Field(
        default_factory=lambda: Path.home() / "netanel" / "teams" / "herald_growth",
    )
    data_dir: Path = Field(default_factory=lambda: Path("data"))
    drafts_dir: Path = Field(default_factory=lambda: Path("drafts"))

    # --- Reaction Settings (D2: volume increase) ---
    max_reactions_per_run: int = Field(default=20, ge=1, le=50)
    reaction_delay: float = Field(
        default=1.5, ge=0.5, le=10.0,
        description="Base seconds between reactions (randomized +/-30%)",
    )

    # --- Comment Settings (D2: volume increase) ---
    max_comments_per_cycle: int = Field(default=8, ge=1, le=15)
    comment_delay: float = Field(
        default=2.5, ge=1.0, le=15.0,
        description="Base seconds between comments (randomized +/-30%)",
    )
    min_reactions_to_comment: int = Field(
        default=3, ge=0, le=100,
        description="Minimum reactions on article before we comment (quality filter)",
    )

    # --- Follow Settings (D2) ---
    max_follows_per_day: int = Field(
        default=200, ge=1, le=500,
        description="Max follows per day (browser-based)",
    )
    follow_delay: float = Field(
        default=3.0, ge=1.0, le=10.0,
        description="Base seconds between follows (randomized +/-30%)",
    )

    # --- Volume Safety Guards (D2) ---
    max_engagements_per_author_per_cycle: int = Field(
        default=2, ge=1, le=5,
        description="Max engagement actions on a single author per cycle",
    )

    # --- Scout Targeting (D1) ---
    max_target_followers: int = Field(
        default=500, ge=10, le=5000,
        description="Max follower count for target authors (prefer smaller accounts)",
    )
    max_target_reactions: int = Field(
        default=5, ge=0, le=50,
        description="Max reaction count on post (under-engaged posts only)",
    )
    max_post_age_hours: int = Field(
        default=48, ge=1, le=168,
        description="Max post age in hours (fresh content only)",
    )

    # --- Browser Settings (Playwright for write operations) ---
    # API doesn't support reactions/comments for regular users.
    # Browser automation handles all write operations.
    devto_email: str = ""
    devto_password: str = ""
    browser_headless: bool = True
    browser_timeout: int = Field(
        default=30, ge=5, le=120,
        description="Page timeout in seconds for browser actions",
    )
    browser_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    use_browser: bool = Field(
        default=True,
        description="Use Playwright browser for write ops (reactions, comments)",
    )

    # --- Target Tags ---
    target_tags: list[str] = Field(default_factory=lambda: DEFAULT_TARGET_TAGS.copy())

    # --- History Bounds (prevent unbounded file growth) ---
    max_reacted_history: int = Field(default=2000, ge=100, le=10000)
    max_commented_history: int = Field(default=1000, ge=100, le=5000)
    max_engagement_log: int = Field(default=10000, ge=1000, le=100000)
    max_learnings: int = Field(default=200, ge=10, le=1000)

    # --- Rate Limit ---
    request_timeout: int = Field(default=30, ge=5, le=120)

    model_config = {
        "env_file": ".env",
        "env_prefix": "GROWTH_",
        "extra": "ignore",
    }

    @field_validator("devto_api_key")
    @classmethod
    def validate_devto_key(cls, v: str) -> str:
        """Warn if devto API key is empty."""
        if not v:
            logger.warning(
                "GROWTH_DEVTO_API_KEY not set. "
                "Get one at dev.to/settings/extensions"
            )
        return v

    @property
    def abs_data_dir(self) -> Path:
        """Absolute path to data directory."""
        return self.project_root / self.data_dir

    @property
    def abs_drafts_dir(self) -> Path:
        """Absolute path to drafts directory."""
        return self.project_root / self.drafts_dir


def load_config() -> GrowthConfig:
    """Load config from .env file. Fails fast on invalid values."""
    config = GrowthConfig()
    logger.info(
        "Growth config loaded: project_root=%s, tags=%d, max_reactions=%d",
        config.project_root, len(config.target_tags), config.max_reactions_per_run,
    )
    return config
