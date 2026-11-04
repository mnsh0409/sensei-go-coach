"""T4: the C1 harness (eval/openings.py, eval/c1_strength.py, C1 in eval/run.py).

Unit tests use a scripted in-process engine; the end-to-end tests play real games through KataGoEngine against
tests/fake_katago.py (synthetic numbers, so the scores mean nothing; the plumbing and the contract are tested).
"""

from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path

import pytest
import yaml

from sensei.coords import gtp_to_rc
from sensei.interfaces import Candidate, EngineAnalysis

c1 = pytest.importorskip("eval.c1_strength")
openings_mod = pytest.importorskip("eval.openings")
run_mod = pytest.importorskip("eval.run")
from sensei.game_go import GoGame  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fake_katago.py"
PRE = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
RUNTIME = yaml.safe_load((ROOT / "configs" / "default.yaml").read_text(encoding="utf-8"))

# Points at least two lines apart from each other, so random openings never touch (no captures in the opening).
SPREAD = [
    "D4",
    "D10",
    "D16",
    "K4",
    "K10",
    "K16",
    "Q4",
    "Q10",
    "Q16",
    "G7",
    "G13",
    "N7",
    "N13",
    "C7",
    "C13",
    "R7",
    "R13",
    "G3",
    "N3",
    "G17",
    "N17",
    "K7",
    "K13",
    "G10",
    "N10",
]


def small_cfg(**changes: object) -> dict:
    cfg = copy.deepcopy(PRE)
    cfg["openings"].update(pilot_n_openings=2, n_openings=2)
    cfg["C1"].update(pilot_games_each=4, n_games=4, bootstrap_resamples=200)
    cfg["adjudication"]["max_plies"] = 30
    cfg["engine"]["max_visits"] = 40
    for dotted, value in changes.items():
        section, key = dotted.split("__")
        cfg[section][key] = value
    return cfg


def to_sgf(moves, size: int = 19, extra: str = "") -> str:
    def point(move: str) -> str:
        r, c = gtp_to_rc(move, size)
        return chr(97 + c) + chr(97 + r)

    body = "".join(f";{color}[{point(move)}]" for color, move in moves)
    return f"(;GM[1]FF[4]SZ[{size}]KM[6.5]RU[Japanese]BR[5k]WR[5k]DT[2025-01-01]{extra}{body})"


def random_opening(rng: random.Random, plies: int = 14):
    points = rng.sample(SPREAD, plies)
    return tuple(("B" if i % 2 == 0 else "W", m) for i, m in enumerate(points))


def make_data(tmp_path: Path, n_pilot: int = 2, n_conf: int = 3, seed: int = 7) -> Path:
    """A splits file plus raw SGFs: pilot games p-*, confirmatory games c-*, one topic game."""
    rng = random.Random(seed)
    raw = tmp_path / "raw"
    raw.mkdir()
    ids = {"pilot": [f"p-{i}" for i in range(n_pilot)], "conf": [f"c-{i}" for i in range(n_conf)]}
    for game_id in ids["pilot"] + ids["conf"] + ["t-0"]:
        (raw / f"{game_id}.sgf").write_text(to_sgf(random_opening(rng)), encoding="utf-8")
    splits = {
        "version": 1,
        "seed": seed,
        "source": "test",
        "raw_dir": str(raw),
        "pilot_openings": ids["pilot"],
        "confirmatory_openings": ids["conf"],
        "c2_positions": {},
        "topic_games": ["t-0"],
        "explainer_positions": [],
    }
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(splits), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------------------------ scripted engine


