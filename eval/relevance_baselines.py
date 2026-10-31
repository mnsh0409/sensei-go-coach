"""Descriptive context for C2: Sensei's lesson next to the engine's best move and a random admissible move.

Not a claim and not pre-registered: added on 2026-10-31, after C2 ran, to show how much of C2's uplift is specific to
Sensei's choice. There is no success criterion.

    python -m eval.relevance_baselines                # latest clean C2 result; KataGo from .env for the far cells
    python -m eval.relevance_baselines --no-engine    # only what the C2 file holds (adjacent bands)
    python -m eval.relevance_baselines --fake         # tests/fake_katago.py instead of KataGo

C2 compares, for each band k, how often players of band k+1 play the move Sensei would teach band k (its lesson s_k)
with how often band k itself plays it. Stronger players also play other good moves more often, so this script puts two
baselines from the same positions next to the lesson: the engine's best move b and a random admissible move r, both as
C2 defined and stored them.

From the C2 file alone (exact: C2's own positions and moves):
- P_j(played = x) for every band j and x in {s_j, s_(j-1), b, r};
- for every adjacent step k -> k+1: the difference and the ratio of the stronger band's rate to the weaker band's, for
  the lesson, b and r; the lesson's difference minus r's and b's; the lesson's ratio divided by r's and b's; and the
  lesson's numbers again on the positions where the lesson is not the engine's best move.

With the engine (the default), the lessons of every weaker band are added on every position: s_k on band j's positions
for j >= k + 2 ("stronger players", which C2 did not compute), and the same comparisons from band k to every stronger
band. Each position is re-analyzed with the settings in the C2 file (its copy of the prereg and its epsilon) and
Sensei's choice is recomputed for every band profile at or below the position's band. Cells C2 already holds (j = k and
j = k + 1) keep C2's stored moves; the recomputed moves for those cells are only compared with them
(`engine.agreement`), because KataGo's multithreaded search is not bit-for-bit repeatable.

CIs: bootstrap over games within every band, all bands resampled jointly as in C2's uplift, `C2.bootstrap_resamples`
replicates at level `C2.ci`; seed: child 3 of the C2 run's seed (C2 uses children 0 to 2). Adjacent steps are
summarized by the arithmetic mean of the differences (the lesson's is C2's U) and the geometric mean of the ratios.

Writes results/relevance_baselines.json and results/relevance_baselines.md with the git commit and the source C2 file's
name and SHA-256. The token source `C2base` (eval/tokens.py) reads the JSON only when it is clean and was computed from
the latest clean C2 result. The JSON has no `claim` field, so results/SUMMARY.md does not list it as a claim.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from eval.c1_strength import QUERY_TIMEOUT_S, RUNTIME_CONFIG, EngineDied, make_selector
from eval.c2_relevance import _choice, parse_bands  # _choice: the exact rule C2 used for s_k
from eval.hardware import engine_metadata, hardware_block, sha256_file
from eval.run import git_info
from eval.splits import DEFAULT_SPLITS, load_splits, position_at, raw_dir
from eval.tokens import DERIVED, latest_clean
from sensei.game_go import GoGame

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
OUT_NAME = DERIVED["C2base"][0]
FAKE_ENGINE = ROOT / "tests" / "fake_katago.py"
COLUMNS = ("band", "game", "ply", "played", "s_own", "s_lower", "best", "rand")
NOTE = (
    "Descriptive context for C2, added after C2 ran (2026-10-31); not pre-registered, no success criterion. "
    "Rates are shares of positions where the player in the game played that move."
)

Pair = tuple[float | None, np.ndarray]
"""A statistic: point estimate (None when undefined) and its bootstrap replicates (NaN where undefined)."""


# ------------------------------------------------------------------------------------------------ records


def records_from_c2(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One record per C2 position: band, game, ply, played, best, rand and the lessons C2 stored.

    `lesson` maps a band index k to s_k on this position (C2 stores the position's own band and the band below);
    `source` says where each lesson came from ("C2" or "recomputed").
    """
    details = result["details"]
    cols = list(details.get("positions_columns") or COLUMNS)
    missing = [c for c in COLUMNS if c not in cols]
    if missing:
        raise ValueError(f"the C2 file's positions lack the columns {missing}")
    out = []
    for i, row in enumerate(details["positions"]):
        r = dict(zip(cols, row, strict=True))
        band = int(r["band"])
        lessons: dict[int, str] = {band: str(r["s_own"])}
        if band > 0 and r["s_lower"] is not None:
            lessons[band - 1] = str(r["s_lower"])
        out.append(
            {
                "index": i,
                "band": band,
                "game": str(r["game"]),
                "ply": int(r["ply"]),
                "played": str(r["played"]),
                "best": str(r["best"]),
                "rand": str(r["rand"]),
                "lesson": lessons,
                "source": {k: "C2" for k in lessons},
            }
        )
    return out


