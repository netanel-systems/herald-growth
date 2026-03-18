"""Tests for growth.responder — OwnPostResponder (dev.to).

Gates 9-11 coverage:
1. responded_comments.json dedup — already-responded comments are skipped
2. New comments are processed (like + reply)
3. Reply prompt rejects generic output
4. Graceful handling of no new comments
5. Graceful handling of API failure (no crash, logs warning)
6. Own comments are skipped and marked to avoid re-checking
"""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from growth.config import GrowthConfig
from growth.responder import OwnPostResponder


def _make_config(tmp_path: Path) -> GrowthConfig:
    """Return a minimal GrowthConfig pointing at a temp data dir."""
    config = GrowthConfig(
        devto_api_key="test-key",
        devto_username="testuser",
        project_root=tmp_path,
        data_dir=Path("data"),
    )
    return config


def _make_comment(id_code: str, username: str, body: str) -> dict:
    """Build a comment dict matching the real Forem API response.

    The ``GET /api/comments?a_id=`` endpoint does NOT return a numeric
    ``id`` — only ``id_code`` (string).  Tests must mirror that reality.
    """
    return {
        "id_code": id_code,
        "body_html": body,
        "user": {"username": username},
    }


def _make_article(article_id: int, title: str, url: str) -> dict:
    return {
        "id": article_id,
        "title": title,
        "url": url,
        "slug": title.lower().replace(" ", "-"),
    }


class TestOwnPostResponderDedup(unittest.TestCase):
    """Test that already-responded comments are skipped."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self, llm_fn=None):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        if llm_fn is None:
            llm_fn = lambda body, title: "Interesting point about that specific thing."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_already_responded_comment_is_skipped(self):
        """Comment in responded_comments.json must be skipped without processing."""
        # Pre-populate responded_comments.json with one comment id_code
        responded_path = self.data_dir / "responded_comments.json"
        responded_path.write_text(json.dumps(["abc123"]))

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("abc123", "someuser", "Great work!")
        ]

        summary = responder.run()

        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["liked"], 0)

    def test_new_comment_is_processed(self):
        """New comment not in responded_comments.json must be liked and replied to."""
        responder = self._make_responder()
        responder.browser.like_comment.return_value = True
        responder.browser.reply_to_comment.return_value = {
            "status": "replied", "comment_id_code": "newcode", "source": "browser",
        }
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("newcode", "reader", "I learned a lot from this section.")
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 1)
        # Verify comment is now marked as responded
        responded_ids = responder.load_responded_ids()
        self.assertIn("newcode", responded_ids)

    def test_comment_without_numeric_id_is_not_skipped(self):
        """Regression: API returns id_code but NO numeric id — must NOT be skipped."""
        responder = self._make_responder()
        responder.browser.like_comment.return_value = True
        responder.browser.reply_to_comment.return_value = {
            "status": "replied", "comment_id_code": "34ohb", "source": "browser",
        }
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        # Real API response: only id_code, no numeric id
        responder.client.get_article_comments.return_value = [
            {"id_code": "34ohb", "body_html": "The benchmark design problem...",
             "user": {"username": "matthewhou"}, "children": []}
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 1)
        responded_ids = responder.load_responded_ids()
        self.assertIn("34ohb", responded_ids)

    def test_own_comment_is_skipped_and_marked(self):
        """Our own comments should be skipped and marked to avoid re-checking."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("owncode", "testuser", "I wrote this.")
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 0)
        # Own comment should be marked responded so it won't be checked again
        responded_ids = responder.load_responded_ids()
        self.assertIn("owncode", responded_ids)


