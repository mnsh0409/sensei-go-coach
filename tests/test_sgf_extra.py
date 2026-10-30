"""Extra offline tests for sensei.sgf: handicap setup stones, comments and variations, missing properties, files."""

from __future__ import annotations

import logging

import pytest

sgf_mod = pytest.importorskip("sensei.sgf")

HANDICAP_GAME = (
    b"(;GM[1]FF[4]SZ[19]KM[0.5]RU[Japanese]HA[2]AB[pd][dp]PB[alice]PW[bob]BR[5k]WR[3k]DT[2025-03-04]"
    b";W[pp];B[dd];W[])"
)

VARIATION_GAME = (
    b"(;GM[1]FF[4]SZ[19]KM[7.5]C[root comment: the text aa is not a move]"
    b";B[pd]C[nice move](;W[dp]C[main line];B[pp])(;W[dd];B[cc]))"
)


def test_handicap_setup_stones_are_recorded():
    g = sgf_mod.read_game(HANDICAP_GAME)
    assert g.handicap == 2
    assert set(g.setup) == {("B", "Q16"), ("B", "D4")}
    assert all(color == "B" for color, _ in g.setup)
    # Setup stones are not moves: the record starts with White's first move.
    assert tuple(g.moves) == (("W", "Q4"), ("B", "D16"), ("W", "pass"))
    assert g.rules == "japanese"
    assert g.komi == 0.5
    assert (g.black_rank, g.white_rank) == ("5k", "3k")
    assert g.date == "2025-03-04"


def test_white_setup_stones_keep_their_color():
    g = sgf_mod.read_game(b"(;GM[1]FF[4]SZ[9]AB[cc]AW[gg];B[ee])")
    assert set(g.setup) == {("B", "C7"), ("W", "G3")}
    assert tuple(g.moves) == (("B", "E5"),)
    assert g.handicap == 0


def test_comments_and_variations_only_main_line():
    g = sgf_mod.read_game(VARIATION_GAME)
    assert tuple(g.moves) == (("B", "Q16"), ("W", "D4"), ("B", "Q4"))
    assert g.komi == 7.5


def test_missing_properties_are_tolerated():
    g = sgf_mod.read_game(b"(;GM[1]FF[4];B[pd];W[dd])")
    assert g.size == 19
    assert g.komi is None
    assert g.rules == ""
    assert g.handicap == 0
    assert g.black_rank == "" and g.white_rank == ""
    assert g.date == ""
    assert g.setup == ()
    assert tuple(g.moves) == (("B", "Q16"), ("W", "D16"))


def test_ff3_tt_pass_and_lowercase_rules():
    g = sgf_mod.read_game(b"(;GM[1]FF[3]SZ[19]RU[ CHINESE ];B[tt];W[dd])")
    assert tuple(g.moves) == (("B", "pass"), ("W", "D16"))
    assert g.rules == "chinese"


def test_record_is_frozen():
    g = sgf_mod.read_game(b"(;GM[1]FF[4]SZ[19];B[pd])")
    with pytest.raises(AttributeError):
        g.size = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an sgf file",
        b"(;GM[1]FF[4]SZ[abc];B[pd])",
        b"(;GM[1]FF[4]SZ[25];B[pd])",  # wider than sensei.coords supports
        b"(;GM[1]FF[4]SZ[9];B[zz])",  # move off the board
        b"(;GM[1]FF[4]SZ[19];B[pd];AW[dd]W[dp])",  # setup stones in the middle of the game
    ],
)
def test_unparsable_games_raise_value_error(data):
    with pytest.raises(ValueError):
        sgf_mod.read_game(data)


def test_iter_sgf_files_skips_bad_files_with_a_warning(tmp_path, caplog):
    (tmp_path / "b_good.sgf").write_bytes(b"(;GM[1]FF[4]SZ[19]KM[7.5];B[pd];W[dp])")
    (tmp_path / "a_bad.sgf").write_bytes(b"garbage")
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "c_good.SGF").write_bytes(b"(;GM[1]FF[4]SZ[9];B[ee])")
    (tmp_path / "notes.txt").write_bytes(b"(;GM[1]FF[4]SZ[9];B[ee])")  # wrong extension: ignored

    with caplog.at_level(logging.WARNING, logger="sensei.sgf"):
        got = list(sgf_mod.iter_sgf_files(tmp_path))

    assert [p.name for p, _ in got] == ["b_good.sgf", "c_good.SGF"]
    assert tuple(got[0][1].moves) == (("B", "Q16"), ("W", "D4"))
    assert got[1][1].size == 9
    assert any("a_bad.sgf" in rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING)


def test_iter_sgf_files_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(sgf_mod.iter_sgf_files(tmp_path / "missing"))
