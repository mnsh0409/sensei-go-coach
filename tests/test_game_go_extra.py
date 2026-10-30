"""Extra T3 tests: random-play fuzz and replay consistency for sensei.game_go. Skip until the module exists."""

from __future__ import annotations

import functools
import random

import pytest

from sensei.coords import PASS, gtp_to_rc, neighbors, rc_to_gtp
from sensei.interfaces import Position

game_mod = pytest.importorskip("sensei.game_go")
GoGame = game_mod.GoGame

SEED = 20261029
FUZZ_GAMES = 80
FUZZ_MAX_MOVES = 150
FUZZ_SIZES = (9, 9, 9, 13, 19)  # small boards make captures and ko fights frequent; 19x19 checks the full board
OTHER = {"B": "W", "W": "B"}


@functools.cache
def neighbor_names(size: int) -> dict[str, list[str]]:
    """GTP name -> neighbouring GTP names, built from sensei.coords only."""
    return {
        rc_to_gtp((r, c), size): [rc_to_gtp(n, size) for n in neighbors((r, c), size)]
        for r in range(size)
        for c in range(size)
    }


def group_of(board: dict[str, str], start: str, size: int) -> tuple[list[str], set[str]]:
    """Stones and liberties of the group containing `start`, computed independently of sensei.game_go."""
    nbrs = neighbor_names(size)
    color = board[start]
    stones = [start]
    seen = {start}
    libs: set[str] = set()
    for s in stones:  # the list grows while it is iterated; every added stone is visited
        for n in nbrs[s]:
            c = board.get(n)
            if c is None:
                libs.add(n)
            elif c == color and n not in seen:
                seen.add(n)
                stones.append(n)
    return stones, libs


def liberty_counts(board: dict[str, str], size: int) -> list[int]:
    """Liberty count of every group on the board."""
    seen: set[str] = set()
    counts = []
    for start in board:
        if start in seen:
            continue
        stones, libs = group_of(board, start, size)
        seen.update(stones)
        counts.append(len(libs))
    return counts


def oracle_play(board: dict[str, str], color: str, move: str, size: int) -> None:
    """Place a stone and remove captured enemy groups, in place. The caller guarantees the move is legal."""
    if move == PASS:
        return
    board[move] = color
    for n in neighbors(gtp_to_rc(move, size), size):
        name = rc_to_gtp(n, size)
        if board.get(name) == OTHER[color]:
            stones, libs = group_of(board, name, size)
            if not libs:
                for s in stones:
                    del board[s]
    _, libs = group_of(board, move, size)
    assert libs, f"{color} {move} was accepted but has no liberties"


def all_points(size: int) -> frozenset[str]:
    return frozenset(rc_to_gtp((r, c), size) for r in range(size) for c in range(size))


def refused(game, pos: Position, move: str) -> bool:
    """True when the game rejects the move with ValueError (cheaper than pytest.raises in a hot loop)."""
    try:
        game.play(pos, move)
    except ValueError:
        return True
    return False


def test_random_play_fuzz():
    """80 seeded random games: no exception, no zero-liberty group, pass always legal, illegal points refused."""
    game = GoGame()
    rng = random.Random(SEED)
    points = {size: all_points(size) for size in set(FUZZ_SIZES)}
    plies = 0
    for g in range(FUZZ_GAMES):
        size = FUZZ_SIZES[g % len(FUZZ_SIZES)]
        pos = game.initial_position(size=size, komi=7.5)
        for _ in range(FUZZ_MAX_MOVES):
            legal = game.legal_moves(pos)
            assert PASS in legal
            board = game.board(pos)
            assert all(n > 0 for n in liberty_counts(board, size)), f"zero-liberty group after {pos.moves}"
            if board:
                assert refused(game, pos, rng.choice(list(board))), "an occupied point was accepted"
            if size * size - len(board) > len(legal) - 1:  # some empty point is illegal (suicide or ko)
                legal_set = set(legal)
                assert not (legal_set & board.keys()), "an occupied point was reported legal"
                illegal = sorted(points[size] - legal_set - board.keys())  # sorted: set order is not reproducible
                assert refused(game, pos, rng.choice(illegal)), "a suicide or ko point was accepted"
            pos = game.play(pos, rng.choice(legal))
            plies += 1
            if game.is_terminal(pos):
                break
        assert PASS in game.legal_moves(pos)
    assert plies > FUZZ_GAMES * 50