class TestOwnPostResponderQualityGate(unittest.TestCase):
    """Test that the reply quality gate rejects generic output."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self, llm_fn):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_generic_reply_is_rejected(self):
        """LLM returning 'Thanks for reading!' must be rejected by quality gate."""
        responder = self._make_responder(
            llm_fn=lambda body, title: "Thanks for reading!"
        )
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("code200", "reader2", "Interesting stuff here.")
        ]

        summary = responder.run()

        # Reply rejected — reply count stays at 0
        self.assertEqual(summary["replied"], 0)

    def test_specific_reply_passes_quality_gate(self):
        """Specific 1-sentence reply must pass the quality gate."""
        responder = self._make_responder(
            llm_fn=lambda body, title: "That section on async patterns is something I ran into too — the backpressure problem is subtle."
        )
        responder.browser.reply_to_comment.return_value = {"status": "replied"}
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("code201", "devfan", "The async section was really well explained.")
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 1)

    def test_validate_reply_rejects_multiple_generic_phrases(self):
        """Each individual generic phrase must be caught."""
        responder = self._make_responder(lambda b, t: "")
        generic_cases = [
            "Thanks for reading!",
            "Thanks for the comment, really helpful.",
            "Glad you liked it!",
            "Great question, here is the answer.",
            "Thank you for reading through this.",
        ]
        for text in generic_cases:
            with self.subTest(text=text):
                self.assertFalse(
                    responder._validate_reply(text),
                    f"Expected quality gate to reject: '{text}'",
                )

    def test_validate_reply_rejects_self_promotion(self):
        """Self-promotion terms must be caught."""
        responder = self._make_responder(lambda b, t: "")
        self.assertFalse(
            responder._validate_reply("Check out my article on Netanel for more.")
        )

    def test_validate_reply_rejects_too_long(self):
        """Reply over 280 chars must be rejected."""
        responder = self._make_responder(lambda b, t: "")
        long_text = "A" * 281
        self.assertFalse(responder._validate_reply(long_text))

    def test_validate_reply_rejects_three_sentences(self):
        """Reply with three sentences must be rejected."""
        responder = self._make_responder(lambda b, t: "")
        three_sent = "This is sentence one. This is sentence two. This is sentence three."
        self.assertFalse(responder._validate_reply(three_sent))


class TestOwnPostResponderNoComments(unittest.TestCase):
    """Test graceful handling when there are no new comments."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        llm_fn = lambda body, title: "That is a solid point about the internals."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_no_articles_returns_zero_summary(self):
        """When fetch_own_articles() returns empty list, run() completes cleanly."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = []

        summary = responder.run()

        self.assertEqual(summary["articles_checked"], 0)
        self.assertEqual(summary["replied"], 0)
        self.assertIn("elapsed_seconds", summary)

    def test_no_comments_on_articles_returns_zero_summary(self):
        """When all articles have no comments, run() completes cleanly."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = []

        summary = responder.run()

        self.assertEqual(summary["comments_found"], 0)
        self.assertEqual(summary["replied"], 0)


class TestOwnPostResponderAPIFailure(unittest.TestCase):
    """Test graceful handling of API failures — no crash, logs warning."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self):
        from growth.client import DevToError
        mock_client = MagicMock()
        mock_browser = MagicMock()
        llm_fn = lambda body, title: "Solid insight about that particular approach."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_fetch_own_articles_api_failure_does_not_crash(self):
        """API failure in fetch_own_articles() must return empty list without raising."""
        from growth.client import DevToError

        responder = self._make_responder()
        responder.client.get_articles_by_username.side_effect = DevToError("API down")

        # Must not raise
        summary = responder.run()
        self.assertEqual(summary["articles_checked"], 0)

    def test_fetch_article_comments_api_failure_does_not_crash(self):
        """API failure in fetch_article_comments() must skip article without crashing."""
        from growth.client import DevToError

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.side_effect = DevToError("Comments API error")

        # Must not raise
        summary = responder.run()
        self.assertEqual(summary["replied"], 0)

    def test_browser_reply_failure_does_not_crash(self):
        """Browser reply failure must be logged and handled without crashing."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("code300", "reader3", "The DB section made sense to me.")
        ]
        # Browser reply raises unexpected exception
        responder.browser.reply_to_comment.side_effect = RuntimeError("Browser crashed")

        # Must not raise
        summary = responder.run()
        self.assertIn("replied", summary)

    def test_llm_failure_does_not_crash(self):
        """LLM exception must be caught, comment marked responded, cycle continues."""
        def failing_llm(body, title):
            raise RuntimeError("LLM service unavailable")

        mock_client = MagicMock()
        mock_browser = MagicMock()
        responder = OwnPostResponder(
            mock_client, self.config, mock_browser, failing_llm,
        )
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("code400", "reader4", "What about error handling?")
        ]

        # Must not raise
        summary = responder.run()
        self.assertEqual(summary["replied"], 0)


