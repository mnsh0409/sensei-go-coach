"""Acceptance tests for T2. Need KataGo: run with  pytest -q -m engine tests/test_engine_katago.py"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from sensei.coords import gtp_to_rc
from sensei.interfaces import Engine, HumanPolicySource, Position
from tests.conftest import require_env

pytestmark = pytest.mark.engine
engine_mod = pytest.importorskip("sensei.engine_katago")

CORNERS = ["D4", "Q16", "D16", "Q4"]


@pytest.fixture(scope="module")
def engine():
    require_env("KATAGO_EXE", "KATAGO_MODEL", "KATAGO_HUMAN_MODEL", "KATAGO_CONFIG")
    eng = engine_mod.KataGoEngine.from_env()
    yield eng
    eng.close()


def empty(size=19):
    return Position(game="go", moves=(), to_move="B", meta={"size": size, "komi": 7.5, "rules": "chinese"})


def corner_position(winner: str) -> Position:
    """The winner holds all four 4-4 points while the loser passed every turn; the loser is to move."""
    moves: list[tuple[str, str]] = []
    if winner == "B":
        for i, pt in enumerate(CORNERS):
            moves.append(("B", pt))
            if i < len(CORNERS) - 1:
                moves.append(("W", "pass"))
        to_move = "W"
    else:
        for pt in CORNERS:
            moves.append(("B", "pass"))
            moves.append(("W", pt))
        to_move = "B"
    return Position(game="go", moves=tuple(moves), to_move=to_move, meta={"size": 19, "komi": 7.5, "rules": "chinese"})


def test_protocols(engine):
    assert isinstance(engine, Engine)
    assert isinstance(engine, HumanPolicySource)


def test_analyze_empty_board(engine):
    a = engine.analyze(empty(), max_visits=50)
    assert len(a.candidates) > 0
    assert abs(sum(a.policy.values()) - 1.0) < 1e-4
    assert all(0.0 <= c.value <= 1.0 for c in a.candidates)
    visits = [c.visits for c in a.candidates]
    assert visits == sorted(visits, reverse=True)
    assert a.ownership is not None and len(a.ownership) == 361


def test_occupied_points_are_not_in_policy(engine):
    pos = Position(game="go", moves=(("B", "Q16"), ("W", "D4")), to_move="B", meta={"size": 19, "komi": 7.5})
    a = engine.analyze(pos, max_visits=50)
    assert "Q16" not in a.policy and "D4" not in a.policy


@pytest.mark.parametrize("winner", ["B", "W"])
def test_values_are_for_the_side_to_move(engine, winner):
    """Fails if the wrapper reports from Black's (or White's) perspective instead of the side to move."""
    a = engine.analyze(corner_position(winner), max_visits=50)
    assert a.root_value < 0.35
    assert max(c.value for c in a.candidates) < 0.5
    row, col = gtp_to_rc("D4")
    assert a.ownership is not None
    assert a.ownership[row * 19 + col] < -0.5  # the opponent of the side to move owns its own corner stone


def test_concurrent_queries_are_matched(engine):
    positions = [corner_position("B"), empty(), corner_position("W"), corner_position("B")]
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda p: engine.analyze(p, max_visits=20), positions))
    for pos, a in zip(positions, results, strict=True):
        assert a.position == pos
    assert results[0].root_value < 0.35 and results[2].root_value < 0.35


def test_human_policy_differs_by_rank(engine):
    pos = empty()
    weak = engine.human_policy(pos, "rank_20k")
    strong = engine.human_policy(pos, "rank_5d")
    assert abs(sum(weak.values()) - 1.0) < 1e-4
    assert abs(sum(strong.values()) - 1.0) < 1e-4
    diff = sum(abs(weak.get(m, 0.0) - strong.get(m, 0.0)) for m in set(weak) | set(strong))
    assert diff > 0.05
