"""Engine latency and throughput on fixed positions, per machine and KataGo backend (TASKS.md T2, poster T10b).

    python scripts/bench_engine.py --backend cuda                       # real KataGo from .env
    python scripts/bench_engine.py --backend cuda --concurrency 1 4 8 --positions 50 --visits 200
    python scripts/bench_engine.py --backend trt --build-label trt10.9-cuda12.8 --repeats 5
    python scripts/bench_engine.py --fake --positions 6 --visits 50     # offline, against tests/fake_katago.py

Writes results/latency_<host>_<backend>.json with the hardware block from eval/README.md and, per concurrency level:
analyze p50/p95/mean latency (ms), human_policy p50/p95/mean latency (ms), visits per second and queries per second.
The positions are random legal games from a fixed seed, so every machine and backend measures the same work.
KataGo's neural-net cache is cleared before each level so that later levels do not reuse earlier evaluations.

Every level is measured `--repeats` times (default 5, levels interleaved: 1, 4, 8, 1, 4, 8, ...). Visits per second
and queries per second are the means over the repeats, with the per-run values and a 95% t-interval
(`visits_per_s_ci95`); latency percentiles pool the samples of all repeats. `katago_build` records the KataGo
binary's SHA-256, its `version` output and the human `--build-label` (for example cuda12.8-cudnn9.8), so a
comparison across machines can show which build each one ran.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import random
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "src", ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from eval.hardware import analysis_settings, hardware_block, sha256_file  # noqa: E402
from eval.run import git_info  # noqa: E402
from sensei.engine_katago import KataGoEngine  # noqa: E402
from sensei.game_go import GoGame  # noqa: E402
from sensei.interfaces import Position  # noqa: E402

SEED = 20261027
HUMAN_PROFILE = "rank_10k"
FAKE_ENGINE = ROOT / "tests" / "fake_katago.py"


def fixed_positions(n: int, seed: int = SEED, size: int = 19) -> list[Position]:
    """n positions from random legal games, 10 to 160 moves long, identical on every machine for a given seed."""
    rng = random.Random(seed)
    game = GoGame()
    out: list[Position] = []
    for i in range(n):
        target = 10 + (150 * i) // max(n - 1, 1)
        pos = game.initial_position(size=size, komi=7.5, rules="chinese")
        while len(pos.moves) < target:
            legal = [m for m in game.legal_moves(pos) if m != "pass"]
            if not legal:
                break
            pos = game.play(pos, rng.choice(legal))
        out.append(pos)
    return out


def _summary_ms(samples: Sequence[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, round(0.95 * (len(ordered) - 1))))
    return {
        "p50_ms": round(statistics.median(ordered) * 1e3, 2),
        "p95_ms": round(ordered[p95_index] * 1e3, 2),
        "mean_ms": round(statistics.fmean(ordered) * 1e3, 2),
    }


def _timed_map(
    fn: Callable[[Position], Any], positions: Sequence[Position], concurrency: int
) -> tuple[list, list, float]:
    """Run fn over positions with `concurrency` threads; return (results, per-call seconds, wall seconds)."""

    def one(pos: Position) -> tuple[Any, float]:
        t0 = time.perf_counter()
        result = fn(pos)
        return result, time.perf_counter() - t0

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        pairs = list(pool.map(one, positions))
    wall = time.perf_counter() - t0
    return [p[0] for p in pairs], [p[1] for p in pairs], wall


def _root_visits(engine: Any, pos: Position, visits: int) -> int:
    """Visits actually spent at the root (rootInfo.visits when the wrapper exposes the raw response)."""
    if hasattr(engine, "analyze_raw"):
        _, raw = engine.analyze_raw(pos, visits)
        return int(raw.get("rootInfo", {}).get("visits", visits))
    analysis = engine.analyze(pos, visits)
    return sum(c.visits for c in analysis.candidates) + 1


def bench_level(engine: Any, positions: Sequence[Position], visits: int, concurrency: int) -> dict[str, Any]:
    if hasattr(engine, "clear_cache"):
        engine.clear_cache()
    root_visits: list[int] = []

    def analyze(pos: Position) -> int:
        n = _root_visits(engine, pos, visits)
        root_visits.append(n)
        return n

    _, analyze_s, analyze_wall = _timed_map(analyze, positions, concurrency)
    if hasattr(engine, "clear_cache"):
        engine.clear_cache()
    _, human_s, human_wall = _timed_map(lambda p: engine.human_policy(p, HUMAN_PROFILE), positions, concurrency)
    return {
        "concurrency": concurrency,
        "analyze": _summary_ms(analyze_s),
        "human_policy": _summary_ms(human_s),
        "visits_per_s": round(sum(root_visits) / analyze_wall, 1),
        "queries_per_s": round(len(positions) / analyze_wall, 2),
        "human_policy_queries_per_s": round(len(positions) / human_wall, 2),
        "wall_s": round(analyze_wall + human_wall, 3),
        "_analyze_s": analyze_s,
        "_human_s": human_s,
    }


def t_interval(values: Sequence[float], level: float = 0.95) -> list[float] | None:
    """Two-sided t-interval for the mean of the repeats; None with fewer than two."""
    if len(values) < 2:
        return None
    from scipy import stats

    mean = statistics.fmean(values)
    half = float(stats.t.ppf(0.5 + level / 2, len(values) - 1)) * statistics.stdev(values) / math.sqrt(len(values))
    return [round(mean - half, 1), round(mean + half, 1)]


def combine_runs(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One level's repeats: means with per-run values and 95% CIs; latency percentiles over the pooled samples."""
    vps = [r["visits_per_s"] for r in runs]
    p50 = [r["analyze"]["p50_ms"] for r in runs]
    analyze = _summary_ms([x for r in runs for x in r["_analyze_s"]])
    analyze["p50_ms_runs"] = p50
    analyze["p50_ms_ci95"] = t_interval(p50)
    return {
        "concurrency": runs[0]["concurrency"],
        "repeats": len(runs),
        "analyze": analyze,
        "human_policy": _summary_ms([x for r in runs for x in r["_human_s"]]),
        "visits_per_s": round(statistics.fmean(vps), 1),
        "visits_per_s_runs": vps,
        "visits_per_s_ci95": t_interval(vps),
        "queries_per_s": round(statistics.fmean(r["queries_per_s"] for r in runs), 2),
        "human_policy_queries_per_s": round(statistics.fmean(r["human_policy_queries_per_s"] for r in runs), 2),
        "wall_s": round(sum(r["wall_s"] for r in runs), 3),
    }


