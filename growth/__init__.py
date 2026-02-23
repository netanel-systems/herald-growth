"""Herald Growth — aggressive dev.to engagement engine.

Two-speed design:
- Reactions (Python cron, every 30 min): mechanical, no LLM
- Comments (nathan-team, 3x daily): Nathan reads articles, writes genuine comments

Modules:
- config: GrowthConfig (pydantic-settings)
- client: DevTo API client for growth operations
- scout: ArticleScout — find rising/fresh/hot articles
- reactor: ReactionEngine — react to articles (standalone cron)
- commenter: CommentEngine — post comments with dedup
- learner: GrowthLearner — track what works, adapt
- tracker: GrowthTracker — follower growth, reciprocity, weekly reports
"""

__version__ = "0.1.0"
