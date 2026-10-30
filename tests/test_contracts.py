"""Contract checks that pass from day one."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from sensei import interfaces as I
from sensei.ranks import profile_to_numeric

ROOT = Path(__file__).resolve().parents[1]


def test_dataclasses_are_frozen(analysis_factory):
    a = analysis_factory([("Q16", 0.6, 0.55, 120), ("D4", 0.4, 0.54, 80)])
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.root_value = 0.0  # type: ignore[misc]


def test_policy_sums_to_one(analysis_factory):
    a = analysis_factory([("Q16", 3.0, 0.55, 120), ("D4", 1.0, 0.54, 80)])
    assert abs(sum(a.policy.values()) - 1.0) < 1e-9


def test_position_class_values():
    assert {c.value for c in I.PositionClass} == {"only_move", "tactical", "strategic"}


def test_protocols_are_runtime_checkable():
    class FakeCurriculum:
        arms = ("a", "b")

        def select(self, rng):
            return "a"

        def update(self, observations):
            return None

    assert isinstance(FakeCurriculum(), I.Curriculum)
    assert not isinstance(object(), I.Curriculum)


def load(name):
    return yaml.safe_load((ROOT / "configs" / name).read_text(encoding="utf-8"))


def test_prereg_has_required_keys():
    cfg = load("prereg.yaml")
    required = ("game", "engine", "selector", "openings", "adjudication", "C1", "C2", "C3", "learner", "seeds")
    for key in required:
        assert key in cfg, key
    assert cfg["frozen"] == "2026-10-27"
    assert cfg["revisions"][0]["before_any_run"] is True
    assert cfg["engine"]["winrate_perspective"] == "SIDETOMOVE"


def test_prereg_c1_design():
    cfg = load("prereg.yaml")
    eps = cfg["C1"]["pilot_epsilons"]
    assert eps == sorted(eps) and all(0 < e < 0.2 for e in eps)
    assert cfg["C1"]["pilot_games_each"] == 2 * cfg["openings"]["pilot_n_openings"]
    assert cfg["C1"]["n_games"] == 2 * cfg["openings"]["n_openings"]
    assert cfg["openings"]["n_openings"] >= 100
    assert 0 < cfg["C1"]["pilot_min_score"] < 0.5


def test_prereg_c2_c3_design():
    cfg = load("prereg.yaml")
    strengths = [profile_to_numeric(b["profile"]) for b in cfg["C2"]["bands"]]
    assert len(strengths) >= 2 and strengths == sorted(strengths)  # weak to strong
    assert cfg["C2"]["epsilon"] == "c1_confirmatory"
    assert {"thompson", "round_robin", "oracle", "greedy_mean"} <= set(cfg["C3"]["policies"])


def test_runtime_config_is_consistent_with_prereg():
    pre, run = load("prereg.yaml"), load("default.yaml")
    assert run["play"]["epsilon"] in pre["C1"]["pilot_epsilons"]
    assert run["play"]["mistake_threshold"] == run["topics"]["loss_bins"][0]
    assert run["selector"]["entropy_low"] < run["selector"]["entropy_high"]
    assert "entropy_low" not in pre["selector"]


def test_scope_mentions_every_claim():
    scope = (ROOT / "SCOPE.md").read_text(encoding="utf-8")
    for claim in ("C1", "C2", "C3"):
        assert f"### {claim}" in scope
