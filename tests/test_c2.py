"""T8: the C2 harness (eval/c2_relevance.py, C2 in eval/run.py).

The statistics are checked on hand-made records; candidate selection on synthetic games with a scripted engine;
the whole claim end to end through eval.run against tests/fake_katago.py (synthetic numbers: plumbing only).
"""

from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path

import pytest
import yaml

c2 = pytest.importorskip("eval.c2_relevance")
run_mod = pytest.importorskip("eval.run")
from tests.test_c1 import PRE, RUNTIME, ScriptedEngine, to_sgf  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_katago.py"
BAND_RANKS = {"15k-10k": "12k", "9k-5k": "7k", "4k-1k": "2k", "1d-3d": "2d"}
# Points three lines apart: stones never touch, so no game has a capture (the fake engine does not capture).
GRID = [f"{col}{row}" for col in "BEHLORT" for row in (2, 5, 8, 11, 14, 17)]


def small_cfg(**changes: object) -> dict:
    cfg = copy.deepcopy(PRE)
    cfg["C2"].update(positions_per_band=6, max_positions_per_game=2, ply_min=12, ply_max=30, bootstrap_resamples=300)
    for dotted, value in changes.items():
        section, key = dotted.split("__")
        cfg[section][key] = value
    return cfg


def game_sgf(rng: random.Random, rank: str, date: str = "2025-03-01", n: int = 32, black_rank: str | None = None):
    points = rng.sample(GRID, n)
    moves = [("B" if i % 2 == 0 else "W", m) for i, m in enumerate(points)]
    sgf = to_sgf(moves)
    return sgf.replace("BR[5k]WR[5k]", f"BR[{black_rank or rank}]WR[{rank}]").replace("DT[2025-01-01]", f"DT[{date}]")


def make_data(tmp_path: Path, games_per_band: int = 5, seed: int = 3) -> tuple[Path, dict]:
    rng = random.Random(seed)
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    c2_positions: dict[str, list] = {}
    for band, rank in BAND_RANKS.items():
        items = []
        for g in range(games_per_band):
            game_id = f"{band}-{g}"
            (raw / f"{game_id}.sgf").write_text(game_sgf(rng, rank), encoding="utf-8")
            items += [{"game": game_id, "ply": ply} for ply in (12, 15, 18, 21)]
        rng.shuffle(items)
        c2_positions[band] = items
    splits = {
        "version": 1,
        "seed": seed,
        "source": "test",
        "raw_dir": str(raw),
        "pilot_openings": [],
        "confirmatory_openings": [],
        "c2_positions": c2_positions,
        "topic_games": [],
        "explainer_positions": [],
    }
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(splits), encoding="utf-8")
    return path, splits


def write_pilot(results: Path, eps: float = 0.02) -> None:
    results.mkdir(parents=True, exist_ok=True)
    data = {"claim": "C1", "pilot": True, "git_dirty": True, "git_commit": "no-git", "details": {"epsilon_star": eps}}
    (results / "pilot_C1_20261030-120000.json").write_text(json.dumps(data), encoding="utf-8")


def rec(band, game, played, s_own, s_lower=None, best="Z", rand="Y", n_adm=2, loss=0.01):
    return {"band": band, "game": game, "played": played, "s_own": s_own, "s_lower": s_lower, "best": best,
            "rand": rand, "n_admissible": n_adm, "loss_own": loss}  # fmt: skip


def test_parse_bands():
    bands = c2.parse_bands(PRE)
    assert [b.name for b in bands] == ["15k-10k", "9k-5k", "4k-1k", "1d-3d"]
    assert bands[0].contains("12k") and bands[0].contains("15k") and not bands[0].contains("9k")
    assert bands[3].contains("1d") and bands[3].contains("3d") and not bands[3].contains("1k")
    bad = copy.deepcopy(PRE)
    bad["C2"]["bands"] = list(reversed(bad["C2"]["bands"]))
    with pytest.raises(ValueError, match="ordered"):
        c2.parse_bands(bad)


def test_uplift_point_estimates_and_joint_bootstrap():
    # Band 0: played = s_own in 2 of 10 positions. Band 1: played = s_lower (band 0's lesson) in 5 of 10 (i < 5),
    # played = s_own in 5 of 10 (i >= 5). Band 2: played = s_lower in 9 of 10.
    weak = [rec(0, f"a{i % 5}", "X" if i < 2 else "Q", "X") for i in range(10)]
    mid = [rec(1, f"b{i % 5}", "L" if i < 5 else "M", "M" if i >= 4 else "N", s_lower="L") for i in range(10)]
    strong = [rec(2, f"c{i % 5}", "K" if i < 9 else "J", "J", s_lower="K") for i in range(10)]
    up = c2.uplift([weak, mid, strong], n_resamples=2000, ci=0.95, seed=1)
    assert up["per_k"][0]["estimate"] == pytest.approx(0.5 - 0.2)
    assert up["per_k"][1]["estimate"] == pytest.approx(0.9 - 0.5)
    assert up["estimate"] == pytest.approx(0.35)
    assert up["own_rate"] == pytest.approx([0.2, 0.5, 0.1]) and up["lower_rate"][1:] == pytest.approx([0.5, 0.9])
    assert up["ci"][0] <= up["estimate"] <= up["ci"][1]
    assert up == c2.uplift([weak, mid, strong], n_resamples=2000, ci=0.95, seed=1)  # deterministic
    with pytest.raises(ValueError):
        c2.uplift([weak], n_resamples=10, ci=0.95, seed=1)


