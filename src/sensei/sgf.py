"""SGF game records through sgfmill.

sgfmill reports points as (row, col) with row 0 at the BOTTOM of the board; `sensei.coords` has row 0 at the
top. The conversion happens here and nowhere else (CLAUDE.md). Moves come out as GTP coordinates ("Q16",
"pass"); only the main line is read, variations are ignored.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sgfmill import sgf as sgfmill_sgf

from sensei.coords import GO_COLUMNS, PASS, rc_to_gtp
from sensei.interfaces import Color, Move

logger = logging.getLogger(__name__)

MAX_SIZE = len(GO_COLUMNS)


@dataclass(frozen=True)
class GameRecord:
    """One SGF game. Missing properties are tolerated: None (komi), 0 (handicap) or "" (text fields).

    moves: the main line as (color, GTP move) with color "B" or "W" and "pass" for a pass.
    setup: AB/AW stones placed on the root node before the first move (handicap placements), as (color, move).
    rules: the RU property lowercased ("chinese", "japanese", ...). Ranks keep the SGF strings ("5k").
    """

    moves: tuple[tuple[Color, Move], ...]
    size: int
    komi: float | None
    rules: str
    handicap: int
    black_rank: str
    white_rank: str
    date: str
    setup: tuple[tuple[Color, Move], ...] = ()


def sgf_point_to_gtp(point: tuple[int, int] | None, size: int) -> Move:
    """sgfmill (row, col) with row 0 at the bottom -> GTP; None -> "pass"."""
    if point is None:
        return PASS
    row, col = point
    return rc_to_gtp((size - 1 - row, col), size)


def _text(node: sgfmill_sgf.Tree_node, identifier: str) -> str:
    """A text property of node, stripped; "" when it is absent or cannot be decoded."""
    try:
        value = node.get(identifier)
    except (KeyError, ValueError):
        return ""
    return str(value).strip()


def read_game(data: bytes) -> GameRecord:
    """Parse one SGF game from bytes. Raises ValueError when sgfmill cannot parse it or the board is unsupported."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        game = sgfmill_sgf.Sgf_game.from_bytes(bytes(data))
    except LookupError as exc:  # sgfmill raises ValueError itself; an unknown CA encoding can surface as LookupError
        raise ValueError(f"unparsable SGF: {exc}") from exc
    size = int(game.get_size())
    if not 1 <= size <= MAX_SIZE:
        raise ValueError(f"unsupported board size {size} (at most {MAX_SIZE})")
    root = game.get_root()

    moves: list[tuple[Color, Move]] = []
    for index, node in enumerate(game.get_main_sequence()):
        if index > 0 and node.has_setup_stones():
            raise ValueError("setup stones outside the root node are not supported")
        color, point = node.get_move()
        if color is None:
            continue
        moves.append((color.upper(), sgf_point_to_gtp(point, size)))

    black_setup, white_setup, _cleared = root.get_setup_stones()
    setup: list[tuple[Color, Move]] = []
    for color, points in (("B", black_setup), ("W", white_setup)):
        for point in sorted(points, key=lambda rc: (size - 1 - rc[0], rc[1])):
            setup.append((color, sgf_point_to_gtp(point, size)))

    komi = float(game.get_komi()) if root.has_property("KM") else None
    handicap = game.get_handicap() or 0  # sgfmill gives None for a missing HA or HA[0]
    return GameRecord(
        moves=tuple(moves),
        size=size,
        komi=komi,
        rules=_text(root, "RU").lower(),
        handicap=int(handicap),
        black_rank=_text(root, "BR"),
        white_rank=_text(root, "WR"),
        date=_text(root, "DT"),
        setup=tuple(setup),
    )


def iter_sgf_files(directory: str | Path) -> Iterator[tuple[Path, GameRecord]]:
    """Yield (path, GameRecord) for every .sgf file under directory (recursive, sorted by path).

    Files that cannot be read or parsed are skipped with a logged warning.
    """
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")
    paths = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".sgf")
    for path in paths:
        try:
            record = read_game(path.read_bytes())
        except (ValueError, OSError) as exc:
            logger.warning("skipping %s: %s", path, exc)
            continue
        yield path, record