class ScriptedEngine:
    """In-process engine: three candidates on the first legal points; numbers from callables of the position.

    Candidate values (side to move): v, v - 0.005, v - 0.05 with visits 30, 15, 10 (all reliable) and score leads
    1.0, 0.5, -2.0. The human policy puts 0.9 on the engine's top move, so at epsilon >= 0.005 Sensei leaves it for
    the second candidate (value loss 0.005, score-lead loss 0.5).
    """

    def __init__(self, black_wr=lambda pos: 0.5, black_lead=lambda pos: 1.0, fail=lambda pos: False):
        self.game = GoGame()
        self.black_wr, self.black_lead, self.fail = black_wr, black_lead, fail
        self.alive = True
        self.calls = 0

    def _top(self, pos):
        return [m for m in self.game.legal_moves(pos) if m != "pass"][:3]

    def analyze_raw(self, pos, max_visits):
        self.calls += 1
        if self.fail(pos):
            raise RuntimeError("scripted failure")
        wb = float(self.black_wr(pos))
        v = wb if pos.to_move == "B" else 1.0 - wb
        moves = self._top(pos)
        rows = zip(moves, (0.5, 0.3, 0.2), (v, v - 0.005, v - 0.05), (30, 15, 10), (1.0, 0.5, -2.0), strict=True)
        cands = tuple(Candidate(move=m, prior=p, value=val, visits=n, score_lead=s) for m, p, val, n, s in rows)
        lead_b = float(self.black_lead(pos))
        raw = {"rootInfo": {"winrate": v, "scoreLead": lead_b if pos.to_move == "B" else -lead_b}}
        return EngineAnalysis(pos, cands, {m: p for m, p in zip(moves, (0.5, 0.3, 0.2), strict=True)}, v), raw

    def analyze(self, pos, max_visits):
        return self.analyze_raw(pos, max_visits)[0]

    def human_policy(self, pos, profile):
        self.calls += 1
        legal = self.game.legal_moves(pos)
        top = self._top(pos)[0]
        rest = (1.0 - 0.9) / (len(legal) - 1)
        return {m: (0.9 if m == top else rest) for m in legal}


OPENING = openings_mod.Opening(index=0, game="g-0", moves=random_opening(random.Random(1), 12))
OTHER = openings_mod.Opening(index=1, game="g-1", moves=random_opening(random.Random(2), 12))


def selector():
    return c1.make_selector(PRE, RUNTIME)


def test_adjudicator_needs_consecutive_decisive_plies():
    adj = c1.Adjudicator(0.97, 4)
    assert [adj.update(w) for w in (0.98, 0.99, 0.5, 0.98, 0.98, 0.98)] == [None] * 6
    assert adj.update(0.99) == "B"
    adj = c1.Adjudicator(0.97, 2)
    assert [adj.update(w) for w in (0.01, 0.99, 0.02, 0.02)] == [None, None, None, "W"]
    assert c1.Adjudicator(0.97, 1).update(0.97) is None  # "above" is strict
    with pytest.raises(ValueError):
        c1.Adjudicator(0.4, 4)


def test_pilot_rule():
    eps = [0.005, 0.01, 0.02]
    assert c1.pilot_rule({0.005: 0.45, 0.01: 0.40, 0.02: 0.30}, eps, 0.35) == (0.01, True)
    assert c1.pilot_rule({0.005: 0.45, 0.01: 0.20, 0.02: 0.35}, eps, 0.35) == (0.02, True)  # the largest qualifies
    assert c1.pilot_rule({0.005: 0.30, 0.01: 0.20, 0.02: 0.10}, eps, 0.35) == (0.005, False)


def test_make_specs_keeps_same_opening_games_adjacent():
    specs = c1.make_specs([OPENING, OTHER], [0.005, 0.02])
    assert [(s.opening.game, s.sensei_color, s.epsilon) for s in specs[:4]] == [
        ("g-0", "B", 0.005),
        ("g-0", "B", 0.02),
        ("g-0", "W", 0.005),
        ("g-0", "W", 0.02),
    ]
    assert [s.index for s in specs] == list(range(8))


def test_game_to_max_plies_scored_by_final_score_lead():
    cfg = small_cfg(adjudication__max_plies=18)
    engine = ScriptedEngine(black_wr=lambda pos: 0.5, black_lead=lambda pos: 2.5)
    spec = c1.GameSpec(0, OPENING, "B", 0.01)
    rec = c1.play_game(spec, engine, cfg, selector(), "rank_10k", None)
    assert rec["reason"] == "max_plies" and rec["plies"] == 18
    assert rec["winner"] == "B" and rec["sensei_points"] == 1.0 and rec["final_black_score_lead"] == 2.5
    assert rec["sensei_moves"] == 3 and rec["left_engine_top"] == 3
    assert rec["value_loss_sum"] == pytest.approx(0.015) and rec["score_lead_loss_sum"] == pytest.approx(1.5)
    assert rec["admissible_sizes"] == {"2": 3}
    assert rec["engine_queries"] == 6 + 3 + 1  # six move analyses, three human policies, one final analysis
    lost = c1.play_game(c1.GameSpec(1, OPENING, "W", 0.01), engine, cfg, selector(), "rank_10k", None)
    assert lost["winner"] == "B" and lost["sensei_points"] == 0.0
    draw = c1.play_game(spec, ScriptedEngine(black_lead=lambda pos: 0.0), cfg, selector(), "rank_10k", None)
    assert draw["winner"] is None and draw["sensei_points"] == 0.5