def test_uplift_ci_is_wide_when_one_game_drives_the_effect():
    # All of band 1's matches come from one game: the game-clustered CI must reach down to 0.
    weak = [rec(0, f"a{i}", "Q", "X") for i in range(20)]
    mid = [rec(1, "b0" if i < 4 else f"b{i}", "L" if i < 4 else "M", "N", s_lower="L") for i in range(20)]
    up = c2.uplift([weak, mid], n_resamples=3000, ci=0.95, seed=2)
    assert up["estimate"] == pytest.approx(0.2) and up["ci"][0] == pytest.approx(0.0)


def test_band_summary_contrasts():
    records = [
        rec(0, f"g{i % 4}", "Z" if i < 6 else "A", "A" if i >= 8 else "B", best="Z", rand="A") for i in range(12)
    ]
    s = c2.band_summary(records, {"bootstrap_resamples": 500, "ci": 0.95}, seed=4)
    assert s["n_positions"] == 12 and s["n_games"] == 4
    assert s["p_played_best"] == pytest.approx(0.5) and s["p_played_rand"] == pytest.approx(0.5)
    assert s["p_played_s_own"] == pytest.approx(4 / 12)
    assert s["D_best"]["estimate"] == pytest.approx(0.5 - 4 / 12) and s["p_played_s_lower"] is None
    assert s["D_best"]["ci"][0] <= s["D_best"]["estimate"] <= s["D_best"]["ci"][1]


def test_collect_band_order_cap_and_skip_reasons(tmp_path):
    splits_path, splits = make_data(tmp_path)
    raw = Path(splits["raw_dir"])
    rng = random.Random(9)
    (raw / "old.sgf").write_text(game_sgf(rng, "12k", date="2023-12-31"), encoding="utf-8")
    (raw / "mixed.sgf").write_text(game_sgf(rng, "12k", black_rank="3d"), encoding="utf-8")
    cfg = small_cfg()
    band, = (b for b in c2.parse_bands(cfg) if b.index == 0)  # fmt: skip
    extra = [{"game": "old", "ply": 13}, {"game": "mixed", "ply": 12}, {"game": "mixed", "ply": 13},
             {"game": "15k-10k-0", "ply": 31}, {"game": "absent", "ply": 12}]  # fmt: skip
    cands = extra + splits["c2_positions"]["15k-10k"]
    engine = ScriptedEngine()
    selected, skipped, queries, errors = c2.collect_band(
        band, None, cands, engine=engine, cfg=cfg, selector=c2.make_selector(cfg, RUNTIME), epsilon=0.02,
        seed=1, directory=raw, concurrency=3,
    )  # fmt: skip
    assert len(selected) == 6 and not errors
    assert [r["order"] for r in selected] == sorted(r["order"] for r in selected)
    assert max(sum(1 for r in selected if r["game"] == g) for g in {r["game"] for r in selected}) <= 2
    # "mixed": Black is 3d (ply 12, Black to move) -> rank skip; ply 13 is White's 12k move -> eligible.
    assert skipped["date"] == 1 and skipped["rank"] == 1 and skipped["ply"] == 1 and skipped["missing_sgf"] == 1
    assert any(r["game"] == "mixed" and r["ply"] == 13 for r in selected)
    first = selected[0]
    assert first["n_admissible"] == 2 and first["s_lower"] is None and first["s_own"] != first["best"]
    assert first["loss_own"] == pytest.approx(0.005)
    assert queries == engine.calls  # every engine call is counted once, including the unused tail
    none, skipped0, _, _ = c2.collect_band(
        band, None, cands, engine=ScriptedEngine(), cfg=cfg, selector=c2.make_selector(cfg, RUNTIME),
        epsilon=0.0, seed=1, directory=raw, concurrency=2,
    )  # fmt: skip
    assert none == [] and skipped0["admissible"] == 21  # 20 split candidates and mixed@13


