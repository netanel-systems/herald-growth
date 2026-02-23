"""Shared storage utilities for JSON data files.

Extracted to avoid duplication between reactor.py and commenter.py.
Both modules need to load/save bounded sets of article IDs.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_json_ids(path: Path, key: str = "article_ids") -> set[int]:
    """Load a set of IDs from a JSON file.

    Returns empty set if file doesn't exist or is corrupted.
    """
    if not path.exists():
        return set()
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get(key, []))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path.name, e)
        return set()


def save_json_ids(
    path: Path, ids: set[int], max_count: int, key: str = "article_ids",
) -> None:
    """Save a bounded set of IDs to a JSON file.

    Keeps only the most recent entries if over max_count.
    """
    ids_list = sorted(ids)
    if len(ids_list) > max_count:
        ids_list = ids_list[-max_count:]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({key: ids_list, "count": len(ids_list)}, f, indent=2)
    logger.info("Saved %d IDs to %s.", len(ids_list), path.name)
