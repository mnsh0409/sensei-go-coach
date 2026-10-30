"""KataGoEngine against tests/fake_katago.py. Runs in the default `pytest -q` (no engine marker, no KataGo needed).

The perspective, ownership, concurrency and human-policy checks mirror tests/test_engine_katago.py; the fake honours
reportAnalysisWinratesAs (defaulting to BLACK like analysis_example.cfg), so a wrapper that forgets the SIDETOMOVE
override fails here just as it would against KataGo.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sensei.coords import gtp_to_rc, rc_to_gtp
from sensei.engine_katago import DEFAULT_STDERR_LOG, ROOT, KataGoEngine
from sensei.interfaces import Engine, EngineAnalysis, HumanPolicySource, Position
from tests.test_engine_katago import CORNERS, corner_position, empty

FAKE = Path(__file__).resolve().parent / "fake_katago.py"


def start_engine(**kwargs) -> KataGoEngine:
    kwargs.setdefault("query_timeout", 60.0)
    return KataGoEngine(
        exe=sys.executable,
        exe_args=[str(FAKE)],
        model="fake-main.bin.gz",
        human_model="fake-human.bin.gz",
        config="fake-analysis.cfg",
        **kwargs,
    )


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    eng = start_engine(stderr_log=tmp_path_factory.mktemp("logs") / "katago_stderr.log")
    yield eng
    eng.close()


def wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_protocols(engine):
    assert isinstance(engine, Engine)
    assert isinstance(engine, HumanPolicySource)


def test_command_line_uses_the_exe_args_hook(engine):
    assert engine.command[:3] == (sys.executable, str(FAKE), "analysis")
    expected = ("-config", "fake-analysis.cfg", "-model", "fake-main.bin.gz", "-human-model", "fake-human.bin.gz")
    assert engine.command[3:] == expected
    assert engine.alive and engine.pid > 0


def test_analyze_empty_board(engine):
    a = engine.analyze(empty(), max_visits=50)
    assert isinstance(a, EngineAnalysis)
    assert a.position == empty()
    assert len(a.candidates) > 0
    assert abs(sum(a.policy.values()) - 1.0) < 1e-6
    assert len(a.policy) == 19 * 19 + 1 and "pass" in a.policy
    assert all(0.0 <= c.value <= 1.0 for c in a.candidates)
    assert all(c.score_lead is not None and len(c.pv) >= 1 and c.pv[0] == c.move for c in a.candidates)
    visits = [c.visits for c in a.candidates]
    assert visits == sorted(visits, reverse=True)
    assert a.ownership is not None and len(a.ownership) == 361
    assert all(-1.0 <= o <= 1.0 for o in a.ownership)
    assert 0.0 <= a.root_value <= 1.0


def test_candidate_priors_come_from_the_policy(engine):
    a = engine.analyze(empty(), max_visits=50)
    for c in a.candidates:
        assert abs(c.prior - a.policy[c.move]) < 1e-9


def test_occupied_points_are_not_in_policy(engine):
    pos = Position(game="go", moves=(("B", "Q16"), ("W", "D4")), to_move="B", meta={"size": 19, "komi": 7.5})
    a = engine.analyze(pos, max_visits=50)
    assert "Q16" not in a.policy and "D4" not in a.policy
    assert len(a.policy) == 19 * 19 + 1 - 2
    assert abs(sum(a.policy.values()) - 1.0) < 1e-6


def test_small_board(engine):
    pos = Position(game="go", moves=(("B", "E5"),), to_move="W", meta={"size": 9, "komi": 7.0, "rules": "japanese"})
    a = engine.analyze(pos, max_visits=30)
    assert len(a.policy) == 81  # 80 empty points plus pass
    assert "E5" not in a.policy and "A1" in a.policy and "J9" in a.policy
    assert a.ownership is not None and len(a.ownership) == 81
    row, col = gtp_to_rc("E5", 9)
    assert a.ownership[row * 9 + col] < -0.5  # Black's stone, seen from White


@pytest.mark.parametrize("winner", ["B", "W"])
def test_values_are_for_the_side_to_move(engine, winner):
    """Fails if the wrapper reports from Black's (or White's) perspective instead of the side to move."""
    a = engine.analyze(corner_position(winner), max_visits=50)
    assert a.root_value < 0.35
    assert max(c.value for c in a.candidates) < 0.5
    row, col = gtp_to_rc("D4")
    assert a.ownership is not None
    assert a.ownership[row * 19 + col] < -0.5  # the opponent of the side to move owns its own corner stone
    assert all(c.score_lead is not None and c.score_lead < 0 for c in a.candidates)


def test_the_override_is_what_makes_the_perspective_right(engine):
    """Sanity check of the fake itself: without the override it reports Black's view, which the wrapper must not."""
    pos = corner_position("B")  # Black is far ahead, White to move
    query = KataGoEngine._base_query(pos, 19)
    query.update({"maxVisits": 20, "includeOwnership": True, "includePolicy": True})
    raw = engine._send(query)  # no overrideSettings: the fake falls back to BLACK like analysis_example.cfg
    assert raw["rootInfo"]["currentPlayer"] == "W"
    assert raw["rootInfo"]["winrate"] > 0.65
    row, col = gtp_to_rc("D4")
    assert raw["ownership"][row * 19 + col] > 0.5


