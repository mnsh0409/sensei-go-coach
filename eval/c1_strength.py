"""C1, strength cost (SCOPE.md "C1 Strength cost (two stages)"; parameters in configs/prereg.yaml).

Sensei plays the raw engine from the openings of eval/openings.py, each opening twice with colours swapped. Both
sides search `engine.max_visits` visits. The raw engine plays the most-visited candidate. Sensei plays the
ConstrainedSelector's choice at temperature `selector.temperature_eval` (0) with the learner policy pi_L = the
HumanSL policy at `C1.learner_profile`, renormalized over legal moves. Many games stay in flight on one KataGo
process (thread pool, `--concurrency`); results are sorted by game index, so completion order never matters.

Adjudication (`adjudication` in the prereg): before each move after the opening, the mover's analysis gives Black's
winrate w (KataGo reports the side to move, CLAUDE.md). The game ends when w > decisive_winrate (Black wins) or
w < 1 - decisive_winrate (White wins) at `decisive_plies` consecutive plies. At `max_plies` plies, or after two
consecutive passes, one more analysis of the final position decides: the side its score lead favours wins (a lead
of exactly 0 is a draw). Sensei earns 1, 0.5 or 0 points per game.

Pilot (`--pilot`): every epsilon in `C1.pilot_epsilons` on the pilot openings. epsilon* is the largest epsilon whose
score rate is at least `C1.pilot_min_score`; if none qualifies, the smallest epsilon, reported as not qualified.
Confirmatory: the confirmatory openings at epsilon*, read from the most recent clean `pilot_C1_*.json`.
Metric: strength cost in Elo = -elo_from_score(pooled score rate), positive = Sensei weaker, with a bootstrap over
openings (eval.stats.elo_cost_ci). Success: the upper end of the CI is below `C1.success_max_cost_elo_upper`.
Also reported per Sensei move: winrate loss V* - V(chosen), score-lead loss (lead of the best-value reliable
candidate minus the chosen one's), admissible-set sizes, position classes and how often Sensei left the
engine's top move.

`C1.learner_profile` is read from the prereg; while it is absent the harness uses DEFAULT_LEARNER_PROFILE and
records a deviation, so the gap is visible in every result until the human approves a prereg revision.
A game that raises (an illegal-move disagreement between GoGame and KataGo, a malformed response) is excluded and
reported as a deviation; if KataGo itself dies the run stops.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from eval.hardware import engine_metadata
from eval.openings import Opening, load_openings, replay
from eval.splits import DEFAULT_SPLITS
from eval.stats import elo_cost_ci, pooled_bootstrap_ci
from eval.tokens import is_clean
from sensei.coords import PASS
from sensei.game_go import GoGame
from sensei.interfaces import Color, EngineAnalysis, Move, Position
from sensei.learner import HumanSLLearner
from sensei.selector import ConstrainedSelector

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "results"
LOGS_DIR = ROOT / "logs"
RUNTIME_CONFIG = ROOT / "configs" / "default.yaml"

DEFAULT_LEARNER_PROFILE = "rank_10k"
"""Used while configs/prereg.yaml has no C1.learner_profile: the app's default declared rank (10k)."""

COLORS: tuple[Color, Color] = ("B", "W")
QUERY_TIMEOUT_S = 300.0
"""A query unanswered this long fails its game (excluded, reported) instead of hanging the run."""
REASONS = ("decisive", "max_plies", "two_passes")
METRIC = "strength cost in Elo from Sensei's score rate against the raw engine (positive = Sensei weaker)"

ProgressFn = Callable[[dict[str, Any], int, int, float], None]


class EngineDied(RuntimeError):
    """KataGo exited during the run; no further game can be scored."""


@dataclass(frozen=True)
class GameSpec:
    """One game: an opening, Sensei's colour and epsilon. `index` orders the results."""

    index: int
    opening: Opening
    sensei_color: Color
    epsilon: float


# ------------------------------------------------------------------------------------------------ adjudication


