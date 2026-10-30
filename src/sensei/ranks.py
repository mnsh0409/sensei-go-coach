"""Go rank scale and the rank posterior over HumanSL profiles.

Numeric scale: 1d = 1.0, 2d = 2.0, ..., 1k = 0.0, 2k = -1.0, 10k = -9.0, 20k = -19.0.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

_LABEL_RE = re.compile(r"^(\d{1,2})([kd])$")
_PREFIXES = ("rank", "preaz")


def label_to_numeric(label: str) -> float:
    """'5k' -> -4.0, '1k' -> 0.0, '1d' -> 1.0, '9d' -> 9.0."""
    m = _LABEL_RE.match(label.strip().lower())
    if not m:
        raise ValueError(f"bad rank label: {label!r}")
    n, kind = int(m.group(1)), m.group(2)
    if kind == "k":
        if not 1 <= n <= 30:
            raise ValueError(f"kyu out of range: {label!r}")
        return float(1 - n)
    if not 1 <= n <= 9:
        raise ValueError(f"dan out of range: {label!r}")
    return float(n)


def numeric_to_label(x: float) -> str:
    """Nearest label, clipped to 30k..9d. 0.4 -> '1k', 0.6 -> '1d', -9.2 -> '10k'."""
    r = math.floor(x + 0.5)
    if r >= 1:
        return f"{min(r, 9)}d"
    return f"{min(1 - r, 30)}k"


def profile_to_numeric(profile: str) -> float:
    """'rank_5k' -> -4.0; 'preaz_3d' -> 3.0."""
    prefix, sep, label = profile.partition("_")
    if not sep or prefix not in _PREFIXES:
        raise ValueError(f"bad profile: {profile!r}")
    return label_to_numeric(label)


def nearest_profile(x: float, grid: Sequence[str]) -> str:
    """Profile on the grid whose numeric rank is closest to x (ties go to the weaker profile)."""
    if not grid:
        raise ValueError("empty profile grid")
    return min(grid, key=lambda p: (abs(profile_to_numeric(p) - x), profile_to_numeric(p)))


def rank_posterior(
    loglik: Mapping[str, float],
    log_prior: Mapping[str, float] | None = None,
) -> tuple[float, float, dict[str, float]]:
    """Posterior over profiles from log-likelihoods. Returns (mean, sd, posterior) on the numeric scale.

    log_prior defaults to uniform. Computed with log-sum-exp for stability.
    """
    if not loglik:
        raise ValueError("no profiles")
    scores = {p: ll + (log_prior.get(p, 0.0) if log_prior else 0.0) for p, ll in loglik.items()}
    top = max(scores.values())
    weights = {p: math.exp(s - top) for p, s in scores.items()}
    z = sum(weights.values())
    post = {p: w / z for p, w in weights.items()}
    mean = sum(post[p] * profile_to_numeric(p) for p in post)
    var = sum(post[p] * (profile_to_numeric(p) - mean) ** 2 for p in post)
    return mean, math.sqrt(max(var, 0.0)), post
