"""Small, dependency-free statistics for benchmark summaries.

The unit is the episode: every function here takes one value per episode (or per matched
pair of episodes), never per frame. Everything is deterministic: the bootstrap uses a fixed
seed, so a summary rebuilt from the same records is identical.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from statistics import NormalDist


def percentile(values: Sequence[float], q: float) -> float | None:
    """The ``q``-th percentile (0-100) with linear interpolation between closest ranks
    (numpy's default method). None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def wilson(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion; None when n is 0."""
    if n == 0:
        return None
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_mean_ci(values: Sequence[float], confidence: float = 0.95, samples: int = 2000,
                      seed: int = 0) -> tuple[float, float] | None:
    """Percentile bootstrap interval for the mean of ``values`` (e.g. paired differences),
    resampling whole pairs. None for fewer than 2 values."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(samples))
    alpha = (1 - confidence) / 2
    return percentile(means, 100 * alpha), percentile(means, 100 * (1 - alpha))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts ``b`` (only the first
    arm succeeded) and ``c`` (only the second); 1.0 when there are no discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)