# ------------------------------------------------------------------------------------------------ statistics


class JointBootstrap:
    """Game-clustered bootstrap within every band, all bands resampled jointly (as C2's uplift).

    Each replicate draws, for every band, as many games as the band has, with replacement. Every statistic of one
    replicate uses the same draw, so contrasts within a band keep their correlation.
    """

    def __init__(self, by_band: Sequence[Sequence[Mapping[str, Any]]], n_resamples: int, seed: int) -> None:
        rng = np.random.default_rng(seed)
        self.n_resamples = int(n_resamples)
        self.by_band = by_band
        self.index: list[dict[str, int]] = []
        self.weights: list[np.ndarray] = []
        for records in by_band:
            games = sorted({str(r["game"]) for r in records})
            self.index.append({g: i for i, g in enumerate(games)})
            n = len(games)
            if n:
                draw = rng.multinomial(n, np.full(n, 1.0 / n), size=self.n_resamples).astype(np.float64)
            else:
                draw = np.zeros((self.n_resamples, 0))
            self.weights.append(draw)

    def rate(
        self,
        band: int,
        hit: Callable[[Mapping[str, Any]], bool],
        keep: Callable[[Mapping[str, Any]], bool] = lambda r: True,
    ) -> tuple[Pair, int]:
        """(estimate, replicates) of the share of kept positions of `band` where `hit` holds, and the kept count."""
        index = self.index[band]
        hits = np.zeros(len(index))
        kept = np.zeros(len(index))
        for r in self.by_band[band]:
            if keep(r):
                g = index[str(r["game"])]
                kept[g] += 1.0
                hits[g] += 1.0 if hit(r) else 0.0
        total = int(kept.sum())
        if total == 0:
            return (None, np.full(self.n_resamples, np.nan)), 0
        w = self.weights[band]
        with np.errstate(divide="ignore", invalid="ignore"):
            reps = (w @ hits) / (w @ kept)
        return (float(hits.sum() / total), reps), total


def _op(f: Callable[..., Any], *xs: Pair) -> Pair:
    """Apply f to the estimates and, elementwise, to the replicates; non-finite estimates become None."""
    with np.errstate(all="ignore"):
        reps = np.asarray(f(*[x[1] for x in xs]), dtype=float)
        if any(x[0] is None for x in xs):
            return None, reps
        est = float(f(*[np.float64(x[0]) for x in xs]))
    return (est if math.isfinite(est) else None), reps


def _sub(a: Pair, b: Pair) -> Pair:
    return _op(lambda x, y: x - y, a, b)


def _div(a: Pair, b: Pair) -> Pair:
    return _op(lambda x, y: x / y, a, b)


def _mean(xs: Sequence[Pair]) -> Pair:
    return _op(lambda *v: sum(v) / len(v), *xs)


def _gmean(xs: Sequence[Pair]) -> Pair:
    return _op(lambda *v: np.exp(sum(np.log(x) for x in v) / len(v)), *xs)


def summarize(stat: Pair, level: float) -> dict[str, Any]:
    """{"estimate", "ci"}; the CI is None when the estimate is undefined or over 1% of the replicates are."""
    est, reps = stat
    if est is None:
        return {"estimate": None, "ci": None}
    finite = reps[np.isfinite(reps)]
    if finite.size == 0 or finite.size < 0.99 * reps.size:
        return {"estimate": est, "ci": None}
    alpha = (1.0 - level) / 2.0
    lo, hi = np.quantile(finite, [alpha, 1.0 - alpha])
    return {"estimate": est, "ci": [float(lo), float(hi)]}


