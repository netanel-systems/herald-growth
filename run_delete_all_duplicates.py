"""Delete all duplicate replies across 13 articles.

55 total comments to delete. Uses DevToBrowser.delete_comment().
Credentials loaded from .env (GROWTH_DEVTO_EMAIL, GROWTH_DEVTO_PASSWORD).

Run from /home/intruder/netanel/teams/herald_growth/:
    .venv/bin/python run_delete_all_duplicates.py
"""

import logging
import os
import sys
import time
from pathlib import Path

# Load .env before importing growth modules
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

# Set env_file so GrowthConfig finds it when loaded from project dir
os.chdir(Path(__file__).parent)

from growth.browser import DevToBrowser  # noqa: E402
from growth.config import GrowthConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deletion map: article_url -> list of comment id_codes to delete
# Keep FIRST comment per user; delete all subsequent duplicates.
# ---------------------------------------------------------------------------

DELETION_MAP: list[tuple[str, list[str]]] = [
    (
        "https://dev.to/klement_gunndu/build-a-rag-pipeline-in-python-that-actually-works-28dg",
        [
            # Article 3321258 — 25 deletes
            # @nyrok duplicates (keep 358km)
            "359gl", "35ae4", "35afh", "35ae3", "35afk",
            "35a4l", "35a91", "35ab0", "35ac5", "35ae8",
            "35afl", "35ahc", "35amo", "35bdc", "35ac6",
            "35aea", "35afm", "35amn",
            # @klement_gunndu duplicates (keep 359h5)
            "359k0", "35ae6", "35afi", "35afj", "35ien", "35ae7", "35b3j",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/5-prompt-engineering-patterns-that-actually-work-in-production-3m4i",
        [
            # Article 3329691 — 5 deletes
            # @mihirkanzariya (keep 35bo0)
            "35cd3",
            # @seryllns_ (keep 35c05)
            "35c3h", "35cd4",
            # @klement_gunndu (keep 35ca5)
            "35cf2", "35cgd",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/how-to-escape-tutorial-hell-and-ship-real-code-1bpf",
        [
            # Article 3326163 — 4 deletes
            # @nyrok (keep 35aha)
            "35am6",
            # @hirave_palak (keep 35e2c)
            "35e5k", "35e6i", "35e93",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/meta-now-lets-you-use-ai-in-coding-interviews-most-candidates-are-doing-it-wrong-2hnn",
        [
            # Article 3354829 — 3 deletes
            # @apex_stack (keep 35j9k)
            "35jap",
            # @freerave (keep 35j9l)
            "35k63", "35jb0",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/test-your-ai-agent-like-a-senior-engineer-4-patterns-that-work-4hml",
        [
            # Article 3348066 — 4 deletes
            # @apex_stack (keep 35gkc)
            "35gp5", "35gm5",
            # @klement_gunndu (keep 35h0k)
            "35icj",
            # @team-luminousprinting (keep 35hdl)
            "35hhb",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/the-ai-engineering-stack-in-2026-what-to-learn-first-1nhj",
        [
            # Article 3351594 — 4 deletes
            # @ptak_dev (keep 35ho6)
            "35i0j", "35i1g", "35i24",
            # @pascalre (keep 35lbi)
            "35lcm",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/build-your-first-mcp-server-in-python-3-patterns-you-need-to-know-5ajf",
        [
            # Article 3342408 — 2 deletes
            # @romainb_ai (keep 35f4l)
            "35f6k", "35fbe",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/llm-as-a-judge-evaluate-your-models-without-human-reviewers-1lbh",
        [
            # Article 3353822 — 3 deletes
            # @apex_stack (keep 35imf)
            "35j9m", "35jb1",
            # @klement_gunndu (keep 35jae)
            "35jb2",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/5-ways-to-cut-your-ai-agents-api-bill-by-80-4fpi",
        [
            # Article 3298436 — 1 delete
            # @mirko_stahnke_a9da18e5549 (keep 354gf)
            "35fid",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/your-first-ai-agent-will-fail-heres-how-to-debug-it-1k4n",
        [
            # Article 3292071 — 2 deletes
            # @liuhaotian2024prog (keep 35ib8)
            "35ici",
            # @klement_gunndu (keep 35id8)
            "35ig1",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/build-an-ai-agent-loop-in-50-lines-of-python-1oc5",
        [
            # Article 3346880 — 1 delete
            # @team-luminousprinting (keep 35hdm)
            "35hhc",
        ],
    ),
    (
        "https://dev.to/klement_gunndu/5-python-scripts-that-automate-your-freelance-workflow-with-ai-3apj",
        [
            # Article 3363131 — 1 delete
            # @dhis_is_jj (keep 35kpe)
            "35l4m",
        ],
    ),
]

HUMAN_DELAY_BETWEEN_ARTICLES = (3.0, 6.0)
HUMAN_DELAY_BETWEEN_COMMENTS = (1.5, 3.5)


def main() -> None:
    total_attempted = 0
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []  # (id_code, article_url)

    config = GrowthConfig()
    logger.info(
        "Starting bulk delete: %d articles, credentials=%s",
        len(DELETION_MAP),
        config.devto_email or "(NOT SET)",
    )

    with DevToBrowser(config) as browser:
        browser.ensure_logged_in()
        logger.info("Logged in successfully.")

        for article_url, id_codes in DELETION_MAP:
            article_short = article_url.split("/")[-1]
            logger.info(
                "=== Article: %s (%d comments to delete) ===",
                article_short, len(id_codes),
            )

            for i, id_code in enumerate(id_codes):
                total_attempted += 1
                logger.info(
                    "Deleting %s (%d/%d on this article)...",
                    id_code, i + 1, len(id_codes),
                )

                success = browser.delete_comment(id_code, article_url)

                if success:
                    deleted.append(id_code)
                    logger.info("DELETED: %s", id_code)
                else:
                    failed.append((id_code, article_url))
                    logger.warning("FAILED: %s", id_code)

                if i < len(id_codes) - 1:
                    delay = HUMAN_DELAY_BETWEEN_COMMENTS[0] + (
                        (HUMAN_DELAY_BETWEEN_COMMENTS[1] - HUMAN_DELAY_BETWEEN_COMMENTS[0])
                        * __import__("random").random()
                    )
                    time.sleep(delay)

            logger.info(
                "Article done. Deleted %d/%d so far total.",
                len(deleted), total_attempted,
            )
            if DELETION_MAP.index((article_url, id_codes)) < len(DELETION_MAP) - 1:
                delay = HUMAN_DELAY_BETWEEN_ARTICLES[0] + (
                    (HUMAN_DELAY_BETWEEN_ARTICLES[1] - HUMAN_DELAY_BETWEEN_ARTICLES[0])
                    * __import__("random").random()
                )
                time.sleep(delay)

    # --- Summary ---
    print("\n" + "=" * 60)
    print("BULK DELETION RESULTS")
    print("=" * 60)
    print(f"Attempted: {total_attempted}")
    print(f"Deleted:   {len(deleted)}")
    print(f"Failed:    {len(failed)}")

    if deleted:
        print("\nSuccessfully deleted:")
        for id_code in deleted:
            print(f"  {id_code}")

    if failed:
        print("\nFailed deletions:")
        for id_code, url in failed:
            print(f"  {id_code}  ({url.split('/')[-1]})")
        print("\nCheck data/screenshots/ for debug images.")

    # Return structured results for output report
    return {
        "total_attempted": total_attempted,
        "deleted": deleted,
        "failed": failed,
    }


if __name__ == "__main__":
    results = main()
    if results and results["failed"]:
        sys.exit(1)
