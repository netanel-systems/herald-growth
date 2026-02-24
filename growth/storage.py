"""Shared storage utilities for JSON data files.

Extracted to avoid duplication between reactor.py and commenter.py.
Both modules need to load/save bounded sets of article IDs.

Atomic writes: Uses temp file + rename to prevent corruption if process
crashes mid-write. Critical for cron jobs that may overlap.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def load_json_ids(path: Path, key: str = "article_ids") -> set[int]:
    """Load a set of IDs from a JSON file.

    Expected JSON format::

        {"article_ids": [1, 2, 3, ...], "count": N}

    The ``article_ids`` list is the source of truth. The ``count`` field
    is informational only — always derive counts from ``len(article_ids)``.
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


def atomic_write_json(path: Path, data: object) -> None:
    """Write JSON data atomically using temp file + rename.

    Prevents data corruption if the process crashes mid-write.
    The temp file is always cleaned up — even if os.replace() fails —
    to avoid orphaned temp files accumulating in the data directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, suffix=".tmp", prefix=f".{path.stem}_",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)  # Atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_err:
            logger.warning(
                "Failed to clean up temp file %s after write error: %s",
                tmp_path, cleanup_err,
            )
        raise


def save_json_ids(
    path: Path, ids: set[int], max_count: int, key: str = "article_ids",
) -> None:
    """Save a bounded set of IDs to a JSON file.

    Written format::

        {"article_ids": [1, 2, 3, ...], "count": N}

    The ``count`` field mirrors ``len(article_ids)`` and is informational only.
    Callers reading the count MUST use ``len(article_ids)`` — not the ``count``
    field — since ``count`` may be stale if the file is modified externally.

    Uses atomic write to prevent corruption. Keeps only the most recent
    entries if over max_count.
    """
    ids_list = sorted(ids)
    if len(ids_list) > max_count:
        ids_list = ids_list[-max_count:]
    atomic_write_json(path, {key: ids_list, "count": len(ids_list)})
    logger.info("Saved %d IDs to %s.", len(ids_list), path.name)