def test_to_move_mismatch_is_detected(engine):
    pos = Position(game="go", moves=(("B", "Q16"),), to_move="B", meta={"size": 19})  # White should be to move
    with pytest.raises(RuntimeError, match="analyzed 'W' to move"):
        engine.analyze(pos, max_visits=10)


def test_concurrent_queries_are_matched(engine):
    positions = []
    for i in range(16):
        stones = [rc_to_gtp((2 + (i * 7 + k * 5) % 15, 2 + (i * 3 + k * 11) % 15)) for k in range(i % 5 + 1)]
        stones = list(dict.fromkeys(stones))
        moves = tuple(("B" if k % 2 == 0 else "W", m) for k, m in enumerate(stones))
        to_move = "W" if len(moves) % 2 == 1 else "B"
        positions.append(Position(game="go", moves=moves, to_move=to_move, meta={"size": 19, "komi": 7.5}))
    positions += [corner_position("B"), empty(), corner_position("W"), corner_position("B")]
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda p: engine.analyze(p, max_visits=20), positions))
    for pos, a in zip(positions, results, strict=True):
        assert a.position == pos
        occupied = {m for _, m in pos.moves if m != "pass"}
        assert occupied.isdisjoint(a.policy)  # the response really belongs to this position
        assert len(a.policy) == 19 * 19 + 1 - len(occupied)
    assert results[16].root_value < 0.35 and results[18].root_value < 0.35


def test_human_policy_differs_by_rank(engine):
    pos = empty()
    weak = engine.human_policy(pos, "rank_20k")
    strong = engine.human_policy(pos, "rank_5d")
    assert abs(sum(weak.values()) - 1.0) < 1e-6
    assert abs(sum(strong.values()) - 1.0) < 1e-6
    diff = sum(abs(weak.get(m, 0.0) - strong.get(m, 0.0)) for m in set(weak) | set(strong))
    assert diff > 0.05
    assert "pass" in weak and len(weak) == 19 * 19 + 1


def test_human_policy_skips_occupied_points(engine):
    pos = Position(game="go", moves=(("B", "Q16"), ("W", "D4"), ("B", "pass")), to_move="W", meta={"size": 19})
    hp = engine.human_policy(pos, "preaz_5k_3d")
    assert "Q16" not in hp and "D4" not in hp and len(hp) == 19 * 19 + 1 - 2
    assert abs(sum(hp.values()) - 1.0) < 1e-6


def test_error_response_raises_and_the_engine_survives(engine):
    illegal = Position(game="go", moves=(("B", "Q16"), ("W", "Q16")), to_move="B", meta={"size": 19})
    with pytest.raises(RuntimeError, match="Illegal move"):
        engine.analyze(illegal, max_visits=10)
    with pytest.raises(RuntimeError, match="humanSLProfile"):
        engine.human_policy(empty(), "rank_99x")
    a = engine.analyze(empty(), max_visits=10)  # the engine and the wrapper keep working afterwards
    assert len(a.candidates) > 0 and engine.alive


def test_version_and_clear_cache(engine):
    assert engine.version().startswith("1.")
    assert engine.clear_cache() is None


def test_stderr_log_is_written(engine):
    log = engine.stderr_log
    assert wait_for(lambda: log.is_file() and "ready to begin handling requests" in log.read_text(encoding="utf-8"))
    for _ in range(8):  # the fake writes a warning line to stderr every few queries
        engine.analyze(empty(), max_visits=5)
    assert wait_for(lambda: "WARNING" in log.read_text(encoding="utf-8"))
    text = log.read_text(encoding="utf-8")
    assert text.startswith("# ") and str(FAKE) in text.splitlines()[0]


def test_default_stderr_log_location(tmp_path):
    eng = start_engine()
    try:
        assert eng.stderr_log == DEFAULT_STDERR_LOG == ROOT / "logs" / "katago_stderr.log"
        eng.analyze(empty(), max_visits=5)
    finally:
        eng.close()
    assert DEFAULT_STDERR_LOG.is_file()


def test_close_terminates_within_10_s(tmp_path):
    eng = start_engine(stderr_log=tmp_path / "err.log")
    eng.analyze(empty(), max_visits=5)
    t0 = time.monotonic()
    eng.close()
    assert time.monotonic() - t0 < 10.0
    assert not eng.alive and eng.returncode == 0
    eng.close()  # idempotent
    with pytest.raises(RuntimeError, match="closed"):
        eng.analyze(empty(), max_visits=5)


