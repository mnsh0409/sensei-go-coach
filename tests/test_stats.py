"""Acceptance tests for T4 (statistics). Skip until eval.stats exists."""

from __future__ import annotations

import math

import numpy as np
import pytest

stats = pytest.importorskip("eval.stats")


def test_elo_from_score():
    assert stats.elo_from_score(0.5) == 0.0
    assert math.isclose(stats.elo_from_score(0.75), 400 * math.log10(3), rel_tol=1e-9)
    assert math.isclose(stats.elo_from_score(0.25), -400 * math.log10(3), rel_tol=1e-9)
    assert math.isfinite(stats.elo_from_score(0.0))
    assert math.isfinite(stats.elo_from_score(1.0))


def test_paired_bootstrap_is_deterministic_and_brackets_mean():
    rng = np.random.default_rng(0)
    x = rng.normal(0.3, 1.0, size=200)
    m1, lo1, hi1 = stats.paired_bootstrap_ci(x, n_resamples=2000, ci=0.95, seed=11)
    m2, lo2, hi2 = stats.paired_bootstrap_ci(x, n_resamples=2000, ci=0.95, seed=11)
    assert (m1, lo1, hi1) == (m2, lo2, hi2)
    assert lo1 < m1 < hi1
    assert math.isclose(m1, float(np.mean(x)), rel_tol=1e-12)


def test_elo_cost_ci_by_opening():
    # Each opening played twice (colors swapped); scores are Sensei's points per game.
    scores_by_opening = [[1.0, 0.0]] * 50 + [[0.0, 0.0]] * 50
    est, lo, hi = stats.elo_cost_ci(scores_by_opening, n_resamples=2000, ci=0.95, seed=3)
    # Score rate 0.25 -> Elo -190.85, so the strength cost is +190.85.
    assert math.isclose(est, 400 * math.log10(3), rel_tol=1e-6)
    assert lo < est < hi


def test_cluster_bootstrap_respects_clusters():
    # 50 games with 4 identical positions each: the effective sample size is 50, not 200.
    rng = np.random.default_rng(5)
    per_game = rng.normal(0.3, 1.0, size=50)
    values = np.repeat(per_game, 4)
    clusters = np.repeat(np.arange(50), 4)
    m, lo, hi = stats.cluster_bootstrap_ci(values, clusters, n_resamples=4000, ci=0.95, seed=2)
    m_naive, lo_n, hi_n = stats.paired_bootstrap_ci(values, n_resamples=4000, ci=0.95, seed=2)
    assert math.isclose(m, float(values.mean()), rel_tol=1e-12)
    assert lo < m < hi
    assert (hi - lo) > 1.5 * (hi_n - lo_n)
    again = stats.cluster_bootstrap_ci(values, clusters, n_resamples=4000, ci=0.95, seed=2)
    assert again == (m, lo, hi)
