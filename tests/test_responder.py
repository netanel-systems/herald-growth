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


def _make_comment(comment_id: int, id_code: str, username: str, body: str) -> dict:
    return {
        "id": comment_id,
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
            _make_comment(100, "abc123", "someuser", "Great work!")
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
            "status": "replied", "comment_id": 101, "source": "browser",
        }
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment(101, "newcode", "reader", "I learned a lot from this section.")
        ]

        summary = responder.run()

        self.assertEqual(summary["replied"], 1)
        # Verify comment is now marked as responded
        responded_ids = responder.load_responded_ids()
        self.assertIn("newcode", responded_ids)

    def test_own_comment_is_skipped_and_marked(self):
        """Our own comments should be skipped and marked to avoid re-checking."""
        responder = self._make_responder()
        responder.client.get_articles_by_username.return_value = [
            _make_article(1, "My Article", "https://dev.to/testuser/my-article")
        ]
        responder.client.get_article_comments.return_value = [
            _make_comment(102, "owncode", "testuser", "I wrote this.")
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
            _make_comment(200, "code200", "reader2", "Interesting stuff here.")
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
            _make_comment(201, "code201", "devfan", "The async section was really well explained.")
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
            _make_comment(300, "code300", "reader3", "The DB section made sense to me.")
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
            _make_comment(400, "code400", "reader4", "What about error handling?")
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


if __name__ == "__main__":
    unittest.main()