def test_bad_sgfs_and_short_games_are_skipped_not_fatal(tmp_path):
    _, splits = make_data(tmp_path)
    raw = Path(splits["raw_dir"])
    good = (raw / "15k-10k-0.sgf").read_text(encoding="utf-8")
    (raw / "truncated.sgf").write_text(good[: len(good) // 2], encoding="utf-8")
    (raw / "nokomi.sgf").write_text(good.replace("KM[6.5]", "KM[abc]"), encoding="utf-8")
    (raw / "short.sgf").write_text(game_sgf(random.Random(4), "12k", n=20), encoding="utf-8")
    cands = [{"game": "truncated", "ply": 12}, {"game": "nokomi", "ply": 12}, {"game": "short", "ply": 25},
             {"game": "15k-10k-1", "ply": 12}]  # fmt: skip
    cfg = small_cfg(C2__positions_per_band=1)
    band = c2.parse_bands(cfg)[0]
    selected, skipped, _, errors = c2.collect_band(
        band, None, cands, engine=ScriptedEngine(), cfg=cfg, selector=c2.make_selector(cfg, RUNTIME), epsilon=0.02,
        seed=1, directory=raw, concurrency=2,
    )  # fmt: skip
    assert [r["game"] for r in selected] == ["15k-10k-1"]
    assert skipped == {"unparsable_sgf": 2, "ply_beyond_game": 1} and len(errors) == 2


def test_engine_errors_are_skipped_and_counted(tmp_path):
    splits_path, splits = make_data(tmp_path)
    cfg = small_cfg()
    band = c2.parse_bands(cfg)[1]
    lower = c2.parse_bands(cfg)[0]
    engine = ScriptedEngine(fail=lambda pos: len(pos.moves) == 15)
    selected, skipped, _, errors = c2.collect_band(
        band, lower, splits["c2_positions"]["9k-5k"], engine=engine, cfg=cfg,
        selector=c2.make_selector(cfg, RUNTIME), epsilon=0.02, seed=1, directory=Path(splits["raw_dir"]),
        concurrency=2,
    )  # fmt: skip
    assert skipped["engine_error"] >= 1 and errors and "scripted failure" in errors[0]
    assert all(r["ply"] != 15 for r in selected) and all(r["s_lower"] is not None for r in selected)


@pytest.fixture(scope="module")
def fake_engine(tmp_path_factory):
    from sensei.engine_katago import KataGoEngine

    engine = KataGoEngine(
        exe=sys.executable,
        exe_args=[str(FAKE)],
        model="fake-main.bin.gz",
        human_model="fake-human.bin.gz",
        config="fake-analysis.cfg",
        stderr_log=tmp_path_factory.mktemp("katago") / "stderr.log",
        query_timeout=60.0,
    )
    yield engine
    engine.close()


def test_c2_end_to_end_through_eval_run(tmp_path, fake_engine, monkeypatch):
    splits_path, _ = make_data(tmp_path, games_per_band=8)
    cfg = small_cfg(C2__positions_per_band=4, C2__min_admissible=1)
    cfg_path = tmp_path / "prereg_small.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    results = tmp_path / "results"
    write_pilot(results, 0.02)

    def runner(cfg, *, seed, pilot, concurrency, results_dir):
        return c2.run_c2(
            cfg, seed=seed, concurrency=concurrency, engine=fake_engine, runtime=RUNTIME,
            results_dir=results_dir, splits_path=splits_path,
        )  # fmt: skip

    monkeypatch.setitem(run_mod.CLAIMS, "C2", runner)
    out = json.loads(run_mod.run("C2", cfg_path, concurrency=4, results_dir=results).read_text(encoding="utf-8"))
    assert out["claim"] == "C2" and out["epsilon"] == 0.02 and out["n"] == 16
    assert isinstance(out["success"], bool) and len(out["ci"]) == 2
    d = out["details"]
    assert [u["weaker"] for u in d["uplift"]["U_k"]] == ["15k-10k", "9k-5k", "4k-1k"]
    assert set(d["per_band"]) == set(BAND_RANKS) and all(b["n_positions"] == 4 for b in d["per_band"].values())
    assert d["per_band"]["15k-10k"]["p_played_s_lower"] is None
    assert d["per_band"]["9k-5k"]["p_played_s_lower"] is not None
    assert len(d["positions"]) == 16 and d["positions_columns"][0] == "band"
    assert d["epsilon_source"] == {"file": "pilot_C1_20261030-120000.json", "kind": "C1 pilot", "clean": False}
    assert any(x["field"] == "epsilon" and "no C1 confirmatory" in x["reason"] for x in out["deviations"])
    assert d["n_games"] == len({(row[0], row[1]) for row in d["positions"]})  # distinct games across bands
    assert out["engine_queries"] >= 16 * 2 and out["katago_version"] == "1.16.4-fake"
    assert "| C2 | no | 16 |" in (results / "SUMMARY.md").read_text(encoding="utf-8")


def test_c2_rejects_pilot_flag():
    with pytest.raises(SystemExit):
        c2.run_claim(PRE, seed=1, pilot=True, concurrency=1)