def test_decisive_adjudication_and_epsilon_zero():
    engine = ScriptedEngine(black_wr=lambda pos: 0.99)
    rec = c1.play_game(c1.GameSpec(0, OPENING, "W", 0.0), engine, small_cfg(), selector(), "rank_10k", None)
    assert rec["reason"] == "decisive" and rec["winner"] == "B" and rec["sensei_points"] == 0.0
    assert rec["plies"] == 12 + 3  # four readings (plies 12 to 15), three moves played
    assert rec["final_black_score_lead"] is None and rec["final_black_winrate"] == pytest.approx(0.99)
    assert rec["left_engine_top"] == 0 and rec["value_loss_sum"] == 0.0  # epsilon 0 admits only the best move


def test_run_games_excludes_a_failing_game_and_stops_when_the_engine_dies():
    first = OTHER.moves[0][1]
    engine = ScriptedEngine(fail=lambda pos: pos.moves[0][1] == first and len(pos.moves) == 14)
    specs = c1.make_specs([OPENING, OTHER], [0.01])
    records = c1.run_games(specs, engine, small_cfg(), selector(), "rank_10k", seed=1, concurrency=3)
    assert [r["index"] for r in records] == [0, 1, 2, 3]
    assert [r["error"] is None for r in records] == [True, True, False, False]
    assert "scripted failure" in records[2]["error"]
    summary = c1.summarize(records, small_cfg()["C1"], seed=1)
    assert summary["n_games"] == 2 and summary["n_excluded"] == 2 and summary["n_openings"] == 1

    def dying(pos):
        if len(pos.moves) == 13:
            engine.alive = False
            return True
        return False

    engine = ScriptedEngine(fail=dying)
    with pytest.raises(c1.EngineDied):
        c1.run_games(specs, engine, small_cfg(), selector(), "rank_10k", seed=1, concurrency=2)


def test_summarize_pools_by_opening():
    base = {
        "error": None,
        "sensei_moves": 10,
        "value_loss_sum": 0.05,
        "score_lead_loss_sum": 2.0,
        "score_lead_loss_n": 10,
        "admissible_sizes": {"1": 4, "3": 6},
        "position_classes": {"tactical": 10},
        "left_engine_top": 5,
        "reason": "max_plies",
        "plies": 100,
        "engine_queries": 150,
    }
    records = []
    for opening in range(20):
        for color, pts in (("B", 1.0), ("W", 0.0 if opening % 2 else 0.5)):
            records.append({**base, "opening_index": opening, "sensei_color": color, "sensei_points": pts})
    s = c1.summarize(records, {"bootstrap_resamples": 500, "ci": 0.95}, seed=3)
    assert s["score"] == pytest.approx(25 / 40) and s["elo_cost"] < 0  # Sensei scored above 0.5: negative cost
    assert s["elo_cost_ci"][0] < s["elo_cost"] < s["elo_cost_ci"][1]
    assert s["score_as_black"] == 1.0 and s["score_as_white"] == 0.25
    assert (s["wins"], s["draws"], s["losses"]) == (20, 10, 10)
    assert s["mean_winrate_loss_per_move"] == pytest.approx(0.005) and s["left_engine_top_rate"] == 0.5
    assert s["admissible_size_hist"] == {"1": 160, "3": 240} and s["reasons"]["max_plies"] == 40


