"""C3 simulation and the result-file contract (eval/README.md). Fast: a reduced config, nothing touches results/."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import yaml

c3 = pytest.importorskip("eval.c3_curriculum")
run_mod = pytest.importorskip("eval.run")

ROOT = Path(__file__).resolve().parents[1]

CONTRACT_KEYS = {
    "claim", "pilot", "git_commit", "git_dirty", "config_path", "config", "deviations", "katago_version", "nets",
    "analysis_config", "hardware", "concurrency", "epsilon", "seed", "started", "finished", "wall_clock_s",
    "engine_queries", "n", "estimate", "ci", "success", "details",
}  # fmt: skip
HARDWARE_KEYS = {"host", "gpu", "driver", "cuda", "katago_backend", "cpu", "ram_gb"}


def reduced_config() -> dict:
    cfg = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
    cfg["C3"].update(n_learners=10, sessions=5, bootstrap_resamples=200)
    return cfg


@pytest.fixture(scope="module")
def fast_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c3")
    cfg_path = tmp / "c3_fast.yaml"
    cfg_path.write_text(yaml.safe_dump(reduced_config()), encoding="utf-8")
    results_dir = tmp / "results"
    out = run_mod.main(["--claim", "C3", "--config", str(cfg_path), "--results-dir", str(results_dir)])
    return out, results_dir, json.loads(out.read_text(encoding="utf-8"))


def test_result_file_follows_the_contract(fast_run):
    out, results_dir, data = fast_run
    assert out.parent == results_dir and out.name.startswith("C3_") and out.suffix == ".json"
    assert CONTRACT_KEYS <= set(data)
    assert data["claim"] == "C3" and data["pilot"] is False
    assert isinstance(data["git_commit"], str) and isinstance(data["git_dirty"], bool)
    assert data["config"]["C3"]["n_learners"] == 10 and data["deviations"] == []
    assert data["katago_version"] is None and data["nets"] is None and data["analysis_config"] is None
    assert data["concurrency"] is None and data["epsilon"] is None and data["engine_queries"] == 0
    assert HARDWARE_KEYS <= set(data["hardware"])
    assert data["seed"] == data["config"]["seeds"]["global"]
    for key in ("started", "finished"):
        assert datetime.fromisoformat(data[key]).tzinfo is not None
    assert data["wall_clock_s"] >= 0.0
    assert data["n"] == 10
    assert isinstance(data["estimate"], float)
    lo, hi = data["ci"]
    assert lo <= data["estimate"] <= hi
    assert isinstance(data["success"], bool)
    assert data["success"] == (data["estimate"] > 0 and lo > 0)


def test_policy_ordering_and_details(fast_run):
    _, _, data = fast_run
    d = data["details"]
    assert d["simulation"] is True
    auc = d["auc"]
    assert set(auc) == {"thompson", "greedy_mean", "round_robin", "uniform_random", "oracle"}
    assert auc["oracle"] <= auc["thompson"] <= auc["uniform_random"]
    assert data["estimate"] == pytest.approx(auc["round_robin"] - auc["thompson"])
    gap = d["gap_closure"]
    assert gap["ci"][0] <= gap["estimate"] <= gap["ci"][1]
    assert d["headline"]["name"] == "gap_closure" and d["headline"]["estimate"] == gap["estimate"]
    assert "estimate" in d["greedy_vs_thompson"] and len(d["greedy_vs_thompson"]["ci"]) == 2
    share = d["identification_share"]
    assert set(share) == set(auc) and all(0.0 <= v <= 1.0 for v in share.values())
    assert share["oracle"] == 1.0
    assert d["identification_session"] == 5  # clamped to the reduced number of sessions
    assert all(len(curve) == 5 for curve in d["mean_curve"].values())


def test_summary_is_regenerated(fast_run):
    _, results_dir, data = fast_run
    text = (results_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "C3 (simulation)" in text
    assert Path(data["config_path"]).name == "c3_fast.yaml"
    assert "gap_closure" in text


def test_simulation_is_deterministic():
    cfg = reduced_config()
    a = c3.run_c3(cfg, seed=7)
    b = c3.run_c3(cfg, seed=7)
    assert a["estimate"] == b["estimate"] and a["ci"] == b["ci"]
    assert a["details"]["auc"] == b["details"]["auc"]
    assert c3.run_c3(cfg, seed=8)["details"]["auc"] != a["details"]["auc"]


def test_oracle_targets_only_weak_topics_and_rates_fall():
    cfg = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
    weak = np.array([2, 5])
    run = c3.simulate_learner(cfg["C3"], weak, "oracle", np.random.default_rng(0))
    assert run.weak == (2, 5)
    assert set(run.selections) == {2, 5}
    assert run.curve[0] == pytest.approx((2 * 0.40 + 6 * 0.10) / 8)
    assert all(later < earlier for earlier, later in zip(run.curve, run.curve[1:], strict=False))
    rr = c3.simulate_learner(cfg["C3"], weak, "round_robin", np.random.default_rng(0))
    assert rr.selections == tuple(range(8)) + tuple(range(8)) + tuple(range(4))
