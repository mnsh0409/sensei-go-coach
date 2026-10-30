"""C2, lesson relevance on real human games (SCOPE.md "C2 Lesson relevance on real human games"; parameters in
configs/prereg.yaml, C2).

Candidates: `data/splits.json` `c2_positions[band]`, a seeded list of (game, ply) whose mover is in the band. Each
candidate is re-checked (19x19, even game, date >= C2.game_date_min, ply within [ply_min, ply_max], mover rank in
the band, rules KataGo can play), analyzed under the game's own rules and komi at `engine.max_visits`, and is
eligible when the admissible set at epsilon* has at least `C2.min_admissible` moves. Per band, the first
`C2.positions_per_band` eligible candidates in list order are used (at most `C2.max_positions_per_game` per game),
which is a uniform sample of eligible positions because the list order is random. Candidates are analyzed in
chunks, several in flight; the selection only looks at list order, so completion order never matters.

In each position: s_k = Sensei's choice at epsilon* (temperature `selector.temperature_eval`) with band k's learner
policy (HumanSL at band k's profile); b = the engine's best reliable move (highest value among reliable candidates,
ties by visits); r = a uniformly random admissible move (seeded per position). The indicators compare these with
the move actually played. Bands with a weaker neighbour also get s_{k-1}, the move Sensei would teach the band
below, on their own positions.

Primary: U_k = P_{k+1}(played = s_k) - P_k(played = s_k) for each band but the strongest; U = mean of U_k. The CI
comes from a bootstrap that draws games with replacement within every band, then recomputes every U_k and U.
Success: the lower end of the CI is above 0. Secondary (descriptive): per band, D_best = P(played = b) -
P(played = s_k) and D_rand = P(played = r) - P(played = s_k) with game-clustered CIs, and the mean winrate loss
V* - V(s_k).
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from eval.c1_strength import (
    QUERY_TIMEOUT_S,
    RESULTS_DIR,
    RUNTIME_CONFIG,
    EngineDied,
    epsilon_for_c2,
    make_selector,
)
from eval.hardware import engine_metadata, sha256_file
from eval.splits import DEFAULT_SPLITS, check_disjoint, game_record, load_splits, position_at, raw_dir
from eval.stats import cluster_bootstrap_ci
from sensei.game_go import GoGame
from sensei.learner import HumanSLLearner
from sensei.ranks import label_to_numeric
from sensei.selector import ConstrainedSelector

METRIC = "cross-band uplift U = mean over k of P_{k+1}(played = s_k) - P_k(played = s_k)"
_CHUNK = 1000


@dataclass(frozen=True)
class Band:
    index: int
    name: str
    profile: str
    lo: float
    hi: float

    def contains(self, rank_label: str) -> bool:
        return self.lo <= label_to_numeric(rank_label) <= self.hi


def parse_bands(cfg: Mapping[str, Any]) -> list[Band]:
    """C2.bands as Band objects; names are "<weaker>-<stronger>" rank labels ("15k-10k", "1d-3d")."""
    bands = []
    for i, b in enumerate(cfg["C2"]["bands"]):
        weak, strong = (label_to_numeric(x.strip()) for x in str(b["name"]).split("-"))
        bands.append(Band(i, str(b["name"]), str(b["profile"]), min(weak, strong), max(weak, strong)))
    for a, b in zip(bands, bands[1:], strict=False):
        if not a.hi < b.lo:
            raise ValueError(f"bands must be ordered weak to strong and disjoint: {a.name} then {b.name}")
    return bands


# ------------------------------------------------------------------------------------------------ one position


def _choice(analysis, selector, engine, pos, legal, profile, epsilon, temperature, rng, cfg) -> str:
    learner = HumanSLLearner(
        engine, (profile,), profile, int(cfg["learner"]["min_moves_for_estimate"]), float(cfg["learner"]["delta"])
    )
    return selector.choose(analysis, learner.policy(pos, legal), epsilon, temperature, rng).move


def evaluate_candidate(
    item: Mapping[str, Any],
    order: int,
    band: Band,
    lower: Band | None,
    *,
    engine: Any,
    cfg: Mapping[str, Any],
    selector: ConstrainedSelector,
    epsilon: float,
    seed: int,
    directory: Path,
) -> dict[str, Any]:
    """Check, analyze and score one candidate. Returns {"eligible": False, "reason": ...} or the full record."""
    c2 = cfg["C2"]
    game_id, ply = str(item["game"]), int(item["ply"])
    base = {"order": order, "band": band.index, "game": game_id, "ply": ply, "queries": 0}
    try:
        rec = game_record(game_id, directory)
    except FileNotFoundError:
        return {**base, "eligible": False, "reason": "missing_sgf"}
    except (OSError, ValueError) as err:  # truncated download, malformed property, off-board move, ...
        return {**base, "eligible": False, "reason": "unparsable_sgf", "error": f"{type(err).__name__}: {err}"}
    if rec.size != int(cfg["game"]["board_size"]) or rec.handicap or rec.setup:
        return {**base, "eligible": False, "reason": "not_even_19x19"}
    if not rec.date or rec.date[:10] < str(c2["game_date_min"]):
        return {**base, "eligible": False, "reason": "date"}
    if not int(c2["ply_min"]) <= ply <= int(c2["ply_max"]):
        return {**base, "eligible": False, "reason": "ply"}
    if ply >= len(rec.moves):
        return {**base, "eligible": False, "reason": "ply_beyond_game"}
    try:
        hp = position_at(game_id, ply, directory)
    except ValueError:
        return {**base, "eligible": False, "reason": "rules_or_komi"}
    try:
        in_band = hp.mover_rank is not None and band.contains(hp.mover_rank)
    except ValueError:
        in_band = False
    if not in_band:
        return {**base, "eligible": False, "reason": "rank"}
    game = GoGame()
    try:
        legal = game.legal_moves(hp.position)
    except ValueError:
        return {**base, "eligible": False, "reason": "illegal_history"}

    try:
        analysis = engine.analyze(hp.position, int(cfg["engine"]["max_visits"]))
        base["queries"] = 1
        if not analysis.candidates:
            return {**base, "eligible": False, "reason": "no_candidates"}
        admissible = selector.admissible(analysis, epsilon)
        if len(admissible) < int(c2["min_admissible"]):
            return {**base, "eligible": False, "reason": "admissible", "n_admissible": len(admissible)}

        rng = np.random.default_rng([seed, band.index, order])
        best = max(selector.reliable(analysis), key=lambda c: (float(c.value), int(c.visits)))
        rand = admissible[int(rng.integers(len(admissible)))].move
        temperature = float(cfg["selector"]["temperature_eval"])
        args = (analysis, selector, engine, hp.position, legal)
        own = _choice(*args, band.profile, epsilon, temperature, rng, cfg)
        base["queries"] = 2
        below = None if lower is None else _choice(*args, lower.profile, epsilon, temperature, rng, cfg)
        base["queries"] = 2 if lower is None else 3
    except Exception as err:  # noqa: BLE001 - a position KataGo rejects is skipped and counted
        if not getattr(engine, "alive", True):
            raise EngineDied(f"KataGo exited at {game_id} ply {ply}: {err}") from err
        return {**base, "eligible": False, "reason": "engine_error", "error": f"{type(err).__name__}: {err}"}
    values = {c.move: float(c.value) for c in analysis.candidates}
    return {
        **base,
        "eligible": True,
        "reason": None,
        "played": hp.played,
        "mover_rank": hp.mover_rank,
        "n_admissible": len(admissible),
        "s_own": own,
        "s_lower": below,
        "best": best.move,
        "rand": rand,
        "loss_own": float(best.value) - values[own],
    }


def collect_band(
    band: Band,
    lower: Band | None,
    candidates: Sequence[Mapping[str, Any]],
    *,
    engine: Any,
    cfg: Mapping[str, Any],
    selector: ConstrainedSelector,
    epsilon: float,
    seed: int,
    directory: Path,
    concurrency: int,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], Counter[str], int, list[str]]:
    """(selected records in list order, skip reasons over the examined candidates, engine queries, first errors)."""
    c2 = cfg["C2"]
    target, per_game_cap = int(c2["positions_per_band"]), int(c2["max_positions_per_game"])
    selected: list[dict[str, Any]] = []
    per_game: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    errors: list[str] = []
    queries = 0
    start = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix=f"c2-{band.index}") as pool:
        while len(selected) < target and start < len(candidates):
            need = target - len(selected)
            size = max(concurrency, int(need * 1.3) + 1)
            chunk = list(enumerate(candidates[start : start + size], start=start))
            start += len(chunk)

            def one(pair: tuple[int, Mapping[str, Any]]) -> dict[str, Any]:
                order, item = pair
                return evaluate_candidate(
                    item, order, band, lower, engine=engine, cfg=cfg, selector=selector, epsilon=epsilon,
                    seed=seed, directory=directory,
                )  # fmt: skip

            for rec in pool.map(one, chunk):  # results come back in list order
                queries += int(rec["queries"])
                if len(selected) >= target:
                    continue  # analyzed in this chunk but not needed
                if not rec["eligible"]:
                    skipped[str(rec["reason"])] += 1
                    if rec.get("error") and len(errors) < 20:
                        errors.append(f"{rec['game']} ply {rec['ply']}: {rec['error']}")
                elif per_game[rec["game"]] >= per_game_cap:
                    skipped["per_game_cap"] += 1
                else:
                    per_game[rec["game"]] += 1
                    selected.append(rec)
            if progress is not None:
                progress(f"C2 band {band.name}: {len(selected)}/{target} eligible after {start} candidates")
    return selected, skipped, queries, errors


# ------------------------------------------------------------------------------------------------ statistics


def _game_sums(records: Sequence[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], float]):
    games = sorted({r["game"] for r in records})
    index = {g: i for i, g in enumerate(games)}
    sums = np.zeros(len(games))
    counts = np.zeros(len(games))
    for r in records:
        sums[index[r["game"]]] += key(r)
        counts[index[r["game"]]] += 1
    return sums, counts


def uplift(
    by_band: Sequence[Sequence[Mapping[str, Any]]],
    n_resamples: int,
    ci: float,
    seed: int,
) -> dict[str, Any]:
    """U_k and U with a bootstrap that resamples games within every band and recomputes everything jointly."""
    k_bands = len(by_band)
    if k_bands < 2 or any(len(b) == 0 for b in by_band):
        raise ValueError("uplift needs at least two bands, each with positions")
    own = [_game_sums(b, lambda r: float(r["played"] == r["s_own"])) for b in by_band]
    lower = [None] + [_game_sums(b, lambda r: float(r["played"] == r["s_lower"])) for b in by_band[1:]]
    own_rate = [s.sum() / c.sum() for s, c in own]
    lower_rate = [None] + [s.sum() / c.sum() for s, c in lower[1:]]
    u_k = np.array([lower_rate[k + 1] - own_rate[k] for k in range(k_bands - 1)])

    rng = np.random.default_rng(seed)
    samples = np.empty((n_resamples, k_bands - 1))
    done = 0
    while done < n_resamples:
        take = min(_CHUNK, n_resamples - done)
        own_s, lower_s = [], []
        for j in range(k_bands):
            n_games = own[j][1].shape[0]
            weights = rng.multinomial(n_games, np.full(n_games, 1.0 / n_games), size=take).astype(float)
            counts = weights @ own[j][1]
            own_s.append(weights @ own[j][0] / counts)
            lower_s.append(None if j == 0 else weights @ lower[j][0] / counts)
        for k in range(k_bands - 1):
            samples[done : done + take, k] = lower_s[k + 1] - own_s[k]
        done += take
    alpha = (1.0 - ci) / 2.0
    u_samples = samples.mean(axis=1)
    lo, hi = np.quantile(u_samples, [alpha, 1.0 - alpha])
    per_k = []
    for k in range(k_bands - 1):
        k_lo, k_hi = np.quantile(samples[:, k], [alpha, 1.0 - alpha])
        per_k.append({"estimate": float(u_k[k]), "ci": [float(k_lo), float(k_hi)]})
    return {
        "estimate": float(u_k.mean()),
        "ci": [float(lo), float(hi)],
        "per_k": per_k,
        "own_rate": [float(x) for x in own_rate],
        "lower_rate": [None if x is None else float(x) for x in lower_rate],
    }


def band_summary(records: Sequence[Mapping[str, Any]], c2: Mapping[str, Any], seed: int) -> dict[str, Any]:
    """Match rates and the secondary contrasts D_best and D_rand with game-clustered CIs."""
    games = [r["game"] for r in records]
    own = np.array([float(r["played"] == r["s_own"]) for r in records])
    best = np.array([float(r["played"] == r["best"]) for r in records])
    rand = np.array([float(r["played"] == r["rand"]) for r in records])
    n_resamples, level = int(c2["bootstrap_resamples"]), float(c2["ci"])
    d_best = cluster_bootstrap_ci(best - own, games, n_resamples, level, seed)
    d_rand = cluster_bootstrap_ci(rand - own, games, n_resamples, level, seed + 1)
    sizes = Counter(int(r["n_admissible"]) for r in records)
    return {
        "n_positions": len(records),
        "n_games": len(set(games)),
        "p_played_s_own": float(own.mean()),
        "p_played_best": float(best.mean()),
        "p_played_rand": float(rand.mean()),
        "p_played_s_lower": (
            None if records[0]["s_lower"] is None else float(np.mean([r["played"] == r["s_lower"] for r in records]))
        ),
        "D_best": {"estimate": d_best[0], "ci": [d_best[1], d_best[2]]},
        "D_rand": {"estimate": d_rand[0], "ci": [d_rand[1], d_rand[2]]},
        "s_own_equals_best_rate": float(np.mean([r["s_own"] == r["best"] for r in records])),
        "mean_winrate_loss_s_own": float(np.mean([r["loss_own"] for r in records])),
        "admissible_size_hist": {str(k): sizes[k] for k in sorted(sizes)},
    }


# ------------------------------------------------------------------------------------------------ the claim


def run_c2(
    cfg: Mapping[str, Any],
    *,
    seed: int,
    concurrency: int,
    engine: Any,
    runtime: Mapping[str, Any],
    results_dir: Path = RESULTS_DIR,
    splits_path: Path = DEFAULT_SPLITS,
    directory: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run C2 on `engine`; return the claim fields for eval.run."""
    c2 = cfg["C2"]
    if c2["epsilon"] != "c1_confirmatory":
        raise ValueError(f"C2.epsilon is {c2['epsilon']!r}; the prereg fixes it to c1_confirmatory (epsilon*)")
    epsilon, epsilon_source, deviations = epsilon_for_c2(results_dir, [float(e) for e in cfg["C1"]["pilot_epsilons"]])
    bands = parse_bands(cfg)
    splits = load_splits(splits_path)
    check_disjoint(splits)
    directory = Path(directory) if directory is not None else raw_dir(splits)
    missing = [b.name for b in bands if b.name not in splits["c2_positions"]]
    if missing:
        raise ValueError(f"{splits_path} has no c2_positions for bands {missing}")

    meta = engine_metadata(engine)
    selector = make_selector(cfg, runtime)
    seeds = np.random.SeedSequence(seed).spawn(3)
    pos_seed, uplift_seed, band_seed = (int(s.generate_state(1)[0]) for s in seeds)
    by_band: list[list[dict[str, Any]]] = []
    candidates_info: dict[str, Any] = {}
    queries = 0
    for band in bands:
        cands = splits["c2_positions"][band.name]
        records, skipped, q, errors = collect_band(
            band,
            bands[band.index - 1] if band.index > 0 else None,
            cands,
            engine=engine,
            cfg=cfg,
            selector=selector,
            epsilon=epsilon,
            seed=pos_seed,
            directory=directory,
            concurrency=concurrency,
            progress=progress,
        )
        queries += q
        by_band.append(records)
        candidates_info[band.name] = {
            "listed": len(cands),
            "skipped": dict(sorted(skipped.items())),
            "first_errors": errors,
        }
        if len(records) < int(c2["positions_per_band"]):
            deviations.append(
                {
                    "field": "C2.positions_per_band",
                    "value": len(records),
                    "reason": f"band {band.name}: only {len(records)} eligible positions among {len(cands)} "
                    "candidates in data/splits.json",
                }
            )
    if any(len(r) == 0 for r in by_band):
        raise RuntimeError(f"a band has no eligible positions: {candidates_info}")

    up = uplift(by_band, int(c2["bootstrap_resamples"]), float(c2["ci"]), uplift_seed)
    band_seeds = [int(s.generate_state(1)[0]) for s in np.random.SeedSequence(band_seed).spawn(len(bands))]
    per_band = {b.name: band_summary(by_band[b.index], c2, band_seeds[b.index]) for b in bands}
    shared = set.intersection(*({r["game"] for r in recs} for recs in by_band)) if by_band else set()
    pairs: dict[str, int] = {}
    for i in range(len(bands)):
        for j in range(i + 1, len(bands)):
            pairs[f"{bands[i].name}|{bands[j].name}"] = len(
                {r["game"] for r in by_band[i]} & {r["game"] for r in by_band[j]}
            )
    details = {
        "metric": METRIC,
        "success_rule": f"lower end of the {round(float(c2['ci']) * 100)}% CI of U > 0",
        "definitions": {
            "s_k": "Sensei's choice at epsilon* with band k's HumanSL profile as the learner policy",
            "b": "the reliable candidate with the highest value (ties: more visits)",
            "r": "a uniformly random admissible move, seeded per position",
        },
        "epsilon_source": epsilon_source,
        "bands": [{"name": b.name, "profile": b.profile} for b in bands],
        "uplift": {
            "U": {"estimate": up["estimate"], "ci": up["ci"]},
            "U_k": [
                {"weaker": bands[k].name, "stronger": bands[k + 1].name, **up["per_k"][k]}
                for k in range(len(bands) - 1)
            ],
        },
        "per_band": per_band,
        "candidates": candidates_info,
        "n_games": len({r["game"] for recs in by_band for r in recs}),
        "games_in_two_bands": pairs,
        "games_in_every_band": len(shared),
        "splits_sha256": sha256_file(splits_path),
        "positions": [
            [r["band"], r["game"], r["ply"], r["played"], r["s_own"], r["s_lower"], r["best"], r["rand"]]
            for recs in by_band
            for r in recs
        ],
        "positions_columns": ["band", "game", "ply", "played", "s_own", "s_lower", "best", "rand"],
        "headline": {"name": "cross-band uplift U", "estimate": up["estimate"], "ci": up["ci"]},
    }
    return {
        **meta,
        "deviations": deviations,
        "concurrency": concurrency,
        "epsilon": epsilon,
        "engine_queries": queries,
        "n": sum(len(r) for r in by_band),
        "estimate": up["estimate"],
        "ci": up["ci"],
        "success": bool(up["ci"][0] > 0.0),
        "details": details,
    }


def _print(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def run_claim(
    cfg: Mapping[str, Any],
    *,
    seed: int,
    pilot: bool,
    concurrency: int | None,
    results_dir: Path | None = None,
) -> dict[str, Any]:
    """Entry point used by eval.run (C2 has no pilot stage: `--pilot` is rejected)."""
    if pilot:
        raise SystemExit("C2 has no pilot stage; run it without --pilot")
    from sensei.engine_katago import KataGoEngine  # lazily: the module imports without KataGo

    runtime = yaml.safe_load(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    engine = KataGoEngine.from_env(query_timeout=QUERY_TIMEOUT_S)
    try:
        out = run_c2(
            cfg,
            seed=seed,
            concurrency=int(concurrency or 8),
            engine=engine,
            runtime=runtime,
            results_dir=results_dir or RESULTS_DIR,
            progress=_print,
        )
    finally:
        engine.close()
    out["katago_backend"] = os.environ.get("KATAGO_BACKEND") or None
    return out
