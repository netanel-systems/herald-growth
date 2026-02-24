"""Tests for GrowthLearner.analyze() — the new pattern extraction method."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from growth.config import GrowthConfig
from growth.learner import GrowthLearner


@pytest.fixture()
def config(tmp_path: Path) -> GrowthConfig:
    """GrowthConfig pointing data_dir at tmp_path."""
    cfg = GrowthConfig()
    cfg.__dict__["_project_root"] = tmp_path
    return cfg


@pytest.fixture()
def learner(tmp_path: Path) -> GrowthLearner:
    """GrowthLearner with data_dir in tmp_path."""
    cfg = GrowthConfig(project_root=tmp_path)
    return GrowthLearner(cfg)


def _write_engagement_log(data_dir: Path, entries: list[dict]) -> None:
    """Write engagement_log.jsonl to data_dir."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "engagement_log.jsonl"
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ── Test 1: No engagement data returns empty list ─────────────────

def test_analyze_no_data_returns_empty(learner: GrowthLearner) -> None:
    """analyze() with no engagement_log.jsonl returns empty list, no crash."""
    result = learner.analyze()
    assert result == []


# ── Test 2: Not enough data per tag (< MIN_EVENTS) skipped ────────

def test_analyze_insufficient_data_no_learnings(learner: GrowthLearner, tmp_path: Path) -> None:
    """Tags with fewer than 5 events produce no learnings (not enough signal)."""
    entries = [
        {"action": "reaction", "tags": ["python"], "timestamp": "2026-01-01T00:00:00+00:00"},
        {"action": "reaction", "tags": ["python"], "timestamp": "2026-01-01T01:00:00+00:00"},
    ]
    _write_engagement_log(learner.data_dir, entries)
    result = learner.analyze()
    assert result == []


# ── Test 3: High-engagement tag stored as 'prioritize' ────────────

def test_analyze_high_engagement_stored(learner: GrowthLearner, tmp_path: Path) -> None:
    """A tag with >= 10 total events and >= 80% engagement ratio stores a prioritize learning."""
    entries = []
    for i in range(12):
        entries.append({
            "action": "reaction",
            "tags": ["python"],
            "timestamp": f"2026-01-01T{i:02d}:00:00+00:00",
        })
    _write_engagement_log(learner.data_dir, entries)

    result = learner.analyze()

    # Should have stored a prioritize-type learning
    assert any(item["action"] == "prioritize" and item["tag"] == "python" for item in result)
    # Learnings file should now exist
    learnings = learner.load_learnings()
    assert any("prioritize" in l["pattern"] and "python" in l["pattern"] for l in learnings)


# ── Test 4: Zero-reciprocity tag stored as 'skip' ────────────────

def test_analyze_zero_reciprocity_stored(learner: GrowthLearner, tmp_path: Path) -> None:
    """A tag with >= 5 events but zero reactions AND zero comments stores a skip learning."""
    # We need to craft data where get_engagement_by_tag returns total >= 5
    # but reactions=0 and comments=0. The current get_engagement_by_tag only
    # counts action=="reaction" and action=="comment". Use a different action type
    # to simulate total events without reactions or comments.
    # Actually: total = reactions + comments in the counter (each event increments total).
    # So if all 6 events are "reaction", reactions=6, total=6 — ratio=1.0 -> prioritize.
    # For zero reciprocity: total >= 5 but reactions==0 AND comments==0.
    # This means the events must come from a type not counted by the learner.
    # Looking at get_engagement_by_tag: total is incremented for any tag in the entry,
    # but reactions/comments are only for action=="reaction" or "comment".
    # So use action="view" (not reaction/comment) to get total without counts.
    entries = []
    for i in range(6):
        entries.append({
            "action": "view",
            "tags": ["badtag"],
            "timestamp": f"2026-01-01T{i:02d}:00:00+00:00",
        })
    _write_engagement_log(learner.data_dir, entries)

    result = learner.analyze()

    assert any(item["action"] == "skip" and item["tag"] == "badtag" for item in result)
    learnings = learner.load_learnings()
    assert any("skip" in l["pattern"] and "badtag" in l["pattern"] for l in learnings)


# ── Test 5: analyze() returns list of new learning dicts ──────────

def test_analyze_returns_list_of_dicts(learner: GrowthLearner, tmp_path: Path) -> None:
    """Each item in result is a dict with 'tag', 'action', 'confidence' keys."""
    entries = [
        {"action": "reaction", "tags": ["ai"], "timestamp": f"2026-01-01T{i:02d}:00:00+00:00"}
        for i in range(12)
    ]
    _write_engagement_log(learner.data_dir, entries)

    result = learner.analyze()

    for item in result:
        assert "tag" in item
        assert "action" in item
        assert "confidence" in item
        assert isinstance(item["confidence"], float)


# ── Test 6: analyze() is safe when engagement_log has corrupt lines ─

def test_analyze_handles_corrupt_lines(learner: GrowthLearner, tmp_path: Path) -> None:
    """analyze() does not crash when engagement_log has malformed JSON lines."""
    data_dir = learner.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "engagement_log.jsonl"
    with open(path, "w") as f:
        f.write("not-json\n")
        f.write(json.dumps({"action": "reaction", "tags": ["python"], "timestamp": "2026-01-01T00:00:00+00:00"}) + "\n")

    # Should not raise
    result = learner.analyze()
    assert isinstance(result, list)


# ── Test 7: Confidence is bounded [0.0, 1.0] ──────────────────────

def test_analyze_confidence_bounded(learner: GrowthLearner, tmp_path: Path) -> None:
    """Confidence values stored by analyze() are always in [0.0, 1.0]."""
    entries = [
        {"action": "reaction", "tags": ["python"], "timestamp": f"2026-01-01T{i:02d}:00:00+00:00"}
        for i in range(20)
    ]
    _write_engagement_log(learner.data_dir, entries)

    learner.analyze()
    learnings = learner.load_learnings()
    for l in learnings:
        assert 0.0 <= l["confidence"] <= 1.0, f"Out-of-bounds confidence: {l['confidence']}"
