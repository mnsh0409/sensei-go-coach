"""Learner model on top of KataGo's HumanSL policies (INTERFACES.md, "Learner").

Rank estimate: for each profile p on the grid, LL(p) = sum over the learner's moves of log(pi_H(m | x, p) + delta);
with a uniform prior the posterior is softmax(LL), computed by `sensei.ranks.rank_posterior`. Below `min_moves`
moves the declared rank is returned with rank_sd = 3.0. The learner policy is the HumanSL policy at the grid
profile nearest the current estimate, renormalized over the legal moves.

Human policies are cached by (pos.moves, pos.to_move, size, komi, rules, profile) in an LRU, so repeated estimates
over a growing history do not re-query the engine.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Hashable, Mapping, Sequence

from sensei.coords import PASS
from sensei.interfaces import HumanPolicySource, LearnerEstimate, Move, Position
from sensei.ranks import label_to_numeric, nearest_profile, profile_to_numeric, rank_posterior

DECLARED_RANK_SD = 3.0
DEFAULT_CACHE_SIZE = 4096


def position_key(pos: Position) -> tuple[Hashable, ...]:
    """The cache key from INTERFACES.md: Position is not hashable because meta is a dict."""
    meta = pos.meta
    return (pos.moves, pos.to_move, meta.get("size", 19), meta.get("komi", 7.5), meta.get("rules", "chinese"))


def normalize_move(move: str) -> Move:
    """"q16" -> "Q16", "PASS" -> "pass". Does not validate against the board."""
    m = str(move).strip()
    return PASS if m.lower() == PASS else m.upper()


def declared_rank_to_numeric(declared: str | float) -> float:
    """Accepts a rank label ("10k", "1d"), a profile ("rank_10k") or a number on the numeric scale."""
    if isinstance(declared, int | float):
        return float(declared)
    text = str(declared).strip()
    try:
        return label_to_numeric(text)
    except ValueError:
        return profile_to_numeric(text)


class HumanSLLearner:
    """LearnerModel backed by a HumanPolicySource (the KataGo wrapper, or a fake in tests)."""

    def __init__(
        self,
        source: HumanPolicySource,
        grid: Sequence[str],
        declared_rank: str | float,
        min_moves: int = 20,
        delta: float = 1e-6,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if not grid:
            raise ValueError("empty profile grid")
        if min_moves < 0:
            raise ValueError("min_moves must be non-negative")
        if delta <= 0:
            raise ValueError("delta must be positive")
        self.source = source
        self.grid: tuple[str, ...] = tuple(grid)
        for profile in self.grid:
            profile_to_numeric(profile)  # fail early on a bad profile name
        self.declared_rank = declared_rank_to_numeric(declared_rank)
        self.min_moves = int(min_moves)
        self.delta = float(delta)
        self._cache: OrderedDict[tuple[Hashable, ...], dict[Move, float]] = OrderedDict()
        self._cache_size = max(int(cache_size), 1)
        self._lock = threading.Lock()
        self._estimate = LearnerEstimate(rank=self.declared_rank, rank_sd=DECLARED_RANK_SD, n_moves=0)

    # ---- state -------------------------------------------------------------------------------------------------

    @property
    def current_estimate(self) -> LearnerEstimate:
        """The most recent estimate (the declared rank until `estimate` has seen enough moves)."""
        return self._estimate

    @property
    def profile(self) -> str:
        """Grid profile nearest the current estimate; the one `policy` uses."""
        return nearest_profile(self._estimate.rank, self.grid)

    # ---- cached human policies -----------------------------------------------------------------------------------

    def human_policy(self, pos: Position, profile: str) -> Mapping[Move, float]:
        """The source's policy at profile for pos, through the LRU cache."""
        key = (*position_key(pos), profile)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        policy = {normalize_move(m): float(p) for m, p in self.source.human_policy(pos, profile).items()}
        with self._lock:
            self._cache[key] = policy
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return policy

    def cache_info(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._cache), "capacity": self._cache_size}

    # ---- LearnerModel --------------------------------------------------------------------------------------------

    def policy(self, pos: Position, legal: Sequence[Move]) -> Mapping[Move, float]:
        """HumanSL policy at the nearest profile, restricted to legal and renormalized to sum to 1."""
        legal_moves = [normalize_move(m) for m in legal]
        if not legal_moves:
            return {}
        raw = self.human_policy(pos, self.profile)
        weights = {m: max(float(raw.get(m, 0.0)), 0.0) for m in legal_moves}
        total = sum(weights.values())
        if total <= 0.0:  # the source gives no mass to any legal move: fall back to uniform
            return {m: 1.0 / len(legal_moves) for m in legal_moves}
        return {m: w / total for m, w in weights.items()}

    def log_likelihoods(self, history: Sequence[tuple[Position, Move]]) -> dict[str, float]:
        """LL(p) for every grid profile over the learner's moves."""
        loglik = {p: 0.0 for p in self.grid}
        for pos, move in history:
            played = normalize_move(move)
            for profile in self.grid:
                prob = self.human_policy(pos, profile).get(played, 0.0)
                loglik[profile] += math.log(prob + self.delta)
        return loglik

    def estimate(self, history: Sequence[tuple[Position, Move]]) -> LearnerEstimate:
        """Posterior mean and sd on the numeric rank scale; the declared rank (sd 3.0) below min_moves moves."""
        moves = list(history)
        n = len(moves)
        if n < self.min_moves:
            est = LearnerEstimate(rank=self.declared_rank, rank_sd=DECLARED_RANK_SD, n_moves=n)
        else:
            mean, sd, _posterior = rank_posterior(self.log_likelihoods(moves))
            est = LearnerEstimate(rank=mean, rank_sd=sd, n_moves=n)
        self._estimate = est
        return est

    def posterior(self, history: Sequence[tuple[Position, Move]]) -> dict[str, float]:
        """Posterior probability of every grid profile (diagnostics; not needed by the protocol)."""
        _mean, _sd, post = rank_posterior(self.log_likelihoods(list(history)))
        return post

    def __repr__(self) -> str:
        est = self._estimate
        return f"HumanSLLearner(profile={self.profile!r}, rank={est.rank:.2f}, sd={est.rank_sd:.2f}, n={est.n_moves})"
