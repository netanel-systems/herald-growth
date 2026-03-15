"""OwnPostResponder — engage with comments on our own dev.to articles.

Runs 2x daily (9 AM and 3 PM UTC) AFTER the main engagement cycles.

For each comment received on our own articles:
1. Like the comment (show appreciation) via Playwright browser
2. Reply with a genuine 1-2 sentence response via LLM + Playwright
3. Deduplicate via responded_comments.json — each comment handled once

Rules:
- Max 1 reply per incoming comment. No thread continuation.
- Never continue beyond the initial reply.
- Max MAX_REPLIES_PER_COMMENTER replies per unique commenter per article across ALL cron runs.
- Troll comments are silently skipped — no like, no reply, marked processed.
- Replies are specific to what the commenter said.
- No self-promotion in replies.
- No generic acknowledgements ("Thanks for reading!" is a violation).

Dev.to API note: comments are fetched via GET /api/comments?a_id={article_id}
using the read-only API client. Writes (like + reply) go through Playwright browser.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from growth.browser import BrowserLoginRequired, DevToBrowser
from growth.client import DevToClient, DevToError
from growth.config import GrowthConfig
from growth.engagement_state import EngagementState
from growth.storage import atomic_write_json

logger = logging.getLogger(__name__)

# Max comments to process per run (rate-limit safety)
MAX_COMMENTS_PER_RUN = 10
# Articles to inspect per run (last N days worth from our own feed)
MAX_OWN_ARTICLES = 10
# Delay between comment engagement actions (seconds)
ENGAGE_DELAY = 5.0
# Maximum articles tracked in replied_per_article.json before rotating oldest
MAX_REPLIED_ARTICLES = 500
# Maximum replies we send to any single commenter on a given article (across all cron runs)
MAX_REPLIES_PER_COMMENTER = 3
# Safety cap: never delete more than this many orphaned replies per run
MAX_ORPHAN_DELETIONS_PER_RUN = 10


class OwnPostResponder:
    """Engage with comments on our own dev.to articles.

    Fetches comments on our own articles, likes each new comment via browser,
    replies using an LLM-generated response, and deduplicates via
    responded_comments.json so each comment is touched exactly once.

    Usage:
        with DevToBrowser(config) as browser:
            responder = OwnPostResponder(client, config, browser, llm_fn)
            summary = responder.run()
    """

    def __init__(
        self,
        client: DevToClient,
        config: GrowthConfig,
        browser: DevToBrowser,
        llm_reply_fn,
        troll_detect_fn=None,
    ) -> None:
        """Initialize the responder.

        Args:
            client: DevToClient for read-only API calls.
            config: GrowthConfig with devto_username and data_dir.
            browser: DevToBrowser instance (must already be started).
            llm_reply_fn: Callable[[str, str], str] — takes (comment_body,
                          article_title) and returns a 1-2 sentence reply string.
            troll_detect_fn: Optional Callable[[str], bool] — takes comment_body
                             and returns True if the comment is trolling. When
                             None, troll detection is disabled (all comments pass).
        """
        self.client = client
        self.config = config
        self.browser = browser
        self.llm_reply_fn = llm_reply_fn
        self.troll_detect_fn = troll_detect_fn
        self.data_dir: Path = config.abs_data_dir
        self._engagement_state: EngagementState | None = None

    @property
    def engagement_state(self) -> EngagementState:
        """Lazy-init engagement state (D5)."""
        if self._engagement_state is None:
            self._engagement_state = EngagementState(self.data_dir)
        return self._engagement_state

    # ── Storage ────────────────────────────────────────────────────────────

    def load_responded_ids(self) -> set[str]:
        """Load comment ID strings we have already responded to.

        Returns empty set if file is missing or corrupted.
        """
        path = self.data_dir / "responded_comments.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                return {str(item) for item in data}
            logger.warning(
                "responded_comments.json: unexpected format %s, returning empty set.",
                type(data).__name__,
            )
            return set()
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load responded_comments.json: %s", exc)
            return set()

    def save_responded_ids(self, ids: set[str]) -> None:
        """Atomically save responded comment IDs. Bounded to 5,000 entries."""
        import os
        import tempfile

        path = self.data_dir / "responded_comments.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        bounded = sorted(ids)[-5000:]
        content = json.dumps(bounded, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, suffix=".tmp", prefix=".responded_",
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info("Saved %d responded comment IDs.", len(bounded))

    def load_replied_per_article(self) -> dict[str, dict[str, int]]:
        """Load the per-article commenter reply count map.

        Returns a dict mapping article_id (str) to a dict of
        {commenter_username: reply_count} for all commenters already replied
        to on that article across all cron runs.
        Returns empty dict if file is missing or corrupted.

        Backward compat: if the on-disk value for an article is a list (old
        format from before this change), each username in that list is treated
        as fully used up — converted to {username: MAX_REPLIES_PER_COMMENTER}.
        This prevents a second reply cycle from sending additional replies to
        users who already received one under the old schema.

        File: data/replied_per_article.json
        """
        path = self.data_dir / "replied_per_article.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                logger.warning(
                    "replied_per_article.json: unexpected format %s, returning empty dict.",
                    type(data).__name__,
                )
                return {}
            result: dict[str, dict[str, int]] = {}
            for k, v in data.items():
                article_id_str = str(k)
                if isinstance(v, list):
                    # Backward compat: old format stored a list of usernames.
                    # Treat every entry as fully used up (MAX_REPLIES_PER_COMMENTER).
                    logger.info(
                        "replied_per_article.json: article %s has legacy list format — "
                        "converting to count dict (treating as MAX_REPLIES_PER_COMMENTER).",
                        article_id_str,
                    )
                    result[article_id_str] = {
                        str(u): MAX_REPLIES_PER_COMMENTER for u in v
                    }
                elif isinstance(v, dict):
                    result[article_id_str] = {
                        str(u): int(count) for u, count in v.items()
                    }
                else:
                    logger.warning(
                        "replied_per_article.json: article %s has unexpected value type %s — skipping.",
                        article_id_str, type(v).__name__,
                    )
            return result
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load replied_per_article.json: %s", exc)
            return {}

    def save_replied_per_article(self, replied: dict[str, dict[str, int]]) -> None:
        """Atomically save the per-article commenter reply count map.

        Bounded to MAX_REPLIED_ARTICLES (500) entries — oldest keys are rotated
        out when the map exceeds the cap. Uses atomic_write_json from storage.py
        to prevent corruption if the process crashes mid-write.

        Args:
            replied: dict mapping article_id (str) -> {commenter_username: reply_count}.
        """
        if len(replied) > MAX_REPLIED_ARTICLES:
            # Rotate: keep the most recently added articles (last N by insertion order).
            # Python dicts preserve insertion order (3.7+); oldest keys are first.
            keys = list(replied.keys())
            keys_to_keep = keys[-MAX_REPLIED_ARTICLES:]
            replied = {k: replied[k] for k in keys_to_keep}
            logger.info(
                "replied_per_article.json rotated to %d articles (cap=%d).",
                MAX_REPLIED_ARTICLES, MAX_REPLIED_ARTICLES,
            )
        path = self.data_dir / "replied_per_article.json"
        atomic_write_json(path, replied)
        logger.info(
            "Saved replied_per_article.json: %d articles tracked.", len(replied),
        )

    # ── Orphan Reply Tracking ───────────────────────────────────────────────

    def load_our_replies(self) -> dict[str, dict]:
        """Load our reply→parent mapping from our_replies.json.

        Returns a dict mapping our comment id_code (str) to a record dict:
            {
                "parent_id_code": str,  # id_code of the parent comment we replied to
                "article_url": str,     # article URL (needed for browser navigation)
                "article_id": int,      # article numeric ID (for fetching comment tree)
            }

        Returns empty dict if file is missing or corrupted. Never raises.
        """
        path = self.data_dir / "our_replies.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                logger.warning(
                    "our_replies.json: unexpected format %s — returning empty dict.",
                    type(data).__name__,
                )
                return {}
            # Validate each entry has the required keys; drop malformed ones
            result: dict[str, dict] = {}
            for our_id, record in data.items():
                if (
                    isinstance(record, dict)
                    and "parent_id_code" in record
                    and "article_url" in record
                    and "article_id" in record
                ):
                    result[str(our_id)] = record
                else:
                    logger.warning(
                        "our_replies.json: entry %s has missing fields — skipping.",
                        our_id,
                    )
            return result
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load our_replies.json: %s", exc)
            return {}

    def save_our_reply(
        self,
        our_id_code: str,
        parent_id_code: str,
        article_url: str,
        article_id: int,
    ) -> None:
        """Record one of our replies in our_replies.json for orphan tracking.

        Must be called immediately after a successful reply is posted. Uses
        atomic_write_json to prevent corruption on crash mid-write.

        Args:
            our_id_code: The id_code of the comment we just posted.
            parent_id_code: The id_code of the parent comment we replied to.
            article_url: Full article URL (needed for browser navigation on delete).
            article_id: Article numeric ID (needed to fetch the comment tree).
        """
        replies = self.load_our_replies()
        replies[str(our_id_code)] = {
            "parent_id_code": str(parent_id_code),
            "article_url": str(article_url),
            "article_id": int(article_id),
        }
        path = self.data_dir / "our_replies.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, replies)
        logger.info(
            "Saved our reply %s (parent=%s) to our_replies.json.",
            our_id_code, parent_id_code,
        )

    def _collect_all_id_codes(self, comments: list[dict]) -> set[str]:
        """Recursively collect every id_code present in a comment tree.

        The Forem API returns top-level comments with a ``children`` list,
        each child also having a ``children`` list, and so on. We collect
        all id_codes at every depth so we can detect whether a specific
        parent comment still exists anywhere in the tree.

        Args:
            comments: Top-level comment list from get_article_comments().

        Returns:
            Flat set of all id_code strings found in the tree.
        """
        found: set[str] = set()
        stack = list(comments)
        while stack:
            comment = stack.pop()
            id_code = comment.get("id_code")
            if id_code:
                found.add(str(id_code))
            children = comment.get("children", [])
            if isinstance(children, list):
                stack.extend(children)
        return found

    def _find_our_reply_id_code(
        self,
        article_id: int,
        parent_id_code: str,
    ) -> str | None:
        """Discover the id_code of our freshly posted reply.

        After ``browser.reply_to_comment()`` succeeds, re-fetches the
        comment tree for the article and searches the parent comment's
        ``children`` list for a comment authored by our own username.
        Returns the id_code of the first matching child, or None if not
        found (e.g. API delay, misconfiguration).

        Args:
            article_id: Numeric article ID to re-fetch comments for.
            parent_id_code: id_code of the parent comment we just replied to.

        Returns:
            id_code string of our reply, or None if not discoverable.
        """
        our_username = (self.config.devto_username or "").lower()
        if not our_username:
            logger.warning(
                "_find_our_reply_id_code: devto_username not configured — "
                "cannot discover reply id_code."
            )
            return None

        comments = self.fetch_article_comments(article_id)

        # Depth-first search for the parent comment, then inspect its children
        stack = list(comments)
        while stack:
            comment = stack.pop()
            if str(comment.get("id_code", "")) == parent_id_code:
                # Found the parent — check its children for our reply
                children = comment.get("children", [])
                if isinstance(children, list):
                    for child in children:
                        child_username = (
                            child.get("user", {}).get("username", "") or ""
                        ).lower()
                        if child_username == our_username:
                            our_id = child.get("id_code")
                            if our_id:
                                return str(our_id)
                return None  # Parent found but our reply not yet visible
            children = comment.get("children", [])
            if isinstance(children, list):
                stack.extend(children)

        return None  # Parent comment not found in tree

    # ── Orphan Cleanup ──────────────────────────────────────────────────────

    def clean_orphaned_replies(self) -> int:
        """Delete any of our replies whose parent comment no longer exists.

        Called at the START of each run() cycle, before processing new
        comments. For each tracked reply in our_replies.json:

        1. Fetch the comment tree for the article via the read-only API.
        2. Check whether the parent id_code is still present in the tree.
        3. If the parent is gone, delete our reply via browser automation.
        4. Log the deletion to engagement_log.jsonl.
        5. Remove the entry from our_replies.json (atomic write).

        Bounded to MAX_ORPHAN_DELETIONS_PER_RUN (10) deletions per run.

        Returns:
            Number of orphaned replies successfully deleted this run.
        """
        replies = self.load_our_replies()
        if not replies:
            logger.info("clean_orphaned_replies: no tracked replies — skipping.")
            return 0

        logger.info(
            "clean_orphaned_replies: checking %d tracked replies for orphans.",
            len(replies),
        )

        # Group tracked replies by article_id to minimise API calls.
        # For each article, we fetch the comment tree once and check all
        # tracked replies for that article in a single pass.
        articles_to_check: dict[int, list[tuple[str, dict]]] = {}
        for our_id, record in replies.items():
            art_id = int(record["article_id"])
            articles_to_check.setdefault(art_id, []).append((our_id, record))

        deleted_count = 0
        entries_to_remove: list[str] = []

        for article_id, tracked in articles_to_check.items():
            if deleted_count >= MAX_ORPHAN_DELETIONS_PER_RUN:
                logger.info(
                    "clean_orphaned_replies: reached cap (%d). Stopping early.",
                    MAX_ORPHAN_DELETIONS_PER_RUN,
                )
                break

            # Fetch the live comment tree for this article (read-only API)
            live_comments = self.fetch_article_comments(article_id)
            live_id_codes = self._collect_all_id_codes(live_comments)

            for our_id, record in tracked:
                if deleted_count >= MAX_ORPHAN_DELETIONS_PER_RUN:
                    break

                parent_id_code = record["parent_id_code"]
                article_url = record["article_url"]

                if parent_id_code in live_id_codes:
                    # Parent still exists — our reply is not orphaned
                    logger.debug(
                        "clean_orphaned_replies: parent %s of our reply %s "
                        "still exists. OK.",
                        parent_id_code, our_id,
                    )
                    continue

                # Parent is gone — our reply is orphaned. Delete it.
                logger.info(
                    "clean_orphaned_replies: parent %s missing for our reply %s "
                    "on article %d — deleting orphan.",
                    parent_id_code, our_id, article_id,
                )

                deleted = False
                if hasattr(self.browser, "delete_comment"):
                    try:
                        deleted = self.browser.delete_comment(our_id, article_url)
                    except Exception as exc:
                        logger.warning(
                            "clean_orphaned_replies: browser.delete_comment(%s) "
                            "raised: %s",
                            our_id, exc,
                        )
                else:
                    logger.warning(
                        "clean_orphaned_replies: browser.delete_comment() not "
                        "available — cannot delete orphan %s.",
                        our_id,
                    )

                if deleted:
                    deleted_count += 1
                    entries_to_remove.append(our_id)
                    self._log_action(
                        "delete_orphaned_reply",
                        our_id,
                        article_id,
                        "",  # article_title not stored in replies map
                        "",  # commenter not relevant here
                        reply_text=f"parent={parent_id_code}",
                    )
                    logger.info(
                        "clean_orphaned_replies: deleted orphaned reply %s "
                        "(parent=%s, article=%d).",
                        our_id, parent_id_code, article_id,
                    )
                else:
                    logger.warning(
                        "clean_orphaned_replies: failed to delete orphan %s "
                        "(parent=%s). Will retry on next run.",
                        our_id, parent_id_code,
                    )

        # Atomically remove successfully deleted entries from our_replies.json
        if entries_to_remove:
            updated = {k: v for k, v in replies.items() if k not in entries_to_remove}
            path = self.data_dir / "our_replies.json"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, updated)
            logger.info(
                "clean_orphaned_replies: removed %d entries from our_replies.json "
                "(%d remaining).",
                len(entries_to_remove), len(updated),
            )

        logger.info(
            "clean_orphaned_replies: done — %d orphan(s) deleted this run.",
            deleted_count,
        )
        return deleted_count

    # ── Troll Detection ─────────────────────────────────────────────────────

    def is_troll_comment(self, comment_body: str) -> bool:
        """Return True if the comment should be treated as trolling.

        When troll_detect_fn is provided, delegates to it. When None (default),
        troll detection is disabled and all comments return False.

        Signals handled by troll_detect_fn: personal attacks, hostile repeated
        comments, bad-faith bait, deliberate provocation.

        Args:
            comment_body: Raw text/HTML of the incoming comment.

        Returns:
            True if the comment is trolling (skip all engagement).
            False if the comment is genuine (proceed normally).
        """
        if self.troll_detect_fn is None:
            return False
        try:
            return bool(self.troll_detect_fn(comment_body))
        except Exception as exc:
            logger.warning(
                "troll_detect_fn raised an exception: %s. "
                "Treating comment as non-troll (safe default).",
                exc,
            )
            return False

    # ── Fetch ───────────────────────────────────────────────────────────────

    def fetch_own_articles(self) -> list[dict]:
        """Fetch our own recent published articles (last MAX_OWN_ARTICLES).

        Uses GET /api/articles?username={username} — read-only, no auth needed.
        Returns list of article dicts with id, title, url, slug.
        Returns empty list on any API failure (logs warning).
        """
        username = self.config.devto_username
        if not username:
            logger.warning(
                "GROWTH_DEVTO_USERNAME not set. Cannot fetch own articles."
            )
            return []
        try:
            articles = self.client.get_articles_by_username(
                username, per_page=MAX_OWN_ARTICLES,
            )
            logger.info(
                "Fetched %d own articles for @%s.", len(articles), username,
            )
            return articles
        except DevToError as exc:
            logger.warning("Failed to fetch own articles: %s", exc)
            return []

    def fetch_article_comments(self, article_id: int) -> list[dict]:
        """Fetch all comments on a single article.

        Uses GET /api/comments?a_id={article_id} — returns top-level comments
        with nested replies. Filters to top-level only (we only respond once
        per incoming comment, not to replies-of-replies).

        Returns list of comment dicts with id, body_html, user.username.
        Returns empty list on any API failure (logs warning).
        """
        try:
            comments = self.client.get_article_comments(article_id)
            logger.debug(
                "Fetched %d comments for article %d.", len(comments), article_id,
            )
            return comments
        except DevToError as exc:
            logger.warning(
                "Failed to fetch comments for article %d: %s", article_id, exc,
            )
            return []

    # ── Engagement ─────────────────────────────────────────────────────────

    def like_comment_via_browser(
        self,
        comment_id_code: str,
        article_url: str,
    ) -> bool:
        """Like a comment using the Playwright browser.

        Dev.to comment liking navigates to the article page and clicks the
        heart icon scoped to the comment node. Returns True on success.

        Note: DevToBrowser.like_comment() is used when available. If not
        implemented on the browser instance, falls back gracefully.

        Args:
            comment_id_code: The ``id_code`` string from the Forem API.
            article_url: Full article URL.
        """
        try:
            if hasattr(self.browser, "like_comment"):
                result = self.browser.like_comment(comment_id_code, article_url)
                return result is not False
            # Fallback: method not yet implemented — log and continue
            logger.info(
                "browser.like_comment() not available for comment %s. "
                "Skipping like, proceeding to reply.",
                comment_id_code,
            )
            return False
        except BrowserLoginRequired:
            logger.error(
                "Cannot like comment %s — login required.", comment_id_code,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Like comment %s failed (non-fatal, continuing to reply): %s",
                comment_id_code, exc,
            )
            return False

    def generate_reply(self, comment_body: str, article_title: str) -> str | None:
        """Generate a genuine 1-2 sentence reply using the LLM function.

        Applies the reply quality gate before returning. Returns None if the
        generated reply fails the quality gate or the LLM raises an exception.

        Args:
            comment_body: Raw text/HTML of the incoming comment.
            article_title: Title of the article being commented on.

        Returns:
            Reply string on success, None on failure or quality rejection.
        """
        try:
            reply = self.llm_reply_fn(comment_body, article_title)
        except Exception as exc:
            logger.warning(
                "LLM reply generation raised an exception: %s", exc,
            )
            return None

        if not reply or not reply.strip():
            logger.warning("LLM returned empty reply. Skipping.")
            return None

        if not self._validate_reply(reply):
            logger.warning(
                "Generated reply failed quality gate: '%s'", reply[:60],
            )
            return None

        return reply.strip()

    def _validate_reply(self, body: str) -> bool:
        """Quality gate: reject replies that violate engagement rules.

        Mirrors commenter.py quality gate with additional reply-specific rules.
        Returns True if reply passes, False if rejected.
        """
        import re

        if not body or not body.strip():
            return False

        if len(body) > 280:
            logger.warning(
                "Reply too long (%d chars, max 280). Rejected.", len(body),
            )
            return False

        sentences = [s for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
        if not (1 <= len(sentences) <= 2):
            logger.warning(
                "Reply must be 1-2 sentences (found %d). Rejected.", len(sentences),
            )
            return False

        if "\n\n" in body:
            logger.warning("Reply contains multiple paragraphs. Rejected.")
            return False

        # Generic replies are a violation
        generic_phrases = [
            "thanks for reading",
            "thanks for the comment",
            "glad you liked it",
            "great question",
            "thanks for your feedback",
            "appreciate your comment",
            "thank you for reading",
        ]
        body_lower = body.lower()
        for phrase in generic_phrases:
            if phrase in body_lower:
                logger.warning(
                    "Generic reply phrase detected: '%s'. Rejected.", phrase,
                )
                return False

        # No self-promotion
        promo_terms = ["netanel", "our product", "check out my", "my article"]
        for term in promo_terms:
            if term in body_lower:
                logger.warning(
                    "Self-promotion in reply: '%s'. Rejected.", term,
                )
                return False

        return True

    # ── Core Loop ──────────────────────────────────────────────────────────

    def run(self) -> dict:
        """Main entry point. Processes comments on our own articles.

        Enforces two additional guards beyond comment-ID dedup:

        1. Per-commenter limit: at most MAX_REPLIES_PER_COMMENTER replies per
           unique commenter per article, across all cron runs. State persisted
           in data/replied_per_article.json.

        2. Troll detection: hostile/bad-faith comments are silently skipped —
           no like, no reply. Marked as processed so they are never re-evaluated.
           Logged as action="skip_troll" in engagement_log.jsonl.

        Returns summary dict with counts for monitoring/logging.
        """
        logger.info("=== OwnPostResponder cycle starting ===")
        start = time.time()

        # Step 0: Clean up orphaned replies before processing new ones.
        # This runs first so the cleanup cap is always enforced regardless of
        # how many new comments we process in this cycle.
        orphans_cleaned = 0
        try:
            orphans_cleaned = self.clean_orphaned_replies()
        except Exception as exc:
            logger.warning(
                "clean_orphaned_replies() raised an unexpected exception: %s. "
                "Continuing with normal engagement cycle.",
                exc,
            )

        responded_ids = self.load_responded_ids()
        replied_per_article = self.load_replied_per_article()
        articles = self.fetch_own_articles()

        if not articles:
            logger.info("No own articles found. Cycle complete.")
            return {
                "articles_checked": 0,
                "comments_found": 0,
                "liked": 0,
                "replied": 0,
                "skipped": 0,
                "trolls_skipped": 0,
                "orphans_cleaned": orphans_cleaned,
                "elapsed_seconds": round(time.time() - start, 1),
            }

        total_comments = 0
        liked_count = 0
        replied_count = 0
        skipped_count = 0
        trolls_skipped = 0
        new_responded: set[str] = set()
        # Track reply counts accumulated in this run: article_id_str -> {username: count}
        replied_this_run: dict[str, dict[str, int]] = {}
        processed_this_run = 0

        for article in articles:
            if processed_this_run >= MAX_COMMENTS_PER_RUN:
                logger.info(
                    "Reached MAX_COMMENTS_PER_RUN (%d). Stopping.", MAX_COMMENTS_PER_RUN,
                )
                break

            article_id = article.get("id")
            article_title = article.get("title", "")
            article_url = article.get("url", "")

            if not article_id:
                continue

            article_id_str = str(article_id)

            comments = self.fetch_article_comments(article_id)
            total_comments += len(comments)

            for comment in comments:
                if processed_this_run >= MAX_COMMENTS_PER_RUN:
                    break

                # Forem API returns 'id_code' (string slug) on the
                # GET /api/comments?a_id= endpoint.  The numeric 'id' is
                # NOT present in that response.  All downstream methods
                # (browser.reply_to_comment, like_comment_via_browser)
                # accept id_code strings.
                comment_id_code: str = str(
                    comment.get("id_code") or ""
                )
                if not comment_id_code:
                    continue

                # Dedup: skip already responded (by comment ID)
                if comment_id_code in responded_ids:
                    skipped_count += 1
                    continue

                commenter_username = (
                    comment.get("user", {}).get("username", "") or ""
                )

                # Skip our own comments
                if (
                    self.config.devto_username
                    and commenter_username.lower() == self.config.devto_username.lower()
                ):
                    # Mark it so we don't re-check it
                    new_responded.add(comment_id_code)
                    skipped_count += 1
                    continue

                # Per-commenter limit: max MAX_REPLIES_PER_COMMENTER replies per
                # unique commenter per article. Sum cross-run persisted count and
                # in-run count to get the total already sent.
                cross_run_count = (
                    replied_per_article.get(article_id_str, {}).get(commenter_username, 0)
                    if commenter_username else 0
                )
                in_run_count = (
                    replied_this_run.get(article_id_str, {}).get(commenter_username, 0)
                    if commenter_username else 0
                )
                total_replied_count = cross_run_count + in_run_count
                if commenter_username and total_replied_count >= MAX_REPLIES_PER_COMMENTER:
                    logger.info(
                        "Skipping comment %s — @%s has reached the reply limit "
                        "(%d/%d) on article %s.",
                        comment_id_code, commenter_username,
                        total_replied_count, MAX_REPLIES_PER_COMMENTER, article_id_str,
                    )
                    # Mark as processed so we do not re-evaluate on the next cron run
                    new_responded.add(comment_id_code)
                    skipped_count += 1
                    continue

                comment_body = comment.get("body_html", "") or comment.get("body_markdown", "") or ""

                # Troll detection: evaluate before any engagement.
                if self.is_troll_comment(comment_body):
                    logger.info(
                        "Troll comment detected: %s by @%s — skipping all engagement.",
                        comment_id_code, commenter_username,
                    )
                    new_responded.add(comment_id_code)
                    trolls_skipped += 1
                    self._log_action(
                        "skip_troll", comment_id_code, article_id,
                        article_title, commenter_username,
                    )
                    processed_this_run += 1
                    continue

                logger.info(
                    "Processing comment %s on article '%s' by @%s",
                    comment_id_code, article_title[:50], commenter_username,
                )

                # Record target reply in engagement state (D5):
                # When someone comments on our own post, treat it as a reply
                # signal if we previously engaged with their content.
                if commenter_username:
                    try:
                        self.engagement_state.record_target_reply(commenter_username)
                    except Exception as es_exc:
                        logger.warning(
                            "EngagementState.record_target_reply failed for @%s: %s",
                            commenter_username, es_exc,
                        )

                # Step 1: Like the comment via browser automation
                liked = self.like_comment_via_browser(comment_id_code, article_url)
                if liked:
                    liked_count += 1
                    self._log_action(
                        "like_comment", comment_id_code, article_id,
                        article_title, commenter_username,
                    )

                time.sleep(ENGAGE_DELAY * 0.4)  # brief pause before reply

                # Step 2: Generate reply
                reply_text = self.generate_reply(comment_body, article_title)
                if reply_text is None:
                    logger.warning(
                        "Could not generate reply for comment %s. Marking responded.",
                        comment_id_code,
                    )
                    new_responded.add(comment_id_code)
                    processed_this_run += 1
                    continue

                # Step 3: Post the reply via browser
                try:
                    result = self.browser.reply_to_comment(
                        comment_id_code, reply_text, article_url,
                    )
                    if result is not None:
                        replied_count += 1
                        new_responded.add(comment_id_code)
                        processed_this_run += 1

                        # Increment the in-run reply count for this commenter on this article
                        if commenter_username:
                            article_counts = replied_this_run.setdefault(article_id_str, {})
                            article_counts[commenter_username] = (
                                article_counts.get(commenter_username, 0) + 1
                            )

                        self._log_action(
                            "reply_comment", comment_id_code, article_id,
                            article_title, commenter_username,
                            reply_text=reply_text,
                        )
                        logger.info(
                            "Replied to comment %s: '%s'",
                            comment_id_code, reply_text[:60],
                        )

                        # Track our reply for orphan cleanup on future runs.
                        # Re-fetch comments to discover the id_code of our
                        # newly posted reply (Forem does not return it in the
                        # browser reply result).
                        try:
                            our_reply_id = self._find_our_reply_id_code(
                                article_id, comment_id_code,
                            )
                            if our_reply_id:
                                self.save_our_reply(
                                    our_reply_id,
                                    comment_id_code,
                                    article_url,
                                    article_id,
                                )
                            else:
                                logger.warning(
                                    "Could not discover id_code of our reply "
                                    "to comment %s — orphan tracking skipped "
                                    "for this reply.",
                                    comment_id_code,
                                )
                        except Exception as track_exc:
                            logger.warning(
                                "save_our_reply() failed for reply to %s: %s. "
                                "Orphan tracking skipped (non-fatal).",
                                comment_id_code, track_exc,
                            )
                    else:
                        logger.error(
                            "Browser returned None for reply to comment %s. "
                            "Marking as processed to prevent retry on next cron run.",
                            comment_id_code,
                        )
                        try:
                            self._log_action(
                                "reply_failed", comment_id_code, article_id,
                                article_title, commenter_username,
                            )
                        except Exception as log_exc:
                            logger.warning("Failed to log reply_failed action: %s", log_exc)
                        new_responded.add(comment_id_code)
                        processed_this_run += 1
                except BrowserLoginRequired:
                    logger.error("Login required — aborting responder cycle.")
                    break
                except Exception as exc:
                    logger.warning(
                        "Unexpected error replying to comment %s: %s",
                        comment_id_code, exc,
                    )

                # Rate-limit safety between comment engagements
                time.sleep(ENGAGE_DELAY)

        # Merge in-run reply counts into the cross-run map
        for art_id, in_run_counts in replied_this_run.items():
            existing = replied_per_article.setdefault(art_id, {})
            for username, count in in_run_counts.items():
                existing[username] = existing.get(username, 0) + count

        # Save both state files atomically
        responded_ids.update(new_responded)
        self.save_responded_ids(responded_ids)
        self.save_replied_per_article(replied_per_article)

        elapsed = time.time() - start
        summary = {
            "articles_checked": len(articles),
            "comments_found": total_comments,
            "liked": liked_count,
            "replied": replied_count,
            "skipped": skipped_count,
            "trolls_skipped": trolls_skipped,
            "orphans_cleaned": orphans_cleaned,
            "elapsed_seconds": round(elapsed, 1),
        }
        logger.info(
            "=== OwnPostResponder complete: %d liked, %d replied, "
            "%d trolls_skipped, %d orphans_cleaned, %.1fs ===",
            liked_count, replied_count, trolls_skipped, orphans_cleaned, elapsed,
        )
        return summary

    # ── Logging ────────────────────────────────────────────────────────────

    def _log_action(
        self,
        action: str,
        comment_id: str,
        article_id: int,
        article_title: str,
        commenter: str,
        reply_text: str = "",
    ) -> None:
        """Append engagement action to engagement_log.jsonl."""
        path = self.data_dir / "engagement_log.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "comment_id": comment_id,
            "article_id": article_id,
            "article_title": article_title[:100],
            "commenter": commenter,
        }
        if reply_text:
            entry["reply_text"] = reply_text[:200]
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
