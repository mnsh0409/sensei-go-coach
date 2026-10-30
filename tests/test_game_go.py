"""Acceptance tests for T3. Skip until sensei.game_go exists."""

from __future__ import annotations

import pytest

from sensei.interfaces import Game

game_mod = pytest.importorskip("sensei.game_go")
GoGame = game_mod.GoGame


def play_all(game, moves):
    pos = game.initial_position(size=19, komi=7.5)
    for m in moves:
        pos = game.play(pos, m)
    return pos


def test_satisfies_protocol():
    assert isinstance(GoGame(), Game)


def test_alternation_and_history():
    g = GoGame()
    pos = play_all(g, ["Q16", "D4"])
    assert pos.to_move == "B"
    assert pos.moves == (("B", "Q16"), ("W", "D4"))


def test_capture():
    g = GoGame()
    pos = play_all(g, ["A2", "B2", "C2", "pass", "B1", "pass", "B3"])
    board = g.board(pos)
    assert board.get("B2") is None
    assert board["B3"] == "B"


def test_occupied_point_is_illegal():
    g = GoGame()
    pos = play_all(g, ["Q16"])
    with pytest.raises(ValueError):
        g.play(pos, "Q16")


def test_suicide_is_illegal():
    g = GoGame()
    pos = play_all(g, ["A2", "Q16", "B1"])
    assert "A1" not in g.legal_moves(pos)
    with pytest.raises(ValueError):
        g.play(pos, "A1")


def test_simple_ko():
    g = GoGame()
    pos = play_all(g, ["B3", "C3", "A2", "D2", "B1", "C1", "Q4", "B2", "C2"])
    board = g.board(pos)
    assert board.get("B2") is None and board["C2"] == "B"
    assert "B2" not in g.legal_moves(pos)
    with pytest.raises(ValueError):
        g.play(pos, "B2")
    pos = play_all(g, ["B3", "C3", "A2", "D2", "B1", "C1", "Q4", "B2", "C2", "Q16", "D16", "B2"])
    assert g.board(pos).get("C2") is None


def test_two_passes_end_the_game():
    g = GoGame()
    pos = play_all(g, ["Q16", "pass"])
    assert not g.is_terminal(pos)
    pos = g.play(pos, "pass")
    assert g.is_terminal(pos)
