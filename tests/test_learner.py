"""Acceptance tests for T6 (offline, with a fake HumanSL source). Skip until sensei.learner exists."""

from __future__ import annotations

import math

import pytest

from sensei.interfaces import LearnerModel, Position
from sensei.ranks import profile_to_numeric

learner_mod = pytest.importorskip("sensei.learner")
HumanSLLearner = learner_mod.HumanSLLearner

GRID = ["rank_15k", "rank_10k", "rank_5k", "rank_1d"]


class FakeHumanSL:
    """Gives the played move probability 0.5 under rank_5k and 0.05 under every other profile."""

    def __init__(self, played_move="Q16"):
        self.played = played_move
        self.calls = 0

    def human_policy(self, pos, profile):
        self.calls += 1
        p = 0.5 if profile == "rank_5k" else 0.05
        return {self.played: p, "D4": 1 - p}


def history(n):
    pos = Position(game="go", moves=(), to_move="B", meta={"size": 19})
    return [(pos, "Q16")] * n


def make(fake, declared="10k"):
    return HumanSLLearner(source=fake, grid=GRID, declared_rank=declared, min_moves=20, delta=1e-6)


def test_satisfies_protocol():
    assert isinstance(make(FakeHumanSL()), LearnerModel)


def test_few_moves_returns_declared_rank():
    est = make(FakeHumanSL(), declared="10k").estimate(history(5))
    assert est.rank == profile_to_numeric("rank_10k")
    assert est.rank_sd == 3.0
    assert est.n_moves == 5


def test_estimate_converges_to_true_profile():
    est = make(FakeHumanSL()).estimate(history(40))
    assert math.isclose(est.rank, profile_to_numeric("rank_5k"), abs_tol=0.05)
    assert est.rank_sd < 0.5
    assert est.n_moves == 40


def test_policy_uses_nearest_profile_and_renormalizes():
    fake = FakeHumanSL()
    model = make(fake)
    model.estimate(history(40))
    pos = history(1)[0][0]
    pol = model.policy(pos, ["Q16", "D4", "C3"])
    assert set(pol) <= {"Q16", "D4", "C3"}
    assert abs(sum(pol.values()) - 1.0) < 1e-9
    assert math.isclose(pol["Q16"], 0.5, abs_tol=1e-9)
