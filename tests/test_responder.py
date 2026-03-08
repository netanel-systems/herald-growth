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
    """Test max-1-reply-per-commenter-per-article enforcement."""

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

    def test_multiple_comments_same_user_same_article_gets_one_reply(self):
        """A user who posts N comments on one article gets exactly 1 reply in-run."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        # Same user posts three comments on the same article
        responder.client.get_article_comments.return_value = [
            _make_comment("c001", "alice", "First comment by alice."),
            _make_comment("c002", "alice", "Second comment by alice."),
            _make_comment("c003", "alice", "Third comment by alice."),
        ]

        summary = responder.run()

        # Only the first comment from alice should trigger a reply
        self.assertEqual(summary["replied"], 1)
        # The other two should be counted as skipped (marked processed)
        self.assertEqual(summary["skipped"], 2)
        # alice recorded in replied_per_article.json
        replied = responder.load_replied_per_article()
        self.assertIn("alice", replied.get("1", []))

    def test_same_user_different_articles_both_get_replies(self):
        """A user may receive one reply per article — restriction is per-article."""
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
        self.assertIn("bob", replied.get("1", []))
        self.assertIn("bob", replied.get("2", []))

    def test_cross_run_persistence_blocks_second_reply(self):
        """Cross-run: a pre-populated replied_per_article.json prevents a second reply."""
        # Pre-populate replied_per_article.json as if a previous cron already replied
        replied_path = self.data_dir / "replied_per_article.json"
        replied_path.write_text(json.dumps({"1": ["alice"]}))

        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        # alice posts a new comment — but we already replied to her in a prior run
        responder.client.get_article_comments.return_value = [
            _make_comment("c020", "alice", "Alice comments again after prior reply."),
        ]

        summary = responder.run()

        # No reply — alice is already in the cross-run map
        self.assertEqual(summary["replied"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_replied_per_article_written_atomically_and_bounded(self):
        """save_replied_per_article caps at MAX_REPLIED_ARTICLES=500 and uses atomic write."""
        from growth.responder import MAX_REPLIED_ARTICLES

        responder = self._make_responder()
        # Build a dict with 600 entries (over cap)
        large_replied = {str(i): ["user1"] for i in range(600)}
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


if __name__ == "__main__":
    unittest.main()
