"""Go rules for Sensei: `GoGame` implements the `Game` protocol from `sensei.interfaces`.

Rules are the ones KataGo applies under its `chinese` preset: simple ko, suicide illegal, area scoring
(verified in KataGo `cpp/game/rules.cpp`, `parseRulesHelper`, 2026-10-29; see CLAUDE.md). Scoring is delegated
to the engine, so `result()` always returns None. Two consecutive passes end the game. The `rules` string in
`pos.meta` is passed through to the engine and does not change what this module enforces.

Coordinates are GTP strings ("Q16", "pass") through `sensei.coords`. Internally a board is a row-major `bytes`
object from the top left (0 empty, 1 Black, 2 White), the same order as KataGo's ownership array.

A `Position` carries its full move list, so the board of a position is replayed from its moves. A small LRU
cache keyed on `(pos.moves, size)` keeps the replayed board of recent positions: a game played move by move
through `play` costs one incremental update per turn, and a position built directly from a move list (an SGF
replay) is replayed from the longest cached prefix. Standard library only.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from functools import cache
from typing import Any

from sensei.coords import GO_COLUMNS, PASS, gtp_to_rc, rc_to_gtp
from sensei.interfaces import Color, Move, Position

EMPTY, BLACK, WHITE = 0, 1, 2
_CODE = {"B": BLACK, "W": WHITE}
_LETTER = {BLACK: "B", WHITE: "W"}
_OTHER = {"B": "W", "W": "B"}
_CACHE_SIZE = 1024

_MoveList = tuple[tuple[Color, Move], ...]
_CacheKey = tuple[_MoveList, int]


# --------------------------------------------------------------------------------------------------------------
# Board geometry (cached per board size)
# --------------------------------------------------------------------------------------------------------------


@cache
def _neighbor_table(size: int) -> tuple[tuple[int, ...], ...]:
    """Neighbour indices of every point, in row-major index order."""
    table = []
    for row in range(size):
        for col in range(size):
            out = []
            if row > 0:
                out.append((row - 1) * size + col)
            if row < size - 1:
                out.append((row + 1) * size + col)
            if col > 0:
                out.append(row * size + col - 1)
            if col < size - 1:
                out.append(row * size + col + 1)
            table.append(tuple(out))
    return tuple(table)


@cache
def _names(size: int) -> tuple[str, ...]:
    """GTP name of every point, in row-major index order (A19 first on 19x19)."""
    return tuple(rc_to_gtp((i // size, i % size), size) for i in range(size * size))


@cache
def _star_points(size: int) -> frozenset[int]:
    if size < 7 or size % 2 == 0:
        return frozenset()
    edge = 3 if size >= 13 else 2
    mid = size // 2
    far = size - 1 - edge
    if size >= 13:
        points = {(r, c) for r in (edge, mid, far) for c in (edge, mid, far)}
    else:
        points = {(edge, edge), (edge, far), (far, edge), (far, far), (mid, mid)}
    return frozenset(r * size + c for r, c in points)


# --------------------------------------------------------------------------------------------------------------
# Board state and the rules
# --------------------------------------------------------------------------------------------------------------


class _State:
    """Board after a move sequence.

    `ko` is the point that `ko_color` may not play on the next move (-1 when there is no ko ban);
    `prisoners` counts the stones captured by Black and by White; `legal` caches the legal move tuple per colour.
    """

    __slots__ = ("board", "ko", "ko_color", "legal", "prisoners")

    def __init__(self, board: bytes, ko: int, ko_color: Color | None, prisoners: tuple[int, int]) -> None:
        self.board = board
        self.ko = ko
        self.ko_color = ko_color
        self.prisoners = prisoners
        self.legal: dict[Color, tuple[Move, ...]] = {}


@cache
def _empty_state(size: int) -> _State:
    return _State(bytes(size * size), -1, None, (0, 0))


def _group(board: bytes | bytearray, start: int, nbrs: tuple[tuple[int, ...], ...]) -> tuple[list[int], set[int]]:
    """Stones of the group containing `start` (an occupied point) and the set of its liberties."""
    color = board[start]
    stones = [start]
    seen = {start}
    libs: set[int] = set()
    for s in stones:  # appending while iterating is deliberate: the loop visits every stone added to the group
        for n in nbrs[s]:
            v = board[n]
            if v == EMPTY:
                libs.add(n)
            elif v == color and n not in seen:
                seen.add(n)
                stones.append(n)
    return stones, libs


def _check_color(color: Any) -> None:
    if color not in _CODE:
        raise ValueError(f"bad colour {color!r}: expected 'B' or 'W'")


def _apply(state: _State, color: Color, move: Move, size: int) -> tuple[_State, Move]:
    """State after `color` plays `move`, with the canonical spelling of the move.

    Raises ValueError for an off-board point, an occupied point, a simple-ko retake or suicide.
    """
    _check_color(color)
    try:
        rc = gtp_to_rc(move, size)
    except ValueError as err:
        raise ValueError(f"illegal move {move!r} for {color}: not a point on the {size}x{size} board") from err
    if rc is None:
        return _State(state.board, -1, None, state.prisoners), PASS
    idx = rc[0] * size + rc[1]
    name = _names(size)[idx]
    board = state.board
    if board[idx] != EMPTY:
        raise ValueError(f"illegal move {name} for {color}: the point is occupied")
    if idx == state.ko and color == state.ko_color:
        raise ValueError(f"illegal move {name} for {color}: simple ko, the ko cannot be retaken immediately")
    me = _CODE[color]
    them = BLACK + WHITE - me
    nbrs = _neighbor_table(size)
    new = bytearray(board)
    new[idx] = me
    captured: list[int] = []
    for n in nbrs[idx]:
        if new[n] == them:
            stones, libs = _group(new, n, nbrs)
            if not libs:
                for s in stones:
                    new[s] = EMPTY
                captured.extend(stones)
    own, libs = _group(new, idx, nbrs)
    if not libs:
        raise ValueError(f"illegal move {name} for {color}: suicide")
    ko, ko_color = -1, None
    if len(captured) == 1 and len(own) == 1 and len(libs) == 1:
        ko, ko_color = captured[0], _OTHER[color]
    black_caps, white_caps = state.prisoners
    if me == BLACK:
        black_caps += len(captured)
    else:
        white_caps += len(captured)
    return _State(bytes(new), ko, ko_color, (black_caps, white_caps)), name


def _legal_indices(state: _State, color: Color, size: int) -> tuple[int, ...]:
    """Indices of the board points `color` may play on. Group liberties are computed only where needed."""
    board = state.board
    nbrs = _neighbor_table(size)
    me = _CODE[color]
    them = BLACK + WHITE - me
    ko = state.ko if state.ko_color == color else -1
    libs_of: dict[int, int] = {}  # stone index -> liberty count of its group, filled on demand
    out = []
    for i in range(size * size):
        if board[i] != EMPTY or i == ko:
            continue
        ok = False
        for n in nbrs[i]:
            v = board[n]
            if v == EMPTY:
                ok = True
                break
            count = libs_of.get(n)
            if count is None:
                stones, libs = _group(board, n, nbrs)
                count = len(libs)
                for s in stones:
                    libs_of[s] = count
            if (v == me and count >= 2) or (v == them and count == 1):
                ok = True
                break
        if ok:
            out.append(i)
    return tuple(out)


# --------------------------------------------------------------------------------------------------------------
# Replay cache
# --------------------------------------------------------------------------------------------------------------

_cache: OrderedDict[_CacheKey, _State] = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: _CacheKey) -> _State | None:
    with _cache_lock:
        state = _cache.get(key)
        if state is not None:
            _cache.move_to_end(key)
        return state


def _cache_put(key: _CacheKey, state: _State) -> None:
    with _cache_lock:
        _cache[key] = state
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def clear_cache() -> None:
    """Drop every cached board (tests use this to force cold replays)."""
    with _cache_lock:
        _cache.clear()


def _state_for(moves: _MoveList, size: int) -> _State:
    """Board state after `moves`, replayed from the longest cached prefix. Every replayed prefix is cached."""
    state = _cache_get((moves, size))
    if state is not None:
        return state
    n = len(moves)
    start, state = 0, _empty_state(size)
    for k in range(n - 1, 0, -1):
        prev = _cache_get((moves[:k], size))
        if prev is not None:
            start, state = k, prev
            break
    for i in range(start, n):
        color, move = moves[i]
        try:
            state, _ = _apply(state, color, move, size)
        except ValueError as err:
            raise ValueError(f"move {i + 1} of the history ({color} {move}) is illegal: {err}") from err
        _cache_put((moves[: i + 1], size), state)
    return state


# --------------------------------------------------------------------------------------------------------------
# The Game
# --------------------------------------------------------------------------------------------------------------


class GoGame:
    """Go as KataGo's `chinese` preset plays it: captures, simple ko, suicide illegal, two passes end the game.

    `play` does not refuse moves after two passes; callers check `is_terminal`, so a recorded game that resumes
    after a pass pair still replays.
    """

    name = "go"

    def initial_position(self, size: int = 19, komi: float = 7.5, rules: str = "chinese") -> Position:
        if not 2 <= size <= len(GO_COLUMNS):
            raise ValueError(f"board size must be between 2 and {len(GO_COLUMNS)}, got {size!r}")
        meta = {"size": int(size), "komi": float(komi), "rules": rules}
        return Position(game=self.name, moves=(), to_move="B", meta=meta)

    @staticmethod
    def _size(pos: Position) -> int:
        return int(pos.meta.get("size", 19))

    def legal_moves(self, pos: Position) -> list[Move]:
        """Board points `pos.to_move` may play, row-major from the top left, then "pass" (always legal)."""
        _check_color(pos.to_move)
        size = self._size(pos)
        state = _state_for(pos.moves, size)
        legal = state.legal.get(pos.to_move)
        if legal is None:
            names = _names(size)
            legal = tuple(names[i] for i in _legal_indices(state, pos.to_move, size)) + (PASS,)
            state.legal[pos.to_move] = legal
        return list(legal)

    def play(self, pos: Position, move: Move) -> Position:
        """Position after `pos.to_move` plays `move`. Raises ValueError with the reason for an illegal move."""
        _check_color(pos.to_move)
        size = self._size(pos)
        state = _state_for(pos.moves, size)
        new_state, name = _apply(state, pos.to_move, move, size)
        moves = (*pos.moves, (pos.to_move, name))
        _cache_put((moves, size), new_state)
        return Position(game=pos.game, moves=moves, to_move=_OTHER[pos.to_move], meta=pos.meta)

    def is_terminal(self, pos: Position) -> bool:
        """True after two consecutive passes."""
        m = pos.moves
        return len(m) >= 2 and m[-1][1].lower() == PASS and m[-2][1].lower() == PASS

    def result(self, pos: Position) -> float | None:
        """Always None: Go scoring is delegated to the sign of the engine's score lead (SCOPE.md)."""
        return None

    def board(self, pos: Position) -> dict[Move, Color]:
        """Occupied points only: GTP coordinate -> "B" or "W"."""
        size = self._size(pos)
        names = _names(size)
        return {names[i]: _LETTER[v] for i, v in enumerate(_state_for(pos.moves, size).board) if v}

    def render(self, pos: Position) -> str:
        """ASCII board with GTP coordinates; X Black, O White, + star point, the last move in parentheses."""
        size = self._size(pos)
        state = _state_for(pos.moves, size)
        last = -1
        last_text = "none"
        if pos.moves:
            color, move = pos.moves[-1]
            last_text = f"{color} {move}"
            rc = gtp_to_rc(move, size)
            if rc is not None:
                last = rc[0] * size + rc[1]
        black_caps, white_caps = state.prisoners
        header = (
            f"{len(pos.moves)} moves played, {pos.to_move} to play, last move {last_text}, "
            f"captured B {black_caps} W {white_caps}"
        )
        if self.is_terminal(pos):
            header += " (game over: two passes)"
        stars = _star_points(size)
        letters = "   " + " ".join(GO_COLUMNS[:size])
        lines = [header, letters]
        for row in range(size):
            label = f"{size - row:>2}"
            cells = []
            for col in range(size):
                idx = row * size + col
                if idx == last:
                    cells.append("(")
                elif col > 0 and idx - 1 == last:
                    cells.append(")")
                else:
                    cells.append(" ")
                v = state.board[idx]
                if v == BLACK:
                    cells.append("X")
                elif v == WHITE:
                    cells.append("O")
                else:
                    cells.append("+" if idx in stars else ".")
            cells.append(")" if last == row * size + size - 1 else " ")
            lines.append(label + "".join(cells) + label)
        lines.append(letters)
        return "\n".join(lines)
