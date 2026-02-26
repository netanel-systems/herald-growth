"""Comment template categories for dev.to — prompt-injection guidance.

Defines 4 template categories used as constraints for LLM-generated comments:
- experience_sharing: relate personal experience to the post topic
- technical_extension: extend the post with a technical observation
- constructive_challenge: respectfully challenge an assumption or approach
- gratitude_with_depth: appreciate with a specific detail that shows deep reading

Each category includes constraints (length, must-include, style).
Rotation logic ensures no consecutive same category.
Question detection helper for engagement log tagging.

Platform-specific constraints for dev.to:
- 2-4 sentences
- Max 600 characters
- Standard markdown

Schema version: D4 (GitLab #14)
"""

import random
import re

# Template categories — prompt-injection instructions for LLM comment generation.
# These are NOT fill-in templates. They are constraints injected into the LLM prompt.
TEMPLATE_CATEGORIES: dict[str, dict] = {
    "experience_sharing": {
        "id": "experience_sharing",
        "instruction": (
            "Share a brief, specific personal experience related to the post's topic. "
            "Reference something concrete from the post (a tool, pattern, or problem). "
            "Keep it conversational and add one insight the author might not have considered."
        ),
        "constraints": {
            "min_sentences": 2,
            "max_sentences": 4,
            "max_chars": 600,
            "must_include": "a specific detail from the post",
            "tone": "conversational, peer-to-peer",
        },
    },
    "technical_extension": {
        "id": "technical_extension",
        "instruction": (
            "Build on the post's technical content with one additional observation. "
            "Reference a specific section, code snippet, or approach from the post. "
            "Add a related technique, gotcha, or optimization the reader should know about."
        ),
        "constraints": {
            "min_sentences": 2,
            "max_sentences": 4,
            "max_chars": 600,
            "must_include": "a concrete technical detail",
            "tone": "knowledgeable but not condescending",
        },
    },
    "constructive_challenge": {
        "id": "constructive_challenge",
        "instruction": (
            "Respectfully question one specific assumption or approach in the post. "
            "Frame it as curiosity, not criticism. Reference the exact point you are "
            "challenging. Offer an alternative perspective or ask about edge cases."
        ),
        "constraints": {
            "min_sentences": 2,
            "max_sentences": 4,
            "max_chars": 600,
            "must_include": "a specific point from the post being questioned",
            "tone": "curious, respectful, not confrontational",
        },
    },
    "gratitude_with_depth": {
        "id": "gratitude_with_depth",
        "instruction": (
            "Express appreciation by referencing a specific detail that shows you "
            "actually read the post deeply. Mention one thing you learned or will "
            "apply. Avoid generic praise entirely."
        ),
        "constraints": {
            "min_sentences": 2,
            "max_sentences": 4,
            "max_chars": 600,
            "must_include": "a specific detail proving deep reading",
            "tone": "genuine, specific, not sycophantic",
        },
    },
}

# Ordered list for rotation
CATEGORY_IDS: list[str] = list(TEMPLATE_CATEGORIES.keys())


def pick_template_category(last_category: str | None = None) -> str:
    """Pick a template category, ensuring no consecutive same category.

    Args:
        last_category: The category used in the previous comment. Pass None
            for the first comment in a cycle.

    Returns:
        A category ID string from CATEGORY_IDS.
    """
    available = [c for c in CATEGORY_IDS if c != last_category]
    if not available:
        available = CATEGORY_IDS
    return random.choice(available)


def get_template_instruction(category: str) -> str:
    """Get the LLM prompt instruction for a template category.

    Args:
        category: One of the CATEGORY_IDS.

    Returns:
        Instruction string for injection into the LLM prompt.

    Raises:
        KeyError: If category is not a valid template category.
    """
    return TEMPLATE_CATEGORIES[category]["instruction"]


def get_template_constraints(category: str) -> dict:
    """Get the constraints for a template category.

    Args:
        category: One of the CATEGORY_IDS.

    Returns:
        Constraints dict with min_sentences, max_sentences, max_chars, etc.

    Raises:
        KeyError: If category is not a valid template category.
    """
    return TEMPLATE_CATEGORIES[category]["constraints"]


def has_question(text: str) -> bool:
    """Detect whether text contains a question.

    Checks for question marks and common question patterns.
    Used for engagement log tagging (comment_has_question field).

    Args:
        text: The comment text to check.

    Returns:
        True if the text contains at least one question.
    """
    if "?" in text:
        return True
    question_patterns = [
        r"\b(?:how|what|why|when|where|which|who|whom|whose|do|does|did|is|are|was|were|can|could|would|should|have|has)\b",
    ]
    for pattern in question_patterns:
        if re.search(pattern, text.lower()):
            sentences = re.split(r"(?<=[.!?])\s+", text)
            for sentence in sentences:
                sentence_lower = sentence.strip().lower()
                if re.match(r"^(?:how|what|why|when|where|which|who|do|does|did|is|are|was|were|can|could|would|should|have|has)\b", sentence_lower):
                    return True
    return False