def katago_build(exe: str, label: str | None) -> dict[str, Any]:
    """Which KataGo binary ran: SHA-256 of the real executable (inside an unpacked AppImage, usr/bin/katago),
    the first lines of `katago version`, and the human label of the release build."""
    path = Path(exe)
    binary = path.parent / "usr" / "bin" / "katago" if path.name == "AppRun" else path
    lines: list[str] | None
    try:
        out = subprocess.run(
            [str(path), "version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        lines = [line.strip() for line in (out.stdout or "").splitlines() if line.strip()][:8]
    except (OSError, ValueError, subprocess.SubprocessError):
        lines = None
    return {
        "label": label,
        "binary": binary.name,
        "sha256": sha256_file(binary) if binary.is_file() else None,
        "version_text": lines,
    }


def make_engine(fake: bool) -> KataGoEngine:
    if fake:
        return KataGoEngine(
            exe=sys.executable,
            exe_args=[str(FAKE_ENGINE)],
            model="fake-main.bin.gz",
            human_model="fake-human.bin.gz",
            config="fake-analysis.cfg",
            query_timeout=120.0,
        )
    return KataGoEngine.from_env()


def run(args: argparse.Namespace) -> Path:
    backend = "fake" if args.fake else args.backend
    host = re.sub(r"[^A-Za-z0-9_-]+", "-", args.host or platform.node() or "host").strip("-") or "host"
    positions = fixed_positions(args.positions, args.seed)
    started = datetime.now().astimezone()
    engine = make_engine(args.fake)
    try:
        version = engine.version()
        engine.analyze(positions[0], min(args.visits, 10))  # warm-up: loads the nets onto the GPU
        runs: dict[str, list[dict[str, Any]]] = {str(c): [] for c in args.concurrency}
        for repeat in range(args.repeats):
            for c in args.concurrency:
                runs[str(c)].append(bench_level(engine, positions, args.visits, c))
            print(f"repeat {repeat + 1}/{args.repeats} done", flush=True)
        levels = {c: combine_runs(rows) for c, rows in runs.items()}
    finally:
        engine.close()
    commit, dirty = git_info(ROOT, ignore_untracked=("results/latency_",))
    result = {
        "kind": "engine_benchmark",
        "git_commit": commit,
        "git_dirty": dirty,
        "katago_version": version,
        "nets": None
        if args.fake
        else {
            "main": {"file": Path(engine.model).name, "sha256": sha256_file(engine.model)},
            "human": {"file": Path(engine.human_model or "").name, "sha256": sha256_file(engine.human_model)},
        },
        "analysis_config": None
        if args.fake
        else {
            "file": Path(engine.config).name,
            "sha256": sha256_file(engine.config),
            **analysis_settings(engine.config),
            "query_overrides": {k: dict(v) for k, v in engine.QUERY_OVERRIDES.items()},
        },
        "hardware": hardware_block(backend),
        "katago_build": None if args.fake else katago_build(engine.exe, args.build_label),
        "repeats": args.repeats,
        "positions": args.positions,
        "positions_seed": args.seed,
        "visits": args.visits,
        "human_profile": HUMAN_PROFILE,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        "levels": levels,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"latency_{host}_{backend}.json"
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--positions", type=int, default=50)
    ap.add_argument("--visits", type=int, default=200)
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    ap.add_argument("--backend", default="cuda", help="label for the KataGo build: cuda, trt (TensorRT), opencl, eigen")
    ap.add_argument("--fake", action="store_true", help="benchmark tests/fake_katago.py instead of KataGo")
    ap.add_argument("--host", default=None, help="host label in the file name (default: the machine name)")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--repeats", type=int, default=5, help="measurements per concurrency level (default 5)")
    ap.add_argument(
        "--build-label",
        default=None,
        help="which KataGo release build, for example cuda12.8-cudnn9.8 or trt10.9-cuda12.8",
    )
    ap.add_argument("--out", default=str(ROOT / "results"))
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.positions < 1 or args.repeats < 1 or any(c < 1 for c in args.concurrency):
        raise SystemExit("--positions, --repeats and every --concurrency value must be at least 1")
    path = run(args)
    data = json.loads(path.read_text(encoding="utf-8"))
    for level, row in data["levels"].items():
        ci = row.get("visits_per_s_ci95")
        ci_text = f" (95% CI {ci[0]:.0f} to {ci[1]:.0f})" if ci else ""
        print(
            f"concurrency {level:>2}: analyze p50 {row['analyze']['p50_ms']:8.1f} ms,"
            f" p95 {row['analyze']['p95_ms']:8.1f} ms; {row['visits_per_s']:10.1f} visits/s{ci_text};"
            f" human_policy p50 {row['human_policy']['p50_ms']:7.1f} ms"
        )
    best = max(data["levels"].values(), key=lambda row: row["visits_per_s"])
    pre = yaml.safe_load((ROOT / "configs" / "prereg.yaml").read_text(encoding="utf-8"))
    for line in c1_estimate(best["visits_per_s"], int(best["concurrency"]), int(pre["engine"]["max_visits"])):
        print(line)
    print(f"wrote {path}")
    return 0


C1_QUERIES_PER_GAME = 250
"""About 250 searches per C1 game (TASKS.md T0); the 1-visit human-policy queries are negligible."""


def c1_estimate(visits_per_s: float, concurrency: int, visits: int) -> list[str]:
    """Rough C1 wall clock at the best measured throughput, for the T0 decision on openings.n_openings."""
    if visits_per_s <= 0:
        return []
    hours = lambda games: games * C1_QUERIES_PER_GAME * visits / visits_per_s / 3600  # noqa: E731
    return [
        f"C1 estimate at {visits_per_s:.0f} visits/s (concurrency {concurrency}, {visits} visits per C1 search): "
        f"pilot 120 games {hours(120):.1f} h, confirmatory 400 games {hours(400):.1f} h, "
        f"200 games {hours(200):.1f} h"
    ]


if __name__ == "__main__":
    sys.exit(main())
