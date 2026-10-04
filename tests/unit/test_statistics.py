"""Phase 9: the benchmark statistics (episode-level, deterministic, stdlib only)."""

import math

import pytest

from dave_agent.evaluation.statistics import bootstrap_mean_ci, mcnemar_exact, mean, percentile, wilson


def test_percentile_interpolates_like_numpy_linear():
    values = [1, 2, 3, 4]
    assert percentile(values, 50) == 2.5
    assert percentile(values, 95) == pytest.approx(3.85)
    assert percentile([7], 95) == 7
    assert percentile([], 50) is None and mean([]) is None


def test_wilson_interval_known_values():
    lo, hi = wilson(0, 3)  # no successes in 3: the upper bound is well above 0
    assert lo == 0.0 and hi == pytest.approx(0.5615, abs=1e-4)
    lo, hi = wilson(15, 30)
    assert lo == pytest.approx(0.3315, abs=1e-4) and hi == pytest.approx(0.6685, abs=1e-4)
    assert wilson(0, 0) is None


def test_mcnemar_exact_two_sided():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 5) == pytest.approx(2 / 32)
    assert mcnemar_exact(1, 9) == pytest.approx(2 * 11 / 1024)
    assert mcnemar_exact(5, 5) == 1.0


def test_bootstrap_is_deterministic_and_brackets_the_mean():
    diffs = [1, 0, 0, 1, -1, 0, 1, 1, 0, 0]
    ci = bootstrap_mean_ci(diffs, 0.95, 2000, seed=3)
    assert ci == bootstrap_mean_ci(diffs, 0.95, 2000, seed=3)
    assert ci[0] <= mean(diffs) <= ci[1] and -1 <= ci[0] < ci[1] <= 1
    assert bootstrap_mean_ci([0, 0, 0], 0.95, 100) == (0, 0)
    assert bootstrap_mean_ci([1], 0.95, 100) is None
    assert not math.isnan(ci[0])