class Adjudicator:
    """Tracks Black's winrate ply by ply and declares a winner after `plies` consecutive decisive readings."""

    def __init__(self, decisive_winrate: float, plies: int) -> None:
        if not 0.5 < decisive_winrate < 1.0 or plies < 1:
            raise ValueError("decisive_winrate must be within (0.5, 1) and plies >= 1")
        self.threshold = float(decisive_winrate)
        self.plies = int(plies)
        self.side: Color | None = None
        self.streak = 0

    def update(self, black_winrate: float) -> Color | None:
        """Record one ply's reading; return the winner once the streak is long enough, else None."""
        if black_winrate > self.threshold:
            side: Color | None = "B"
        elif black_winrate < 1.0 - self.threshold:
            side = "W"
        else:
            side = None
        if side is None:
            self.side, self.streak = None, 0
            return None
        self.streak = self.streak + 1 if side == self.side else 1
        self.side = side
        return side if self.streak >= self.plies else None


def pilot_rule(scores: Mapping[float, float], epsilons: Sequence[float], min_score: float) -> tuple[float, bool]:
    """(epsilon*, qualified): the largest epsilon scoring >= min_score, else the smallest epsilon (not qualified)."""
    qualified = [float(e) for e in epsilons if scores[float(e)] >= min_score]
    if qualified:
        return max(qualified), True
    return min(float(e) for e in epsilons), False


# ------------------------------------------------------------------------------------------------ one game


class _CountingEngine:
    """Per-game view of the shared engine that counts the queries this game sends (single-threaded per game)."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.queries = 0

    def analyze(self, pos: Position, max_visits: int) -> EngineAnalysis:
        self.queries += 1
        return self.engine.analyze(pos, max_visits)

    def analyze_raw(self, pos: Position, max_visits: int) -> tuple[EngineAnalysis, dict[str, Any]]:
        self.queries += 1
        return self.engine.analyze_raw(pos, max_visits)

    def human_policy(self, pos: Position, profile: str) -> Mapping[Move, float]:
        self.queries += 1
        return self.engine.human_policy(pos, profile)


def _black(value: float, to_move: Color) -> float:
    """A side-to-move winrate as Black's winrate."""
    return value if to_move == "B" else 1.0 - value


