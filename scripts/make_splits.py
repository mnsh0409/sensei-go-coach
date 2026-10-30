"""Draw the evaluation splits (data/splits.json) from the public games that scripts/fetch_games.py stored (T1b).

    python scripts/make_splits.py
    python scripts/make_splits.py --index data/raw/ogs/index.jsonl --out data/splits.json
    python scripts/make_splits.py --dry-run          # only say whether the games on disk are enough

Input: <index dir>/index.jsonl (the accepted games) and the SGF files next to it; the moves and the at-game-time rank
labels (BR, WR) come from the SGF through sensei.sgf.read_game, the same parser the evaluation harness uses. Games
before the minimum date, not 19x19, with handicap, of a speed that --speeds does not list, or that position_at would
refuse (no komi; with a newer eval/splits.py also rules without a KataGo preset) are left out and counted by reason.
Output: data/splits.json in the schema of eval/splits.py, data/splits_summary.md (counts per split and per band, date
range, rank distribution), and the counts table between the splits markers of data/SOURCES.md.

Sizes come from configs/prereg.yaml (openings, C2) and configs/default.yaml (topics.fit_games); the explainer set is
always 10 positions (2 opening, 4 middle game, 2 endgame, 2 only-move candidates).

How the games are shared out (all draws are seeded with seeds.global, so the same games always give the same file):
  1. C2 positions, one band after the other, the band with the fewest candidate games first. A position (game, ply)
     is eligible when the mover's at-game-time rank is in the band, ply_min <= ply <= ply_max and the played move is
     not a pass. Games are taken in random order, each contributing up to max_positions_per_game random plies, until
     the band has positions_per_band positions. A game is used by one band only.
  2. Explainer positions (10): phases opening (ply 10 to 49), middle (50 to 149), end (150 to 200) and
     only_move_candidate (ply 20 to 200, and the previous move captured stones or put a group into atari). The
     mover's rank must be on the app's dropdown (20k to 5d) and becomes the declared rank; the slots cycle through
     the C2 bands so that the declared ranks are spread, and fall back to any rank on the dropdown.
  3. Openings: pilot_n_openings + n_openings games whose first `plies` moves alternate B/W from Black, contain no
     pass, and differ from every other selected opening (also up to rotation and reflection of the board).
  4. Topic-fitting games: topics.fit_games further games.
No game appears in two splits (eval.splits.check_disjoint is run before anything is written). Every list is in drawing
order, which is random, so the first k entries of a list are themselves a random subset (C1 may lower n_openings).
If the games on disk cannot fill a split, nothing is written and the error says how many more games to fetch.

Not decided here: C2 also drops positions whose admissible set has fewer than C2.min_admissible moves; that needs the
engine and happens in eval/c2_relevance.py, which keeps the first C2.positions_per_band candidates (in list order) that
pass and records a deviation when fewer do. The splits are drawn once, so the reserve is decided before the first draw:
`splits.c2_reserve_factor` in configs/default.yaml (2 in the repository; 1, no reserve, when the key is absent) times
C2.positions_per_band candidates are drawn per band, because only-move positions fail the admissible-set check and a
small epsilon* fails more of them. The analysis still uses the first C2.positions_per_band that pass, so the reserve
changes no pre-registered quantity. --positions-per-band N overrides it (for example when the dan band is short of
games and a smaller reserve is acceptable).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
for extra in (SCRIPTS, ROOT / "src", ROOT):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import yaml  # noqa: E402
from fetch_games import DEFAULT_MIN_DATE, DEFAULT_SEED, band_of, game_name  # noqa: E402

from eval.splits import SPLIT_KEYS, check_disjoint  # noqa: E402
from sensei.coords import gtp_to_rc, neighbors, rc_to_gtp  # noqa: E402
from sensei.game_go import GoGame  # noqa: E402
from sensei.interfaces import Position  # noqa: E402
from sensei.ranks import label_to_numeric  # noqa: E402
from sensei.sgf import read_game  # noqa: E402

try:  # a newer eval/splits.py maps SGF rules to KataGo presets, and position_at refuses games without a preset
    from eval.splits import katago_rules  # noqa: E402
except ImportError:  # an older one hands the SGF rules to KataGo unchanged
    katago_rules = None

PREREG = ROOT / "configs" / "prereg.yaml"
DEFAULT_CONFIG = ROOT / "configs" / "default.yaml"
DEFAULT_INDEX = ROOT / "data" / "raw" / "ogs" / "index.jsonl"
DEFAULT_OUT = ROOT / "data" / "splits.json"
DEFAULT_RESERVE_FACTOR = 1.0
"""C2 candidates per band as a multiple of C2.positions_per_band when default.yaml has no splits.c2_reserve_factor."""
SOURCES_BEGIN = "<!-- splits:begin -->"
SOURCES_END = "<!-- splits:end -->"
BOARD_SIZE = 19

EXPLAINER_PLAN: tuple[tuple[str, int], ...] = (("opening", 2), ("middle", 4), ("end", 2), ("only_move_candidate", 2))
PHASE_PLIES = {"opening": (10, 49), "middle": (50, 149), "end": (150, 200), "only_move_candidate": (20, 200)}
DECLARED_RANKS = (-19.0, 5.0)  # 20k to 5d on the numeric scale: the rank dropdown of the app (SCOPE.md, step 1)

Moves = tuple[tuple[str, str], ...]


# ----------------------------------------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Band:
    name: str
    profile: str = ""


@dataclass(frozen=True)
class SplitConfig:
    seed: int = DEFAULT_SEED
    source: str = "ogs"
    min_date: str = DEFAULT_MIN_DATE
    bands: tuple[Band, ...] = (
        Band("15k-10k", "rank_12k"),
        Band("9k-5k", "rank_7k"),
        Band("4k-1k", "rank_2k"),
        Band("1d-3d", "rank_2d"),
    )
    positions_per_band: int = 500  # candidates drawn per band: splits.c2_reserve_factor x c2_target, or the CLI value
    c2_target: int = 500  # positions the C2 analysis uses per band (C2.positions_per_band), whatever is drawn
    max_per_game: int = 5
    ply_min: int = 20
    ply_max: int = 200
    pilot_openings: int = 20
    openings: int = 200
    opening_plies: int = 12
    topic_games: int = 300
    explainer_plan: tuple[tuple[str, int], ...] = EXPLAINER_PLAN

    @property
    def explainer_total(self) -> int:
        return sum(n for _, n in self.explainer_plan)


def _dig(cfg: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(cfg, Mapping) or key not in cfg:
            return default
        cfg = cfg[key]
    return cfg


def _load_yaml(path: Path) -> dict[str, Any]:
    if not Path(path).is_file():
        raise FileNotFoundError(f"config file not found: {Path(path).resolve()}")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return dict(data) if isinstance(data, Mapping) else {}


def load_config(prereg_path: Path = PREREG, default_path: Path = DEFAULT_CONFIG) -> SplitConfig:
    """Split sizes and filters from configs/prereg.yaml and configs/default.yaml (built-in values for missing keys)."""
    base = SplitConfig()
    prereg = _load_yaml(prereg_path)
    default = _load_yaml(default_path) if Path(default_path).is_file() else {}
    bands = tuple(
        Band(str(b["name"]), str(b.get("profile", ""))) for b in (_dig(prereg, "C2", "bands", default=None) or [])
    )
    per_band = int(_dig(prereg, "C2", "positions_per_band", default=base.c2_target))
    factor = float(_dig(default, "splits", "c2_reserve_factor", default=DEFAULT_RESERVE_FACTOR))
    if factor < 1.0:
        raise ValueError(f"splits.c2_reserve_factor must be at least 1, got {factor}")
    return SplitConfig(
        seed=int(_dig(prereg, "seeds", "global", default=base.seed)),
        source=base.source,
        min_date=str(_dig(prereg, "C2", "game_date_min", default=base.min_date)),
        bands=bands or base.bands,
        positions_per_band=math.ceil(factor * per_band),
        c2_target=per_band,
        max_per_game=int(_dig(prereg, "C2", "max_positions_per_game", default=base.max_per_game)),
        ply_min=int(_dig(prereg, "C2", "ply_min", default=base.ply_min)),
        ply_max=int(_dig(prereg, "C2", "ply_max", default=base.ply_max)),
        pilot_openings=int(_dig(prereg, "openings", "pilot_n_openings", default=base.pilot_openings)),
        openings=int(_dig(prereg, "openings", "n_openings", default=base.openings)),
        opening_plies=int(_dig(prereg, "openings", "plies", default=base.opening_plies)),
        topic_games=int(_dig(default, "topics", "fit_games", default=base.topic_games)),
    )


def rng_for(seed: int, *label: str) -> random.Random:
    """A generator that depends only on the seed and the label, so one split's draw never shifts another's."""
    digest = hashlib.sha256(":".join([str(seed), *label]).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# ----------------------------------------------------------------------------------------------------------------------
# The pool of fetched games
# ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolGame:
    name: str
    date: str  # YYYY-MM-DD
    speed: str
    moves: Moves
    black_rank: str
    white_rank: str
    komi: float | None
    rules: str

    def rank_of(self, color: str) -> str:
        return self.black_rank if color == "B" else self.white_rank


def rank_number(label: str) -> float | None:
    try:
        return label_to_numeric(label)
    except ValueError:
        return None


def read_index(path: Path) -> tuple[list[dict[str, Any]], int]:
    """The rows of index.jsonl, one per game (the last line of a game wins), and the number of unreadable lines."""
    rows: dict[str, dict[str, Any]] = {}
    damaged = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            damaged += 1
            continue
        name = row.get("game") if isinstance(row, dict) else None
        if not isinstance(name, str) and isinstance(row, dict) and isinstance(row.get("id"), int):
            name = game_name(row["id"])
        if not isinstance(name, str):
            damaged += 1
            continue
        rows[name] = row
    ordered = sorted(
        rows.values(), key=lambda r: (r.get("id") if isinstance(r.get("id"), int) else 0, str(r.get("game")))
    )
    return ordered, damaged


def playable(rules: str, komi: float | None) -> bool:
    """Whether eval.splits.position_at can build a position from a game with these rules and this komi."""
    if komi is None:
        return False
    if katago_rules is None:
        return True
    try:
        katago_rules(rules)
    except ValueError:
        return False
    return True


def load_pool(index_path: Path, min_date: str, speeds: Sequence[str] = ()) -> tuple[list[PoolGame], Counter[str]]:
    """The usable games listed in the index, and why the others were left out (reason -> count)."""
    index_path = Path(index_path)
    if not index_path.is_file():
        raise FileNotFoundError(f"{index_path.resolve()} not found: run scripts/fetch_games.py first")
    rows, damaged = read_index(index_path)
    skipped: Counter[str] = Counter()
    if damaged:
        skipped["unreadable_index_line"] = damaged
    games: list[PoolGame] = []
    for row in rows:
        name = row.get("game") if isinstance(row.get("game"), str) else game_name(row["id"])
        path = index_path.parent / f"{name}.sgf"
        if not path.is_file():
            skipped["missing_sgf"] += 1
            continue
        try:
            rec = read_game(path.read_bytes())
        except (ValueError, OSError):
            skipped["unreadable_sgf"] += 1
            continue
        date = str(row.get("date") or rec.date or "")[:10]
        speed = str(row.get("speed") or "")
        if rec.size != BOARD_SIZE:
            skipped["not_19x19"] += 1
        elif rec.handicap or rec.setup:
            skipped["handicap_or_setup"] += 1
        elif not date:
            skipped["no_date"] += 1
        elif date < min_date:
            skipped["before_min_date"] += 1
        elif speeds and speed not in speeds:
            skipped["speed_not_selected"] += 1
        elif not playable(rec.rules, rec.komi):
            skipped["rules_or_komi"] += 1
        else:
            games.append(PoolGame(name, date, speed, rec.moves, rec.black_rank, rec.white_rank, rec.komi, rec.rules))
    return games, skipped


# ----------------------------------------------------------------------------------------------------------------------
# Go helpers: openings and forcing moves
# ----------------------------------------------------------------------------------------------------------------------


def opening_of(game: PoolGame, plies: int) -> tuple[str, ...] | None:
    """The first `plies` moves when they alternate B/W from Black, contain no pass and are legal under the C1 rules
    (GoGame: simple ko, suicide illegal, as eval.openings replays them), else None."""
    if len(game.moves) < plies:
        return None
    for i, (color, move) in enumerate(game.moves[:plies]):
        if color != ("B" if i % 2 == 0 else "W") or move == "pass":
            return None
    rules = GoGame()
    pos = rules.initial_position(size=BOARD_SIZE)
    try:
        for _, move in game.moves[:plies]:
            pos = rules.play(pos, move)
    except ValueError:
        return None
    return tuple(move for _, move in game.moves[:plies])


def canonical_opening(moves: Sequence[str], size: int = BOARD_SIZE) -> tuple[str, ...]:
    """The smallest of the eight rotations and reflections of an opening: equal openings compare equal."""
    points = [gtp_to_rc(m, size) for m in moves]
    last = size - 1
    variants = []
    for flip_rows in (False, True):
        for flip_cols in (False, True):
            for transpose in (False, True):
                seq = []
                for rc in points:
                    assert rc is not None
                    r, c = rc
                    r, c = (last - r if flip_rows else r), (last - c if flip_cols else c)
                    r, c = (c, r) if transpose else (r, c)
                    seq.append(rc_to_gtp((r, c), size))
                variants.append(tuple(seq))
    return min(variants)


def liberties(board: Mapping[str, str], start: str, size: int = BOARD_SIZE) -> int:
    """Number of liberties of the group that holds the stone at `start`."""
    color = board[start]
    seen = {start}
    stack = [start]
    empty: set[str] = set()
    while stack:
        point = stack.pop()
        rc = gtp_to_rc(point, size)
        assert rc is not None
        for nb in neighbors(rc, size):
            name = rc_to_gtp(nb, size)
            occupant = board.get(name)
            if occupant is None:
                empty.add(name)
            elif occupant == color and name not in seen:
                seen.add(name)
                stack.append(name)
    return len(empty)


def forcing_previous_move(game: PoolGame, ply: int) -> str | None:
    """'capture' or 'atari' when moves[ply - 1] took stones or left an adjacent enemy group with one liberty.

    Raises ValueError when the game record is not a legal game (for example a suicide under NZ or Ing rules).
    """
    if ply < 1 or ply > len(game.moves):
        return None
    color, move = game.moves[ply - 1]
    rc = gtp_to_rc(move, BOARD_SIZE)
    if rc is None:
        return None
    meta = {"size": BOARD_SIZE, "komi": game.komi if game.komi is not None else 7.5, "rules": game.rules or "chinese"}
    engine = GoGame()
    before = engine.board(Position(game="go", moves=game.moves[: ply - 1], to_move=color, meta=meta))
    after = engine.board(Position(game="go", moves=game.moves[:ply], to_move="W" if color == "B" else "B", meta=meta))
    if len(after) != len(before) + 1:
        return "capture"
    enemy = "W" if color == "B" else "B"
    for nb in neighbors(rc, BOARD_SIZE):
        name = rc_to_gtp(nb, BOARD_SIZE)
        if after.get(name) == enemy and liberties(after, name) == 1:
            return "atari"
    return None


# ----------------------------------------------------------------------------------------------------------------------
# Drawing the splits
# ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shortage:
    split: str  # for example "c2_positions 1d-3d"
    need: int
    have: int
    unit: str  # "positions" or "games"
    detail: str  # what the games have to look like
    more_games: int | None  # further games of that kind that would close the gap (None = no basis for an estimate)
    share: float | None  # share of the fetched games that are of that kind (None = not applicable)


class NotEnoughGames(Exception):
    def __init__(self, shortages: Sequence[Shortage], pool_size: int, index_path: Path) -> None:
        self.shortages = list(shortages)
        self.pool_size = pool_size
        self.index_path = index_path
        super().__init__(self.message())

    def fetch_target(self) -> int:
        """Games to have in total, as a rough estimate: the largest of the per-split estimates."""
        extra = 0
        for s in self.shortages:
            if s.more_games is None:
                extra = max(extra, 100, math.ceil(self.pool_size / 4))  # no basis for an estimate: a guess
            elif s.share:
                extra = max(extra, math.ceil(s.more_games / s.share))
            else:
                extra = max(extra, s.more_games)
        return self.pool_size + extra

    def message(self) -> str:
        lines = [f"Not enough games to fill the splits: {self.pool_size} usable games in {self.index_path.parent}."]
        for s in self.shortages:
            lines.append(f"  {s.split}: {s.have} of {s.need} {s.unit}.")
            if s.more_games is None:
                lines.append(f"      Need more games {s.detail}; none of the unused games qualifies, so no estimate.")
            elif s.share:
                lines.append(
                    f"      Need about {s.more_games} more games {s.detail}; they are {100 * s.share:.1f}% of the games"
                    f" fetched, so fetch about {math.ceil(s.more_games / s.share)} more games in total."
                )
            else:
                lines.append(f"      Need about {s.more_games} more games {s.detail}.")
        lines.append(
            f"Fetch more (resumable, same folder): python scripts/fetch_games.py --target {self.fetch_target()}"
        )
        lines.append("Nothing was written.")
        return "\n".join(lines)


@dataclass
class Drawn:
    """The outcome of one drawing: the splits, and what it took."""

    splits: dict[str, Any]
    games: dict[str, PoolGame]
    shortages: list[Shortage] = field(default_factory=list)
    band_pool: dict[str, tuple[int, int]] = field(
        default_factory=dict
    )  # band -> (candidate games, positions available)
    duplicate_openings: int = 0


def eligible_plies(game: PoolGame, band: str, bands: Sequence[str], cfg: SplitConfig) -> list[int]:
    """Plies of the game that are C2 positions of the band: the mover is in the band and plays a move, not a pass."""
    in_band = {color for color in ("B", "W") if band_of(game.rank_of(color), bands) == band}
    if not in_band:
        return []
    out = []
    for ply in range(cfg.ply_min, min(cfg.ply_max, len(game.moves) - 1) + 1):
        color, move = game.moves[ply]
        if move != "pass" and color in in_band:
            out.append(ply)
    return out


def draw_c2(
    games: Sequence[PoolGame], cfg: SplitConfig, used: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], list[Shortage], dict[str, tuple[int, int]]]:
    names = [b.name for b in cfg.bands]
    candidates: dict[str, list[tuple[PoolGame, list[int]]]] = {b: [] for b in names}
    for game in games:
        for band in names:
            plies = eligible_plies(game, band, names, cfg)
            if plies:
                candidates[band].append((game, plies))
    pool_info = {b: (len(c), sum(min(cfg.max_per_game, len(p)) for _, p in c)) for b, c in candidates.items()}

    positions: dict[str, list[dict[str, Any]]] = {b: [] for b in names}
    shortages: list[Shortage] = []
    for band in sorted(names, key=lambda b: (len(candidates[b]), names.index(b))):  # the scarcest band picks first
        rng = rng_for(cfg.seed, "c2", band)
        order = list(candidates[band])
        rng.shuffle(order)
        taken = 0
        games_used = 0
        for game, plies in order:
            if taken >= cfg.positions_per_band:
                break
            if game.name in used:
                continue
            k = min(cfg.max_per_game, len(plies), cfg.positions_per_band - taken)
            for ply in sorted(rng.sample(plies, k)):
                positions[band].append({"game": game.name, "ply": ply})
            used.add(game.name)
            taken += k
            games_used += 1
        if taken < cfg.positions_per_band:
            n_candidates, n_positions = pool_info[band]
            missing = cfg.positions_per_band - taken
            shortages.append(
                Shortage(
                    split=f"c2_positions {band}",
                    need=cfg.positions_per_band,
                    have=taken,
                    unit=f"positions (from {games_used} games, at most {cfg.max_per_game} per game)",
                    detail=f"with a player ranked {band}",
                    # None when no fetched game has such a player: there is nothing to base an estimate on
                    more_games=math.ceil(missing / max(n_positions / n_candidates, 1.0)) if n_candidates else None,
                    share=n_candidates / len(games) if n_candidates else None,
                )
            )
    return positions, shortages, pool_info


def _explainer_choice(
    game: PoolGame, phase: str, band: str | None, bands: Sequence[str], rng: random.Random
) -> tuple[int, str] | None:
    lo, hi = PHASE_PLIES[phase]
    usable = set()  # colours whose player can be a declared rank (and is in the wanted band)
    for color in ("B", "W"):
        number = rank_number(game.rank_of(color))
        if number is not None and DECLARED_RANKS[0] <= number <= DECLARED_RANKS[1]:
            if band is None or band_of(game.rank_of(color), bands) == band:
                usable.add(color)
    plies = [ply for ply in range(lo, min(hi, len(game.moves) - 1) + 1) if game.moves[ply][0] in usable]
    plies = [ply for ply in plies if game.moves[ply][1] != "pass"]
    rng.shuffle(plies)
    for ply in plies:
        if phase == "only_move_candidate":
            try:
                if forcing_previous_move(game, ply) is None:
                    continue
            except ValueError:
                return None  # the record is not a legal game under these rules: leave the game alone
        return ply, game.rank_of(game.moves[ply][0])
    return None


def draw_explainer(
    games: Sequence[PoolGame], cfg: SplitConfig, used: set[str]
) -> tuple[list[dict[str, Any]], list[Shortage]]:
    rng = rng_for(cfg.seed, "explainer")
    order = [g for g in games if g.name not in used]
    rng.shuffle(order)
    names = [b.name for b in cfg.bands]
    slots = [phase for phase, n in cfg.explainer_plan for _ in range(n)]
    chosen: list[dict[str, Any]] = []
    missing: Counter[str] = Counter()
    for i, phase in enumerate(slots):
        wanted: list[str | None] = [names[i % len(names)], None] if names else [None]
        found: tuple[PoolGame, int, str] | None = None
        for band in wanted:
            for game in order:
                if game.name in used:
                    continue
                hit = _explainer_choice(game, phase, band, names, rng)
                if hit is not None:
                    found = (game, hit[0], hit[1])
                    break
            if found:
                break
        if found is None:
            missing[phase] += 1
            continue
        game, ply, label = found
        used.add(game.name)
        chosen.append({"game": game.name, "ply": ply, "phase": phase, "declared_rank": label})
    shortages = [
        Shortage(
            split=f"explainer_positions {phase}",
            need=dict(cfg.explainer_plan)[phase],
            have=dict(cfg.explainer_plan)[phase] - n,
            unit="positions",
            detail=_phase_detail(phase),
            more_games=None,
            share=None,
        )
        for phase, n in missing.items()
    ]
    return chosen, shortages


def _phase_detail(phase: str) -> str:
    lo, hi = PHASE_PLIES[phase]
    extra = " where the previous move is a capture or an atari" if phase == "only_move_candidate" else ""
    return f"with a mover between 20k and 5d and a move at ply {lo} to {hi}{extra}"


def draw_openings(
    games: Sequence[PoolGame], cfg: SplitConfig, used: set[str]
) -> tuple[list[str], list[str], list[Shortage], int]:
    need = cfg.pilot_openings + cfg.openings
    rng = rng_for(cfg.seed, "openings")
    order = [g for g in games if g.name not in used]
    rng.shuffle(order)
    seen: set[tuple[str, ...]] = set()
    picked: list[str] = []
    duplicates = 0
    for game in order:
        if len(picked) >= need:
            break
        opening = opening_of(game, cfg.opening_plies)
        if opening is None:
            continue
        canon = canonical_opening(opening)
        if canon in seen:
            duplicates += 1
            continue
        seen.add(canon)
        picked.append(game.name)
        used.add(game.name)
    shortages = []
    if len(picked) < need:
        shortages.append(
            Shortage(
                split="openings (pilot + confirmatory)",
                need=need,
                have=len(picked),
                unit="games",
                detail=f"with {cfg.opening_plies} alternating non-pass opening moves that no other selected game has",
                more_games=need - len(picked),
                share=None,
            )
        )
    return picked[: cfg.pilot_openings], picked[cfg.pilot_openings :], shortages, duplicates


def draw_topic_games(games: Sequence[PoolGame], cfg: SplitConfig, used: set[str]) -> tuple[list[str], list[Shortage]]:
    rng = rng_for(cfg.seed, "topics")
    order = [g for g in games if g.name not in used]
    rng.shuffle(order)
    picked = [g.name for g in order[: cfg.topic_games]]
    used.update(picked)
    shortages = []
    if len(picked) < cfg.topic_games:
        shortages.append(
            Shortage(
                split="topic_games",
                need=cfg.topic_games,
                have=len(picked),
                unit="games",
                detail="of any rank",
                more_games=cfg.topic_games - len(picked),
                share=None,
            )
        )
    return picked, shortages


def raw_dir_text(index_path: Path) -> str:
    folder = Path(index_path).resolve().parent
    try:
        return folder.relative_to(ROOT).as_posix()
    except ValueError:
        return folder.as_posix()


def draw_splits(games: Sequence[PoolGame], cfg: SplitConfig, *, created: str, raw_dir: str) -> Drawn:
    """Draw every split from the pool. The result has `shortages` when some split could not be filled."""
    used: set[str] = set()
    c2, shortages, band_pool = draw_c2(games, cfg, used)
    explainer, more = draw_explainer(games, cfg, used)
    shortages += more
    pilot, confirmatory, more, duplicates = draw_openings(games, cfg, used)
    shortages += more
    topics, more = draw_topic_games(games, cfg, used)
    shortages += more
    splits = {
        "version": 1,
        "seed": cfg.seed,
        "source": cfg.source,
        "created": created,
        "raw_dir": raw_dir,
        "pilot_openings": pilot,
        "confirmatory_openings": confirmatory,
        "c2_positions": {b.name: c2[b.name] for b in cfg.bands},
        "topic_games": topics,
        "explainer_positions": explainer,
    }
    return Drawn(splits, {g.name: g for g in games}, shortages, band_pool, duplicates)


# ----------------------------------------------------------------------------------------------------------------------
# Output: splits.json, the summary and the SOURCES.md table
# ----------------------------------------------------------------------------------------------------------------------


JSON_WIDTH = 110  # lists of game names longer than this are wrapped onto several lines in splits.json


def _wrapped(items: Sequence[Any], indent: str) -> list[str]:
    """Fill lines with JSON values up to JSON_WIDTH wide; every line but the last ends with a comma."""
    rows: list[list[str]] = [[]]
    width = len(indent)
    for item in items:
        text = json.dumps(item, ensure_ascii=False)
        if rows[-1] and width + len(text) + 2 > JSON_WIDTH:
            rows.append([])
            width = len(indent)
        rows[-1].append(text)
        width += len(text) + 2
    return [indent + ", ".join(row) + ("," if n < len(rows) - 1 else "") for n, row in enumerate(rows)]


def dump_splits(splits: Mapping[str, Any]) -> str:
    """JSON with one line per position and wrapped lists of names, so that the file diffs and reads well."""

    def compact(value: Any) -> str:
        return json.dumps(value, separators=(", ", ": "), ensure_ascii=False)

    lines = ["{"]
    keys = list(splits)
    for n, key in enumerate(keys):
        value = splits[key]
        comma = "," if n < len(keys) - 1 else ""
        if key == "c2_positions":
            lines.append(f'  "{key}": {{')
            bands = list(value)
            for m, band in enumerate(bands):
                items = value[band]
                tail = "," if m < len(bands) - 1 else ""
                if not items:
                    lines.append(f'    "{band}": []{tail}')
                    continue
                lines.append(f'    "{band}": [')
                lines += [f"      {compact(p)}{',' if i < len(items) - 1 else ''}" for i, p in enumerate(items)]
                lines.append(f"    ]{tail}")
            lines.append(f"  }}{comma}")
        elif key == "explainer_positions":
            if not value:
                lines.append(f'  "{key}": []{comma}')
                continue
            lines.append(f'  "{key}": [')
            lines += [f"    {compact(p)}{',' if i < len(value) - 1 else ''}" for i, p in enumerate(value)]
            lines.append(f"  ]{comma}")
        else:
            one_line = f'  "{key}": {compact(value)}{comma}'
            if isinstance(value, list) and len(one_line) > JSON_WIDTH:
                lines.append(f'  "{key}": [')
                lines += _wrapped(value, "    ")
                lines.append(f"  ]{comma}")
            else:
                lines.append(one_line)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def sorted_labels(labels: Iterable[str]) -> list[str]:
    unique = set(labels)
    return sorted(unique, key=lambda s: (rank_number(s) is None, rank_number(s) or 0.0, s))


def _date_range(dates: Iterable[str]) -> str:
    ds = sorted(d for d in dates if d)
    return f"{ds[0]} to {ds[-1]}" if ds else "n/a"


@dataclass(frozen=True)
class SplitRow:
    name: str
    target: int
    drawn: int
    games: int
    dates: str


def split_rows(drawn: Drawn, cfg: SplitConfig) -> list[SplitRow]:
    s, g = drawn.splits, drawn.games

    def row(name: str, target: int, drawn_n: int, names: Iterable[str]) -> SplitRow:
        unique = sorted(set(names))
        return SplitRow(name, target, drawn_n, len(unique), _date_range(g[n].date for n in unique))

    rows = [
        row("pilot_openings", cfg.pilot_openings, len(s["pilot_openings"]), s["pilot_openings"]),
        row("confirmatory_openings", cfg.openings, len(s["confirmatory_openings"]), s["confirmatory_openings"]),
    ]
    for band in cfg.bands:
        items = s["c2_positions"][band.name]
        rows.append(
            row(
                f"c2_positions {band.name} ({band.profile or 'no profile'})",
                cfg.positions_per_band,
                len(items),
                [p["game"] for p in items],
            )
        )
    rows.append(row("topic_games", cfg.topic_games, len(s["topic_games"]), s["topic_games"]))
    rows.append(
        row(
            "explainer_positions",
            cfg.explainer_total,
            len(s["explainer_positions"]),
            [p["game"] for p in s["explainer_positions"]],
        )
    )
    return rows


def reserve_note(cfg: SplitConfig) -> str:
    """One sentence on how the number of C2 candidates per band relates to what the C2 analysis uses."""
    drawn, used = cfg.positions_per_band, cfg.c2_target
    if drawn > used:
        return (
            f"C2 candidates per band: {drawn}, which is {drawn - used} more than the {used} positions analysed;"
            f" eval/c2_relevance.py keeps the first {used} (in list order) that pass its engine checks."
        )
    if drawn == used:
        return (
            f"C2 candidates per band: {drawn}, no reserve. eval/c2_relevance.py records a deviation when fewer than"
            f" {used} of them pass its engine checks (an admissible set of at least C2.min_admissible moves);"
            " --positions-per-band N draws N candidates per band to leave a reserve."
        )
    return f"C2 candidates per band: {drawn}, fewer than the {used} that C2.positions_per_band asks for."


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def sources_block(drawn: Drawn, cfg: SplitConfig, skipped: Mapping[str, int]) -> list[str]:
    pool = list(drawn.games.values())
    lines = [
        f"Recorded by `python scripts/make_splits.py` (created {drawn.splits['created']}, seed {cfg.seed}) from"
        f" {len(pool)} usable games fetched from {drawn.splits['source']} ({_date_range(g.date for g in pool)};"
        f" minimum date {cfg.min_date}).",
        "",
        *_table(
            ["Split", "Target", "Drawn", "Games", "Game dates"],
            [(r.name, r.target, r.drawn, r.games, r.dates) for r in split_rows(drawn, cfg)],
        ),
    ]
    lines += ["", reserve_note(cfg)]
    if skipped:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(skipped.items()))
        lines += ["", f"Games in the index that were left out: {parts}."]
    return lines


def summary_markdown(drawn: Drawn, cfg: SplitConfig, skipped: Mapping[str, int], speeds: Sequence[str]) -> str:
    s, games = drawn.splits, drawn.games
    pool = list(games.values())
    out = [
        "# Splits summary",
        "",
        "Written by `python scripts/make_splits.py`; do not edit by hand. The games themselves are in `data/raw/`"
        " (not committed); `data/splits.json` holds only game IDs and plies.",
        "",
        f"- created: {s['created']}; seed: {s['seed']} (`seeds.global`); source: {s['source']};"
        f" raw_dir: `{s['raw_dir']}`",
        f"- games read: {len(pool)}, dated {_date_range(g.date for g in pool)}"
        f" (minimum date `C2.game_date_min` = {cfg.min_date})",
        f"- speeds: {', '.join(speeds) if speeds else 'all'}",
        f"- {reserve_note(cfg)}",
        f"- left out while reading the index: {', '.join(f'{k}={v}' for k, v in sorted(skipped.items())) or 'nothing'}",
        "- openings skipped because an earlier pick was the same up to rotation or reflection:"
        f" {drawn.duplicate_openings}",
        "",
        "## Counts per split",
        "",
        *_table(
            ["Split", "Target", "Drawn", "Games", "Game dates"],
            [(r.name, r.target, r.drawn, r.games, r.dates) for r in split_rows(drawn, cfg)],
        ),
        "",
        "## C2 positions per band",
        "",
        f"A position is the board before move index `ply` ({cfg.ply_min} <= ply <= {cfg.ply_max}); the mover's"
        f" at-game-time rank is inside the band; at most {cfg.max_per_game} positions per game; no game in two bands.",
        "",
    ]
    c2_rows = []
    for band in cfg.bands:
        items = s["c2_positions"][band.name]
        by_game = Counter(p["game"] for p in items)
        movers = sorted_labels(games[p["game"]].rank_of(games[p["game"]].moves[p["ply"]][0]) for p in items)
        plies = [p["ply"] for p in items]
        pool_games, pool_positions = drawn.band_pool.get(band.name, (0, 0))
        c2_rows.append(
            (
                band.name,
                band.profile,
                len(items),
                len(by_game),
                f"{len(items) / len(by_game):.1f}" if by_game else "n/a",
                f"{movers[0]} to {movers[-1]}" if movers else "n/a",
                f"{min(plies)} / {int(statistics.median(plies))} / {max(plies)}" if plies else "n/a",
                f"{pool_games} / {pool_positions}",
            )
        )
    out += _table(
        [
            "Band",
            "HumanSL profile",
            "Positions",
            "Games",
            "Per game",
            "Mover ranks",
            "Ply min / median / max",
            "Pool games / positions",
        ],
        c2_rows,
    )
    out += ["", "## Explainer positions", ""]
    out += _table(
        ["Game", "Ply", "Phase", "Declared rank"],
        [(p["game"], p["ply"], p["phase"], p["declared_rank"]) for p in s["explainer_positions"]],
    )

    def players(names: Iterable[str]) -> list[str]:
        return [label for n in names for label in (games[n].black_rank, games[n].white_rank)]

    columns: dict[str, list[str]] = {
        "pool (players)": players(games),
        "openings (players)": players([*s["pilot_openings"], *s["confirmatory_openings"]]),
        "topic games (players)": players(s["topic_games"]),
        "C2 positions (movers)": [
            games[p["game"]].rank_of(games[p["game"]].moves[p["ply"]][0]) for b in s["c2_positions"].values() for p in b
        ],
        "explainer (movers)": [p["declared_rank"] for p in s["explainer_positions"]],
    }
    counts = {name: Counter(labels) for name, labels in columns.items()}
    labels = sorted_labels(label for labels in columns.values() for label in labels)
    out += ["", "## Rank distribution (at-game-time labels)", ""]
    out += _table(["Rank", *columns], [(label, *(counts[name][label] for name in columns)) for label in labels])
    out += [
        "",
        "## Reproduce",
        "",
        "```",
        "python scripts/fetch_games.py --ids-from data/splits.json   # re-fetch exactly these games",
        "python scripts/make_splits.py --dry-run                     # check that the games on disk are enough",
        "```",
    ]
    return "\n".join(out) + "\n"


def update_sources(path: Path, block: Sequence[str]) -> bool:
    """Replace the text between the splits markers of SOURCES.md. False when the file or the markers are missing."""
    path = Path(path)
    if not path.is_file():
        return False
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    begin, end = text.find(SOURCES_BEGIN), text.find(SOURCES_END)
    if begin < 0 or end < begin:
        return False
    new = text[: begin + len(SOURCES_BEGIN)] + "\n" + "\n".join(block) + "\n" + text[end:]
    path.write_bytes(new.replace("\n", newline).encode("utf-8"))
    return True


# ----------------------------------------------------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", default=str(DEFAULT_INDEX), help="index.jsonl of fetch_games.py (SGFs sit next to it)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="splits file to write (default data/splits.json)")
    ap.add_argument("--summary", help="summary file to write (default: splits_summary.md next to --out)")
    ap.add_argument("--sources", help="SOURCES.md whose splits table is updated (default: SOURCES.md next to --out)")
    ap.add_argument("--no-update-sources", action="store_true", help="leave SOURCES.md alone")
    ap.add_argument("--prereg", default=str(PREREG), help="pre-registered parameters (default configs/prereg.yaml)")
    ap.add_argument("--default-config", default=str(DEFAULT_CONFIG), help="runtime settings (topics.fit_games)")
    ap.add_argument("--seed", type=int, help="seed (default seeds.global in the prereg file)")
    ap.add_argument("--speeds", help="comma-separated game speeds to keep, e.g. live,rapid,blitz (default: all)")
    ap.add_argument(
        "--positions-per-band",
        type=int,
        help="C2 candidates per band (default C2.positions_per_band; more leaves a reserve for candidates that"
        " the engine checks of eval/c2_relevance.py reject)",
    )
    ap.add_argument("--created", help="timestamp to record (default: now); fixes the file for byte-wise comparisons")
    ap.add_argument(
        "--force", action="store_true", help="overwrite an existing splits file (never after a C1 or C2 run)"
    )
    ap.add_argument("--dry-run", action="store_true", help="draw and report, write nothing")
    return ap


def report_lines(drawn: Drawn, cfg: SplitConfig, skipped: Mapping[str, int], pool_size: int) -> list[str]:
    lines = [f"Pool: {pool_size} usable games" + (f"; left out: {dict(skipped)}" if skipped else "")]
    for band in cfg.bands:
        games_n, positions_n = drawn.band_pool.get(band.name, (0, 0))
        lines.append(
            f"  band {band.name}: {games_n} games with an eligible mover, {positions_n} positions at most"
            f" {cfg.max_per_game} per game"
        )
    for r in split_rows(drawn, cfg):
        lines.append(f"  {r.name}: {r.drawn} of {r.target} ({r.games} games, {r.dates})")
    lines.append(reserve_note(cfg))
    return lines


def main(
    argv: Sequence[str] | None = None,
    *,
    log: Callable[[str], None] = print,
    now: Callable[[], datetime] = datetime.now,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(Path(args.prereg), Path(args.default_config))
    except (FileNotFoundError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        log(f"error: cannot read the configuration: {exc}")
        return 2
    changes: dict[str, Any] = {}
    if args.seed is not None:
        changes["seed"] = args.seed
    if args.positions_per_band is not None:
        changes["positions_per_band"] = args.positions_per_band
    if changes:
        cfg = replace(cfg, **changes)
    speeds = [s.strip() for s in (args.speeds or "").split(",") if s.strip()]
    out_path = Path(args.out)
    if out_path.exists() and not args.force and not args.dry_run:
        log(
            f"error: {out_path} already exists. The splits are drawn once and never regenerated after an"
            " evaluation run; use --force only before any C1 or C2 run."
        )
        return 2
    index_path = Path(args.index)
    try:
        games, skipped = load_pool(index_path, cfg.min_date, speeds)
    except FileNotFoundError as exc:
        log(f"error: {exc}")
        return 2
    created = args.created or now().astimezone().isoformat(timespec="seconds")
    drawn = draw_splits(games, cfg, created=created, raw_dir=raw_dir_text(index_path))
    for line in report_lines(drawn, cfg, skipped, len(games)):
        log(line)
    if drawn.shortages:
        log(str(NotEnoughGames(drawn.shortages, len(games), index_path)))
        return 2
    check_disjoint(drawn.splits)
    missing_keys = [k for k in SPLIT_KEYS if k not in drawn.splits]
    if missing_keys:  # cannot happen; guards against a schema change in eval/splits.py
        log(f"error: the splits lack {missing_keys}")
        return 2
    if args.dry_run:
        log("Dry run: the games on disk are enough. Nothing was written.")
        return 0
    _write_text(out_path, dump_splits(drawn.splits))
    summary_path = Path(args.summary) if args.summary else out_path.with_name("splits_summary.md")
    _write_text(summary_path, summary_markdown(drawn, cfg, skipped, speeds))
    log(f"Wrote {out_path} and {summary_path}.")
    if not args.no_update_sources:
        sources_path = Path(args.sources) if args.sources else out_path.with_name("SOURCES.md")
        if update_sources(sources_path, sources_block(drawn, cfg, skipped)):
            log(f"Updated the splits table in {sources_path}.")
        elif args.sources:
            log(
                f"warning: {sources_path} has no {SOURCES_BEGIN} ... {SOURCES_END} block; the counts were not recorded."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
