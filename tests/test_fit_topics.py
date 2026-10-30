"""T7: topic-model persistence and scripts/fit_topics.py end to end against tests/fake_katago.py."""

from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

import numpy as np
import pytest
import yaml

from sensei.topics import LdaMistakeTopics
from tests.test_c2 import game_sgf

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ["phase:opening", "phase:middle", "phase:end", "region:corner", "region:edge", "region:center",
          "class:tactical", "class:strategic", "local:contact", "local:capture", "local:tenuki"]  # fmt: skip


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_topic_model_json_round_trip(tmp_path):
    rng = random.Random(0)
    bags = [{t: rng.randint(1, 3) for t in rng.sample(TOKENS, 4)} for _ in range(150)]
    model = LdaMistakeTopics(4, seed=7)
    model.fit(bags)
    path = model.save(tmp_path / "topics.json", suggested_names=["a", "b", "c", "d"])
    loaded = LdaMistakeTopics.load(path)
    for bag in bags[:25] + [{"unknown": 1}, {"phase:end": 2, "unknown": 5}]:
        assert np.allclose(model.assign(bag), loaded.assign(bag), atol=1e-9)
    assert loaded.describe(2, 5) == pytest.approx(model.describe(2, 5))
    assert json.loads(path.read_text(encoding="utf-8"))["suggested_names"] == ["a", "b", "c", "d"]
    with pytest.raises(ValueError):
        LdaMistakeTopics.from_json({"kind": "something else"})


def test_fit_topics_offline(tmp_path):
    root = tmp_path / "repo"
    (root / "configs").mkdir(parents=True)
    pre = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
    runtime = yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))
    runtime["play"]["mistake_threshold"] = -1.0  # every move counts: the fake engine's numbers make no mistakes
    runtime["topics"]["loss_bins"] = [-1.0, 0.10, 0.20]  # and every loss gets a severity
    runtime["topics"]["n_topics"] = 3
    (root / "configs" / "prereg.yaml").write_text(yaml.safe_dump(pre), encoding="utf-8")
    (root / "configs" / "default.yaml").write_text(yaml.safe_dump(runtime), encoding="utf-8")
    raw = tmp_path / "raw"
    raw.mkdir()
    rng = random.Random(5)
    ids = [f"t-{i}" for i in range(3)]
    for game_id in ids:
        (raw / f"{game_id}.sgf").write_text(game_sgf(rng, "5k", n=30), encoding="utf-8")
    (raw / "t-bad.sgf").write_text(game_sgf(rng, "5k", n=30).replace("KM[6.5]", ""), encoding="utf-8")
    splits = {"version": 1, "seed": 1, "source": "test", "raw_dir": str(raw), "pilot_openings": [],
              "confirmatory_openings": [], "c2_positions": {}, "topic_games": [*ids, "t-bad"],
              "explainer_positions": []}  # fmt: skip
    splits_path = tmp_path / "splits.json"
    splits_path.write_text(json.dumps(splits), encoding="utf-8")
    out = tmp_path / "results"
    fit = load_script("fit_topics")
    args = ["--fake", "--root", str(root), "--splits", str(splits_path), "--out", str(out), "--concurrency", "2"]
    assert fit.main(args) == 0

    data = json.loads((out / "topics_v1.json").read_text(encoding="utf-8"))
    corpus = data["corpus"]
    assert corpus["games_listed"] == 4 and corpus["games_used"] == 3 and corpus["games_with_errors"] == ["t-bad"]
    assert corpus["moves"] == corpus["mistakes"] == 90 and corpus["documents"] == 3 * 2 * 3  # 15 per player, 5 each
    assert corpus["engine_queries"] >= 90 and data["hardware"]["katago_backend"] == "fake"
    assert len(data["suggested_names"]) == 3 and data["settings"]["fit_visits"] == runtime["topics"]["fit_visits"]
    model = LdaMistakeTopics.load(out / "topics_v1.json")
    assert len(model.assign({"phase:opening": 1})) == 3
    md = (out / "topics_v1.md").read_text(encoding="utf-8")
    assert "## Topic 0" in md and "HUMAN gate" in md and "Example: game" in md
    with pytest.raises(SystemExit, match="exists"):
        fit.main(args)