def test_context_manager(tmp_path):
    with start_engine(stderr_log=tmp_path / "err.log") as eng:
        assert eng.alive
        a = eng.analyze(empty(), max_visits=5)
        assert a.root_value > 0.0
    assert not eng.alive


def test_engine_death_fails_pending_queries(tmp_path):
    eng = start_engine(stderr_log=tmp_path / "err.log")
    errors: list[BaseException] = []

    def slow_query():
        try:
            eng.analyze(empty(), max_visits=2_000_000)  # the fake sleeps 20 us per visit: 40 s
        except RuntimeError as exc:
            errors.append(exc)

    t = threading.Thread(target=slow_query)
    t.start()
    assert wait_for(lambda: len(eng._pending) == 1)
    eng._proc.kill()
    t.join(timeout=15.0)
    assert not t.is_alive()
    assert len(errors) == 1 and "closed its stdout" in str(errors[0])
    eng.close()


def test_routing_of_warnings_and_unrouteable_errors(tmp_path):
    """White-box: a warning line does not end its query (the result follows); an id-less error fails every query."""
    eng = start_engine(stderr_log=tmp_path / "err.log")
    try:
        qid, pending = eng._register()
        eng._handle_line(json.dumps({"id": qid, "warning": "rules converted", "field": "rules"}))
        assert not pending.event.is_set()  # still waiting for the analysis response
        eng._handle_line(json.dumps({"id": qid, "rootInfo": {"currentPlayer": "B"}, "moveInfos": []}))
        assert pending.event.is_set() and pending.error is None

        qid, pending = eng._register()
        eng._handle_line(json.dumps({"id": qid, "isDuringSearch": True, "rootInfo": {}}))  # partial report: ignored
        assert not pending.event.is_set()
        eng._handle_line(json.dumps({"id": qid, "warning": "w", "rootInfo": {"currentPlayer": "B"}, "moveInfos": []}))
        assert pending.event.is_set() and pending.error is None and pending.result["rootInfo"]["currentPlayer"] == "B"

        a, pa = eng._register()
        b, pb = eng._register()
        eng._handle_line('{"id": "unknown-id", "rootInfo": {}}')  # stale response: dropped
        eng._handle_line("this is not json")
        assert not pa.event.is_set() and not pb.event.is_set()
        eng._handle_line('{"error": "Could not parse json"}')
        assert pa.event.is_set() and pb.event.is_set()
        assert "names no query" in str(pa.error) and "names no query" in str(pb.error)
        assert eng._pending == {}
    finally:
        eng.close()


def test_from_env_checks_every_path(monkeypatch, tmp_path):
    real = tmp_path / "exists.txt"
    real.write_text("x", encoding="utf-8")
    for name in ("KATAGO_EXE", "KATAGO_MODEL", "KATAGO_HUMAN_MODEL", "KATAGO_CONFIG"):
        monkeypatch.setenv(name, str(real))
    monkeypatch.setenv("KATAGO_HUMAN_MODEL", str(tmp_path / "missing-human.bin.gz"))
    with pytest.raises(FileNotFoundError) as info:
        KataGoEngine.from_env()
    assert "KATAGO_HUMAN_MODEL" in str(info.value) and str(tmp_path / "missing-human.bin.gz") in str(info.value)

    monkeypatch.setenv("KATAGO_HUMAN_MODEL", str(real))
    monkeypatch.delenv("KATAGO_CONFIG")
    monkeypatch.setattr("sensei.engine_katago._load_dotenv", lambda: None)
    with pytest.raises(FileNotFoundError, match="KATAGO_CONFIG"):
        KataGoEngine.from_env()


def test_corner_positions_are_what_the_katago_test_expects():
    pos = corner_position("B")
    assert [m for _, m in pos.moves if m != "pass"] == CORNERS and pos.to_move == "W"
    pos = corner_position("W")
    assert pos.to_move == "B" and len(pos.moves) == 8


def test_human_policy_sends_the_human_sl_guide_settings(engine, monkeypatch):
    """Analysis_Engine.md, Human SL Analysis Guide: history on, more symmetries; analyze keeps the defaults."""
    sent = []
    original = engine._send

    def spy(query):
        sent.append(json.loads(json.dumps(query)))
        return original(query)

    monkeypatch.setattr(engine, "_send", spy)
    pos = corner_position("B")
    engine.human_policy(pos, "rank_10k")
    engine.analyze(pos, 20)
    human, analysis = sent[0]["overrideSettings"], sent[1]["overrideSettings"]
    assert human["humanSLProfile"] == "rank_10k" and human["reportAnalysisWinratesAs"] == "SIDETOMOVE"
    assert human["ignorePreRootHistory"] is False and human["rootNumSymmetriesToSample"] == 8
    assert analysis == {"reportAnalysisWinratesAs": "SIDETOMOVE"}