@pytest.mark.parametrize("size", [9, 19])
def test_replay_matches_incremental_board(size):
    """board(pos) equals a board maintained move by move, and cold replays from the move list agree with it."""
    game = GoGame()
    rng = random.Random(SEED + size)
    pos = game.initial_position(size=size, komi=7.5)
    oracle: dict[str, str] = {}
    boards = [dict(oracle)]
    legals = [game.legal_moves(pos)]
    for _ in range(120):
        legal = [m for m in game.legal_moves(pos) if m != PASS] or [PASS]
        move = rng.choice(legal)
        color = pos.to_move
        pos = game.play(pos, move)
        oracle_play(oracle, color, move, size)
        assert game.board(pos) == oracle
        boards.append(dict(oracle))
        legals.append(game.legal_moves(pos))
    # Positions built directly from the move list, with the replay cache emptied, in a random order.
    order = list(range(len(boards)))
    rng.shuffle(order)
    game_mod.clear_cache()
    for k in order:
        fresh = Position(game="go", moves=pos.moves[:k], to_move="B" if k % 2 == 0 else "W", meta=pos.meta)
        assert game.board(fresh) == boards[k]
        assert game.legal_moves(fresh) == legals[k]


def test_illegal_move_messages_name_the_reason():
    game = GoGame()
    pos = game.initial_position()
    with pytest.raises(ValueError, match="board"):
        game.play(pos, "Z99")
    with pytest.raises(ValueError, match="board"):
        game.play(pos, "I5")
    pos = game.play(pos, "Q16")
    with pytest.raises(ValueError, match="occupied"):
        game.play(pos, "Q16")
    for m in ["pass", "A2", "pass", "B1"]:
        pos = game.play(pos, m)
    with pytest.raises(ValueError, match="suicide"):
        game.play(pos, "A1")
    pos = game.initial_position()
    for m in ["B3", "C3", "A2", "D2", "B1", "C1", "Q4", "B2", "C2"]:
        pos = game.play(pos, m)
    with pytest.raises(ValueError, match="ko"):
        game.play(pos, "B2")


def test_history_moves_are_canonical_and_terminal_needs_two_passes():
    game = GoGame()
    pos = game.initial_position(size=9, komi=5.5, rules="japanese")
    assert pos.meta == {"size": 9, "komi": 5.5, "rules": "japanese"}
    pos = game.play(pos, "e5")
    pos = game.play(pos, " PASS ")
    assert pos.moves == (("B", "E5"), ("W", "pass"))
    assert not game.is_terminal(pos)
    pos = game.play(pos, "c3")
    pos = game.play(pos, "pass")
    assert not game.is_terminal(pos)
    pos = game.play(pos, "pass")
    assert game.is_terminal(pos)
    assert game.result(pos) is None


def test_render_shows_coordinates_and_stones():
    game = GoGame()
    pos = game.initial_position(size=9)
    for m in ["E5", "C3"]:
        pos = game.play(pos, m)
    text = game.render(pos)
    lines = text.splitlines()
    assert lines[1].split() == list("ABCDEFGHJ")
    assert lines[-1] == lines[1]
    row5 = next(line for line in lines if line.startswith(" 5"))
    assert "X" in row5
    row3 = next(line for line in lines if line.startswith(" 3"))
    assert "(O)" in row3
    assert "B to play" in lines[0]