def play_game(
    spec: GameSpec,
    engine: Any,
    cfg: Mapping[str, Any],
    selector: ConstrainedSelector,
    learner_profile: str,
    rng: np.random.Generator,
) -> dict[str, Any]:
    """Play one game to its adjudicated end; return its record (see `run_games` for the fields)."""
    t0 = time.perf_counter()
    game = GoGame()
    visits = int(cfg["engine"]["max_visits"])
    adj_cfg = cfg["adjudication"]
    max_plies = int(adj_cfg["max_plies"])
    temperature = float(cfg["selector"]["temperature_eval"])
    counting = engine if isinstance(engine, _CountingEngine) else _CountingEngine(engine)
    learner = HumanSLLearner(
        counting,
        (learner_profile,),
        learner_profile,
        int(cfg["learner"]["min_moves_for_estimate"]),
        float(cfg["learner"]["delta"]),
    )
    adjudicator = Adjudicator(float(adj_cfg["decisive_winrate"]), int(adj_cfg["decisive_plies"]))

    pos = replay(cfg, spec.opening.moves)
    sensei_moves = 0
    value_loss_sum = 0.0
    lead_loss_sum, lead_loss_n = 0.0, 0
    admissible_sizes: Counter[int] = Counter()
    classes: Counter[str] = Counter()
    left_top = 0
    winner: Color | None = None
    reason = ""
    black_winrate: float | None = None
    black_lead: float | None = None

    while True:
        if game.is_terminal(pos):
            reason = "two_passes"
            break
        if len(pos.moves) >= max_plies:
            reason = "max_plies"
            break
        analysis = counting.analyze(pos, visits)
        black_winrate = _black(float(analysis.root_value), pos.to_move)
        decided = adjudicator.update(black_winrate)
        if decided is not None:
            winner, reason = decided, "decisive"
            break
        if not analysis.candidates:
            move: Move = PASS
        elif pos.to_move == spec.sensei_color:
            policy = learner.policy(pos, game.legal_moves(pos))
            choice = selector.choose(analysis, policy, spec.epsilon, temperature, rng)
            move = choice.move
            sensei_moves += 1
            value_loss_sum += float(choice.value_loss)
            admissible_sizes[len(choice.admissible)] += 1
            classes[choice.position_class.value] += 1
            left_top += int(choice.move != analysis.candidates[0].move)
            best = max(selector.reliable(analysis), key=lambda c: (float(c.value), int(c.visits)))
            chosen = next(c for c in analysis.candidates if c.move == choice.move)
            if best.score_lead is not None and chosen.score_lead is not None:
                lead_loss_sum += float(best.score_lead) - float(chosen.score_lead)
                lead_loss_n += 1
        else:
            move = analysis.candidates[0].move  # the raw engine: most visits (the wrapper sorts by visits)
        pos = game.play(pos, move)  # ValueError if GoGame and KataGo disagree on legality: the game is excluded

    if reason != "decisive":
        analysis, raw = counting.analyze_raw(pos, visits)
        black_winrate = _black(float(analysis.root_value), pos.to_move)
        lead = (raw.get("rootInfo") or {}).get("scoreLead")
        if lead is None:
            raise RuntimeError("final analysis has no rootInfo.scoreLead")
        black_lead = float(lead) if pos.to_move == "B" else -float(lead)
        winner = "B" if black_lead > 0 else "W" if black_lead < 0 else None

    points = 0.5 if winner is None else (1.0 if winner == spec.sensei_color else 0.0)
    return {
        "index": spec.index,
        "opening_index": spec.opening.index,
        "opening": spec.opening.game,
        "sensei_color": spec.sensei_color,
        "epsilon": spec.epsilon,
        "winner": winner,
        "sensei_points": points,
        "reason": reason,
        "plies": len(pos.moves),
        "final_black_winrate": None if black_winrate is None else round(black_winrate, 6),
        "final_black_score_lead": None if black_lead is None else round(black_lead, 3),
        "sensei_moves": sensei_moves,
        "value_loss_sum": value_loss_sum,
        "score_lead_loss_sum": lead_loss_sum,
        "score_lead_loss_n": lead_loss_n,
        "admissible_sizes": {str(k): v for k, v in sorted(admissible_sizes.items())},
        "position_classes": dict(sorted(classes.items())),
        "left_engine_top": left_top,
        "engine_queries": counting.queries,
        "seconds": round(time.perf_counter() - t0, 3),
        "error": None,
    }


def _error_record(spec: GameSpec, err: BaseException, queries: int = 0) -> dict[str, Any]:
    last = traceback.extract_tb(err.__traceback__)[-1:] if err.__traceback__ else []
    where = f" at {Path(last[0].filename).name}:{last[0].lineno}" if last else ""
    return {
        "index": spec.index,
        "opening_index": spec.opening.index,
        "opening": spec.opening.game,
        "sensei_color": spec.sensei_color,
        "epsilon": spec.epsilon,
        "error": f"{type(err).__name__}: {err}{where}",
        "engine_queries": queries,
    }


# ------------------------------------------------------------------------------------------------ many games


def make_specs(openings: Sequence[Opening], epsilons: Sequence[float]) -> list[GameSpec]:
    """Every opening x colour x epsilon. Same-opening games are adjacent, so they share KataGo's NN cache."""
    specs: list[GameSpec] = []
    for opening in openings:
        for color in COLORS:
            for eps in epsilons:
                specs.append(GameSpec(len(specs), opening, color, float(eps)))
    return specs