def test_find_epsilon_star_prefers_the_latest_clean_pilot(tmp_path):
    def pilot(name, eps, dirty, commit="abc1234"):
        data = {
            "claim": "C1",
            "pilot": True,
            "git_dirty": dirty,
            "git_commit": commit,
            "details": {"epsilon_star": eps},
        }
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")

    eps = [0.005, 0.01, 0.02]
    with pytest.raises(FileNotFoundError, match="run the pilot first"):
        c1.find_epsilon_star(tmp_path, eps)
    pilot("pilot_C1_20261030-100000.json", 0.01, dirty=True)
    assert c1.find_epsilon_star(tmp_path, eps)[0::2] == (0.01, False)
    pilot("pilot_C1_20261029-100000.json", 0.02, dirty=False)
    pilot("pilot_C1_20261031-100000.json", 0.005, dirty=False, commit="no-git")  # git-less counts as dirty
    value, path, clean = c1.find_epsilon_star(tmp_path, eps)
    assert (value, path.name, clean) == (0.02, "pilot_C1_20261029-100000.json", True)
    pilot("pilot_C1_20261101-100000.json", 0.03, dirty=False)
    with pytest.raises(ValueError, match="not in C1.pilot_epsilons"):
        c1.find_epsilon_star(tmp_path, eps)


