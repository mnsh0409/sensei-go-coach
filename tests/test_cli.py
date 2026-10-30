"""T3: the terminal loop runs offline (self-check, and a 20-ply auto game against the fake engine)."""

from __future__ import annotations

import pytest

cli = pytest.importorskip("sensei.cli")


def test_self_check(capsys):
    assert cli.main(["--self-check", "--moves", "20"]) == 0
    assert "self-check ok: 20 legal moves played" in capsys.readouterr().out


@pytest.mark.parametrize("white", ["raw", "sensei"])
def test_auto_game_against_the_fake_engine(capsys, white):
    assert cli.main(["--auto", "--fake", "--engine", white, "--moves", "20"]) == 0
    out = capsys.readouterr().out
    assert "game ok: 20 moves" in out
    assert "score lead for Black" in out
    if white == "sensei":
        assert "sensei:" in out and "admissible" in out
