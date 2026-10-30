"""Acceptance tests for T7 (curriculum). Skip until sensei.curriculum exists."""

from __future__ import annotations

import numpy as np
import pytest

from sensei.interfaces import Curriculum

curr_mod = pytest.importorskip("sensei.curriculum")
ThompsonCurriculum = curr_mod.ThompsonCurriculum


def run(cur, probs_by_round, rng, trials=10):
    picks = []
    for probs in probs_by_round:
        arm = cur.select(rng)
        s = int(rng.binomial(trials, probs[arm]))
        cur.update({arm: (s, trials - s)})
        picks.append(arm)
    return picks


def test_satisfies_protocol():
    assert isinstance(ThompsonCurriculum(arms=["a", "b"], discount=1.0, prior=(1.0, 1.0)), Curriculum)


def test_stationary_finds_highest_mistake_rate():
    rng = np.random.default_rng(1)
    cur = ThompsonCurriculum(arms=["a", "b", "c"], discount=1.0, prior=(1.0, 1.0))
    probs = {"a": 0.2, "b": 0.7, "c": 0.3}
    picks = run(cur, [probs] * 300, rng)
    assert picks[-100:].count("b") > 70


def test_discounting_tracks_a_change():
    rng = np.random.default_rng(2)
    cur = ThompsonCurriculum(arms=["a", "b", "c"], discount=0.9, prior=(1.0, 1.0))
    before = {"a": 0.2, "b": 0.7, "c": 0.3}
    after = {"a": 0.2, "b": 0.1, "c": 0.7}
    picks = run(cur, [before] * 200 + [after] * 100, rng)
    assert picks[-50:].count("c") > 25


def test_update_with_several_arms_decays_once():
    cur = ThompsonCurriculum(arms=["a", "b"], discount=0.5, prior=(1.0, 1.0))
    cur.update({"a": (4, 0), "b": (0, 4)})
    cur.update({})
    alpha, beta = cur.posterior("a")
    assert abs(alpha - (1.0 + 0.5 * 4)) < 1e-9
    assert abs(beta - 1.0) < 1e-9
