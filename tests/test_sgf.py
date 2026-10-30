"""Acceptance tests for T1b. Skip until sensei.sgf exists."""

from __future__ import annotations

import pytest

sgf_mod = pytest.importorskip("sensei.sgf")

GAME = (
    b"(;GM[1]FF[4]SZ[19]KM[7.5]RU[Chinese]HA[0]PB[alice]PW[bob]BR[5k]WR[4k]DT[2025-01-02]RE[B+R]"
    b";B[pd];W[dp];B[];W[dd];B[pp])"
)


def test_read_game_converts_to_gtp():
    g = sgf_mod.read_game(GAME)
    assert tuple(g.moves) == (("B", "Q16"), ("W", "D4"), ("B", "pass"), ("W", "D16"), ("B", "Q4"))
    assert g.size == 19
    assert g.komi == 7.5
    assert g.rules == "chinese"
    assert g.handicap == 0
    assert (g.black_rank, g.white_rank) == ("5k", "4k")
    assert g.date == "2025-01-02"


def test_read_game_on_9x9():
    g = sgf_mod.read_game(b"(;GM[1]FF[4]SZ[9]KM[6.5];B[ee];W[cg])")
    assert g.size == 9
    assert tuple(g.moves) == (("B", "E5"), ("W", "C3"))
