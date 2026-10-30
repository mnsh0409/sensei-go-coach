"""Constrained re-ranking of engine candidates: the most instructive move whose value loss stays within epsilon.

The exact rule is fixed in INTERFACES.md ("Selector"). Everything here is deterministic given the inputs and,
at temperature > 0, the numpy Generator passed in. The selector never looks at anything but engine numbers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from sensei.interfaces import Candidate, Choice, EngineAnalysis, Move, PositionClass


def instructiveness(pi_e: Mapping[Move, float], pi_l: Mapping[Move, float], delta: float) -> dict[Move, float]:
    """I(a) = pi_E(a) * log((pi_E(a) + delta) / (pi_L(a) + delta)) for every key of pi_e.

    The pointwise contribution of a to KL(pi_E || pi_L). A move missing from pi_l has pi_L(a) = 0.
    """
    if delta <= 0.0:
        raise ValueError(f"delta must be positive, got {delta!r}")
    return {a: float(p) * math.log((float(p) + delta) / (float(pi_l.get(a, 0.0)) + delta)) for a, p in pi_e.items()}


def policy_entropy(policy: Mapping[Move, float]) -> float:
    """Shannon entropy of a policy in nats; zero-probability entries contribute nothing."""
    return float(-sum(p * math.log(p) for p in policy.values() if p > 0.0))


class ConstrainedSelector:
    """Selector (interfaces.Selector) implementing the rule in INTERFACES.md.

    delta: smoothing inside the instructiveness log; min_visits_abs and min_visits_frac: the reliability
    threshold max(min_visits_abs, min_visits_frac * total visits); entropy_low and entropy_high: position class
    thresholds on the entropy of the engine policy, in nats.
    """

    def __init__(
        self,
        delta: float,
        min_visits_abs: int,
        min_visits_frac: float,
        entropy_low: float,
        entropy_high: float,
    ) -> None:
        if delta <= 0.0:
            raise ValueError("delta must be positive")
        if min_visits_abs < 0 or not 0.0 <= min_visits_frac <= 1.0:
            raise ValueError("min_visits_abs must be >= 0 and min_visits_frac within [0, 1]")
        if entropy_low > entropy_high:
            raise ValueError("entropy_low must not exceed entropy_high")
        self.delta = float(delta)
        self.min_visits_abs = int(min_visits_abs)
        self.min_visits_frac = float(min_visits_frac)
        self.entropy_low = float(entropy_low)
        self.entropy_high = float(entropy_high)

    # Reliability rule (also used by the mistake check, INTERFACES.md "Mistakes and mistake topics").

    def reliable(self, analysis: EngineAnalysis) -> tuple[Candidate, ...]:
        """Candidates with N(a) >= max(min_visits_abs, min_visits_frac * sum N); the most visited one if none."""
        cands = analysis.candidates
        if not cands:
            raise ValueError("analysis has no candidates")
        total = sum(int(c.visits) for c in cands)
        threshold = max(float(self.min_visits_abs), self.min_visits_frac * total)
        kept = tuple(c for c in cands if c.visits >= threshold)
        if kept:
            return kept
        return (max(cands, key=lambda c: c.visits),)

    def best_value(self, analysis: EngineAnalysis) -> float:
        """V* = max V(a) over reliable candidates."""
        return max(float(c.value) for c in self.reliable(analysis))

    def admissible(self, analysis: EngineAnalysis, epsilon: float) -> tuple[Candidate, ...]:
        """A = { a reliable : V* - V(a) <= epsilon }, in candidate order (visits descending). Never empty."""
        if epsilon < 0.0:
            raise ValueError("epsilon must be >= 0")
        reliable = self.reliable(analysis)
        v_star = max(float(c.value) for c in reliable)
        return tuple(c for c in reliable if v_star - float(c.value) <= epsilon)

    def position_class(self, analysis: EngineAnalysis) -> PositionClass:
        """ONLY_MOVE below entropy_low, STRATEGIC above entropy_high, TACTICAL otherwise (entropy in nats)."""
        h = policy_entropy(analysis.policy)
        if h < self.entropy_low:
            return PositionClass.ONLY_MOVE
        if h > self.entropy_high:
            return PositionClass.STRATEGIC
        return PositionClass.TACTICAL

    def choose(
        self,
        analysis: EngineAnalysis,
        learner_policy: Mapping[Move, float],
        epsilon: float,
        temperature: float,
        rng: Any,
    ) -> Choice:
        if temperature < 0.0:
            raise ValueError("temperature must be >= 0")
        admissible = self.admissible(analysis, epsilon)
        v_star = max(float(c.value) for c in admissible)  # the best reliable candidate is always admissible
        pi_e = {c.move: float(analysis.policy.get(c.move, 0.0)) for c in admissible}
        scores = instructiveness(pi_e, learner_policy, self.delta)

        if temperature == 0.0:
            # argmax I; ties by higher V, then by more visits; then candidate order (max keeps the first).
            chosen = max(admissible, key=lambda c: (scores[c.move], float(c.value), int(c.visits)))
        else:
            logits = np.array([scores[c.move] for c in admissible], dtype=float) / temperature
            logits -= logits.max()
            probs = np.exp(logits)
            probs /= probs.sum()
            chosen = admissible[int(rng.choice(len(admissible), p=probs))]

        return Choice(
            move=chosen.move,
            instructiveness=scores[chosen.move],
            value_loss=v_star - float(chosen.value),
            position_class=self.position_class(analysis),
            scores=scores,
            admissible=tuple(c.move for c in admissible),
        )
