"""Statistics for the evaluation harness (eval/README.md, "Modules").

Every bootstrap is deterministic given its seed (`numpy.random.default_rng(seed)`) and uses the percentile
method: the interval is the (1 - ci) / 2 and 1 - (1 - ci) / 2 quantiles of the resampled statistic.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

SCORE_CLIP = 1e-3
"""Score rates are clipped to [SCORE_CLIP, 1 - SCORE_CLIP] before the Elo transform, so 0 and 1 stay finite."""

_CHUNK = 1000
"""Resamples drawn per chunk; keeps the index matrix small for large samples."""


def elo_from_score(s: float) -> float:
    """Elo difference from a score rate, clipped to [1e-3, 1 - 1e-3]: 400 * log10(s / (1 - s))."""
    s = min(max(float(s), SCORE_CLIP), 1.0 - SCORE_CLIP)
    return 400.0 * math.log10(s / (1.0 - s))


def _check(n_resamples: int, ci: float) -> None:
    if n_resamples < 1:
        raise ValueError("need at least one resample")
    if not 0.0 < ci < 1.0:
        raise ValueError(f"ci must be within (0, 1), got {ci!r}")


def _quantile_bounds(samples: np.ndarray, ci: float) -> tuple[float, float]:
    alpha = (1.0 - ci) / 2.0
    finite = samples[np.isfinite(samples)]
    if finite.size == 0:
        return math.nan, math.nan
    lo, hi = np.quantile(finite, [alpha, 1.0 - alpha])
    return float(lo), float(hi)


def _mean_of_resampled(values: np.ndarray, n_resamples: int, ci: float, seed: int) -> tuple[float, float]:
    """Vectorized bootstrap of the mean of `values` (one unit per entry)."""
    n = values.shape[0]
    rng = np.random.default_rng(seed)
    means = np.empty(n_resamples, dtype=float)
    done = 0
    while done < n_resamples:
        take = min(_CHUNK, n_resamples - done)
        idx = rng.integers(0, n, size=(take, n))
        means[done : done + take] = values[idx].mean(axis=1)
        done += take
    return _quantile_bounds(means, ci)


def paired_bootstrap_ci(
    values: Sequence[float] | np.ndarray,
    n_resamples: int,
    ci: float,
    seed: int,
) -> tuple[float, float, float]:
    """(mean, lo, hi) of the mean of `values` by bootstrap over its entries.

    For a paired design pass the per-unit differences (per opening, per learner): the CI then respects the pairing.
    """
    x = np.asarray(values, dtype=float).ravel()
    if x.size == 0:
        raise ValueError("values is empty")
    _check(n_resamples, ci)
    lo, hi = _mean_of_resampled(x, n_resamples, ci, seed)
    return float(np.mean(x)), lo, hi


def pooled_bootstrap_ci(
    sums: Sequence[float] | np.ndarray,
    counts: Sequence[float] | np.ndarray,
    n_resamples: int,
    ci: float,
    seed: int,
    transform: Callable[[float], float] | None = None,
) -> tuple[float, float, float]:
    """(estimate, lo, hi) of sum(sums) / sum(counts) by bootstrap over units, optionally transformed.

    Each unit (opening, game) contributes a sum and a count; the statistic is the pooled ratio, so units with
    more observations weigh more, as in a pooled score rate or a pooled mean over positions.
    """
    s = np.asarray(sums, dtype=float).ravel()
    c = np.asarray(counts, dtype=float).ravel()
    if s.size == 0 or s.shape != c.shape:
        raise ValueError("sums and counts must be non-empty and of equal length")
    if c.sum() <= 0:
        raise ValueError("counts sum to zero")
    _check(n_resamples, ci)
    f = transform or (lambda v: v)
    estimate = f(float(s.sum() / c.sum()))
    n = s.shape[0]
    rng = np.random.default_rng(seed)
    stats = np.empty(n_resamples, dtype=float)
    done = 0
    while done < n_resamples:
        take = min(_CHUNK, n_resamples - done)
        idx = rng.integers(0, n, size=(take, n))
        ratios = s[idx].sum(axis=1) / c[idx].sum(axis=1)
        stats[done : done + take] = [f(float(r)) for r in ratios]
        done += take
    lo, hi = _quantile_bounds(stats, ci)
    return estimate, lo, hi


def elo_cost_ci(
    scores_by_opening: Sequence[Sequence[float]],
    n_resamples: int,
    ci: float,
    seed: int,
) -> tuple[float, float, float]:
    """Strength cost in Elo (positive = Sensei weaker) with a bootstrap over openings.

    `scores_by_opening[i]` holds Sensei's points (1, 0.5, 0) in the games of opening i (two games, colours
    swapped). The cost is -elo_from_score(pooled score rate); openings are resampled with replacement.
    """
    if not scores_by_opening:
        raise ValueError("no openings")
    sums = [float(sum(games)) for games in scores_by_opening]
    counts = [float(len(games)) for games in scores_by_opening]
    if any(cnt == 0 for cnt in counts):
        raise ValueError("every opening needs at least one game")
    # 0.0 - x rather than -x, so an even score gives 0.0 and not -0.0 in result files.
    return pooled_bootstrap_ci(sums, counts, n_resamples, ci, seed, transform=lambda s: 0.0 - elo_from_score(s))


def cluster_bootstrap_ci(
    values: Sequence[float] | np.ndarray,
    clusters: Sequence[Any] | np.ndarray,
    n_resamples: int,
    ci: float,
    seed: int,
) -> tuple[float, float, float]:
    """(mean, lo, hi) of the pooled mean of `values`, resampling whole clusters (for example games) with replacement."""
    x = np.asarray(values, dtype=float).ravel()
    labels = np.asarray(clusters).ravel()
    if x.size == 0 or x.shape != labels.shape:
        raise ValueError("values and clusters must be non-empty and of equal length")
    _, inverse = np.unique(labels, return_inverse=True)
    inverse = inverse.ravel()
    sums = np.bincount(inverse, weights=x)
    counts = np.bincount(inverse).astype(float)
    return pooled_bootstrap_ci(sums, counts, n_resamples, ci, seed)


def ratio_of_means_ci(
    numerators: Sequence[float] | np.ndarray,
    denominators: Sequence[float] | np.ndarray,
    n_resamples: int,
    ci: float,
    seed: int,
) -> tuple[float, float, float]:
    """(ratio, lo, hi) of mean(numerators) / mean(denominators), resampling the paired units together.

    Used for C3's gap closure, where both means are over the same learners. Resamples whose denominator mean is
    zero are dropped from the interval; a zero point-estimate denominator gives nan.
    """
    num = np.asarray(numerators, dtype=float).ravel()
    den = np.asarray(denominators, dtype=float).ravel()
    if num.size == 0 or num.shape != den.shape:
        raise ValueError("numerators and denominators must be non-empty and of equal length")
    _check(n_resamples, ci)
    d = float(den.mean())
    ratio = float(num.mean() / d) if d != 0.0 else math.nan
    n = num.shape[0]
    rng = np.random.default_rng(seed)
    stats = np.empty(n_resamples, dtype=float)
    done = 0
    while done < n_resamples:
        take = min(_CHUNK, n_resamples - done)
        idx = rng.integers(0, n, size=(take, n))
        with np.errstate(divide="ignore", invalid="ignore"):
            stats[done : done + take] = num[idx].mean(axis=1) / den[idx].mean(axis=1)
        done += take
    lo, hi = _quantile_bounds(stats, ci)
    return ratio, lo, hi