def compute(
    by_band: Sequence[Sequence[Mapping[str, Any]]], n_resamples: int, level: float, seed: int
) -> dict[str, Any]:
    """Rates, every step from a band to a stronger band, and the adjacent-step summaries (see the module docstring)."""
    n_bands = len(by_band)
    boot = JointBootstrap(by_band, n_resamples, seed)

    def s(stat: Pair) -> dict[str, Any]:
        return summarize(stat, level)

    lesson: list[list[dict[str, Any] | None]] = [[None] * n_bands for _ in range(n_bands)]
    lesson_nb: list[list[dict[str, Any] | None]] = [[None] * n_bands for _ in range(n_bands)]
    lesson_is_best: list[list[float | None]] = [[None] * n_bands for _ in range(n_bands)]
    pl: dict[tuple[int, int], Pair] = {}
    pnb: dict[tuple[int, int], Pair] = {}
    for k in range(n_bands):
        for j in range(k, n_bands):
            have = [r for r in by_band[j] if k in r["lesson"]]
            if not have:
                continue
            stat, n = boot.rate(j, lambda r, k=k: r["played"] == r["lesson"][k], lambda r, k=k: k in r["lesson"])
            sources = sorted({str(r["source"][k]) for r in have})
            lesson[k][j] = {**s(stat), "n": n, "source": sources[0] if len(sources) == 1 else "mixed"}
            pl[k, j] = stat
            stat_nb, n_nb = boot.rate(
                j,
                lambda r, k=k: r["played"] == r["lesson"][k],
                lambda r, k=k: k in r["lesson"] and r["lesson"][k] != r["best"],
            )
            lesson_nb[k][j] = {**s(stat_nb), "n": n_nb}
            pnb[k, j] = stat_nb
            lesson_is_best[k][j] = sum(1 for r in have if r["lesson"][k] == r["best"]) / len(have)
    best, rand = [], []
    pb: dict[int, Pair] = {}
    pr: dict[int, Pair] = {}
    for j in range(n_bands):
        stat, n = boot.rate(j, lambda r: r["played"] == r["best"])
        best.append({**s(stat), "n": n})
        pb[j] = stat
        stat, n = boot.rate(j, lambda r: r["played"] == r["rand"])
        rand.append({**s(stat), "n": n})
        pr[j] = stat

    steps: list[dict[str, Any]] = []
    parts: dict[tuple[int, int], dict[str, Pair]] = {}
    for k in range(n_bands):
        for j in range(k + 1, n_bands):
            if (k, j) not in pl or (k, k) not in pl:
                continue
            p = {
                "L": _sub(pl[k, j], pl[k, k]),
                "Lr": _div(pl[k, j], pl[k, k]),
                "B": _sub(pb[j], pb[k]),
                "Br": _div(pb[j], pb[k]),
                "R": _sub(pr[j], pr[k]),
                "Rr": _div(pr[j], pr[k]),
                "N": _sub(pnb[k, j], pnb[k, k]),
                "Nr": _div(pnb[k, j], pnb[k, k]),
            }
            p["LmR"], p["LmB"] = _sub(p["L"], p["R"]), _sub(p["L"], p["B"])
            p["LoR"], p["LoB"] = _div(p["Lr"], p["Rr"]), _div(p["Lr"], p["Br"])
            parts[k, j] = p
            steps.append(
                {
                    "weaker": k,
                    "stronger": j,
                    "gap": j - k,
                    "lesson": {"diff": s(p["L"]), "ratio": s(p["Lr"])},
                    "best": {"diff": s(p["B"]), "ratio": s(p["Br"])},
                    "rand": {"diff": s(p["R"]), "ratio": s(p["Rr"])},
                    "lesson_minus_rand": s(p["LmR"]),
                    "lesson_minus_best": s(p["LmB"]),
                    "lesson_over_rand": s(p["LoR"]),
                    "lesson_over_best": s(p["LoB"]),
                    "lesson_not_best": {"diff": s(p["N"]), "ratio": s(p["Nr"])},
                }
            )

    adjacent: dict[str, Any] | None = None
    adj = [parts[k, k + 1] for k in range(n_bands - 1) if (k, k + 1) in parts]
    if adj and len(adj) == n_bands - 1:

        def m(key: str) -> dict[str, Any]:
            return s(_mean([p[key] for p in adj]))

        def g(key: str) -> dict[str, Any]:
            return s(_gmean([p[key] for p in adj]))

        adjacent = {
            "n_steps": len(adj),
            "lesson_diff_mean": m("L"),
            "best_diff_mean": m("B"),
            "rand_diff_mean": m("R"),
            "lesson_minus_rand_mean": m("LmR"),
            "lesson_minus_best_mean": m("LmB"),
            "lesson_ratio_gmean": g("Lr"),
            "best_ratio_gmean": g("Br"),
            "rand_ratio_gmean": g("Rr"),
            "lesson_over_rand_gmean": g("LoR"),
            "lesson_over_best_gmean": g("LoB"),
            "lesson_not_best_diff_mean": m("N"),
            "lesson_not_best_ratio_gmean": g("Nr"),
        }
    full = next((st for st in steps if st["weaker"] == 0 and st["stronger"] == n_bands - 1), None)
    own_not_best = [
        sum(1 for r in recs if r["band"] in r["lesson"] and r["lesson"][r["band"]] != r["best"]) for recs in by_band
    ]
    total = sum(len(recs) for recs in by_band)
    return {
        "rates": {"lesson": lesson, "lesson_not_best": lesson_nb, "best": best, "rand": rand},
        "lesson_is_best_share": lesson_is_best,
        "not_best_share": (sum(own_not_best) / total) if total else None,
        "not_best_share_by_band": [
            (n / len(recs)) if recs else None for n, recs in zip(own_not_best, by_band, strict=True)
        ],
        "steps": steps,
        "adjacent": adjacent,
        "full_range": full,
    }


