"""Entry point for own-post reply engagement cycle.

Cron: 9 AM UTC and 3 PM UTC daily.
Reads comments on our own dev.to articles, likes each one, generates a specific reply.
"""

import json
import logging
import sys

import anthropic

from growth.browser import DevToBrowser
from growth.client import DevToClient
from growth.config import load_config
from growth.responder import OwnPostResponder

logger = logging.getLogger(__name__)

REPLY_PROMPT = """You are replying to a comment on @klement_gunndu's dev.to article.

Your reply must:
- Be 1-2 sentences, under 250 characters total
- Directly address what the person said — quote or paraphrase their specific point
- Sound like a senior developer talking to a peer — direct, warm, no corporate speak
- NOT include self-promotion, links, or generic acknowledgements ("Thanks for reading!" is a violation)
- If they ask a question, answer it. If they share an opinion, engage with it specifically.

Article title: {article_title}
Their comment: {comment_body}

Write only the reply text. No explanation."""


def make_llm_fn(client: anthropic.Anthropic):
    def llm_reply_fn(comment_body: str, article_title: str) -> str:
        prompt = REPLY_PROMPT.format(
            article_title=article_title[:200],
            comment_body=comment_body[:300],
        )
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    return llm_reply_fn


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        config = load_config()
        anthropic_client = anthropic.Anthropic()
        devto_client = DevToClient(config)
        with DevToBrowser(config) as browser:
            responder = OwnPostResponder(
                devto_client, config, browser, make_llm_fn(anthropic_client)
            )
            summary = responder.run()
        print(json.dumps(summary, indent=2))
    except Exception as e:
        logger.error("Responder failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
