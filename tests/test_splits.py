"""eval/splits.py against a tiny synthetic data directory."""

from __future__ import annotations

import json

import pytest

splits_mod = pytest.importorskip("eval.splits")

GAME_A = b"(;GM[1]FF[4]SZ[19]KM[6.5]RU[Japanese]BR[12k]WR[11k];B[pd];W[dp];B[pq];W[dd];B[qk];W[nc])"
GAME_B = b"(;GM[1]FF[4]SZ[19]KM[7.5]RU[Chinese]BR[3k]WR[2k];B[dd];W[pp];B[];W[pd])"


def make_dir(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ogs-1.sgf").write_bytes(GAME_A)
    (raw / "ogs-2.sgf").write_bytes(GAME_B)
    splits = {
        "version": 1,
        "seed": 1,
        "source": "test",
        "raw_dir": str(raw),
        "pilot_openings": ["ogs-1"],
        "confirmatory_openings": [],
        "c2_positions": {"15k-10k": [{"game": "ogs-2", "ply": 1}]},
        "topic_games": [],
        "explainer_positions": [],
    }
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(splits), encoding="utf-8")
    return path, raw


def test_load_and_disjoint(tmp_path):
    path, raw = make_dir(tmp_path)
    splits = splits_mod.load_splits(path)
    splits_mod.check_disjoint(splits)
    assert splits_mod.raw_dir(splits) == raw
    bad = dict(splits, topic_games=["ogs-1"])
    with pytest.raises(ValueError, match="share"):
        splits_mod.check_disjoint(bad)


def test_opening_and_position(tmp_path):
    _, raw = make_dir(tmp_path)
    assert splits_mod.opening_moves("ogs-1", 4, raw) == (("B", "Q16"), ("W", "D4"), ("B", "Q3"), ("W", "D16"))
    with pytest.raises(ValueError, match="no-pass"):
        splits_mod.opening_moves("ogs-2", 4, raw)  # Black passes at move 3
    hp = splits_mod.position_at("ogs-1", 2, raw)
    assert hp.played == "Q3" and hp.position.to_move == "B" and hp.mover_rank == "12k"
    assert hp.position.meta == {"size": 19, "komi": 6.5, "rules": "japanese"}
    assert splits_mod.position_at("ogs-1", 3, raw).mover_rank == "11k"
    with pytest.raises(FileNotFoundError, match="fetch_games"):
        splits_mod.game_record("ogs-9", raw)
