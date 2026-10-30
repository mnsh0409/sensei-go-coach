"""Discounted Thompson sampling over mistake topics (INTERFACES.md "Curriculum").

Beta-Bernoulli arms, one per topic. A success is an observed mistake, so the curriculum targets the topics
where the learner errs most. Discount 1.0 is stationary Thompson sampling; below 1.0 the posterior forgets,
which matters because a learner's mistake rates fall as they improve.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np


class ThompsonCurriculum:
    """Curriculum (interfaces.Curriculum) with Beta(alpha_k, beta_k) posteriors and decay toward the prior."""

    def __init__(self, arms: Iterable[str], discount: float, prior: tuple[float, float]) -> None:
        self.arms: tuple[str, ...] = tuple(arms)
        if not self.arms:
            raise ValueError("a curriculum needs at least one arm")
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("duplicate arms")
        if not 0.0 < discount <= 1.0:
            raise ValueError(f"discount must be in (0, 1], got {discount!r}")
        alpha0, beta0 = (float(prior[0]), float(prior[1]))
        if alpha0 <= 0.0 or beta0 <= 0.0:
            raise ValueError("prior parameters must be positive")
        self.discount = float(discount)
        self.alpha0 = alpha0
        self.beta0 = beta0
        self._alpha = dict.fromkeys(self.arms, alpha0)
        self._beta = dict.fromkeys(self.arms, beta0)

    def posterior(self, arm: str) -> tuple[float, float]:
        """(alpha, beta) of the arm's current Beta posterior."""
        return self._alpha[arm], self._beta[arm]

    def posterior_mean(self, arm: str) -> float:
        alpha, beta = self.posterior(arm)
        return alpha / (alpha + beta)

    def select(self, rng: Any) -> str:
        """Draw theta_k ~ Beta(alpha_k, beta_k) for every arm with the given numpy Generator; return the argmax."""
        alphas = np.array([self._alpha[a] for a in self.arms], dtype=float)
        betas = np.array([self._beta[a] for a in self.arms], dtype=float)
        draws = np.asarray(rng.beta(alphas, betas), dtype=float)
        return self.arms[int(np.argmax(draws))]

    def update(self, observations: Mapping[str, tuple[float, float]]) -> None:
        """One call per round: decay every arm toward the prior once, then add the observed counts.

        alpha <- discount * (alpha - alpha0) + alpha0 (same for beta), then alpha += successes, beta += failures.
        """
        unknown = [arm for arm in observations if arm not in self._alpha]
        if unknown:
            raise KeyError(f"unknown arms: {unknown!r}")
        for arm, (successes, failures) in observations.items():
            if successes < 0 or failures < 0:
                raise ValueError(f"negative counts for {arm!r}: {(successes, failures)!r}")
        for arm in self.arms:
            self._alpha[arm] = self.discount * (self._alpha[arm] - self.alpha0) + self.alpha0
            self._beta[arm] = self.discount * (self._beta[arm] - self.beta0) + self.beta0
        for arm, (successes, failures) in observations.items():
            self._alpha[arm] += float(successes)
            self._beta[arm] += float(failures)

    def state(self) -> dict[str, tuple[float, float]]:
        """Posterior of every arm, for profiles and logs."""
        return {arm: self.posterior(arm) for arm in self.arms}

    def load_state(self, state: Mapping[str, tuple[float, float]]) -> None:
        """Restore posteriors saved by `state` (arms missing from `state` keep the prior)."""
        unknown = [arm for arm in state if arm not in self._alpha]
        if unknown:
            raise KeyError(f"unknown arms: {unknown!r}")
        for arm, (alpha, beta) in state.items():
            if alpha <= 0.0 or beta <= 0.0:
                raise ValueError(f"non-positive posterior for {arm!r}")
            self._alpha[arm] = float(alpha)
            self._beta[arm] = float(beta)
