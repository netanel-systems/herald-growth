"""Entry point for daily follower snapshot and weekly growth report.

Cron: 6 AM UTC daily (before reaction + comment cycles).
Takes a follower snapshot so the monitoring dashboard always has fresh data.
On Sundays: also generates the weekly growth report (followers + reciprocity + learner insights).
"""

import json
import logging
import sys
from datetime import datetime, timezone

from growth.client import DevToClient
from growth.config import load_config
from growth.learner import GrowthLearner
from growth.tracker import GrowthTracker

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        config = load_config()
        client = DevToClient(config)
        learner = GrowthLearner(config)
        tracker = GrowthTracker(client, config, learner)

        is_sunday = datetime.now(timezone.utc).weekday() == 6
        if is_sunday:
            result = tracker.get_weekly_report()
            logger.info("Weekly report generated: %d followers", result["followers"]["current_count"])
        else:
            result = tracker.check_followers()
            logger.info(
                "Snapshot taken: %d followers (+%d new)",
                result["current_count"],
                len(result.get("new_followers", [])),
            )

        print(json.dumps(result, indent=2))
    except Exception as e:
        logger.error("Tracker failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