def run_games(
    specs: Sequence[GameSpec],
    engine: Any,
    cfg: Mapping[str, Any],
    selector: ConstrainedSelector,
    learner_profile: str,
    *,
    seed: int,
    concurrency: int,
    games_log: Path | None = None,
    progress: ProgressFn | None = None,
) -> list[dict[str, Any]]:
    """Play every game with `concurrency` games in flight; return the records sorted by index.

    Record fields: index, opening_index, opening, sensei_color, epsilon, winner ("B", "W" or None for a draw),
    sensei_points, reason, plies, final_black_winrate, final_black_score_lead (None after a decisive end),
    sensei_moves, value_loss_sum, score_lead_loss_sum, score_lead_loss_n, admissible_sizes, position_classes,
    left_engine_top, engine_queries, seconds, error (None, or the exception for an excluded game).
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    streams = np.random.SeedSequence(seed).spawn(len(specs))
    records: list[dict[str, Any]] = []
    log_file = None
    if games_log is not None:
        games_log.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(games_log, "a", encoding="utf-8")  # noqa: SIM115 - closed in finally
    t0 = time.perf_counter()

    def one(spec: GameSpec) -> dict[str, Any]:
        counting = _CountingEngine(engine)  # per game, so an excluded game still reports the queries it spent
        try:
            rng = np.random.default_rng(streams[spec.index])
            return play_game(spec, counting, cfg, selector, learner_profile, rng)
        except Exception as err:  # noqa: BLE001 - an excluded game is recorded, not fatal
            if not getattr(engine, "alive", True):
                raise EngineDied(f"KataGo exited during game {spec.index}: {err}") from err
            return _error_record(spec, err, counting.queries)

    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="c1-game")
    finished = False
    try:
        pending: set[Future[dict[str, Any]]] = {pool.submit(one, spec) for spec in specs}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                record = fut.result()  # re-raises EngineDied
                records.append(record)
                if log_file is not None:
                    log_file.write(json.dumps(record) + "\n")
                    log_file.flush()
                if progress is not None:
                    progress(record, len(records), len(specs), time.perf_counter() - t0)
        finished = True
    finally:
        # On an error or Ctrl-C, queued games are cancelled and running ones fail once the caller closes KataGo.
        pool.shutdown(wait=finished, cancel_futures=not finished)
        if log_file is not None:
            log_file.close()
    return sorted(records, key=lambda r: r["index"])


def print_progress(record: Mapping[str, Any], done: int, total: int, elapsed: float) -> None:
    """One stderr line per finished game with an ETA from the mean time per game so far."""
    eta = elapsed / done * (total - done)
    if record.get("error"):
        outcome = f"EXCLUDED ({record['error']})"
    else:
        outcome = f"sensei {record['sensei_points']:g} ({record['reason']}, {record['plies']} plies)"
    print(
        f"C1 {done}/{total} eps={record['epsilon']:g} opening={record['opening']} sensei={record['sensei_color']}: "
        f"{outcome}; elapsed {elapsed / 60:.1f} min, ETA {eta / 60:.1f} min",
        file=sys.stderr,
        flush=True,
    )


# ------------------------------------------------------------------------------------------------ statistics


def summarize(records: Sequence[Mapping[str, Any]], c1: Mapping[str, Any], seed: int) -> dict[str, Any]:
    """Score rate, Elo cost (bootstrap over openings) and per-move statistics of one epsilon's games."""
    scored = [r for r in records if r.get("error") is None]
    excluded = [r for r in records if r.get("error") is not None]
    if not scored:
        raise RuntimeError(f"every game was excluded; first error: {excluded[0]['error'] if excluded else 'none'}")
    by_opening: dict[int, list[float]] = {}
    for r in scored:
        by_opening.setdefault(int(r["opening_index"]), []).append(float(r["sensei_points"]))
    groups = [by_opening[k] for k in sorted(by_opening)]
    n_resamples, level = int(c1["bootstrap_resamples"]), float(c1["ci"])
    cost, cost_lo, cost_hi = elo_cost_ci(groups, n_resamples, level, seed)
    rate, rate_lo, rate_hi = pooled_bootstrap_ci(
        [sum(g) for g in groups], [len(g) for g in groups], n_resamples, level, seed
    )

    def color_rate(color: str) -> float | None:
        pts = [float(r["sensei_points"]) for r in scored if r["sensei_color"] == color]
        return round(sum(pts) / len(pts), 4) if pts else None

    moves = sum(int(r["sensei_moves"]) for r in scored)
    lead_n = sum(int(r["score_lead_loss_n"]) for r in scored)
    sizes: Counter[int] = Counter()
    classes: Counter[str] = Counter()
    for r in scored:
        sizes.update({int(k): int(v) for k, v in r["admissible_sizes"].items()})
        classes.update({str(k): int(v) for k, v in r["position_classes"].items()})
    points = [float(r["sensei_points"]) for r in scored]
    return {
        "n_games": len(scored),
        "n_excluded": len(excluded),
        "n_openings": len(groups),
        "score": rate,
        "score_ci": [rate_lo, rate_hi],
        "elo_cost": cost,
        "elo_cost_ci": [cost_lo, cost_hi],
        "wins": sum(1 for p in points if p == 1.0),
        "draws": sum(1 for p in points if p == 0.5),
        "losses": sum(1 for p in points if p == 0.0),
        "score_as_black": color_rate("B"),
        "score_as_white": color_rate("W"),
        "sensei_moves": moves,
        "mean_winrate_loss_per_move": sum(float(r["value_loss_sum"]) for r in scored) / moves if moves else None,
        "mean_score_lead_loss_per_move": (
            sum(float(r["score_lead_loss_sum"]) for r in scored) / lead_n if lead_n else None
        ),
        "left_engine_top_rate": sum(int(r["left_engine_top"]) for r in scored) / moves if moves else None,
        "admissible_size_hist": {str(k): sizes[k] for k in sorted(sizes)},
        "position_classes": dict(sorted(classes.items())),
        "reasons": {reason: sum(1 for r in scored if r["reason"] == reason) for reason in REASONS},
        "mean_plies": sum(int(r["plies"]) for r in scored) / len(scored),
        "engine_queries": sum(int(r.get("engine_queries", 0)) for r in records),
    }


