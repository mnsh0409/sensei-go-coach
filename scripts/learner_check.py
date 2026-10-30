"""Rank estimation on held-out human games (TASKS.md T6; descriptive only, not a claim).

    python scripts/learner_check.py                    # KataGo from .env, 5 games per C2 band
    python scripts/learner_check.py --fake --games-per-band 1

For each C2 band, the first `--games-per-band` distinct games in that band's candidate list (data/splits.json,
games played after the HumanSL net's release) are replayed. The player whose rank is in the band is the learner:
HumanSLLearner.estimate runs on their moves after 20, 40 and 80 moves and on all of them, over
`learner.profile_grid`. results/learner_check.json lists the estimates against the rank in the SGF, with the mean
absolute error in ranks per checkpoint, the hardware block and the git commit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from eval.c2_relevance import parse_bands  # noqa: E402
from eval.hardware import engine_metadata, hardware_block  # noqa: E402
from eval.run import git_info  # noqa: E402
from eval.splits import DEFAULT_SPLITS, game_record, katago_rules, load_splits, raw_dir  # noqa: E402
from sensei.coords import PASS  # noqa: E402
from sensei.game_go import GoGame  # noqa: E402
from sensei.learner import HumanSLLearner  # noqa: E402
from sensei.ranks import label_to_numeric, numeric_to_label  # noqa: E402

CHECKPOINTS = (20, 40, 80)
FAKE_ENGINE = ROOT / "tests" / "fake_katago.py"


def pick_games(splits: Mapping[str, Any], band_name: str, n: int) -> list[str]:
    seen: list[str] = []
    for item in splits["c2_positions"].get(band_name, []):
        game = str(item["game"])
        if game not in seen:
            seen.append(game)
        if len(seen) == n:
            break
    return seen


def check_game(game_id: str, band: Any, directory: Path, engine: Any, pre: Mapping[str, Any]) -> dict[str, Any]:
    rec = game_record(game_id, directory)
    if rec.komi is None:
        return {"game": game_id, "band": band.name, "error": "no komi"}
    try:
        black_in = band.contains(rec.black_rank)
    except ValueError:
        black_in = False
    color = "B" if black_in else "W"
    true_label = rec.black_rank if color == "B" else rec.white_rank
    game = GoGame()
    pos = game.initial_position(size=rec.size, komi=float(rec.komi), rules=katago_rules(rec.rules))
    history = []
    try:
        for c, move in rec.moves:
            if c == color and move != PASS:
                history.append((pos, move))
            pos = game.play(pos, move)
    except ValueError as err:
        if not history:
            return {"game": game_id, "band": band.name, "error": f"replay: {err}"}
    learner = HumanSLLearner(
        engine,
        pre["learner"]["profile_grid"],
        true_label,
        int(pre["learner"]["min_moves_for_estimate"]),
        float(pre["learner"]["delta"]),
    )
    estimates = {}
    points = [(str(n), n) for n in CHECKPOINTS if n < len(history)] + [("all", len(history))]
    for key, n in points:
        est = learner.estimate(history[:n])
        estimates[key] = {
            "n_moves": est.n_moves,
            "rank": round(est.rank, 3),
            "rank_sd": round(est.rank_sd, 3),
            "label": numeric_to_label(est.rank),
        }
    return {
        "game": game_id,
        "band": band.name,
        "color": color,
        "true_rank": true_label,
        "true_numeric": label_to_numeric(true_label),
        "moves": len(history),
        "estimates": estimates,
        "error": None,
    }


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (*(str(n) for n in CHECKPOINTS), "all"):
        errs = [abs(r["estimates"][key]["rank"] - r["true_numeric"]) for r in rows if key in r.get("estimates", {})]
        out[key] = {"n_games": len(errs), "mean_abs_error_ranks": round(sum(errs) / len(errs), 3) if errs else None}
    return out


def make_engine(fake: bool) -> Any:
    from sensei.engine_katago import KataGoEngine

    if fake:
        return KataGoEngine(
            exe=sys.executable,
            exe_args=[str(FAKE_ENGINE)],
            model="fake-main.bin.gz",
            human_model="fake-human.bin.gz",
            config="fake-analysis.cfg",
            query_timeout=120.0,
        )
    return KataGoEngine.from_env(query_timeout=300.0)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games-per-band", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--fake", action="store_true", help="tests/fake_katago.py instead of KataGo")
    ap.add_argument("--splits", default=None)
    ap.add_argument("--out", default=None, help="output file (default: results/learner_check.json)")
    args = ap.parse_args(argv)
    pre = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
    splits_path = Path(args.splits) if args.splits else DEFAULT_SPLITS
    splits = load_splits(splits_path)
    directory = raw_dir(splits)
    jobs = [(g, band) for band in parse_bands(pre) for g in pick_games(splits, band.name, args.games_per_band)]
    if not jobs:
        raise SystemExit(f"{splits_path} has no C2 candidates to draw games from")
    started = datetime.now().astimezone()
    t0 = time.perf_counter()
    engine = make_engine(args.fake)
    try:
        meta = engine_metadata(engine)
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            rows = list(pool.map(lambda job: check_game(job[0], job[1], directory, engine, pre), jobs))
    finally:
        engine.close()
    commit, dirty = git_info(ROOT)
    ok = [r for r in rows if r["error"] is None]
    result = {
        "kind": "learner_check",
        "descriptive": True,
        "git_commit": commit,
        "git_dirty": dirty,
        "created": started.isoformat(timespec="seconds"),
        "wall_clock_s": round(time.perf_counter() - t0, 1),
        "hardware": hardware_block("fake" if args.fake else (os.environ.get("KATAGO_BACKEND") or None)),
        **meta,
        "profile_grid": pre["learner"]["profile_grid"],
        "checkpoints": [*CHECKPOINTS, "all"],
        "summary": summarize(ok),
        "per_band": {band.name: summarize([r for r in ok if r["band"] == band.name]) for band in parse_bands(pre)},
        "games": rows,
    }
    out = Path(args.out) if args.out else ROOT / "results" / "learner_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for key, s in result["summary"].items():
        print(f"after {key} moves: mean absolute error {s['mean_abs_error_ranks']} ranks over {s['n_games']} games")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
