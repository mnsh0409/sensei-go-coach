"""T6: scripts/learner_check.py end to end against tests/fake_katago.py (plumbing only)."""

from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

from tests.test_c2 import game_sgf

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_learner_check_offline(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    rng = random.Random(2)
    (raw / "weak.sgf").write_text(game_sgf(rng, "12k", n=42), encoding="utf-8")
    (raw / "mixed.sgf").write_text(game_sgf(rng, "7k", n=42, black_rank="1d"), encoding="utf-8")
    splits = {"version": 1, "seed": 1, "source": "test", "raw_dir": str(raw), "pilot_openings": [],
              "confirmatory_openings": [], "topic_games": [], "explainer_positions": [],
              "c2_positions": {"15k-10k": [{"game": "weak", "ply": 20}, {"game": "weak", "ply": 22}],
                               "9k-5k": [{"game": "mixed", "ply": 21}]}}  # fmt: skip
    splits_path = tmp_path / "splits.json"
    splits_path.write_text(json.dumps(splits), encoding="utf-8")
    out = tmp_path / "learner_check.json"
    lc = load_script("learner_check")
    assert lc.main(["--fake", "--splits", str(splits_path), "--out", str(out), "--concurrency", "2"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    games = {g["game"]: g for g in data["games"]}
    assert set(games) == {"weak", "mixed"} and all(g["error"] is None for g in games.values())
    assert games["weak"]["color"] == "B" and games["weak"]["moves"] == 21
    assert set(games["weak"]["estimates"]) == {"20", "all"}  # 21 moves: one checkpoint, then all of them
    assert games["weak"]["estimates"]["all"]["rank_sd"] < 3.0  # estimated from moves, not the declared fallback
    assert games["mixed"]["color"] == "W" and games["mixed"]["true_rank"] == "7k"  # Black is 1d, outside 9k-5k
    assert data["summary"]["all"]["n_games"] == 2 and data["summary"]["all"]["mean_abs_error_ranks"] is not None
    assert data["hardware"]["katago_backend"] == "fake" and data["descriptive"] is True
