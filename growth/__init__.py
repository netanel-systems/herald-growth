"""Herald Growth — aggressive dev.to engagement engine.

Two-speed design with Playwright browser automation for write operations:
- Reactions (Python cron, every 10 min): Playwright browser, no LLM, $0
- Comments (nathan-team, 3x daily): Nathan writes, Playwright posts

API client handles reads (articles, comments, followers).
Browser handles writes (reactions, comments) — Forem API doesn't support
these for regular users (admin-only endpoints).

Modules:
- config: GrowthConfig (pydantic-settings, +browser settings)
- client: DevTo API client (reads only)
- browser: DevToBrowser — Playwright headless Chromium (writes)
- scout: ArticleScout — find rising/fresh/hot articles
- reactor: ReactionEngine — react via browser (standalone cron)
- commenter: CommentEngine — post comments via browser with dedup
- learner: GrowthLearner — track what works, adapt
- tracker: GrowthTracker — follower growth, reciprocity, weekly reports
"""

__version__ = "0.1.0"
