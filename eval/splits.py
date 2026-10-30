"""The data splits: game IDs and plies in data/splits.json (committed), game records in data/raw/ (not committed).

Schema of data/splits.json (written once by scripts/make_splits.py, never regenerated after an evaluation run):

    {
      "version": 1,
      "seed": 20261027,
      "source": "ogs",
      "created": "2026-10-30T10:00:00+08:00",
      "raw_dir": "data/raw/ogs",
      "pilot_openings": ["ogs-123", ...],                       # openings.pilot_n_openings game IDs
      "confirmatory_openings": ["ogs-456", ...],                # openings.n_openings game IDs, disjoint from pilot
      "c2_positions": {"15k-10k": [{"game": "ogs-789", "ply": 57}, ...], ...},   # keys = C2.bands names
      "topic_games": ["ogs-...", ...],                          # topics.fit_games game IDs
      "explainer_positions": [{"game": "ogs-...", "ply": 30, "phase": "opening", "declared_rank": "10k"}, ...]
    }

A game ID names the file `<raw_dir>/<game>.sgf`. A ply p means the position before move index p (0-based), so the
move played there is moves[p] and its mover is the SGF's player for that move. Splits never share a game.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from sensei.interfaces import Move, Position
from sensei.sgf import GameRecord, read_game

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPLITS = ROOT / "data" / "splits.json"
SPLIT_KEYS = ("pilot_openings", "confirmatory_openings", "c2_positions", "topic_games", "explainer_positions")


def load_splits(path: str | Path = DEFAULT_SPLITS) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in SPLIT_KEYS if k not in data]
    if missing:
        raise ValueError(f"{path} lacks {', '.join(missing)}")
    return data


def games_in(splits: Mapping[str, Any]) -> dict[str, set[str]]:
    """Game IDs per split (C2 bands merged), for the disjointness check."""
    out = {
        "pilot_openings": set(splits["pilot_openings"]),
        "confirmatory_openings": set(splits["confirmatory_openings"]),
        "topic_games": set(splits["topic_games"]),
        "explainer_positions": {p["game"] for p in splits["explainer_positions"]},
        "c2_positions": {p["game"] for band in splits["c2_positions"].values() for p in band},
    }
    return out


def check_disjoint(splits: Mapping[str, Any]) -> None:
    """Raise ValueError if any two splits share a game."""
    sets = games_in(splits)
    names = list(sets)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            shared = sets[a] & sets[b]
            if shared:
                raise ValueError(f"splits {a} and {b} share {len(shared)} games, e.g. {sorted(shared)[:3]}")


def raw_dir(splits: Mapping[str, Any], root: Path = ROOT) -> Path:
    d = Path(splits.get("raw_dir", "data/raw/ogs"))
    return d if d.is_absolute() else root / d


@lru_cache(maxsize=4096)
def _record(path: str, mtime_ns: int, size: int) -> GameRecord:
    """Parsed record, cached per file version (a rewritten file is parsed again)."""
    return read_game(Path(path).read_bytes())


def game_record(game: str, directory: Path) -> GameRecord:
    path = Path(directory) / f"{game}.sgf"
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing: run scripts/fetch_games.py --ids-from data/splits.json")
    st = path.stat()
    return _record(str(path), st.st_mtime_ns, st.st_size)


def opening_moves(game: str, plies: int, directory: Path) -> tuple[tuple[str, Move], ...]:
    """The first `plies` moves of a game, Black first and alternating, no passes (checked)."""
    rec = game_record(game, directory)
    moves = tuple(rec.moves[:plies])
    if len(moves) < plies:
        raise ValueError(f"{game} has only {len(rec.moves)} moves, fewer than the {plies}-ply opening")
    for i, (color, move) in enumerate(moves):
        if color != ("B" if i % 2 == 0 else "W") or move == "pass":
            raise ValueError(f"{game} move {i + 1} ({color} {move}) breaks the alternating no-pass opening")
    return moves


# SGF RU values (lowercased) -> KataGo rules presets (KataGo cpp/game/rules.cpp, parseRulesHelper, master, read
# 2026-10-29; it accepts all six OGS rule names, "goe" and "ing" as the Ing preset).
KATAGO_RULES = {
    "": "chinese",
    "chinese": "chinese",
    "cn": "chinese",
    "japanese": "japanese",
    "jp": "japanese",
    "korean": "korean",
    "kr": "korean",
    "aga": "aga",
    "bga": "bga",
    "new zealand": "new-zealand",
    "new-zealand": "new-zealand",
    "nz": "new-zealand",
    "tromp-taylor": "tromp-taylor",
    "tromp taylor": "tromp-taylor",
    "tt": "tromp-taylor",
    "ing": "ing",
    "goe": "ing",
}


def katago_rules(sgf_rules: str) -> str:
    """KataGo's preset for an SGF RU value; ValueError for rules that have no KataGo preset."""
    key = (sgf_rules or "").strip().lower()
    if key not in KATAGO_RULES:
        raise ValueError(f"rules {sgf_rules!r} have no KataGo preset")
    return KATAGO_RULES[key]


@dataclass(frozen=True)
class HumanPosition:
    game: str
    ply: int
    position: Position
    played: Move
    mover_rank: str | None


def position_at(game: str, ply: int, directory: Path) -> HumanPosition:
    """The position before move index `ply` under the game's own rules and komi, and the move a human played.

    Rules are mapped to KataGo's presets (`katago_rules`); a game without komi or with unsupported rules raises
    ValueError, so the caller can skip it.
    """
    rec = game_record(game, directory)
    if not 0 <= ply < len(rec.moves):
        raise ValueError(f"{game} has {len(rec.moves)} moves; ply {ply} is out of range")
    if rec.komi is None:
        raise ValueError(f"{game} has no komi")
    color, played = rec.moves[ply]
    meta = {"size": rec.size, "komi": rec.komi, "rules": katago_rules(rec.rules)}
    pos = Position(game="go", moves=tuple(rec.moves[:ply]), to_move=color, meta=meta)
    rank = rec.black_rank if color == "B" else rec.white_rank
    return HumanPosition(game, ply, pos, played, rank)