def find_epsilon_star(results_dir: Path, epsilons: Sequence[float]) -> tuple[float, Path, bool]:
    """(epsilon*, pilot file, clean) from the most recent pilot_C1_*.json; any clean file wins over dirty ones."""
    found: list[tuple[bool, str, float, Path]] = []
    for path in sorted(Path(results_dir).glob("pilot_C1_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, Mapping) or data.get("claim") != "C1" or not data.get("pilot"):
            continue
        eps = (data.get("details") or {}).get("epsilon_star")
        if eps is None:
            continue
        found.append((is_clean(data), path.name, float(eps), path))
    if not found:
        raise FileNotFoundError(
            f"no pilot_C1_*.json with details.epsilon_star in {results_dir}; run the pilot first: "
            "python -m eval.run --claim C1 --config configs/prereg.yaml --pilot"
        )
    pool = [f for f in found if f[0]] or found
    clean, _, eps, path = max(pool, key=lambda f: f[1])  # file names sort chronologically
    if not any(abs(eps - float(e)) < 1e-12 for e in epsilons):
        raise ValueError(f"{path.name} gives epsilon* = {eps}, which is not in C1.pilot_epsilons {list(epsilons)}")
    return eps, path, clean


def latest_confirmatory(results_dir: Path) -> tuple[Path, dict[str, Any], bool] | None:
    """(file, data, clean) of the most recent C1 confirmatory result, clean files first; None if there is none."""
    found: list[tuple[bool, str, Path, dict[str, Any]]] = []
    for path in sorted(Path(results_dir).glob("C1_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, Mapping) and data.get("claim") == "C1" and not data.get("pilot") and "epsilon" in data:
            found.append((is_clean(data), path.name, path, dict(data)))
    if not found:
        return None
    pool = [f for f in found if f[0]] or found
    clean, _, path, data = max(pool, key=lambda f: f[1])
    return path, data, clean


def epsilon_for_c2(results_dir: Path, epsilons: Sequence[float]) -> tuple[float, dict[str, Any], list[dict[str, Any]]]:
    """(epsilon, source, deviations) for C2, whose prereg epsilon is `c1_confirmatory`.

    The epsilon of the most recent C1 confirmatory result; without one, the pilot's epsilon* with a deviation (C2 run
    before the confirmatory C1 finished). A disagreement between the two, or a dirty source, is also a deviation.
    """
    deviations: list[dict[str, Any]] = []
    try:
        pilot_eps, pilot_path, pilot_clean = find_epsilon_star(results_dir, epsilons)
    except FileNotFoundError:
        pilot_eps, pilot_path, pilot_clean = None, None, False
    conf = latest_confirmatory(results_dir)
    if conf is not None:
        path, data, clean = conf
        eps = float(data["epsilon"])
        source = {"file": path.name, "kind": "C1 confirmatory", "clean": clean}
        if not clean:
            deviations.append(
                {"field": "epsilon", "value": eps, "reason": f"epsilon from {path.name}, a dirty or git-less C1 run"}
            )
        if pilot_eps is not None and abs(pilot_eps - eps) > 1e-12:
            deviations.append(
                {
                    "field": "epsilon",
                    "value": eps,
                    "reason": f"the most recent pilot ({pilot_path.name}) gives epsilon* = {pilot_eps:g}; C2 uses the "
                    f"confirmatory C1 epsilon {eps:g} as pre-registered",
                }
            )
        return eps, source, deviations
    if pilot_eps is None:
        raise FileNotFoundError(f"no C1 result in {results_dir}: run the C1 pilot (and the confirmatory C1) first")
    deviations.append(
        {
            "field": "epsilon",
            "value": pilot_eps,
            "reason": f"no C1 confirmatory result yet; epsilon* taken from the pilot {pilot_path.name}"
            + ("" if pilot_clean else ", a dirty or git-less run (debugging only)"),
        }
    )
    return pilot_eps, {"file": pilot_path.name, "kind": "C1 pilot", "clean": pilot_clean}, deviations


# ------------------------------------------------------------------------------------------------ the claim


def learner_profile_of(cfg: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(profile, deviations): C1.learner_profile, or the default with a deviation while the prereg lacks it."""
    profile = cfg["C1"].get("learner_profile")
    if profile:
        return str(profile), []
    return DEFAULT_LEARNER_PROFILE, [
        {
            "field": "C1.learner_profile",
            "value": DEFAULT_LEARNER_PROFILE,
            "reason": "configs/prereg.yaml (v2) does not fix the learner profile behind pi_L in C1; the harness "
            "default (the app's default declared rank) was used. Needs the human's approval as a prereg revision.",
        }
    ]


def make_selector(cfg: Mapping[str, Any], runtime: Mapping[str, Any]) -> ConstrainedSelector:
    """The selector with the prereg's delta and reliability rule; entropy thresholds (reporting only) from runtime."""
    return ConstrainedSelector(
        delta=float(cfg["selector"]["delta"]),
        min_visits_abs=int(cfg["engine"]["min_candidate_visits_abs"]),
        min_visits_frac=float(cfg["engine"]["min_candidate_visits_frac"]),
        entropy_low=float(runtime["selector"]["entropy_low"]),
        entropy_high=float(runtime["selector"]["entropy_high"]),
    )


def _compact(record: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "index",
        "opening",
        "sensei_color",
        "epsilon",
        "winner",
        "sensei_points",
        "reason",
        "plies",
        "final_black_winrate",
        "final_black_score_lead",
        "engine_queries",
        "seconds",
        "error",
    )
    return {k: record.get(k) for k in keys}


def run_c1(
    cfg: Mapping[str, Any],
    *,
    seed: int,
    pilot: bool,
    concurrency: int,
    engine: Any,
    runtime: Mapping[str, Any],
    results_dir: Path = RESULTS_DIR,
    splits_path: Path = DEFAULT_SPLITS,
    raw_dir: Path | None = None,
    games_log: Path | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Run the pilot or the confirmatory stage on `engine`; return the claim fields for eval.run."""
    c1 = cfg["C1"]
    profile, deviations = learner_profile_of(cfg)
    epsilons = [float(e) for e in c1["pilot_epsilons"]]
    star_source: dict[str, Any] | None = None
    if pilot:
        run_epsilons = epsilons
    else:
        eps_star, star_path, star_clean = find_epsilon_star(results_dir, epsilons)
        run_epsilons = [eps_star]
        star_source = {"file": star_path.name, "clean": star_clean}
        if not star_clean:
            deviations.append(
                {
                    "field": "epsilon",
                    "value": eps_star,
                    "reason": f"epsilon* read from {star_path.name}, a pilot from a dirty or git-less checkout "
                    "(debugging only)",
                }
            )
    openings, openings_info = load_openings(
        cfg, "pilot" if pilot else "confirmatory", splits_path=splits_path, directory=raw_dir
    )
    expected = int(c1["pilot_games_each"] if pilot else c1["n_games"])
    if 2 * len(openings) != expected:
        raise ValueError(f"{len(openings)} openings x 2 colours != {expected} games per epsilon in the prereg")

    meta = engine_metadata(engine)
    selector = make_selector(cfg, runtime)
    game_seed, boot_seed = np.random.SeedSequence(seed).spawn(2)
    specs = make_specs(openings, run_epsilons)
    records = run_games(
        specs,
        engine,
        cfg,
        selector,
        profile,
        seed=int(game_seed.generate_state(1)[0]),
        concurrency=concurrency,
        games_log=games_log,
        progress=progress,
    )
    for r in records:
        if r.get("error") is not None:
            deviations.append({"field": "game", "value": r["index"], "reason": f"excluded: {r['error']}"})

    boot_seeds = [int(s.generate_state(1)[0]) for s in boot_seed.spawn(len(run_epsilons))]
    per_eps = {
        eps: summarize([r for r in records if r["epsilon"] == eps], c1, boot_seeds[i])
        for i, eps in enumerate(run_epsilons)
    }
    common = {
        "metric": METRIC,
        "learner_profile": profile,
        "temperature": float(cfg["selector"]["temperature_eval"]),
        "openings": openings_info,
        "entropy_thresholds": {k: runtime["selector"][k] for k in ("entropy_low", "entropy_high")},
        "games": [_compact(r) for r in records],
    }
    engine_queries = sum(int(r.get("engine_queries", 0)) for r in records)

    if pilot:
        scores = {eps: float(s["score"]) for eps, s in per_eps.items()}
        eps_star, qualified = pilot_rule(scores, epsilons, float(c1["pilot_min_score"]))
        star = per_eps[eps_star]
        details = {
            **common,
            "rule": "epsilon* = the largest pilot epsilon with score >= C1.pilot_min_score, else the smallest",
            "epsilon_star": eps_star,
            "epsilon_star_qualified": qualified,
            "per_epsilon": {f"{eps:g}": s for eps, s in per_eps.items()},
            "headline": {"name": "epsilon*", "estimate": eps_star, "ci": None},
        }
        return {
            **meta,
            "deviations": deviations,
            "concurrency": concurrency,
            "epsilon": eps_star,
            "engine_queries": engine_queries,
            "n": sum(s["n_games"] for s in per_eps.values()),
            "estimate": star["elo_cost"],
            "ci": star["elo_cost_ci"],
            "success": None,
            "details": details,
        }

    eps_star = run_epsilons[0]
    summary = per_eps[eps_star]
    threshold = float(c1["success_max_cost_elo_upper"])
    details = {
        **common,
        "epsilon_star_source": star_source,
        "success_rule": f"upper end of the {round(float(c1['ci']) * 100)}% CI of the Elo cost < {threshold:g}",
        "summary": summary,
        "headline": {"name": "score rate vs raw engine", "estimate": summary["score"], "ci": summary["score_ci"]},
    }
    return {
        **meta,
        "deviations": deviations,
        "concurrency": concurrency,
        "epsilon": eps_star,
        "engine_queries": engine_queries,
        "n": summary["n_games"],
        "estimate": summary["elo_cost"],
        "ci": summary["elo_cost_ci"],
        "success": bool(summary["elo_cost_ci"][1] < threshold),
        "details": details,
    }


def run_claim(
    cfg: Mapping[str, Any],
    *,
    seed: int,
    pilot: bool,
    concurrency: int | None,
    results_dir: Path | None = None,
) -> dict[str, Any]:
    """Entry point used by eval.run: KataGo from .env, the committed splits, progress on stderr."""
    from sensei.engine_katago import KataGoEngine  # lazily: the module imports without KataGo

    runtime = yaml.safe_load(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    games_log = LOGS_DIR / f"{'pilot_' if pilot else ''}C1_games_{stamp}.jsonl"
    engine = KataGoEngine.from_env(query_timeout=QUERY_TIMEOUT_S)
    try:
        out = run_c1(
            cfg,
            seed=seed,
            pilot=pilot,
            concurrency=int(concurrency or 8),
            engine=engine,
            runtime=runtime,
            results_dir=results_dir or RESULTS_DIR,
            games_log=games_log,
            progress=print_progress,
        )
    finally:
        engine.close()
    out["katago_backend"] = os.environ.get("KATAGO_BACKEND") or None
    out.setdefault("details", {})["games_log"] = games_log.relative_to(ROOT).as_posix()
    return out
