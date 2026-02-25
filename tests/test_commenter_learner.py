"""Tests for CommentEngine learner integration points.

Covers:
- get_learnings_context() — returns learnings list for prompt injection
- run_learner_analyze() — calls analyze() safely, never crashes cycle
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from growth.commenter import CommentEngine
from growth.config import GrowthConfig


@pytest.fixture()
def config(tmp_path: Path) -> GrowthConfig:
    """GrowthConfig with tmp_path as project root."""
    return GrowthConfig(project_root=tmp_path)


@pytest.fixture()
def engine(config: GrowthConfig) -> CommentEngine:
    """CommentEngine with mocked client (no network calls)."""
    return CommentEngine(client=MagicMock(), config=config)


# ── get_learnings_context tests ───────────────────────────────────

def test_get_learnings_context_no_learnings_returns_empty(engine: CommentEngine) -> None:
    """With no learnings.json, returns empty list."""
    result = engine.get_learnings_context()
    assert result == []


def test_get_learnings_context_returns_insights(
    engine: CommentEngine, tmp_path: Path
) -> None:
    """With stored high-confidence learnings, returns formatted bullet strings."""
    from growth.learner import GrowthLearner
    learner = GrowthLearner(engine.config)
    learner.store_learning(
        pattern="tag 'python' yields high engagement — prioritize",
        confidence=0.85,
        evidence="tag=python reactions=10 comments=2 total=12",
    )
    learner.store_learning(
        pattern="tag 'ai' yields high engagement — prioritize",
        confidence=0.90,
        evidence="tag=ai reactions=8 comments=3 total=11",
    )

    result = engine.get_learnings_context(max_learnings=5)

    assert len(result) == 2
    # Each insight should be a bullet string
    for insight in result:
        assert isinstance(insight, str)
        assert insight.startswith("- ")


def test_get_learnings_context_max_learnings_respected(
    engine: CommentEngine, tmp_path: Path
) -> None:
    """max_learnings parameter limits the number of insights returned."""
    from growth.learner import GrowthLearner
    learner = GrowthLearner(engine.config)
    for i in range(10):
        learner.store_learning(
            pattern=f"tag 'tag{i}' yields high engagement",
            confidence=0.80,
            evidence=f"tag=tag{i} total=10",
        )

    result = engine.get_learnings_context(max_learnings=3)
    assert len(result) <= 3


def test_get_learnings_context_filters_low_confidence(
    engine: CommentEngine, tmp_path: Path
) -> None:
    """Learnings with confidence < 0.5 are excluded from prompt context."""
    from growth.learner import GrowthLearner
    learner = GrowthLearner(engine.config)
    # Low confidence — should be filtered by get_insights_for_prompt
    learner.store_learning(
        pattern="tag 'maybe' might work",
        confidence=0.3,
        evidence="tag=maybe total=6",
    )

    result = engine.get_learnings_context()
    assert result == []


def test_get_learnings_context_exception_returns_empty(engine: CommentEngine) -> None:
    """Any exception from GrowthLearner returns empty list — never crashes cycle."""
    with patch("growth.commenter.GrowthLearner") as mock_cls:
        mock_cls.return_value.get_insights_for_prompt.side_effect = RuntimeError("learner broken")

        result = engine.get_learnings_context()

    assert result == []


def test_get_learnings_context_returns_list_of_strings(
    engine: CommentEngine, tmp_path: Path
) -> None:
    """Return type is always list[str] — caller can directly join into prompt."""
    from growth.learner import GrowthLearner
    learner = GrowthLearner(engine.config)
    learner.store_learning(
        pattern="tag 'python' is strong",
        confidence=0.75,
        evidence="tag=python total=8",
    )

    result = engine.get_learnings_context()
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, str)


# ── run_learner_analyze tests ─────────────────────────────────────

def test_run_learner_analyze_calls_analyze(engine: CommentEngine) -> None:
    """run_learner_analyze() calls GrowthLearner.analyze()."""
    with patch("growth.commenter.GrowthLearner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.analyze.return_value = []
        mock_cls.return_value = mock_instance

        engine.run_learner_analyze()

        mock_instance.analyze.assert_called_once()


def test_run_learner_analyze_exception_does_not_raise(engine: CommentEngine) -> None:
    """run_learner_analyze() swallows all exceptions — never crashes the cycle."""
    with patch("growth.commenter.GrowthLearner") as mock_cls:
        mock_cls.return_value.analyze.side_effect = RuntimeError("analyze broken")

        # Should not raise
        engine.run_learner_analyze()


def test_run_learner_analyze_logs_count(engine: CommentEngine, caplog) -> None:
    """run_learner_analyze() logs the number of new learnings at INFO level."""
    import logging
    with patch("growth.commenter.GrowthLearner") as mock_cls:
        mock_cls.return_value.analyze.return_value = [{"tag": "python"}]

        with caplog.at_level(logging.INFO, logger="growth.commenter"):
            engine.run_learner_analyze()

    assert "1 new learnings" in caplog.text


def test_run_learner_analyze_passes_config(engine: CommentEngine) -> None:
    """run_learner_analyze() instantiates GrowthLearner with the engine's config."""
    with patch("growth.commenter.GrowthLearner") as mock_cls:
        mock_cls.return_value.analyze.return_value = []

        engine.run_learner_analyze()

        mock_cls.assert_called_once_with(engine.config)
