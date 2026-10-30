"""Acceptance tests for T4. Skip until sensei.selector exists."""

from __future__ import annotations

import math

import numpy as np
import pytest

from sensei.interfaces import PositionClass

selector_mod = pytest.importorskip("sensei.selector")
ConstrainedSelector = selector_mod.ConstrainedSelector
instructiveness = selector_mod.instructiveness

# move, prior, winrate, visits
CANDS = [("Q16", 0.5, 0.60, 300), ("R14", 0.3, 0.59, 100), ("C3", 0.2, 0.55, 50)]


def make_selector():
    return ConstrainedSelector(
        delta=1e-6, min_visits_abs=10, min_visits_frac=0.05, entropy_low=0.5, entropy_high=2.0
    )


def test_instructiveness_formula():
    pe = {"a": 0.5, "b": 0.5}
    pl = {"a": 0.9}
    got = instructiveness(pe, pl, delta=1e-6)
    assert math.isclose(got["a"], 0.5 * math.log((0.5 + 1e-6) / (0.9 + 1e-6)), rel_tol=1e-9)
    assert math.isclose(got["b"], 0.5 * math.log((0.5 + 1e-6) / (0.0 + 1e-6)), rel_tol=1e-9)


def test_epsilon_zero_returns_best(analysis_factory):
    a = analysis_factory(CANDS)
    learner = {"Q16": 0.9, "R14": 0.01, "C3": 0.09}
    c = make_selector().choose(a, learner, epsilon=0.0, temperature=0.0, rng=np.random.default_rng(0))
    assert c.move == "Q16"
    assert c.value_loss == 0.0
    assert c.admissible == ("Q16",)


def test_prefers_move_the_learner_misses(analysis_factory):
    a = analysis_factory(CANDS)
    learner = {"Q16": 0.9, "R14": 0.01, "C3": 0.09}
    c = make_selector().choose(a, learner, epsilon=0.02, temperature=0.0, rng=np.random.default_rng(0))
    assert c.move == "R14"
    assert set(c.admissible) == {"Q16", "R14"}
    assert math.isclose(c.value_loss, 0.01, abs_tol=1e-12)
    assert c.scores["R14"] > c.scores["Q16"]


def test_identical_policies_fall_back_to_value(analysis_factory):
    a = analysis_factory(CANDS)
    c = make_selector().choose(a, dict(a.policy), epsilon=0.1, temperature=0.0, rng=np.random.default_rng(0))
    assert c.move == "Q16"


def test_low_visit_candidates_are_dropped(analysis_factory):
    cands = CANDS + [("D4", 0.01, 0.80, 3)]  # best winrate but only 3 visits
    a = analysis_factory(cands)
    learner = {"Q16": 0.9, "R14": 0.09, "C3": 0.009, "D4": 0.001}
    c = make_selector().choose(a, learner, epsilon=0.05, temperature=0.0, rng=np.random.default_rng(0))
    assert "D4" not in c.admissible
    assert c.move != "D4"
    assert c.value_loss <= 0.05 + 1e-12  # measured against Q16, not D4


def test_all_low_visits_falls_back_to_most_visited(analysis_factory):
    a = analysis_factory([("Q16", 0.5, 0.50, 4), ("D4", 0.5, 0.70, 3)])
    c = make_selector().choose(a, {"Q16": 0.99}, epsilon=0.5, temperature=0.0, rng=np.random.default_rng(0))
    assert c.admissible == ("Q16",)
    assert c.move == "Q16"
    assert c.value_loss == 0.0


def test_value_loss_never_exceeds_epsilon(analysis_factory):
    rng = np.random.default_rng(123)
    sel = make_selector()
    for _ in range(300):
        k = int(rng.integers(2, 12))
        moves = [f"{'ABCDEFGHJKLMNOPQRST'[i]}{int(rng.integers(1, 20))}" for i in range(k)]
        priors = rng.dirichlet(np.ones(k))
        values = rng.uniform(0.3, 0.7, size=k)
        visits = rng.integers(1, 400, size=k)
        a = analysis_factory(list(zip(moves, priors, values, visits, strict=True)))
        learner = dict(zip(moves, rng.dirichlet(np.ones(k)), strict=True))
        eps = float(rng.uniform(0.0, 0.1))
        temp = float(rng.choice([0.0, 0.5]))
        c = sel.choose(a, learner, epsilon=eps, temperature=temp, rng=rng)
        assert 0.0 <= c.value_loss <= eps + 1e-12
        assert c.move in c.admissible
        assert set(c.scores) == set(c.admissible)


def test_temperature_samples_only_admissible_and_favours_instructive(analysis_factory):
    a = analysis_factory(CANDS)
    learner = {"Q16": 0.9, "R14": 0.01, "C3": 0.09}
    sel = make_selector()
    rng = np.random.default_rng(7)
    picks = [sel.choose(a, learner, epsilon=0.02, temperature=0.5, rng=rng).move for _ in range(400)]
    assert set(picks) <= {"Q16", "R14"}
    assert picks.count("R14") > picks.count("Q16")


def test_position_class(analysis_factory):
    sel = make_selector()
    rng = np.random.default_rng(0)
    peaked = analysis_factory([("Q16", 0.98, 0.6, 300), ("D4", 0.02, 0.5, 20)])
    assert sel.choose(peaked, {}, 0.0, 0.0, rng).position_class == PositionClass.ONLY_MOVE
    coords = [f"{c}{r}" for c in "ABCDEFGHJKLMNOPQRST" for r in range(1, 20)]
    flat_policy = {m: 1 / len(coords) for m in coords}
    flat = analysis_factory([("Q16", 0.01, 0.6, 300), ("D4", 0.01, 0.59, 200)], policy=flat_policy)
    assert sel.choose(flat, {}, 0.0, 0.0, rng).position_class == PositionClass.STRATEGIC
    mid_policy = {"Q16": 0.4, "D4": 0.3, "C3": 0.2, "R14": 0.1}
    mid = analysis_factory([("Q16", 0.4, 0.6, 300), ("D4", 0.3, 0.59, 200)], policy=mid_policy)
    assert sel.choose(mid, {}, 0.0, 0.0, rng).position_class == PositionClass.TACTICAL