class TestOwnPostResponderStorage(unittest.TestCase):
    """Test load/save responded_comments.json operations."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        llm_fn = lambda b, t: "That is a specific and relevant observation."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_load_from_missing_file_returns_empty_set(self):
        """load_responded_ids() returns empty set when file does not exist."""
        responder = self._make_responder()
        result = responder.load_responded_ids()
        self.assertEqual(result, set())

    def test_load_from_corrupted_file_returns_empty_set(self):
        """load_responded_ids() returns empty set for corrupted JSON."""
        path = self.data_dir / "responded_comments.json"
        path.write_text("{not valid json")
        responder = self._make_responder()
        result = responder.load_responded_ids()
        self.assertEqual(result, set())

    def test_save_and_reload_ids(self):
        """IDs saved via save_responded_ids() must round-trip through load."""
        responder = self._make_responder()
        ids = {"abc", "def", "ghi"}
        responder.save_responded_ids(ids)
        loaded = responder.load_responded_ids()
        self.assertEqual(loaded, ids)

    def test_save_is_bounded_to_5000(self):
        """save_responded_ids() must cap at 5,000 entries."""
        responder = self._make_responder()
        ids = {str(i) for i in range(6000)}
        responder.save_responded_ids(ids)
        loaded = responder.load_responded_ids()
        self.assertLessEqual(len(loaded), 5000)


class TestOwnPostResponderCommenterLimit(unittest.TestCase):
    """Test max-MAX_REPLIES_PER_COMMENTER-replies-per-commenter-per-article enforcement."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self, llm_fn=None):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        mock_browser.like_comment.return_value = True
        mock_browser.reply_to_comment.return_value = {"status": "replied"}
        if llm_fn is None:
            llm_fn = lambda body, title: "Interesting take on that specific pattern."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_multiple_comments_same_user_same_article_respects_limit(self):
        """A user who posts N comments on one article gets at most MAX_REPLIES_PER_COMMENTER replies."""
        from growth.responder import MAX_REPLIES_PER_COMMENTER

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        # alice posts 5 comments; limit is MAX_REPLIES_PER_COMMENTER
        responder.client.get_article_comments.return_value = [
            _make_comment("c001", "alice", "First comment by alice."),
            _make_comment("c002", "alice", "Second comment by alice."),
            _make_comment("c003", "alice", "Third comment by alice."),
            _make_comment("c004", "alice", "Fourth comment by alice."),
            _make_comment("c005", "alice", "Fifth comment by alice."),
        ]

        summary = responder.run()

        # alice gets exactly MAX_REPLIES_PER_COMMENTER (3) replies
        self.assertEqual(summary["replied"], MAX_REPLIES_PER_COMMENTER)
        # The remaining 2 comments are skipped
        self.assertEqual(summary["skipped"], 5 - MAX_REPLIES_PER_COMMENTER)
        # alice recorded in replied_per_article.json with count = MAX_REPLIES_PER_COMMENTER
        replied = responder.load_replied_per_article()
        self.assertIn("alice", replied.get("1", {}))
        self.assertEqual(replied["1"]["alice"], MAX_REPLIES_PER_COMMENTER)

    def test_same_user_different_articles_both_get_replies(self):
        """A user may receive replies per article — restriction is per-article."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "Article One", "https://dev.to/testuser/article-one"),
            _make_article(2, "Article Two", "https://dev.to/testuser/article-two"),
        ]

        def side_effect_comments(article_id):
            if article_id == 1:
                return [_make_comment("c010", "bob", "Comment on article one.")]
            return [_make_comment("c011", "bob", "Comment on article two.")]

        responder.client.get_article_comments.side_effect = side_effect_comments

        summary = responder.run()

        self.assertEqual(summary["replied"], 2)
        replied = responder.load_replied_per_article()
        self.assertIn("bob", replied.get("1", {}))
        self.assertIn("bob", replied.get("2", {}))

    def test_cross_run_persistence_blocks_reply_at_limit(self):
        """Cross-run: a pre-populated replied_per_article.json at max count prevents further replies."""
        from growth.responder import MAX_REPLIES_PER_COMMENTER

        # Pre-populate with alice already at MAX_REPLIES_PER_COMMENTER (new dict format)
        replied_path = self.data_dir / "replied_per_article.json"
        replied_path.write_text(json.dumps({"1": {"alice": MAX_REPLIES_PER_COMMENTER}}))

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        # alice posts a new comment — but she is already at the limit
        responder.client.get_article_comments.return_value = [
            _make_comment("c020", "alice", "Alice comments again after reaching limit."),
        ]

        summary = responder.run()

        # No reply — alice is at MAX_REPLIES_PER_COMMENTER
        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_user_with_count_zero_gets_first_reply(self):
        """A user with no prior reply count (count=0) is under the limit and gets a reply."""
        from growth.responder import MAX_REPLIES_PER_COMMENTER

        # Pre-populate with alice at count=0 (below MAX_REPLIES_PER_COMMENTER=1)
        replied_path = self.data_dir / "replied_per_article.json"
        replied_path.write_text(json.dumps({"1": {"alice": 0}}))

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("c030", "alice", "Alice posts a comment — count was 0."),
        ]

        summary = responder.run()

        # count=0 < MAX_REPLIES_PER_COMMENTER=1, so a reply IS sent
        self.assertEqual(summary["replied"], 1)
        replied = responder.load_replied_per_article()
        # Count should now be 1 (0 cross-run + 1 in-run)
        self.assertEqual(replied["1"]["alice"], MAX_REPLIES_PER_COMMENTER)

    def test_user_with_count_3_gets_no_reply(self):
        """A user with count=3 (at MAX_REPLIES_PER_COMMENTER) gets no further replies."""
        from growth.responder import MAX_REPLIES_PER_COMMENTER

        replied_path = self.data_dir / "replied_per_article.json"
        replied_path.write_text(json.dumps({"1": {"alice": MAX_REPLIES_PER_COMMENTER}}))

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("c031", "alice", "Alice comments but is at the limit."),
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_backward_compat_list_format_loaded_as_fully_used(self):
        """Legacy list format on disk is loaded without error as fully-used entries."""
        from growth.responder import MAX_REPLIES_PER_COMMENTER

        # Write the old list-format file (as currently exists on disk)
        replied_path = self.data_dir / "replied_per_article.json"
        replied_path.write_text(json.dumps({"3321258": ["nyrok"]}))

        responder = self._make_responder()
        loaded = responder.load_replied_per_article()

        # Must parse without error and treat legacy entry as fully used up
        self.assertIn("3321258", loaded)
        self.assertIn("nyrok", loaded["3321258"])
        self.assertEqual(loaded["3321258"]["nyrok"], MAX_REPLIES_PER_COMMENTER)

    def test_backward_compat_list_format_blocks_reply_on_next_run(self):
        """After loading legacy list format, commenter is correctly blocked on the next run."""
        # Write the old list-format file
        replied_path = self.data_dir / "replied_per_article.json"
        replied_path.write_text(json.dumps({"1": ["alice"]}))

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        # alice posts a new comment — but she is in the legacy list (fully used up)
        responder.client.get_article_comments.return_value = [
            _make_comment("c040", "alice", "Alice comments, was in legacy list."),
        ]

        summary = responder.run()

        # No reply — alice is treated as having MAX_REPLIES_PER_COMMENTER from the legacy list
        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_replied_per_article_written_atomically_and_bounded(self):
        """save_replied_per_article caps at MAX_REPLIED_ARTICLES=500 and uses atomic write."""
        from growth.responder import MAX_REPLIED_ARTICLES

        responder = self._make_responder()
        # Build a dict with 600 entries (over cap), using new dict[str, dict[str, int]] format
        large_replied = {str(i): {"user1": 1} for i in range(600)}
        responder.save_replied_per_article(large_replied)

        loaded = responder.load_replied_per_article()
        self.assertLessEqual(len(loaded), MAX_REPLIED_ARTICLES)

    def test_load_replied_per_article_missing_file_returns_empty(self):
        """load_replied_per_article() returns {} when file does not exist."""
        responder = self._make_responder()
        self.assertEqual(responder.load_replied_per_article(), {})

    def test_load_replied_per_article_corrupted_file_returns_empty(self):
        """load_replied_per_article() returns {} for corrupted JSON."""
        path = self.data_dir / "replied_per_article.json"
        path.write_text("{not valid json")
        responder = self._make_responder()
        self.assertEqual(responder.load_replied_per_article(), {})


class TestOwnPostResponderTrollDetection(unittest.TestCase):
    """Test troll detection — hostile comments get no engagement."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self, troll_detect_fn=None, llm_fn=None):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        mock_browser.like_comment.return_value = True
        mock_browser.reply_to_comment.return_value = {"status": "replied"}
        if llm_fn is None:
            llm_fn = lambda body, title: "Good point about that specific mechanism."
        return OwnPostResponder(
            mock_client, self.config, mock_browser, llm_fn,
            troll_detect_fn=troll_detect_fn,
        )

    def test_troll_comment_gets_no_like_and_no_reply(self):
        """A comment flagged by troll_detect_fn must receive no like and no reply."""
        # Always-troll detector
        responder = self._make_responder(troll_detect_fn=lambda body: True)
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("t001", "troll_user", "You are a complete fraud and idiot."),
        ]

        summary = responder.run()

        self.assertEqual(summary["liked"], 0)
        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["trolls_skipped"], 1)
        # Browser must not have been called for like or reply
        responder.browser.like_comment.assert_not_called()
        responder.browser.reply_to_comment.assert_not_called()

    def test_troll_comment_marked_as_processed(self):
        """Troll comment must be added to responded_comments.json so it is not re-evaluated."""
        responder = self._make_responder(troll_detect_fn=lambda body: True)
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("t002", "troll_user", "Hostile bait comment."),
        ]

        responder.run()

        responded_ids = responder.load_responded_ids()
        self.assertIn("t002", responded_ids)

    def test_troll_comment_logged_as_skip_troll(self):
        """Troll comment must produce an engagement_log.jsonl entry with action=skip_troll."""
        responder = self._make_responder(troll_detect_fn=lambda body: True)
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("t003", "troll_user", "You should quit writing."),
        ]

        responder.run()

        log_path = self.data_dir / "engagement_log.jsonl"
        self.assertTrue(log_path.exists(), "engagement_log.jsonl must be created")
        entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        troll_entries = [e for e in entries if e.get("action") == "skip_troll"]
        self.assertEqual(len(troll_entries), 1)
        self.assertEqual(troll_entries[0]["comment_id"], "t003")

    def test_genuine_comment_not_flagged_when_troll_detect_returns_false(self):
        """A genuine comment where troll_detect_fn returns False must be processed normally."""
        responder = self._make_responder(troll_detect_fn=lambda body: False)
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("g001", "genuine_user", "Really helpful breakdown of the cache layer."),
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 1)
        self.assertEqual(summary["trolls_skipped"], 0)

    def test_no_troll_detect_fn_means_all_comments_pass(self):
        """When troll_detect_fn is None (default), troll detection is disabled."""
        responder = self._make_responder(troll_detect_fn=None)
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("g002", "reader", "The concurrency section was eye-opening."),
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 1)
        self.assertEqual(summary["trolls_skipped"], 0)

    def test_troll_detect_fn_exception_is_safe_default(self):
        """An exception in troll_detect_fn must not crash the cycle — defaults to non-troll."""
        def exploding_detector(body):
            raise RuntimeError("Detector service down")

        responder = self._make_responder(troll_detect_fn=exploding_detector)
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("g003", "reader2", "What library did you use for this?"),
        ]

        # Must not raise — exception in detector is caught safely
        summary = responder.run()
        self.assertEqual(summary["trolls_skipped"], 0)
        # Comment proceeds normally (safe default = non-troll)
        self.assertEqual(summary["replied"], 1)

    def test_mixed_troll_and_genuine_comments_processed_correctly(self):
        """In a batch with mixed comments, trolls are skipped and genuine ones replied to."""
        call_count = 0

        def selective_detector(body):
            return "fraud" in body.lower() or "idiot" in body.lower()

        responder = self._make_responder(troll_detect_fn=selective_detector)
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment("m001", "genuine1", "The architecture diagram really helped me."),
            _make_comment("m002", "troll1", "You are a fraud and this is wrong."),
            _make_comment("m003", "genuine2", "Have you benchmarked this against alternatives?"),
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 2)
        self.assertEqual(summary["trolls_skipped"], 1)

    def test_summary_includes_trolls_skipped_key(self):
        """run() summary dict must always contain trolls_skipped key."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = []

        summary = responder.run()

        self.assertIn("trolls_skipped", summary)


class TestOurRepliesStorage(unittest.TestCase):
    """Test load/save our_replies.json — the orphan tracking map."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        llm_fn = lambda b, t: "That is a specific and relevant observation."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_load_our_replies_missing_file_returns_empty_dict(self):
        """load_our_replies() returns {} when our_replies.json does not exist."""
        responder = self._make_responder()
        result = responder.load_our_replies()
        self.assertEqual(result, {})

    def test_load_our_replies_corrupted_file_returns_empty_dict(self):
        """load_our_replies() returns {} for corrupted JSON."""
        path = self.data_dir / "our_replies.json"
        path.write_text("{not valid json")
        responder = self._make_responder()
        result = responder.load_our_replies()
        self.assertEqual(result, {})

    def test_load_our_replies_wrong_type_returns_empty_dict(self):
        """load_our_replies() returns {} if file contains a list instead of dict."""
        path = self.data_dir / "our_replies.json"
        path.write_text(json.dumps(["abc", "def"]))
        responder = self._make_responder()
        result = responder.load_our_replies()
        self.assertEqual(result, {})

    def test_save_our_reply_creates_file_and_round_trips(self):
        """save_our_reply() writes to our_replies.json and load_our_replies() reads it back."""
        responder = self._make_responder()
        responder.save_our_reply(
            our_id_code="reply1",
            parent_id_code="parent1",
            article_url="https://dev.to/user/article",
            article_id=42,
        )
        loaded = responder.load_our_replies()
        self.assertIn("reply1", loaded)
        self.assertEqual(loaded["reply1"]["parent_id_code"], "parent1")
        self.assertEqual(loaded["reply1"]["article_url"], "https://dev.to/user/article")
        self.assertEqual(loaded["reply1"]["article_id"], 42)

    def test_save_our_reply_accumulates_multiple_entries(self):
        """Multiple calls to save_our_reply() accumulate entries in the file."""
        responder = self._make_responder()
        responder.save_our_reply("r1", "p1", "https://dev.to/u/a1", 1)
        responder.save_our_reply("r2", "p2", "https://dev.to/u/a2", 2)
        loaded = responder.load_our_replies()
        self.assertIn("r1", loaded)
        self.assertIn("r2", loaded)
        self.assertEqual(len(loaded), 2)

    def test_load_our_replies_skips_malformed_entries(self):
        """load_our_replies() silently skips entries with missing required fields."""
        path = self.data_dir / "our_replies.json"
        # "bad" entry is missing article_id
        path.write_text(json.dumps({
            "good": {"parent_id_code": "p1", "article_url": "https://x.com", "article_id": 1},
            "bad": {"parent_id_code": "p2"},  # missing article_url and article_id
        }))
        responder = self._make_responder()
        loaded = responder.load_our_replies()
        self.assertIn("good", loaded)
        self.assertNotIn("bad", loaded)