def test_load_openings(tmp_path):
    splits = make_data(tmp_path, n_pilot=2, n_conf=3)
    cfg = small_cfg()
    ops, info = openings_mod.load_openings(cfg, "confirmatory", splits_path=splits)
    assert [o.game for o in ops] == ["c-0", "c-1"] and info["available"] == 3 and info["used"] == 2
    assert all(len(o.moves) == 12 for o in ops) and info["splits_sha256"]
    with pytest.raises(ValueError, match="needs 5"):
        openings_mod.load_openings(small_cfg(openings__n_openings=5), "confirmatory", splits_path=splits)
    raw = tmp_path / "raw"
    (raw / "p-1.sgf").write_text(to_sgf(OPENING.moves, extra="HA[2]AB[dd][pp]"), encoding="utf-8")
    with pytest.raises(ValueError, match="handicap"):
        openings_mod.load_openings(cfg, "pilot", splits_path=splits)
    bad = list(OPENING.moves)
    bad[5] = ("W", bad[0][1])  # White plays on Black's first stone
    (raw / "p-1.sgf").write_text(to_sgf(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="illegal"):
        openings_mod.load_openings(cfg, "pilot", splits_path=splits)


# ------------------------------------------------------------------------------------------------ fake KataGo


@pytest.fixture(scope="module")
def fake_engine(tmp_path_factory):
    from sensei.engine_katago import KataGoEngine

    log = tmp_path_factory.mktemp("katago") / "stderr.log"
    engine = KataGoEngine(
        exe=sys.executable,
        exe_args=[str(FAKE)],
        model="fake-main.bin.gz",
        human_model="fake-human.bin.gz",
        config="fake-analysis.cfg",
        stderr_log=log,
        query_timeout=60.0,
    )
    yield engine
    engine.close()


def strip_timing(records):
    return [{k: v for k, v in r.items() if k != "seconds"} for r in records]


def test_concurrency_does_not_change_the_results(tmp_path, fake_engine):
    splits = make_data(tmp_path)
    ops, _ = openings_mod.load_openings(small_cfg(), "pilot", splits_path=splits)
    specs = c1.make_specs(ops, [0.02])
    serial = c1.run_games(specs, fake_engine, small_cfg(), selector(), "rank_10k", seed=5, concurrency=1)
    parallel = c1.run_games(specs, fake_engine, small_cfg(), selector(), "rank_10k", seed=5, concurrency=4)
    assert strip_timing(serial) == strip_timing(parallel)
    assert all(r["error"] is None for r in serial)


def test_pilot_then_confirmatory_through_eval_run(tmp_path, fake_engine, monkeypatch):
    splits = make_data(tmp_path)
    cfg_path = tmp_path / "prereg_small.yaml"
    cfg_path.write_text(yaml.safe_dump(small_cfg()), encoding="utf-8")
    results = tmp_path / "results"
    games_log = tmp_path / "logs" / "games.jsonl"
    progress_lines = []

    def runner(cfg, *, seed, pilot, concurrency, results_dir):
        return c1.run_c1(
            cfg,
            seed=seed,
            pilot=pilot,
            concurrency=concurrency,
            engine=fake_engine,
            runtime=RUNTIME,
            results_dir=results_dir,
            splits_path=splits,
            games_log=games_log,
            progress=lambda rec, done, total, elapsed: progress_lines.append((done, total)),
        )

    monkeypatch.setitem(run_mod.CLAIMS, "C1", runner)
    pilot_path = run_mod.run("C1", cfg_path, pilot=True, concurrency=4, results_dir=results)
    pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
    assert pilot_path.name.startswith("pilot_C1_") and pilot["pilot"] is True and pilot["success"] is None
    assert pilot["n"] == 12 and pilot["engine_queries"] > 12 and pilot["concurrency"] == 4
    details = pilot["details"]
    assert set(details["per_epsilon"]) == {"0.005", "0.01", "0.02"}
    assert details["epsilon_star"] == pilot["epsilon"] and details["epsilon_star"] in (0.005, 0.01, 0.02)
    assert [g["index"] for g in details["games"]] == list(range(12))
    assert details["learner_profile"] == "rank_10k" and details["openings"]["used"] == 2
    assert not any(d["field"] == "C1.learner_profile" for d in pilot["deviations"])  # fixed in the prereg
    assert pilot["katago_version"] == "1.16.4-fake" and pilot["nets"]["main"]["sha256"] is None
    assert {"host", "gpu", "katago_backend"} <= set(pilot["hardware"])
    assert progress_lines[-1] == (12, 12) and len(games_log.read_text(encoding="utf-8").splitlines()) == 12

    conf_path = run_mod.run("C1", cfg_path, pilot=False, concurrency=4, results_dir=results)
    conf = json.loads(conf_path.read_text(encoding="utf-8"))
    assert conf["epsilon"] == details["epsilon_star"] and conf["n"] == 4
    assert isinstance(conf["success"], bool) and len(conf["ci"]) == 2
    # clean means the pilot file records a clean git tree: false on a dev machine, true on a fresh CI checkout
    assert conf["details"]["epsilon_star_source"] == {"file": pilot_path.name, "clean": not pilot["git_dirty"]}
    dirty_note = any(d["field"] == "epsilon" and "dirty" in d["reason"] for d in conf["deviations"])
    assert dirty_note == pilot["git_dirty"]  # a pilot from a dirty tree is logged as a deviation
    assert conf["details"]["summary"]["n_games"] == 4
    summary = (results / "SUMMARY.md").read_text(encoding="utf-8")
    assert "| C1 | yes | 12 |" in summary and "| C1 | no | 4 |" in summary

    with_profile = small_cfg()
    with_profile["C1"]["learner_profile"] = "rank_7k"
    cfg_path.write_text(yaml.safe_dump(with_profile), encoding="utf-8")
    again = json.loads(run_mod.run("C1", cfg_path, pilot=True, concurrency=4, results_dir=results).read_text())
    assert again["details"]["learner_profile"] == "rank_7k"
    assert not any(d["field"] == "C1.learner_profile" for d in again["deviations"])


def test_epsilon_for_c2_prefers_the_confirmatory_c1(tmp_path):
    eps = [0.005, 0.01, 0.02]

    def write(name, **data):
        base = {"claim": "C1", "git_dirty": False, "git_commit": "abc1234"}
        (tmp_path / name).write_text(json.dumps({**base, **data}), encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="no C1 result"):
        c1.epsilon_for_c2(tmp_path, eps)
    write("pilot_C1_20261030-100000.json", pilot=True, details={"epsilon_star": 0.01})
    value, source, devs = c1.epsilon_for_c2(tmp_path, eps)
    assert (value, source["kind"]) == (0.01, "C1 pilot") and "no C1 confirmatory" in devs[0]["reason"]
    write("C1_20261031-100000.json", pilot=False, epsilon=0.01)
    value, source, devs = c1.epsilon_for_c2(tmp_path, eps)
    expected = {"file": "C1_20261031-100000.json", "kind": "C1 confirmatory", "clean": True}
    assert (value, source, devs) == (0.01, expected, [])
    write("pilot_C1_20261101-100000.json", pilot=True, details={"epsilon_star": 0.02})  # a later pilot disagrees
    value, _, devs = c1.epsilon_for_c2(tmp_path, eps)
    assert value == 0.01 and len(devs) == 1 and "gives epsilon* = 0.02" in devs[0]["reason"]


def test_a_prereg_without_learner_profile_falls_back_with_a_deviation():
    cfg = copy.deepcopy(PRE)
    cfg["C1"].pop("learner_profile", None)
    profile, deviations = c1.learner_profile_of(cfg)
    assert profile == "rank_10k" and deviations[0]["field"] == "C1.learner_profile"
    assert c1.learner_profile_of(PRE) == ("rank_10k", [])
