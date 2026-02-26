"""A/B testing infrastructure -- dev.to platform.

Provides random group assignment (control/variant 50/50), Fisher's
Exact Test for statistical significance, and declarative test config.

One active test per platform at a time. Test config is declarative
(change via config, not code).

Schema version: X3-infra (GitLab #14)
"""

import logging
import math
import random

logger = logging.getLogger(__name__)


def assign_group() -> str:
    """Randomly assign to 'control' or 'variant' group (50/50).

    Returns:
        'control' or 'variant'.
    """
    return random.choice(["control", "variant"])


def should_use_variant(ab_test_enabled: bool, group: str) -> bool:
    """Check if the current engagement should use the variant treatment.

    Args:
        ab_test_enabled: Whether A/B testing is currently enabled.
        group: The assigned group ('control' or 'variant').

    Returns:
        True if variant treatment should be applied.
    """
    if not ab_test_enabled:
        return False
    return group == "variant"


def _log_factorial(n: int) -> float:
    """Compute log(n!) using math.lgamma for numerical stability."""
    return math.lgamma(n + 1)


def fishers_exact_test(
    control_successes: int,
    control_total: int,
    variant_successes: int,
    variant_total: int,
) -> dict:
    """Compute Fisher's Exact Test (one-tailed, variant > control).

    Tests whether the variant group has a significantly higher success
    rate than the control group.

    Uses the hypergeometric distribution for exact p-value computation.
    Numerically stable via log-space computation.

    Args:
        control_successes: Number of successes in control group.
        control_total: Total observations in control group.
        variant_successes: Number of successes in variant group.
        variant_total: Total observations in variant group.

    Returns:
        Dict with:
        - p_value: float (0-1)
        - significant: bool (True if p < 0.05)
        - control_rate: float (0-1)
        - variant_rate: float (0-1)
        - lift_percent: float (relative improvement)
    """
    if control_total <= 0 or variant_total <= 0:
        return {
            "p_value": 1.0,
            "significant": False,
            "control_rate": 0.0,
            "variant_rate": 0.0,
            "lift_percent": 0.0,
            "error": "insufficient_data",
        }

    control_rate = control_successes / control_total
    variant_rate = variant_successes / variant_total
    lift = ((variant_rate - control_rate) / control_rate * 100) if control_rate > 0 else 0.0

    a = control_successes
    b = control_total - control_successes
    c = variant_successes
    d = variant_total - variant_successes
    n = a + b + c + d

    def _log_p_table(a: int, b: int, c: int, d: int) -> float:
        return (
            _log_factorial(a + b) + _log_factorial(c + d)
            + _log_factorial(a + c) + _log_factorial(b + d)
            - _log_factorial(n) - _log_factorial(a)
            - _log_factorial(b) - _log_factorial(c) - _log_factorial(d)
        )

    observed_log_p = _log_p_table(a, b, c, d)

    p_value = 0.0
    row1_total = a + b
    row2_total = c + d
    col1_total = a + c

    min_a = max(0, col1_total - row2_total)
    max_a = min(row1_total, col1_total)

    for a_i in range(min_a, max_a + 1):
        b_i = row1_total - a_i
        c_i = col1_total - a_i
        d_i = row2_total - c_i
        if b_i < 0 or c_i < 0 or d_i < 0:
            continue
        log_p = _log_p_table(a_i, b_i, c_i, d_i)
        if log_p <= observed_log_p + 1e-10:
            p_value += math.exp(log_p)

    p_value = min(p_value, 1.0)

    return {
        "p_value": round(p_value, 6),
        "significant": p_value < 0.05,
        "control_rate": round(control_rate, 4),
        "variant_rate": round(variant_rate, 4),
        "lift_percent": round(lift, 2),
    }


def check_test_complete(
    control_total: int,
    variant_total: int,
    min_samples: int = 50,
) -> dict:
    """Check if an A/B test has enough samples to evaluate.

    Args:
        control_total: Number of observations in control group.
        variant_total: Number of observations in variant group.
        min_samples: Minimum samples per group for evaluation.

    Returns:
        Dict with:
        - complete: bool (True if both groups have min_samples)
        - control_count: int
        - variant_count: int
        - samples_needed: int (max remaining needed across groups)
    """
    control_remaining = max(0, min_samples - control_total)
    variant_remaining = max(0, min_samples - variant_total)

    return {
        "complete": control_remaining == 0 and variant_remaining == 0,
        "control_count": control_total,
        "variant_count": variant_total,
        "samples_needed": max(control_remaining, variant_remaining),
    }
