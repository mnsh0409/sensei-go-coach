"""eval/relevance_baselines.py: descriptive context for C2 (not a claim).

The statistics are checked on hand-made records; the file round trip without an engine on a hand-made C2 result; the
engine part on synthetic games with an in-process engine whose human policy depends on the profile, and once through
the command line against tests/fake_katago.py (synthetic numbers: plumbing only).
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import pytest
import yaml

rb = pytest.importorskip("eval.relevance_baselines")
run_mod = pytest.importorskip("eval.run")
from tests.test_c1 import PRE, RUNTIME, ScriptedEngine  # noqa: E402
from tests.test_c2 import make_data, small_cfg, write_pilot  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_katago.py"


def rec(band, game, played, lessons, best="B", rand="R", recomputed=()):
    return {
        "index": 0,
        "band": band,
        "game": game,
        "ply": 30,
        "played": played,
        "best": best,
        "rand": rand,
        "lesson": dict(lessons),
        "source": {k: ("recomputed" if k in recomputed else "C2") for k in lessons},
    }


def three_bands(with_far: bool = True):
    """Band 0: P(s0) 0.2, P(b) 0.3, P(r) 0.2. Band 1: P(s0) 0.4, P(s1) 0.1, P(b) 0.4, P(r) 0.1.
    Band 2: P(s0) 0.5 (recomputed), P(s1) 0.2, P(s2) 0.1, P(b) 0.1, P(r) 0.1. In band 0, s0 is the best move once."""
    b0 = ["S0", "S0", "B", "B", "B", "R", "R", "X", "X", "X"]
    b1 = ["S0", "S0", "S0", "S0", "S1", "B", "B", "B", "B", "R"]
    b2 = ["S0", "S0", "S0", "S0", "S0", "S1", "S1", "S2", "B", "R"]
    band0 = [rec(0, f"a{i % 5}", m, {0: "B" if i == 9 else "S0"}) for i, m in enumerate(b0)]
    band1 = [rec(1, f"b{i % 5}", m, {1: "S1", 0: "S0"}) for i, m in enumerate(b1)]
    far = {0: "S0"} if with_far else {}
    band2 = [rec(2, f"c{i % 5}", m, {2: "S2", 1: "S1", **far}, recomputed=(0,)) for i, m in enumerate(b2)]
    return [band0, band1, band2]


def test_rates_steps_and_adjacent_summaries():
    out = rb.compute(three_bands(), n_resamples=500, level=0.95, seed=1)
    lesson = out["rates"]["lesson"]
    assert lesson[0][0]["estimate"] == pytest.approx(0.2) and lesson[0][1]["estimate"] == pytest.approx(0.4)
    assert lesson[0][2]["estimate"] == pytest.approx(0.5) and lesson[0][2]["source"] == "recomputed"
    assert lesson[1][1]["estimate"] == pytest.approx(0.1) and lesson[1][2]["estimate"] == pytest.approx(0.2)
    assert lesson[2][2]["estimate"] == pytest.approx(0.1) and lesson[1][0] is None and lesson[0][0]["n"] == 10
    assert [c["estimate"] for c in out["rates"]["best"]] == pytest.approx([0.3, 0.4, 0.1])
    assert [c["estimate"] for c in out["rates"]["rand"]] == pytest.approx([0.2, 0.1, 0.1])
    # positions where the lesson is not the best move: band 0 drops position 9 (s0 = b there)
    assert out["rates"]["lesson_not_best"][0][0]["estimate"] == pytest.approx(2 / 9)
    assert out["rates"]["lesson_not_best"][0][0]["n"] == 9 and out["lesson_is_best_share"][0][0] == pytest.approx(0.1)
    assert out["not_best_share"] == pytest.approx(29 / 30)

    steps = {(s["weaker"], s["stronger"]): s for s in out["steps"]}
    assert set(steps) == {(0, 1), (0, 2), (1, 2)}
    s01 = steps[0, 1]
    assert s01["lesson"]["diff"]["estimate"] == pytest.approx(0.2) and s01["lesson"]["ratio"][
        "estimate"
    ] == pytest.approx(2.0)
    assert s01["best"]["ratio"]["estimate"] == pytest.approx(4 / 3) and s01["rand"]["ratio"][
        "estimate"
    ] == pytest.approx(0.5)
    assert s01["lesson_minus_rand"]["estimate"] == pytest.approx(0.3)
    assert s01["lesson_over_rand"]["estimate"] == pytest.approx(4.0)
    assert s01["lesson_over_best"]["estimate"] == pytest.approx(1.5)
    assert steps[0, 2]["lesson"]["ratio"]["estimate"] == pytest.approx(2.5) and steps[0, 2]["gap"] == 2
    assert out["full_range"] == steps[0, 2]

    adj = out["adjacent"]
    assert adj["n_steps"] == 2 and adj["lesson_diff_mean"]["estimate"] == pytest.approx(0.15)
    assert adj["lesson_ratio_gmean"]["estimate"] == pytest.approx(2.0)
    assert adj["best_ratio_gmean"]["estimate"] == pytest.approx(math.sqrt(4 / 3 * 0.25))
    assert adj["rand_ratio_gmean"]["estimate"] == pytest.approx(math.sqrt(0.5))
    assert adj["lesson_over_rand_gmean"]["estimate"] == pytest.approx(math.sqrt(4.0 * 2.0))
    for stat in (adj["lesson_diff_mean"], adj["best_diff_mean"], s01["lesson_minus_rand"]):
        assert stat["ci"][0] <= stat["estimate"] <= stat["ci"][1]
    assert out == rb.compute(three_bands(), n_resamples=500, level=0.95, seed=1)  # deterministic


def test_undefined_ratios_are_none_not_errors():
    bands = three_bands()
    for r in bands[0]:
        r["played"] = "X"  # band 0 never plays its lesson, the best or the random move
    out = rb.compute(bands, n_resamples=200, level=0.95, seed=2)
    s01 = next(s for s in out["steps"] if (s["weaker"], s["stronger"]) == (0, 1))
    assert s01["lesson"]["ratio"] == {"estimate": None, "ci": None}
    assert s01["lesson"]["diff"]["estimate"] == pytest.approx(0.4)
    assert out["adjacent"]["lesson_ratio_gmean"]["estimate"] is None


def c2_result(bands, cfg, *, estimate):
    rows = []
    for recs in bands:
        for r in recs:
            j = r["band"]
            rows.append([j, r["game"], r["ply"], r["played"], r["lesson"][j], r["lesson"].get(j - 1) if j else None,
                         r["best"], r["rand"]])  # fmt: skip
    return {
        "claim": "C2",
        "pilot": False,
        "git_commit": "abc1234",
        "git_dirty": False,
        "config": cfg,
        "seed": 20261027,
        "epsilon": 0.01,
        "n": len(rows),
        "estimate": estimate,
        "ci": [0.05, 0.25],
        "details": {"positions": rows, "positions_columns": list(rb.COLUMNS)},
    }


def three_band_cfg():
    cfg = copy.deepcopy(PRE)
    cfg["C2"]["bands"] = cfg["C2"]["bands"][:3]
    cfg["C2"]["bootstrap_resamples"] = 300
    return cfg


def test_no_engine_file_round_trip(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    source = results / "C2_20261031-134717.json"
    source.write_text(json.dumps(c2_result(three_bands(with_far=False), three_band_cfg(), estimate=0.15)), "utf-8")
    out = results / "relevance_baselines.json"
    assert rb.main(["--no-engine", "--source", str(source), "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["kind"] == "relevance_baselines" and data["descriptive"] and "claim" not in data
    assert data["engine"] == {"ran": False} and data["matches_c2_U"] is True and data["warnings"] == []
    assert data["source"]["file"] == source.name and len(data["source"]["sha256"]) == 64
    assert data["bands"] == ["15k-10k", "9k-5k", "4k-1k"] and data["n_positions"] == [10, 10, 10]
    assert data["rates"]["lesson"][0][2] is None  # two bands up needs the engine
    assert [(s["weaker"], s["stronger"]) for s in data["steps"]] == [(0, 1), (1, 2)]
    assert data["hardware"]["katago_backend"] is None and data["bootstrap"]["resamples"] == 300
    md = (results / "relevance_baselines.md").read_text(encoding="utf-8")
    assert (
        "Sensei's lesson for 15k-10k | 20.0%" in md
        and "not computed" in md
        and "reproduced from the C2 file: yes" in md
    )
    run_mod.regenerate_summary(results)  # a descriptive file is not a claim row
    assert "relevance_baselines.json" not in (results / "SUMMARY.md").read_text(encoding="utf-8").split("Files in")[0]

    wrong = c2_result(three_bands(with_far=False), three_band_cfg(), estimate=0.5)
    source.write_text(json.dumps(wrong), encoding="utf-8")
    assert rb.main(["--no-engine", "--source", str(source), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["matches_c2_U"] is False
    with pytest.raises(ValueError, match="not a confirmatory C2"):
        rb.build({**wrong, "pilot": True}, source)


class ProfileEngine(ScriptedEngine):
    """ScriptedEngine whose human policy depends on the profile: rank_12k and rank_2k put 0.9 on the engine's top move
    (Sensei then teaches the second candidate), every other profile puts 0.9 on the second (Sensei teaches the top)."""

    def human_policy(self, pos, profile):
        self.calls += 1
        legal = self.game.legal_moves(pos)
        top, second = self._top(pos)[:2]
        liked = top if profile in ("rank_12k", "rank_2k") else second
        rest = 0.1 / (len(legal) - 1)
        return {m: (0.9 if m == liked else rest) for m in legal}


def c2_run(tmp_path, monkeypatch, engine, **cfg_changes):
    splits_path, splits = make_data(tmp_path, games_per_band=8)
    cfg = small_cfg(C2__positions_per_band=4, **cfg_changes)
    cfg_path = tmp_path / "prereg_small.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    results = tmp_path / "results"
    write_pilot(results, 0.02)
    from eval import c2_relevance as c2

    def runner(cfg, *, seed, pilot, concurrency, results_dir):
        return c2.run_c2(
            cfg, seed=seed, concurrency=concurrency, engine=engine, runtime=RUNTIME, results_dir=results_dir,
            splits_path=splits_path,
        )  # fmt: skip

    monkeypatch.setitem(run_mod.CLAIMS, "C2", runner)
    path = run_mod.run("C2", cfg_path, concurrency=2, results_dir=results)
    return path, splits_path, Path(splits["raw_dir"])


def test_engine_adds_the_stronger_band_cells(tmp_path, monkeypatch):
    path, _, raw = c2_run(tmp_path, monkeypatch, ProfileEngine())
    result = json.loads(path.read_text(encoding="utf-8"))
    engine = ProfileEngine()
    data = rb.build(result, path, engine=engine, runtime=RUNTIME, directory=raw, concurrency=3)
    eng = data["engine"]
    assert eng["ran"] and eng["errors"] == 0 and eng["positions"] == 16
    assert eng["queries"] == engine.calls == sum(1 + (b + 1) for b in range(4)) * 4
    assert eng["agreement"]["own"] == [1.0] * 4 and eng["agreement"]["best"] == [1.0] * 4
    assert eng["agreement"]["lower"] == [None, 1.0, 1.0, 1.0]
    lesson = data["rates"]["lesson"]
    assert {(k, j) for k in range(4) for j in range(4) if lesson[k][j] and lesson[k][j]["source"] == "recomputed"} == {
        (0, 2),
        (0, 3),
        (1, 3),
    }
    assert all(lesson[k][j]["n"] == 4 for k in range(4) for j in range(k, 4))
    assert len(data["steps"]) == 6 and data["full_range"]["stronger"] == 3 and data["matches_c2_U"] is True
    # the in-process engine has no nets: the comparison with the C2 run's nets finds nothing to flag
    assert not [w for w in data["warnings"] if "net" in w]


def test_main_against_the_fake_katago(tmp_path, monkeypatch):
    from sensei.engine_katago import KataGoEngine

    engine = KataGoEngine(
        exe=sys.executable, exe_args=[str(FAKE)], model="fake-main.bin.gz", human_model="fake-human.bin.gz",
        config="fake-analysis.cfg", stderr_log=tmp_path / "stderr.log", query_timeout=60.0,
    )  # fmt: skip
    try:
        path, splits_path, _ = c2_run(tmp_path, monkeypatch, engine, C2__min_admissible=1)
    finally:
        engine.close()
    out = tmp_path / "results" / "relevance_baselines.json"
    args = ["--fake", "--source", str(path), "--splits", str(splits_path), "--out", str(out), "--concurrency", "2"]
    assert rb.main(args) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["engine"]["ran"] and data["engine"]["errors"] == 0 and data["hardware"]["katago_backend"] == "fake"
    assert data["katago_version"] == "1.16.4-fake" and data["rates"]["lesson"][0][3]["source"] == "recomputed"
    assert all(v is not None for v in data["engine"]["agreement"]["own"])
    assert "## Reproduction check" in (out.with_suffix(".md")).read_text(encoding="utf-8")
