"""One-off script: delete duplicate replies to nyrok and soytuber on article 3321258.

Background:
    MAX_REPLIES_PER_COMMENTER was previously 3. Two users on article 3321258
    each received 2 replies from @klement_gunndu. The second reply is a duplicate
    and must be deleted.

Duplicates to delete:
    - nyrok:    id_code=35ab1  (klement's reply to nyrok's second top-level
                                comment 35a9f, posted 2026-03-08T06:34:05Z)
    - soytuber: id_code=35aph  (klement's reply to soytuber's follow-up comment
                                35anp, posted 2026-03-08T14:33:45Z)

Usage:
    cd ~/netanel/teams/herald_growth
    .venv/bin/python delete_duplicates.py
"""

import logging
import sys
from pathlib import Path

# Load env so GrowthConfig picks up credentials
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from growth.config import GrowthConfig  # noqa: E402
from growth.browser import DevToBrowser  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ARTICLE_URL = "https://dev.to/klement_gunndu/build-a-rag-pipeline-in-python-that-actually-works-28dg"

# (user, id_code, reason)
DUPLICATES = [
    ("nyrok",    "35ab1", "second reply to nyrok (first=358km, this one is extra)"),
    ("soytuber", "35aph", "second reply to soytuber (first=35anl, this one is extra)"),
]


def main() -> int:
    config = GrowthConfig()
    logger.info("Config loaded. Username=%s", config.devto_username)

    results = {}

    with DevToBrowser(config) as browser:
        browser.ensure_logged_in()
        logger.info("Browser logged in successfully.")

        for user, id_code, reason in DUPLICATES:
            logger.info(
                "Deleting duplicate reply %s (user=%s, reason=%s)",
                id_code, user, reason,
            )
            success = browser.delete_comment(id_code, ARTICLE_URL)
            results[id_code] = {
                "user": user,
                "success": success,
                "reason": reason,
            }
            if success:
                logger.info("DELETED: %s (for %s)", id_code, user)
            else:
                logger.error("FAILED to delete: %s (for %s)", id_code, user)

    # Print summary
    print("\n=== DELETION RESULTS ===")
    all_ok = True
    for id_code, r in results.items():
        status = "OK" if r["success"] else "FAILED"
        print(f"  [{status}] id_code={id_code} user={r['user']}")
        if not r["success"]:
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