# ------------------------------------------------------------------------------------------------ the engine part


def recompute_lessons(
    records: Sequence[dict[str, Any]],
    *,
    engine: Any,
    cfg: Mapping[str, Any],
    runtime: Mapping[str, Any],
    epsilon: float,
    directory: Path,
    concurrency: int,
    seed: int,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Re-analyze every position; add s_k for every band k <= band - 2 to the records (in place).

    Returns the `engine` block: queries, errors, and how often the recomputed own lesson, lower lesson and best move
    equal C2's stored ones, per band.
    """
    bands = parse_bands(cfg)
    selector = make_selector(cfg, runtime)
    temperature = float(cfg["selector"]["temperature_eval"])
    visits = int(cfg["engine"]["max_visits"])
    game = GoGame()

    def one(r: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"queries": 0, "error": None, "best": None, "n_admissible": None, "lessons": {}}
        where = f"{r['game']} ply {r['ply']}"
        try:
            hp = position_at(str(r["game"]), int(r["ply"]), directory)
        except (OSError, ValueError) as err:
            out["error"] = f"{where}: {type(err).__name__}: {err}"
            return out
        if str(hp.played) != str(r["played"]):
            out["error"] = f"{where}: the game record has {hp.played}, the C2 file {r['played']}"
            return out
        try:
            legal = game.legal_moves(hp.position)
            analysis = engine.analyze(hp.position, visits)
            out["queries"] = 1
            if not analysis.candidates:
                out["error"] = f"{where}: no candidates"
                return out
            out["n_admissible"] = len(selector.admissible(analysis, epsilon))
            out["best"] = max(selector.reliable(analysis), key=lambda c: (float(c.value), int(c.visits))).move
            rng = np.random.default_rng([seed, int(r["band"]), int(r["index"])])
            for k in range(int(r["band"]) + 1):
                out["lessons"][k] = _choice(
                    analysis, selector, engine, hp.position, legal, bands[k].profile, epsilon, temperature, rng, cfg
                )
                out["queries"] += 1
        except Exception as err:  # noqa: BLE001 - a position KataGo rejects is counted, not fatal
            if not getattr(engine, "alive", True):
                raise EngineDied(f"KataGo exited at {where}: {err}") from err
            out["error"] = f"{where}: {type(err).__name__}: {err}"
        return out

    t0 = time.perf_counter()
    outs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix="c2base") as pool:
        for i, o in enumerate(pool.map(one, records), start=1):  # results come back in record order
            outs.append(o)
            if progress is not None and (i % 100 == 0 or i == len(records)):
                rate = i / max(1e-9, time.perf_counter() - t0)
                progress(
                    f"relevance baselines: {i}/{len(records)} positions, "
                    f"about {(len(records) - i) / rate / 60:.1f} min left"
                )

    n_bands = len(bands)
    agree = {key: [[0, 0] for _ in range(n_bands)] for key in ("own", "lower", "best")}
    own_hits = [[0, 0, 0] for _ in range(n_bands)]  # recomputed-own matches, stored-own matches, positions
    errors: list[str] = []
    single = 0
    for r, o in zip(records, outs, strict=True):
        j = int(r["band"])
        if o["error"] is not None:
            errors.append(o["error"])
            continue
        lessons = o["lessons"]
        for key, stored, new in (
            ("own", r["lesson"].get(j), lessons.get(j)),
            ("lower", r["lesson"].get(j - 1) if j > 0 else None, lessons.get(j - 1) if j > 0 else None),
            ("best", r["best"], o["best"]),
        ):
            if stored is not None and new is not None:
                agree[key][j][0] += int(stored == new)
                agree[key][j][1] += 1
        own_hits[j][0] += int(lessons[j] == r["played"])
        own_hits[j][1] += int(r["lesson"][j] == r["played"])
        own_hits[j][2] += 1
        single += int(o["n_admissible"] is not None and o["n_admissible"] < 2)
        for k in range(j - 1):  # k <= j - 2: the cells C2 did not compute
            r["lesson"][k] = lessons[k]
            r["source"][k] = "recomputed"
    return {
        "ran": True,
        "positions": len(records),
        "errors": len(errors),
        "first_errors": errors[:20],
        "queries": int(sum(o["queries"] for o in outs)),
        "seconds": round(time.perf_counter() - t0, 1),
        "agreement": {key: [(a / n if n else None) for a, n in rows] for key, rows in agree.items()},
        "own_rate_recomputed": [(h[0] / h[2] if h[2] else None) for h in own_hits],
        "own_rate_stored": [(h[1] / h[2] if h[2] else None) for h in own_hits],
        "single_admissible": single,
    }


# ------------------------------------------------------------------------------------------------ the file


def build(
    result: Mapping[str, Any],
    source_path: Path,
    *,
    engine: Any = None,
    runtime: Mapping[str, Any] | None = None,
    directory: Path | None = None,
    concurrency: int = 8,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Everything in results/relevance_baselines.json except the git and hardware fields (main adds those)."""
    if result.get("claim") != "C2" or result.get("pilot"):
        raise ValueError(f"{source_path.name} is not a confirmatory C2 result")
    cfg = result["config"]
    c2 = cfg["C2"]
    bands = parse_bands(cfg)
    epsilon = float(result["epsilon"])
    records = records_from_c2(result)
    seeds = np.random.SeedSequence(int(result["seed"])).spawn(5)
    boot_seed, choice_seed = int(seeds[3].generate_state(1)[0]), int(seeds[4].generate_state(1)[0])
    warnings: list[str] = []
    meta: dict[str, Any] = {"katago_version": None, "nets": None, "analysis_config": None}
    engine_block: dict[str, Any] = {"ran": False}
    if engine is not None:
        if directory is None or runtime is None:
            raise ValueError("the engine part needs the raw game directory and the runtime config")
        meta = engine_metadata(engine)
        for name in ("main", "human"):
            then = ((result.get("nets") or {}).get(name) or {}).get("sha256")
            now = ((meta.get("nets") or {}).get(name) or {}).get("sha256")
            if then != now:
                warnings.append(f"{name} net differs from the C2 run (C2 {then}, now {now})")
        then = (result.get("analysis_config") or {}).get("sha256")
        now = (meta.get("analysis_config") or {}).get("sha256")
        if then != now:
            warnings.append(f"analysis config differs from the C2 run (C2 {then}, now {now})")
        engine_block = recompute_lessons(
            records, engine=engine, cfg=cfg, runtime=runtime, epsilon=epsilon, directory=directory,
            concurrency=concurrency, seed=choice_seed, progress=progress,
        )  # fmt: skip
        if engine_block["errors"]:
            warnings.append(f"{engine_block['errors']} positions could not be recomputed (engine.first_errors)")
    by_band = [[r for r in records if r["band"] == b.index] for b in bands]
    level = float(c2["ci"])
    stats = compute(by_band, int(c2["bootstrap_resamples"]), level, boot_seed)
    u_here = (stats["adjacent"] or {}).get("lesson_diff_mean", {}).get("estimate")
    matches = (
        u_here is not None and result.get("estimate") is not None and abs(u_here - float(result["estimate"])) < 1e-9
    )
    if not matches:
        warnings.append(
            f"the mean adjacent lesson difference {u_here} does not reproduce C2's U {result.get('estimate')}"
        )
    return {
        **meta,
        "source": {
            "file": source_path.name,
            "sha256": sha256_file(source_path),
            "git_commit": result.get("git_commit"),
            "git_dirty": result.get("git_dirty"),
            "epsilon": epsilon,
            "n": result.get("n"),
            "U": result.get("estimate"),
            "U_ci": result.get("ci"),
        },
        "matches_c2_U": bool(matches),
        "warnings": warnings,
        "bootstrap": {
            "resamples": int(c2["bootstrap_resamples"]),
            "ci": level,
            "seed": boot_seed,
            "clusters": "games, resampled within every band, all bands jointly",
        },
        "definitions": {
            "lesson": "s_k: Sensei's choice at epsilon with band k's HumanSL profile as the learner policy (C2's rule)",
            "best": "b: the engine's best reliable move, as stored by C2",
            "rand": "r: a uniformly random admissible move, as stored by C2",
            "rates": "rates.lesson[k][j] = share of band j's positions where the player played s_k (j >= k)",
            "diff": "stronger band's rate minus the weaker band's rate",
            "ratio": "stronger band's rate divided by the weaker band's rate",
            "lesson_over_rand": "the lesson's ratio divided by the random admissible move's ratio "
            "(above 1: the lesson rises faster)",
            "lesson_not_best": "the lesson's rates on the positions where s_k is not b",
        },
        "bands": [b.name for b in bands],
        "profiles": [b.profile for b in bands],
        "n_positions": [len(recs) for recs in by_band],
        "n_games": [len({r["game"] for r in recs}) for recs in by_band],
        "engine": engine_block,
        **stats,
    }


# ------------------------------------------------------------------------------------------------ markdown


def _pct(cell: Mapping[str, Any] | None, mark: bool = False) -> str:
    if not cell or cell.get("estimate") is None:
        return ""
    text = f"{100 * cell['estimate']:.1f}%"
    if cell.get("ci"):
        text += f" [{100 * cell['ci'][0]:.1f}, {100 * cell['ci'][1]:.1f}]"
    return text + ("*" if mark and cell.get("source") == "recomputed" else "")


def _num(cell: Mapping[str, Any] | None, scale: float = 1.0, signed: bool = False, digits: int = 2) -> str:
    if not cell or cell.get("estimate") is None:
        return "n/a"
    f = f"{{:{'+' if signed else ''}.{digits}f}}"
    text = f.format(scale * cell["estimate"])
    if cell.get("ci"):
        text += f" [{f.format(scale * cell['ci'][0])}, {f.format(scale * cell['ci'][1])}]"
    return text


def to_markdown(data: Mapping[str, Any]) -> str:
    bands = list(data["bands"])
    rates = data["rates"]
    src = data["source"]
    level = round(100 * float(data["bootstrap"]["ci"]))
    lines = [
        "# C2 in context: Sensei's lesson, the engine's best move and a random admissible move",
        "",
        f"Generated by `python -m eval.relevance_baselines` at commit `{data.get('git_commit')}` (dirty: "
        f"{data.get('git_dirty')}) on {data.get('created')} from `{src['file']}` (C2 commit `{src['git_commit']}`, "
        f"epsilon {src['epsilon']:g}, {src['n']} positions).",
        "",
        "Descriptive, added after C2 ran: not pre-registered, no success criterion. A rate is the share of positions "
        f"where the player in the game played that move. Brackets: {level}% CIs from a bootstrap over games.",
        "",
        "## How often players in each band play each move",
        "",
        "| Move | " + " | ".join(bands) + " |",
        "|---|" + "---|" * len(bands),
    ]
    for k, name in enumerate(bands):
        cells = [_pct(rates["lesson"][k][j], mark=True) if j >= k else "" for j in range(len(bands))]
        lines.append(f"| Sensei's lesson for {name} | " + " | ".join(cells) + " |")
    lines.append("| Engine's best move | " + " | ".join(_pct(c) for c in rates["best"]) + " |")
    lines.append("| Random admissible move | " + " | ".join(_pct(c) for c in rates["rand"]) + " |")
    far = any(rates["lesson"][k][j] for k in range(len(bands)) for j in range(k + 2, len(bands)))
    lines += [
        "",
        "Rows: the move Sensei would teach a player of that band, evaluated on positions from that band and every "
        "stronger band. "
        + (
            "Cells marked * were recomputed with KataGo for this file; the others use C2's stored moves."
            if far
            else "Lessons two or more bands up were not computed (run without `--no-engine` to add them)."
        ),
        "",
        "## From a band to a stronger band, as a ratio (stronger band's rate divided by the weaker band's)",
        "",
        "| Lesson for | Played by | Lesson | Engine's best | Random admissible | Lesson / random | Lesson / best |",
        "|---|---|---|---|---|---|---|",
    ]
    for st in data["steps"]:
        lines.append(
            f"| {bands[st['weaker']]} | {bands[st['stronger']]} | {_num(st['lesson']['ratio'])} | "
            f"{_num(st['best']['ratio'])} | {_num(st['rand']['ratio'])} | {_num(st['lesson_over_rand'])} | "
            f"{_num(st['lesson_over_best'])} |"
        )
    adj = data.get("adjacent")
    if adj:
        lines.append(
            f"| Adjacent steps, geometric mean | | {_num(adj['lesson_ratio_gmean'])} | "
            f"{_num(adj['best_ratio_gmean'])} | "
            f"{_num(adj['rand_ratio_gmean'])} | {_num(adj['lesson_over_rand_gmean'])} | "
            f"{_num(adj['lesson_over_best_gmean'])} |"
        )
    lines += [
        "",
        "Above 1: the stronger band plays that move more often. Lesson / random above 1: the lesson's rate rises "
        "faster "
        "than a random admissible move's.",
        "",
        "## The same steps as differences (percentage points)",
        "",
        "| Lesson for | Played by | Lesson | Engine's best | Random admissible | Lesson minus random "
        "| Lesson minus best |",
        "|---|---|---|---|---|---|---|",
    ]
    for st in data["steps"]:
        lines.append(
            f"| {bands[st['weaker']]} | {bands[st['stronger']]} | {_num(st['lesson']['diff'], 100, True, 1)} | "
            f"{_num(st['best']['diff'], 100, True, 1)} | {_num(st['rand']['diff'], 100, True, 1)} | "
            f"{_num(st['lesson_minus_rand'], 100, True, 1)} | {_num(st['lesson_minus_best'], 100, True, 1)} |"
        )
    if adj:
        lines.append(
            f"| Adjacent steps, mean | | {_num(adj['lesson_diff_mean'], 100, True, 1)} | "
            f"{_num(adj['best_diff_mean'], 100, True, 1)} | {_num(adj['rand_diff_mean'], 100, True, 1)} | "
            f"{_num(adj['lesson_minus_rand_mean'], 100, True, 1)} | "
            f"{_num(adj['lesson_minus_best_mean'], 100, True, 1)} |"
        )
    u_ci = src.get("U_ci") or [None, None]
    lines += [
        "",
        f"The lesson's adjacent mean is C2's U ({100 * float(src['U']):+.2f} points, C2's own CI "
        f"[{100 * float(u_ci[0]):+.2f}, {100 * float(u_ci[1]):+.2f}]); reproduced from the C2 file: "
        f"{'yes' if data['matches_c2_U'] else 'NO'}.",
        "",
        "## Positions where the lesson is not the engine's best move",
        "",
        "Share of positions where the band's own lesson differs from the engine's best move: "
        + ", ".join(
            f"{name} {100 * x:.0f}%"
            for name, x in zip(bands, data["not_best_share_by_band"], strict=True)
            if x is not None
        )
        + ".",
        "",
        "| Lesson for | " + " | ".join(bands) + " |",
        "|---|" + "---|" * len(bands),
    ]
    for k, name in enumerate(bands):
        cells = [_pct(rates["lesson_not_best"][k][j]) if j >= k else "" for j in range(len(bands))]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    if adj:
        lines += [
            "",
            "Adjacent steps on these positions: mean difference "
            f"{_num(adj['lesson_not_best_diff_mean'], 100, True, 1)} "
            f"points, geometric mean ratio {_num(adj['lesson_not_best_ratio_gmean'])}.",
        ]
    eng = data["engine"]
    lines += ["", "## Reproduction check", ""]
    if eng.get("ran"):

        def agree(key: str) -> str:
            vals = eng["agreement"][key]
            return ", ".join(f"{b} {100 * v:.0f}%" for b, v in zip(bands, vals, strict=True) if v is not None)

        lines += [
            f"KataGo re-analyzed {eng['positions']} positions in {eng['seconds']} s ({eng['queries']} queries, "
            f"{eng['errors']} errors). The recomputed move equals C2's stored move for the band's own lesson in "
            f"{agree('own')}; for the lesson of the band below in {agree('lower')}; for the engine's best move in "
            f"{agree('best')}. Recomputed admissible sets with a single move: {eng['single_admissible']}.",
        ]
    else:
        lines.append("No engine run (`--no-engine`): every number above comes from the C2 file.")
    if data.get("warnings"):
        lines += ["", "Warnings:", "", *[f"- {w}" for w in data["warnings"]]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ CLI


def _print(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def make_engine(fake: bool) -> Any:
    from sensei.engine_katago import KataGoEngine  # lazily: the module imports without KataGo

    if fake:
        return KataGoEngine(
            exe=sys.executable,
            exe_args=[str(FAKE_ENGINE)],
            model="fake-main.bin.gz",
            human_model="fake-human.bin.gz",
            config="fake-analysis.cfg",
            query_timeout=120.0,
        )
    return KataGoEngine.from_env(query_timeout=QUERY_TIMEOUT_S)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=None, help="C2 result file (default: the latest clean results/C2_*.json)")
    ap.add_argument("--no-engine", action="store_true", help="only the comparisons the C2 file holds")
    ap.add_argument("--fake", action="store_true", help="tests/fake_katago.py instead of KataGo")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--splits", default=None, help="splits file for the raw game directory (default data/splits.json)")
    ap.add_argument("--out", default=None, help=f"output JSON (default results/{OUT_NAME}); the .md goes beside it")
    args = ap.parse_args(argv)

    commit, dirty = git_info(ROOT)
    started = datetime.now().astimezone()
    t0 = time.perf_counter()
    if args.source:
        source = Path(args.source)
        result = json.loads(source.read_text(encoding="utf-8"))
    else:
        source, result = latest_clean(RESULTS_DIR, "C2", False)
    engine = None
    directory = None
    runtime = yaml.safe_load(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    if not args.no_engine:
        splits_path = Path(args.splits) if args.splits else DEFAULT_SPLITS
        splits = load_splits(splits_path)
        directory = raw_dir(splits)
        engine = make_engine(args.fake)
    try:
        body = build(
            result, source, engine=engine, runtime=runtime, directory=directory, concurrency=args.concurrency,
            progress=_print,
        )  # fmt: skip
    finally:
        if engine is not None:
            engine.close()
    if engine is not None:
        expected = (result.get("details") or {}).get("splits_sha256")
        if expected and sha256_file(splits_path) != expected:
            body["warnings"].append(f"{splits_path.name} differs from the splits file C2 used")
    backend = None if engine is None else ("fake" if args.fake else (os.environ.get("KATAGO_BACKEND") or None))
    data = {
        "kind": "relevance_baselines",
        "descriptive": True,
        "post_hoc": True,
        "note": NOTE,
        "git_commit": commit,
        "git_dirty": dirty,
        "created": started.isoformat(timespec="seconds"),
        "wall_clock_s": round(time.perf_counter() - t0, 1),
        "hardware": hardware_block(backend),
        **body,
    }
    out = Path(args.out) if args.out else RESULTS_DIR / OUT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    md = out.with_suffix(".md")
    md.write_text(to_markdown(data), encoding="utf-8")
    adj = data.get("adjacent") or {}
    if adj:
        print(
            "adjacent steps, geometric mean ratio: lesson "
            f"{_num(adj['lesson_ratio_gmean'])}, engine's best {_num(adj['best_ratio_gmean'])}, random admissible "
            f"{_num(adj['rand_ratio_gmean'])}; lesson / random {_num(adj['lesson_over_rand_gmean'])}"
        )
    for w in data["warnings"]:
        print(f"warning: {w}")
    print(f"wrote {out} and {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
