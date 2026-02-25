"""CommentEngine — post comments on dev.to articles.

Used by nathan-team comment cycles (3x daily). Nathan reads articles
and writes the comments — this module handles posting and dedup.

Comments are 1-2 sentences, specific to the article, natural.
See knowledge/comment-style-guide.md for rules.

Write operations use Playwright headless browser when available
(Forem API doesn't support comments for regular users).
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from growth.browser import DevToBrowser
from growth.client import DevToClient, DevToError
from growth.config import GrowthConfig
from growth.learner import GrowthLearner
from growth.storage import load_json_ids, save_json_ids

logger = logging.getLogger(__name__)


class CommentEngine:
    """Posts comments and manages comment history with dedup.

    When a browser instance is provided, uses Playwright for posting.
    Falls back to API client (admin-only, will fail for regular users).
    """

    def __init__(
        self,
        client: DevToClient,
        config: GrowthConfig,
        browser: DevToBrowser | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.browser = browser
        self.data_dir = config.abs_data_dir

    def load_commented_ids(self) -> set[int]:
        """Load article IDs we already commented on."""
        return load_json_ids(self.data_dir / "commented.json")

    def save_commented_ids(self, commented_ids: set[int]) -> None:
        """Save commented IDs, bounded to max_commented_history."""
        save_json_ids(
            self.data_dir / "commented.json", commented_ids,
            max_count=self.config.max_commented_history,
        )

    def load_commented_details(self) -> list[dict]:
        """Load detailed comment history (for performance tracking)."""
        path = self.data_dir / "comment_history.jsonl"
        if not path.exists():
            return []
        entries: list[dict] = []
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load comment_history.jsonl: %s", e)
        return entries

    def get_learnings_context(self, max_learnings: int = 5) -> list[str]:
        """Return top learnings as bullet-point strings for LLM prompt injection.

        Callers (e.g. nathan-team comment scripts) should inject the returned
        list into their system or user prompt so the LLM writes comments that
        reflect what has historically worked well.

        Safe: any exception from the learner returns an empty list so the
        comment cycle continues unaffected.

        Returns:
            list[str]: each item is a bullet-point learning string,
                       e.g. ["- tag 'python' yields high engagement (confidence: 0.85)"].
                       Empty list if no learnings or on any error.
        """
        try:
            learner = GrowthLearner(self.config)
            insights = learner.get_insights_for_prompt(max_insights=max_learnings)
            if insights:
                logger.info("Learner context: %d insights for prompt.", len(insights))
            return insights
        except Exception:
            logger.exception("GrowthLearner.get_insights_for_prompt() raised — no context injected.")
            return []

    def run_learner_analyze(self) -> None:
        """Run GrowthLearner.analyze() after a comment cycle completes.

        Callers should invoke this once after all comments in a cycle are posted
        so that engagement patterns are extracted and stored for future cycles.

        Safe: any exception is logged and silently swallowed so the
        cycle result is never affected by learner errors.
        """
        try:
            learner = GrowthLearner(self.config)
            new_learnings = learner.analyze()
            logger.info(
                "GrowthLearner.analyze() complete: %d new learnings after comment cycle.",
                len(new_learnings),
            )
        except Exception:
            logger.exception("GrowthLearner.analyze() raised after comment cycle — continuing.")

    def post_comment(
        self,
        article_id: int,
        body: str,
        article_title: str = "",
        author: str = "",
        article_url: str = "",
    ) -> dict | None:
        """Post a comment and log it.

        Uses Playwright browser when available, otherwise API client.
        Returns result dict on success, None on failure.
        """
        # Validate comment quality before posting
        if not self._validate_comment(body):
            logger.warning(
                "Comment rejected by quality gate for article %d: '%s'",
                article_id, body[:50],
            )
            return None

        try:
            # Use browser for posting when enabled
            if self.config.use_browser:
                if self.browser is None:
                    logger.error(
                        "Browser mode enabled but no browser instance provided. "
                        "Cannot post comment on article %d.", article_id,
                    )
                    return None
                if not article_url:
                    logger.warning(
                        "No article URL for browser comment on %d. Skipping.",
                        article_id,
                    )
                    return None
                result = self.browser.post_comment(article_id, body, article_url)
                method = "browser"
            else:
                result = self.client.post_comment(article_id, body)
                method = "api"

            if result is None:
                logger.warning(
                    "Comment posting returned None for article %d via %s.",
                    article_id, method,
                )
                return None

            logger.info(
                "Comment posted on article %d (%s) via %s: '%s'",
                article_id, author, method, body[:60],
            )

            # Log to comment history (for learner)
            self._log_comment(article_id, body, article_title, author, result)

            # Log to engagement log
            self._log_engagement(article_id, body, article_title, author, method)

            return result

        except DevToError as e:
            logger.exception("Failed to post comment on article %d: %s", article_id, e)
            return None
        except Exception as e:
            logger.exception(
                "Unexpected error posting comment on article %d: %s", article_id, e,
            )
            return None

    def _validate_comment(self, body: str) -> bool:
        """Quality gate: reject comments that violate our rules.

        Returns True if comment passes, False if rejected.
        """
        # Must not be empty
        if not body or not body.strip():
            logger.warning("Empty comment rejected.")
            return False

        # Must be short (1-2 sentences, roughly under 280 chars)
        if len(body) > 280:
            logger.warning("Comment too long (%d chars). Max 280.", len(body))
            return False

        # Must be 1-2 sentences
        # Use lookbehind to avoid splitting on abbreviations (e.g. "Dr. Smith")
        sentences = [s for s in re.split(r'(?<=[.!?])\s+', body) if s.strip()]
        if not (1 <= len(sentences) <= 2):
            logger.warning("Comment must be 1-2 sentences (found %d).", len(sentences))
            return False

        # Must not contain multiple paragraphs
        if "\n\n" in body:
            logger.warning("Comment contains multiple paragraphs. Rejected.")
            return False

        # Must not contain generic phrases
        generic_phrases = [
            "great article", "thanks for sharing", "well written",
            "very insightful", "i totally agree", "nice post",
            "awesome article", "love this", "game-changer",
            "thanks for writing",
        ]
        body_lower = body.lower()
        for phrase in generic_phrases:
            pattern = r'\b' + re.escape(phrase) + r'\b'
            if re.search(pattern, body_lower):
                logger.warning("Generic phrase detected: '%s'", phrase)
                return False

        # Must not contain self-promotion
        promo_terms = ["netanel", "our product", "check out my", "my article"]
        for term in promo_terms:
            if term in body_lower:
                logger.warning("Self-promotion detected: '%s'", term)
                return False

        return True

    def _log_comment(
        self,
        article_id: int,
        body: str,
        article_title: str,
        author: str,
        api_result: dict,
    ) -> None:
        """Log comment details for performance tracking by learner."""
        path = self.data_dir / "comment_history.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "article_id": article_id,
            "article_title": article_title[:100],
            "author": author,
            "comment_text": body,
            "comment_id": api_result.get("id_code", ""),
            "char_count": len(body),
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _log_engagement(
        self,
        article_id: int,
        body: str,
        article_title: str,
        author: str,
        method: str = "api",
    ) -> None:
        """Append to shared engagement_log.jsonl."""
        path = self.data_dir / "engagement_log.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "comment",
            "article_id": article_id,
            "article_title": article_title[:100],
            "author": author,
            "comment_length": len(body),
            "method": method,
        }
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        # NOTE: Do NOT trim here — trim is O(N) and should be called once per cycle,
        # not per comment (which would be O(N²)). Call trim_engagement_log() once
        # after all comments in a cycle are posted.

    def trim_engagement_log(self) -> None:
        """Trim engagement log to max_engagement_log entries. Atomic write.

        Prevents unbounded growth. Same logic as ReactionEngine.
        """
        import os
        import tempfile

        path = self.data_dir / "engagement_log.jsonl"
        if not path.exists():
            return
        lines = [line for line in path.read_text().strip().split("\n") if line.strip()]
        if len(lines) > self.config.max_engagement_log:
            trimmed = lines[-self.config.max_engagement_log:]
            content = "\n".join(trimmed) + "\n"
            fd, tmp_path = tempfile.mkstemp(
                dir=path.parent, suffix=".tmp", prefix=".engagement_",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            logger.info(
                "Trimmed engagement log: %d -> %d entries.",
                len(lines), len(trimmed),
            )
