"""Tests for ReactionEngine learner integration points.

Covers:
- _filter_by_learner() — removes candidates with low-performing tags
- _run_learner_analyze() — calls analyze() safely, never crashes cycle
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from growth.config import GrowthConfig
from growth.reactor import ReactionEngine


@pytest.fixture()
def config(tmp_path: Path) -> GrowthConfig:
    """Minimal GrowthConfig with tmp_path as project root."""
    return GrowthConfig(project_root=tmp_path)


@pytest.fixture()
def engine(config: GrowthConfig) -> ReactionEngine:
    """ReactionEngine with mocked client and scout (no network calls)."""
    eng = ReactionEngine(config)
    eng.client = MagicMock()
    eng.scout = MagicMock()
    return eng


def _article(article_id: int, tags: list[str]) -> dict:
    """Build a minimal article dict."""
    return {
        "id": article_id,
        "title": f"Article {article_id}",
        "url": f"https://dev.to/author/article-{article_id}",
        "tag_list": tags,
        "tags": tags,
        "user": {"username": "author"},
    }


# ── _filter_by_learner tests ──────────────────────────────────────

def test_filter_by_learner_no_learnings_passes_all(engine: ReactionEngine) -> None:
    """With no learnings.json, all candidates pass through unfiltered."""
    candidates = [_article(1, ["python"]), _article(2, ["ai"])]
    result = engine._filter_by_learner(candidates)
    assert len(result) == 2


def test_filter_by_learner_removes_skipped_tag(engine: ReactionEngine, tmp_path: Path) -> None:
    """Candidates whose tags match a skip-learning are removed."""
    # Pre-seed a skip learning for 'badtag'
    from growth.learner import GrowthLearner
    learner = GrowthLearner(engine.config)
    learner.store_learning(
        pattern="skip tag 'badtag' — zero reciprocity across 10 events",
        confidence=0.8,
        evidence="tag=badtag reactions=0 comments=0 total=10",
    )

    candidates = [
        _article(1, ["python"]),    # good tag
        _article(2, ["badtag"]),    # should be filtered out
        _article(3, ["ai"]),        # good tag
    ]
    result = engine._filter_by_learner(candidates)

    assert len(result) == 2
    ids = [a["id"] for a in result]
    assert 1 in ids
    assert 3 in ids
    assert 2 not in ids  # badtag article removed


def test_filter_by_learner_keeps_all_good_tags(engine: ReactionEngine, tmp_path: Path) -> None:
    """Candidates with tags not in skip-learnings all pass through."""
    from growth.learner import GrowthLearner
    learner = GrowthLearner(engine.config)
    learner.store_learning(
        pattern="skip tag 'badtag' — zero reciprocity across 5 events",
        confidence=0.75,
        evidence="tag=badtag reactions=0 comments=0 total=5",
    )

    candidates = [_article(i, ["python", "ai"]) for i in range(5)]
    result = engine._filter_by_learner(candidates)
    assert len(result) == 5


def test_filter_by_learner_exception_returns_all(engine: ReactionEngine) -> None:
    """If GrowthLearner raises any exception, all candidates are returned unfiltered."""
    candidates = [_article(1, ["python"]), _article(2, ["ai"])]

    with patch("growth.reactor.GrowthLearner") as mock_learner_cls:
        mock_learner_cls.return_value.should_skip_tag.side_effect = RuntimeError("learner broken")
        result = engine._filter_by_learner(candidates)

    assert len(result) == 2  # fallback: unfiltered


def test_filter_by_learner_empty_candidates(engine: ReactionEngine) -> None:
    """Empty candidate list returns empty list."""
    result = engine._filter_by_learner([])
    assert result == []


def test_filter_by_learner_skips_low_confidence_learnings(
    engine: ReactionEngine, tmp_path: Path
) -> None:
    """Skip learnings with confidence < 0.7 should NOT filter candidates."""
    from growth.learner import GrowthLearner
    learner = GrowthLearner(engine.config)
    # Store a skip learning below the threshold (0.7 minimum for skip)
    learner.store_learning(
        pattern="skip tag 'marginal' — zero reciprocity across 3 events",
        confidence=0.5,  # below 0.7 threshold in should_skip_tag
        evidence="tag=marginal reactions=0 comments=0 total=3",
    )

    candidates = [_article(1, ["marginal"])]
    result = engine._filter_by_learner(candidates)
    # Low-confidence skip learning should not filter out the article
    assert len(result) == 1


# ── _run_learner_analyze tests ────────────────────────────────────

def test_run_learner_analyze_calls_analyze(engine: ReactionEngine) -> None:
    """_run_learner_analyze() calls GrowthLearner.analyze()."""
    with patch("growth.reactor.GrowthLearner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.analyze.return_value = []
        mock_cls.return_value = mock_instance

        engine._run_learner_analyze()

        mock_instance.analyze.assert_called_once()


def test_run_learner_analyze_exception_does_not_raise(engine: ReactionEngine) -> None:
    """_run_learner_analyze() swallows all exceptions — never crashes the cycle."""
    with patch("growth.reactor.GrowthLearner") as mock_cls:
        mock_cls.return_value.analyze.side_effect = RuntimeError("analyze broken")

        # Should not raise
        engine._run_learner_analyze()


def test_run_learner_analyze_logs_count(engine: ReactionEngine, caplog) -> None:
    """_run_learner_analyze() logs the number of new learnings at INFO level."""
    import logging
    with patch("growth.reactor.GrowthLearner") as mock_cls:
        mock_cls.return_value.analyze.return_value = [{"tag": "python"}, {"tag": "ai"}]

        with caplog.at_level(logging.INFO, logger="growth.reactor"):
            engine._run_learner_analyze()

    assert "2 new learnings" in caplog.text
