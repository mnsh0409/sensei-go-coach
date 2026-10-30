"""Opening sets for C1 from data/splits.json (eval/README.md, "Modules"; SCOPE.md, shared settings).

An opening is the first `openings.plies` moves of one public game in the split, Black first and alternating, no
passes, replayed under the pre-registered rules (`game` in configs/prereg.yaml) to prove every move is legal there.
The pilot uses `openings.pilot_n_openings` games of `pilot_openings`, the confirmatory run the first
`openings.n_openings` games of `confirmatory_openings` (the list order is the seeded order of data/splits.json, so
a lowered n_openings keeps a prefix). Games with setup stones or a handicap, or of another board size, are rejected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.hardware import sha256_file
from eval.splits import DEFAULT_SPLITS, check_disjoint, game_record, load_splits, opening_moves, raw_dir
from sensei.game_go import GoGame
from sensei.interfaces import Color, Move, Position

SPLIT_KEYS = {"pilot": "pilot_openings", "confirmatory": "confirmatory_openings"}
COUNT_KEYS = {"pilot": "pilot_n_openings", "confirmatory": "n_openings"}


@dataclass(frozen=True)
class Opening:
    """Opening number `index` (0-based, in split order): the first plies of game `game`."""

    index: int
    game: str
    moves: tuple[tuple[Color, Move], ...]


def initial_position(cfg: Mapping[str, Any]) -> Position:
    """The empty board under the pre-registered size, komi and rules."""
    g = cfg["game"]
    return GoGame().initial_position(size=int(g["board_size"]), komi=float(g["komi"]), rules=str(g["rules"]))


def replay(cfg: Mapping[str, Any], moves: tuple[tuple[Color, Move], ...]) -> Position:
    """The position after `moves` from the pre-registered empty board; ValueError on an illegal or out-of-turn move."""
    game = GoGame()
    pos = initial_position(cfg)
    for color, move in moves:
        if color != pos.to_move:
            raise ValueError(f"{color} {move} is out of turn ({pos.to_move} to move)")
        pos = game.play(pos, move)
    return pos


def load_openings(
    cfg: Mapping[str, Any],
    which: str,
    *,
    splits_path: str | Path = DEFAULT_SPLITS,
    directory: str | Path | None = None,
) -> tuple[list[Opening], dict[str, Any]]:
    """(openings, info) for `which` in ("pilot", "confirmatory").

    `info` records the split, how many IDs it holds and how many are used, the plies, the number of distinct move
    sequences (two games may share a joseki) and the SHA-256 of the splits file, for the result's details.
    """
    if which not in SPLIT_KEYS:
        raise ValueError(f"which must be one of {sorted(SPLIT_KEYS)}, got {which!r}")
    splits_path = Path(splits_path)
    splits = load_splits(splits_path)
    check_disjoint(splits)
    key = SPLIT_KEYS[which]
    ids = [str(g) for g in splits[key]]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{splits_path} {key} lists a game twice")
    n = int(cfg["openings"][COUNT_KEYS[which]])
    if len(ids) < n:
        raise ValueError(f"{splits_path} {key} has {len(ids)} games; openings.{COUNT_KEYS[which]} needs {n}")
    plies = int(cfg["openings"]["plies"])
    size = int(cfg["game"]["board_size"])
    directory = Path(directory) if directory is not None else raw_dir(splits)

    openings: list[Opening] = []
    for index, game_id in enumerate(ids[:n]):
        rec = game_record(game_id, directory)
        if rec.size != size:
            raise ValueError(f"{game_id} is {rec.size}x{rec.size}; the openings are {size}x{size}")
        if rec.setup or rec.handicap:
            raise ValueError(f"{game_id} has setup stones or a handicap; openings come from even games only")
        moves = opening_moves(game_id, plies, directory)
        try:
            replay(cfg, moves)
        except ValueError as err:
            raise ValueError(f"{game_id}: opening illegal under the pre-registered rules: {err}") from err
        openings.append(Opening(index=index, game=game_id, moves=moves))

    info = {
        "split": key,
        "available": len(ids),
        "used": n,
        "plies": plies,
        "distinct_sequences": len({o.moves for o in openings}),
        "splits_file": splits_path.name,
        "splits_sha256": sha256_file(splits_path),
    }
    return openings, info