class TestCollectAllIdCodes(unittest.TestCase):
    """Test _collect_all_id_codes() — recursive comment tree traversal."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        llm_fn = lambda b, t: "Specific and relevant."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_flat_list_collects_all_id_codes(self):
        """Flat comment list: all top-level id_codes are collected."""
        responder = self._make_responder()
        comments = [
            {"id_code": "a1", "children": []},
            {"id_code": "a2", "children": []},
        ]
        result = responder._collect_all_id_codes(comments)
        self.assertEqual(result, {"a1", "a2"})

    def test_nested_children_collected(self):
        """Nested children at arbitrary depth are collected."""
        responder = self._make_responder()
        comments = [
            {
                "id_code": "root",
                "children": [
                    {
                        "id_code": "child1",
                        "children": [
                            {"id_code": "grandchild1", "children": []},
                        ],
                    },
                ],
            },
        ]
        result = responder._collect_all_id_codes(comments)
        self.assertEqual(result, {"root", "child1", "grandchild1"})

    def test_empty_list_returns_empty_set(self):
        """Empty comment list returns empty set."""
        responder = self._make_responder()
        self.assertEqual(responder._collect_all_id_codes([]), set())

    def test_comment_without_id_code_is_skipped(self):
        """Comments missing id_code are not added to the set."""
        responder = self._make_responder()
        comments = [{"body_html": "no id code here", "children": []}]
        result = responder._collect_all_id_codes(comments)
        self.assertEqual(result, set())


class TestCleanOrphanedReplies(unittest.TestCase):
    """Test clean_orphaned_replies() — core orphan detection and deletion logic."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _make_config(self.tmp)
        self.data_dir = self.config.abs_data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _make_responder(self, llm_fn=None):
        mock_client = MagicMock()
        mock_browser = MagicMock()
        mock_browser.delete_comment.return_value = True
        if llm_fn is None:
            llm_fn = lambda b, t: "Good point about that specific aspect."
        return OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

    def test_no_tracked_replies_returns_zero(self):
        """clean_orphaned_replies() returns 0 when our_replies.json is empty/missing."""
        responder = self._make_responder()
        result = responder.clean_orphaned_replies()
        self.assertEqual(result, 0)
        responder.browser.delete_comment.assert_not_called()

    def test_parent_still_exists_no_deletion(self):
        """When the parent comment is still in the live tree, no deletion occurs."""
        responder = self._make_responder()
        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 10)

        # Live comment tree includes the parent
        responder.client.get_article_comments.return_value = [
            {"id_code": "parent1", "children": []},
        ]

        result = responder.clean_orphaned_replies()
        self.assertEqual(result, 0)
        responder.browser.delete_comment.assert_not_called()

    def test_parent_missing_triggers_deletion(self):
        """When the parent comment is absent from the live tree, the reply is deleted."""
        responder = self._make_responder()
        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 10)

        # Live tree does NOT contain parent1
        responder.client.get_article_comments.return_value = [
            {"id_code": "other_comment", "children": []},
        ]

        result = responder.clean_orphaned_replies()
        self.assertEqual(result, 1)
        responder.browser.delete_comment.assert_called_once_with(
            "our1", "https://dev.to/u/a"
        )

    def test_deleted_entry_removed_from_our_replies_json(self):
        """Successfully deleted orphan is removed from our_replies.json."""
        responder = self._make_responder()
        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 10)

        # Parent is gone
        responder.client.get_article_comments.return_value = []

        responder.clean_orphaned_replies()

        remaining = responder.load_our_replies()
        self.assertNotIn("our1", remaining)

    def test_failed_deletion_keeps_entry_in_our_replies_json(self):
        """If delete fails, the entry stays in our_replies.json for next run retry."""
        responder = self._make_responder()
        responder.browser.delete_comment.return_value = False  # deletion fails
        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 10)

        # Parent is gone
        responder.client.get_article_comments.return_value = []

        result = responder.clean_orphaned_replies()

        # Count is 0 — deletion failed
        self.assertEqual(result, 0)
        # Entry stays for retry
        remaining = responder.load_our_replies()
        self.assertIn("our1", remaining)

    def test_cap_at_max_orphan_deletions_per_run(self):
        """clean_orphaned_replies() deletes at most MAX_ORPHAN_DELETIONS_PER_RUN replies."""
        from growth.responder import MAX_ORPHAN_DELETIONS_PER_RUN

        responder = self._make_responder()

        # Add MAX_ORPHAN_DELETIONS_PER_RUN + 5 tracked replies, all orphaned
        for i in range(MAX_ORPHAN_DELETIONS_PER_RUN + 5):
            responder.save_our_reply(
                f"our{i}", f"parent{i}", "https://dev.to/u/a", 10,
            )

        # No live comments — all parents missing
        responder.client.get_article_comments.return_value = []

        result = responder.clean_orphaned_replies()
        self.assertLessEqual(result, MAX_ORPHAN_DELETIONS_PER_RUN)

    def test_deletion_logged_to_engagement_log(self):
        """Each orphan deletion is recorded in engagement_log.jsonl."""
        responder = self._make_responder()
        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 10)

        # Parent is gone
        responder.client.get_article_comments.return_value = []

        responder.clean_orphaned_replies()

        log_path = self.data_dir / "engagement_log.jsonl"
        self.assertTrue(log_path.exists(), "engagement_log.jsonl must be created")
        entries = [
            json.loads(line)
            for line in log_path.read_text().splitlines()
            if line.strip()
        ]
        orphan_entries = [e for e in entries if e.get("action") == "delete_orphaned_reply"]
        self.assertEqual(len(orphan_entries), 1)
        self.assertEqual(orphan_entries[0]["comment_id"], "our1")

    def test_browser_delete_exception_is_non_fatal(self):
        """Exception raised by browser.delete_comment() does not crash the cycle."""
        responder = self._make_responder()
        responder.browser.delete_comment.side_effect = RuntimeError("browser crashed")
        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 10)

        # Parent is gone
        responder.client.get_article_comments.return_value = []

        # Must not raise
        result = responder.clean_orphaned_replies()
        self.assertEqual(result, 0)

    def test_multiple_articles_fetch_comments_once_each(self):
        """Comments are fetched once per article, not once per tracked reply."""
        responder = self._make_responder()
        # Two replies on article 10, one on article 20
        responder.save_our_reply("our1", "p1", "https://dev.to/u/a1", 10)
        responder.save_our_reply("our2", "p2", "https://dev.to/u/a1", 10)
        responder.save_our_reply("our3", "p3", "https://dev.to/u/a2", 20)

        responder.client.get_article_comments.return_value = []  # all parents gone

        responder.clean_orphaned_replies()

        # get_article_comments called twice (once per article), not three times
        self.assertEqual(responder.client.get_article_comments.call_count, 2)

    def test_parent_in_nested_child_not_treated_as_orphan(self):
        """A parent that is a nested child in the tree is NOT treated as orphaned."""
        responder = self._make_responder()
        responder.save_our_reply("our1", "deep_child", "https://dev.to/u/a", 10)

        # Parent exists but is deeply nested
        responder.client.get_article_comments.return_value = [
            {
                "id_code": "root",
                "children": [
                    {
                        "id_code": "deep_child",
                        "children": [],
                    }
                ],
            }
        ]

        result = responder.clean_orphaned_replies()
        self.assertEqual(result, 0)
        responder.browser.delete_comment.assert_not_called()

    def test_browser_without_delete_comment_does_not_crash(self):
        """If browser lacks delete_comment(), clean_orphaned_replies() logs and continues."""
        mock_client = MagicMock()
        mock_browser = MagicMock(spec=[])  # spec=[] means no attributes
        llm_fn = lambda b, t: "Specific point."
        responder = OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)

        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 10)
        responder.client.get_article_comments.return_value = []

        # Must not raise
        result = responder.clean_orphaned_replies()
        self.assertEqual(result, 0)

    def test_run_summary_includes_orphans_cleaned_key(self):
        """run() summary dict must always contain orphans_cleaned key."""
        mock_client = MagicMock()
        mock_browser = MagicMock()
        mock_browser.delete_comment.return_value = True
        llm_fn = lambda b, t: "Specific and relevant."
        responder = OwnPostResponder(mock_client, self.config, mock_browser, llm_fn)
        responder.client.get_articles_by_username.return_value = []

        summary = responder.run()

        self.assertIn("orphans_cleaned", summary)

    def test_run_calls_clean_orphaned_replies_before_new_comments(self):
        """run() invokes clean_orphaned_replies() before processing new comments."""
        responder = self._make_responder()

        # Pre-populate one tracked reply with missing parent
        responder.save_our_reply("our1", "parent1", "https://dev.to/u/a", 1)

        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/u/a"),
        ]
        # Live tree has no parent1 — orphan should be cleaned
        responder.client.get_article_comments.return_value = []

        summary = responder.run()

        # Orphan cleaned
        self.assertGreater(summary["orphans_cleaned"], 0)
        responder.browser.delete_comment.assert_called()


if __name__ == "__main__":
    unittest.main()
