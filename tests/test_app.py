"""T9: the web app, offline, in fake mode (fake KataGo subprocess and canned explanations; no network, no model).

Everything goes through FastAPI's TestClient, the way the page calls the server. The module's app is built once
(one fake engine for all tests); every test uses its own learner ids so that the tests do not interfere.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
app_mod = pytest.importorskip("sensei.app")

from sensei.explainer import find_coords  # noqa: E402
from sensei.interfaces import Position  # noqa: E402

CENTER_POINTS = ["K10", "K11", "K9", "L10", "J10", "L11", "J9", "L9", "J11", "M10", "H10", "N10", "K12", "K8"]
CORNER_POINTS = ["R3", "C4", "C16", "R17", "D17", "C3", "Q3", "R16", "C17", "D3", "E3", "Q17", "R4", "D4"]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from fastapi.testclient import TestClient

    root = tmp_path_factory.mktemp("sensei_app")
    app = app_mod.create_app(
        "fake",
        profiles_dir=root / "profiles",
        sessions_dir=root / "sessions",
        seed=7,
        engine_stderr_log=root / "engine.log",
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            service=app.state.service,
            components=app.state.components,
            root=root,
            profiles=root / "profiles",
            sessions=root / "sessions",
        )


def new_game(env, learner, rank="10k"):
    r = env.client.post("/api/game", json={"learner_id": learner, "rank": rank})
    assert r.status_code == 200, r.text
    return r.json()


def state_of(env, learner):
    r = env.client.get("/api/state", params={"learner_id": learner})
    assert r.status_code == 200, r.text
    return r.json()


def play_free_point(env, learner, candidates=CENTER_POINTS):
    """Play the first of `candidates` that is still empty; returns (point, response json)."""
    board = state_of(env, learner)["board"]
    point = next(p for p in candidates if p not in board)
    r = env.client.post("/api/move", json={"learner_id": learner, "move": point})
    assert r.status_code == 200, r.text
    return point, r.json()


def free_point(env, learner):
    board = state_of(env, learner)["board"]
    return next(p for p in CENTER_POINTS if p not in board)


# ---- the page and the basics ---------------------------------------------------------------------


def test_index_page_has_the_board_and_the_mode_badge(env):
    r = env.client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    assert 'id="board"' in html
    assert re.search(r'id="mode-badge"[^>]*>\s*demo \(fake engine\)\s*<', html)
    assert 'data-mode="fake"' in html
    assert "__MODE" not in html  # the placeholders were filled
    assert "<title>" in html
    # accessibility basics: the chat input has a label and the main actions are real buttons
    assert 'for="ask-input"' in html and 'id="ask-input"' in html
    assert '<button id="btn-new"' in html and '<button id="btn-end"' in html


def test_page_loads_nothing_from_the_network(env):
    html = env.client.get("/").text
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", html))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls
    assert not re.search(r"<script[^>]+src=", html)
    assert all(href.startswith("data:") for href in re.findall(r"<link[^>]+href=\"([^\"]*)\"", html))


def test_page_headers_enforce_the_same_rule_in_the_browser(env):
    r = env.client.get("/")
    csp = r.headers["content-security-policy"]
    script = re.findall(r"<script>(.*?)</script>", r.text, flags=re.S)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    assert f"script-src 'sha256-{digest}'" in csp  # only this page's script may run
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-eval" not in csp and "script-src 'unsafe-inline'" not in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-cache"


def test_page_script_is_valid_javascript(env, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the page script was not syntax-checked")
    scripts = re.findall(r"<script>(.*?)</script>", env.client.get("/").text, flags=re.S)
    assert len(scripts) == 1
    page_js = tmp_path / "page.js"
    page_js.write_text(scripts[0], encoding="utf-8")
    done = subprocess.run([node, "--check", str(page_js)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr


def test_info_describes_the_demo_mode(env):
    info = env.client.get("/api/info").json()
    assert info["mode"] == "fake" and info["mode_label"] == "demo (fake engine)"
    assert info["playable"] is True
    assert info["ranks"][0] == "20k" and info["ranks"][-1] == "5d" and len(info["ranks"]) == 25
    assert info["default_rank"] in info["ranks"]
    assert info["board_size"] == 19 and info["komi"] == 7.5


def test_request_validation(env):
    post = env.client.post
    assert post("/api/game", json={"learner_id": "bad id!", "rank": "10k"}).status_code == 422
    assert post("/api/game", json={"learner_id": "../etc", "rank": "10k"}).status_code == 422
    assert post("/api/game", json={"learner_id": "val_ok", "rank": "30k"}).status_code == 422
    assert post("/api/move", json={"learner_id": "val_ok", "move": "D"}).status_code == 422
    assert post("/api/ask", json={"learner_id": "val_ok", "question": "   "}).status_code == 422
    assert post("/api/move", json={"learner_id": "nobody_here", "move": "D4"}).status_code == 404
    assert post("/api/ask", json={"learner_id": "nobody_here", "question": "hi"}).status_code == 404
    assert post("/api/end", json={"learner_id": "nobody_here"}).status_code == 404
    assert env.client.get("/api/state", params={"learner_id": "nobody_here"}).status_code == 404


# ---- one game ------------------------------------------------------------------------------------


def test_new_game_starts_from_the_empty_board(env):
    st = new_game(env, "t_start", "7k")
    assert st["mode"] == "fake" and st["declared_rank"] == "7k"
    assert st["board"] == {} and st["last_move"] is None and st["move_number"] == 0
    assert st["to_move"] == "B" and st["learner_color"] == "B" and st["your_turn"] is True
    assert st["captured"] == {"B": 0, "W": 0}
    assert st["game_over"] is False and st["ended"] is False
    assert st["size"] == 19 and st["komi"] == 7.5
    assert st["profile"]["rank_label"] == "7k" and st["profile"]["games_played"] == 0
    assert st["profile"]["target_topic"] in app_mod.DemoTopics.names
    assert st["history"] == []


def test_a_game_of_several_moves_with_sensei_replies(env):
    learner = "t_flow"
    new_game(env, learner, "12k")
    target = state_of(env, learner)["profile"]["target_topic"]
    threshold = env.components.cfg["play"]["mistake_threshold"]
    played = []
    for n in range(1, 6):
        point, body = play_free_point(env, learner)
        played.append(point)
        report, sensei, state = body["report"], body["sensei"], body["state"]

        assert report["move"] == point and report["ply"] == 2 * n - 1
        assert report["loss"] >= 0.0 and report["loss_pct"] == pytest.approx(100 * report["loss"], abs=0.051)
        assert report["is_mistake"] == (report["loss"] > threshold)
        assert (report["severity"] is not None) == report["is_mistake"]
        assert isinstance(report["topic"], str) and report["best_move"]
        # only mistakes in the target topic come with an explanation
        assert (report["explanation"] is not None) == (report["is_mistake"] and report["topic"] == target)

        assert sensei is not None and sensei["ply"] == 2 * n and sensei["move"] not in played
        assert isinstance(sensei["explanation"], str) and len(sensei["explanation"]) > 20
        assert sensei["move"] in sensei["explanation"]
        assert sensei["value_loss"] >= 0.0 and sensei["n_admissible"] >= 1
        assert sensei["position_class"] in {"strategic", "tactical", "only_move"}
        assert set(sensei["timing_ms"]) >= {"engine_ms", "human_policy_ms", "explanation_ms"}

        assert state["move_number"] == 2 * n and state["to_move"] == "B" and state["your_turn"] is True
        assert state["board"][point] == "B" and state["board"][sensei["move"]] == "W"
        assert state["last_move"] == {"color": "W", "move": sensei["move"], "ply": 2 * n, "by": "sensei"}
        assert len(state["board"]) == 2 * n  # no captures in these moves

    full = state_of(env, learner)
    assert full["move_number"] == 10
    kinds = [e["kind"] for e in full["history"]]
    assert kinds == ["learner", "sensei"] * 5
    assert [e["move"] for e in full["history"] if e["kind"] == "learner"] == played


def test_pass_is_a_move_too(env):
    learner = "t_pass"
    new_game(env, learner)
    body = env.client.post("/api/pass", json={"learner_id": learner}).json()
    assert body["report"]["move"] == "pass"
    assert body["sensei"] is not None  # Sensei does not pass back on an empty board
    assert body["state"]["move_number"] == 2 and body["state"]["last_move"]["by"] == "sensei"
    assert body["state"]["board"] == {body["sensei"]["move"]: "W"}


def test_illegal_moves_are_rejected_with_a_reason(env):
    learner = "t_illegal"
    new_game(env, learner)
    point, _ = play_free_point(env, learner)
    before = state_of(env, learner)

    occupied = env.client.post("/api/move", json={"learner_id": learner, "move": point})
    assert occupied.status_code == 400 and "occupied" in occupied.json()["detail"]

    sensei_point = next(p for p, c in before["board"].items() if c == "W")
    also = env.client.post("/api/move", json={"learner_id": learner, "move": sensei_point.lower()})
    assert also.status_code == 400 and "occupied" in also.json()["detail"]

    off_board = env.client.post("/api/move", json={"learner_id": learner, "move": "Z99"})
    assert off_board.status_code == 400 and "board" in off_board.json()["detail"]
    no_i = env.client.post("/api/move", json={"learner_id": learner, "move": "I5"})
    assert no_i.status_code == 400 and no_i.json()["detail"]

    after = state_of(env, learner)
    assert after["board"] == before["board"] and after["move_number"] == before["move_number"]
    assert len(after["history"]) == len(before["history"])  # nothing was recorded for the refused moves


def test_the_server_is_the_referee_for_suicide(env):
    learner = "t_suicide"
    new_game(env, learner)
    game = env.service.games[learner]
    # Black to move; A1 is surrounded by two white stones and would have no liberty: the rules forbid it.
    s = game.session
    moves = (("B", "K10"), ("W", "A2"), ("B", "K11"), ("W", "B1"))
    s.pos = Position(game="go", moves=moves, to_move="B", meta=s.pos.meta)
    r = env.client.post("/api/move", json={"learner_id": learner, "move": "A1"})
    assert r.status_code == 400 and "suicide" in r.json()["detail"].lower()
    assert state_of(env, learner)["move_number"] == 4


# ---- Ask Sensei ----------------------------------------------------------------------------------


def test_ask_about_a_move_returns_text_a_variation_and_tool_calls(env):
    learner = "t_ask"
    new_game(env, learner, "8k")
    play_free_point(env, learner)
    asked = free_point(env, learner)
    r = env.client.post("/api/ask", json={"learner_id": learner, "question": f"Why not {asked}?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["text"], str) and asked in body["text"]
    assert body["to_move"] == "B" and body["ply"] == 2
    assert isinstance(body["variations"], list) and body["variations"]
    for line in body["variations"]:
        assert isinstance(line, list) and all(isinstance(m, str) for m in line)
    assert body["variations"][0][0] == asked and len(body["variations"][0]) >= 2
    names = [c["name"] for c in body["tool_calls"]]
    assert "evaluate_move" in names and "show_variation" in names and "analyze_position" in names
    assert all(c["error"] is None for c in body["tool_calls"])
    assert body["guard"]["ungrounded"] == []


def test_ask_without_a_move_shows_the_engines_line_and_never_invents_points(env):
    learner = "t_ask2"
    new_game(env, learner)
    play_free_point(env, learner)
    body = env.client.post("/api/ask", json={"learner_id": learner, "question": "What is the best move here?"}).json()
    assert body["variations"] and len(body["variations"][0]) >= 2
    # every point named in the answer comes from a tool result: the variation or the candidates it lists
    mentioned = set(find_coords(body["text"], 19))
    line = set(body["variations"][0])
    assert line <= mentioned
    assert body["guard"]["ungrounded"] == [] and body["guard"]["dropped"] == []


def test_ask_about_the_learner_answers_from_the_profile(env):
    learner = "t_ask3"
    new_game(env, learner, "15k")
    play_free_point(env, learner)
    body = env.client.post("/api/ask", json={"learner_id": learner, "question": "How am I doing?"}).json()
    assert "15k" in body["text"]
    assert body["variations"] == []
    assert [c["name"] for c in body["tool_calls"]][-1] == "get_learner_profile"


def test_ask_about_an_illegal_point_says_so(env):
    learner = "t_ask4"
    new_game(env, learner)
    point, _ = play_free_point(env, learner)
    body = env.client.post("/api/ask", json={"learner_id": learner, "question": f"What about {point}?"}).json()
    assert "not a legal move" in body["text"]
    assert any(c["error"] for c in body["tool_calls"])


def test_asks_are_kept_in_the_state_history(env):
    learner = "t_hist"
    new_game(env, learner)
    play_free_point(env, learner)
    env.client.post("/api/ask", json={"learner_id": learner, "question": "How am I doing?"})
    history = state_of(env, learner)["history"]
    assert [e["kind"] for e in history] == ["learner", "sensei", "ask"]
    assert history[2]["question"] == "How am I doing?" and history[2]["text"]
    assert [e["seq"] for e in history] == sorted(e["seq"] for e in history)


# ---- the end of a game, the profile and the recording --------------------------------------------


@pytest.fixture(scope="module")
def finished(env):
    """One finished game with an ask in it: learner id, moves each, recording name and the summary."""
    learner = "t_finished"
    recording = new_game(env, learner, "9k")["recording"]
    for _ in range(4):
        play_free_point(env, learner)
    env.client.post("/api/ask", json={"learner_id": learner, "question": f"Why not {free_point(env, learner)}?"})
    play_free_point(env, learner)
    r = env.client.post("/api/end", json={"learner_id": learner})
    assert r.status_code == 200, r.text
    return SimpleNamespace(learner=learner, n=5, recording=recording, summary=r.json())


def test_end_returns_the_summary(env, finished):
    s = finished.summary
    assert s["result"]["winner"] in {"B", "W"} and isinstance(s["result"]["black_lead"], float)
    assert s["result"]["learner_won"] == (s["result"]["winner"] == "B")
    rank = s["rank"]
    assert set(rank) >= {"estimate", "label", "sd", "n_moves", "min_moves", "declared_only"}
    assert rank["n_moves"] == finished.n and rank["declared_only"] is True  # too few moves for an estimate
    assert rank["label"] == "9k" and rank["sd"] > 0
    assert s["moves_this_game"] == finished.n and 0 <= s["mistakes_this_game"] <= finished.n
    assert len(s["weakest_topics"]) <= 2
    for topic in s["weakest_topics"]:
        assert topic["topic"] in app_mod.DemoTopics.names and topic["moves"] >= 3
    assert s["next_target"] in app_mod.DemoTopics.names
    assert s["games_played"] == 1 and s["declared_rank"] == "9k"


def test_end_is_idempotent_and_closes_the_game(env, finished):
    again = env.client.post("/api/end", json={"learner_id": finished.learner})
    assert again.status_code == 200 and again.json() == finished.summary  # the curriculum is not updated twice
    st = state_of(env, finished.learner)
    assert st["ended"] is True and st["your_turn"] is False and st["awaiting_sensei"] is False
    move = env.client.post("/api/move", json={"learner_id": finished.learner, "move": "A1"})
    assert move.status_code == 409 and "ended" in move.json()["detail"]
    ask = env.client.post("/api/ask", json={"learner_id": finished.learner, "question": "hello"})
    assert ask.status_code == 409


def test_end_saves_the_profile(env, finished):
    path = env.profiles / f"{finished.learner}.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    assert profile["learner_id"] == finished.learner and profile["games_played"] == 1
    assert profile["target_topic"] == finished.summary["next_target"]
    assert set(profile["curriculum_state"]) == set(app_mod.DemoTopics.names)
    assert sum(t["moves"] for t in profile["topics"].values()) == finished.n


def test_a_returning_learner_continues_from_the_saved_profile(env):
    learner = "t_return"
    new_game(env, learner, "11k")
    for _ in range(2):
        play_free_point(env, learner)
    summary = env.client.post("/api/end", json={"learner_id": learner}).json()
    again = new_game(env, learner, "11k")
    assert again["board"] == {} and again["move_number"] == 0 and again["ended"] is False
    assert again["profile"]["games_played"] == 1 and again["profile"]["target_topic"] == summary["next_target"]
    summary2 = env.client.post("/api/end", json={"learner_id": learner}).json()
    assert summary2["games_played"] == 2


def test_the_game_is_recorded_to_sessions(env, finished):
    path = env.sessions / f"{finished.recording}.json"
    assert path.is_file() and path.name.startswith(f"{finished.learner}_"), "no recording was written"
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["format"] == "sensei-session/1" and rec["learner_id"] == finished.learner
    assert rec["mode"] == "fake" and rec["declared_rank"] == "9k" and rec["status"] == "ended"
    assert rec["game"] == {"size": 19, "komi": 7.5, "rules": "chinese"}
    assert len(rec["moves"]) == 2 * finished.n
    assert [m["by"] for m in rec["moves"]] == ["learner", "sensei"] * finished.n
    assert [m["ply"] for m in rec["moves"]] == list(range(1, 2 * finished.n + 1))
    assert len(rec["reports"]) == finished.n and len(rec["sensei_moves"]) == finished.n
    assert all(s["explanation"] for s in rec["sensei_moves"])
    assert len(rec["asks"]) == 1 and rec["asks"][0]["variations"]
    assert rec["summary"] == finished.summary
    assert not list(env.sessions.glob("*.tmp")), "an atomic write left its temporary file behind"


def test_replay_endpoints_serve_the_recording(env, finished):
    listing = env.client.get("/api/replay").json()
    row = next((r for r in listing["recordings"] if r["name"] == finished.recording), None)
    assert row is not None, listing
    assert row["learner_id"] == finished.learner
    assert row["mode"] == "fake" and row["n_moves"] == 2 * finished.n and row["has_summary"] is True
    assert listing["default"] in {r["name"] for r in listing["recordings"]}
    got = env.client.get(f"/api/replay/{finished.recording}")
    assert got.status_code == 200
    rec = got.json()
    assert rec["learner_id"] == finished.learner and len(rec["moves"]) == 2 * finished.n
    assert rec["summary"] == finished.summary
    assert env.client.get("/api/replay/does_not_exist").status_code == 404
    assert env.client.get("/api/replay/..%2F..%2Fpyproject").status_code == 404


def test_a_recording_replays_to_the_final_board_without_go_rules(env):
    """What the page's replay does: add each move's stone and remove its recorded captures."""
    learner = "t_rebuild"
    new_game(env, learner)
    for _ in range(4):
        play_free_point(env, learner)
    st = state_of(env, learner)
    rec = env.client.get(f"/api/replay/{st['recording']}").json()
    board: dict[str, str] = {}
    for m in rec["moves"]:
        if m["move"] != "pass":
            board[m["move"]] = m["color"]
        for point in m["captured"]:
            board.pop(point)
    assert board == st["board"]


def test_a_game_in_progress_is_already_replayable(env):
    learner = "t_partial"
    new_game(env, learner)
    play_free_point(env, learner)
    name = state_of(env, learner)["recording"]
    rec = env.client.get(f"/api/replay/{name}").json()
    assert rec["status"] == "in_progress" and len(rec["moves"]) == 2 and rec["summary"] is None


def test_starting_again_abandons_the_old_game(env):
    learner = "t_again"
    first = new_game(env, learner)
    play_free_point(env, learner)
    second = new_game(env, learner)
    assert second["board"] == {} and second["move_number"] == 0 and second["recording"] != first["recording"]
    old = env.client.get(f"/api/replay/{first['recording']}").json()
    assert old["status"] == "abandoned" and len(old["moves"]) == 2


def test_recording_a_capture(env):
    """The recording stores the points each move removes (the client has no Go rules)."""
    learner = "t_capture"
    new_game(env, learner)
    game = env.service.games[learner]
    meta = game.session.pos.meta
    before = Position(game="go", moves=(("B", "B1"), ("W", "A1")), to_move="B", meta=meta)
    after = env.components.game.play(before, "A2")
    assert "A1" not in env.components.game.board(after)
    env.service._record_move(game, before, after, "learner")
    recorded = game.recorder.data["moves"][-1]
    assert recorded == {"ply": 3, "color": "B", "move": "A2", "by": "learner", "captured": ["A1"]}
    game.session.pos = after
    st = state_of(env, learner)
    assert st["captured"] == {"B": 1, "W": 0}
    assert st["board"] == {"B1": "B", "A2": "B"}


# ---- the game ending by passes -------------------------------------------------------------------


def test_two_passes_end_the_play_but_not_the_summary(env):
    learner = "t_over"
    new_game(env, learner)
    play_free_point(env, learner)
    s = env.service.games[learner].session
    s.pos = Position(game="go", moves=(*s.pos.moves, ("B", "pass"), ("W", "pass")), to_move="B", meta=s.pos.meta)
    st = state_of(env, learner)
    assert st["game_over"] is True and st["your_turn"] is False and st["awaiting_sensei"] is False
    refused = env.client.post("/api/move", json={"learner_id": learner, "move": free_point(env, learner)})
    assert refused.status_code == 409 and "passed" in refused.json()["detail"]
    assert env.client.post("/api/pass", json={"learner_id": learner}).status_code == 409
    summary = env.client.post("/api/end", json={"learner_id": learner})
    assert summary.status_code == 200 and summary.json()["result"]["winner"] in {"B", "W"}


def test_demo_turns_stay_out_of_the_poster_log_unless_asked(env):
    assert env.components.turns_log is None  # synthetic timings must not end up in logs/turns.jsonl


def test_the_fake_engines_stderr_stays_out_of_the_real_katago_log():
    from sensei.engine_katago import DEFAULT_STDERR_LOG

    assert app_mod.FAKE_STDERR_LOG != DEFAULT_STDERR_LOG
    assert app_mod.FAKE_STDERR_LOG.parent == DEFAULT_STDERR_LOG.parent  # still in the gitignored logs/


def test_each_sensei_turn_is_logged_with_the_hardware_block(tmp_path):
    from fastapi.testclient import TestClient

    log = tmp_path / "logs" / "turns.jsonl"
    app = app_mod.create_app(
        "fake",
        profiles_dir=tmp_path / "p",
        sessions_dir=tmp_path / "s",
        turns_log=log,
        seed=1,
        engine_stderr_log=tmp_path / "engine.log",
    )
    with TestClient(app) as client:
        client.post("/api/game", json={"learner_id": "turn_log", "rank": "10k"})
        for _ in range(2):
            board = client.get("/api/state", params={"learner_id": "turn_log"}).json()["board"]
            move = next(p for p in CENTER_POINTS if p not in board)
            assert client.post("/api/move", json={"learner_id": "turn_log", "move": move}).status_code == 200
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [line["ply"] for line in lines] == [2, 4]
    for line in lines:
        assert line["learner"] == "turn_log" and {"engine_ms", "human_policy_ms", "explanation_ms"} <= set(line)
        assert line["hardware"]["katago_backend"] == "fake" and line["hardware"]["host"]


# ---- concurrency ---------------------------------------------------------------------------------


def test_two_learners_can_play_at_once_without_interfering(env):
    ranks = {"t_conc_a": "15k", "t_conc_b": "3d"}
    for learner, rank in ranks.items():
        new_game(env, learner, rank)
    barrier = threading.Barrier(len(ranks))

    def play(learner: str) -> dict:
        barrier.wait(timeout=30)
        mine = []
        for _ in range(4):
            order = CENTER_POINTS if learner.endswith("a") else CENTER_POINTS[::-1]
            point, _body = play_free_point(env, learner, order)
            mine.append(point)
        answer = env.client.post("/api/ask", json={"learner_id": learner, "question": "How am I doing?"}).json()
        return {"mine": mine, "answer": answer["text"], "state": state_of(env, learner)}

    with ThreadPoolExecutor(max_workers=len(ranks)) as pool:
        results = dict(zip(ranks, pool.map(play, ranks), strict=True))

    for learner, rank in ranks.items():
        res, st = results[learner], results[learner]["state"]
        assert st["learner_id"] == learner and st["declared_rank"] == rank
        assert st["move_number"] == 8 and len(st["board"]) == 8
        assert all(st["board"][p] == "B" for p in res["mine"])
        assert rank in res["answer"]  # Ask Sensei saw this learner's profile, not the other one's
        other = next(r for lid, r in ranks.items() if lid != learner)
        assert other not in res["answer"]
    a, b = (results[k]["state"] for k in ranks)
    assert a["recording"] != b["recording"]
    for learner in ranks:
        rec = env.client.get(f"/api/replay/{results[learner]['state']['recording']}").json()
        assert rec["learner_id"] == learner and len(rec["moves"]) == 8
        assert [m["move"] for m in rec["moves"] if m["by"] == "learner"] == results[learner]["mine"]


def test_each_game_has_its_own_explainer(env):
    for learner in ("t_own_a", "t_own_b"):
        new_game(env, learner)
    a, b = env.service.games["t_own_a"], env.service.games["t_own_b"]
    assert a.explainer is not b.explainer and a.session is not b.session and a.lock is not b.lock


# ---- live mode over a scripted model -------------------------------------------------------------

SCRIPTED_TEXT = "Sensei keeps the position balanced here and leaves a clear shape to build on."
SCRIPTED_ANSWER = "That is the line the engine gives; it is drawn on the board."


def model_reply(text=None, tool_calls=(), prompt_tokens=120, completion_tokens=25):
    """One chat completion in the OpenAI shape that MetaExplainer reads."""
    calls = [
        SimpleNamespace(
            id=f"call_{i}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args))
        )
        for i, (name, args) in enumerate(tool_calls, start=1)
    ]
    message = SimpleNamespace(content=text, tool_calls=calls or None)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class ScriptedModel:
    """A stand-in for the OpenAI client: plain sentences for explanations; for Ask Sensei the tool calls in
    `plan`, then a final answer. It records every request."""

    def __init__(self):
        self.calls = []
        self.plan = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        asking = "tools" in kwargs
        if asking and self.plan and not any(m.get("role") == "tool" for m in kwargs["messages"]):
            return model_reply(tool_calls=self.plan)
        return model_reply(SCRIPTED_ANSWER if asking else SCRIPTED_TEXT)


def test_live_mode_runs_the_real_explainer_over_a_scripted_model(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    model = ScriptedModel()
    api_log = tmp_path / "api_calls.jsonl"
    monkeypatch.setattr(app_mod.metaapi, "DEFAULT_LOG_PATH", api_log)
    monkeypatch.setattr(app_mod.metaapi, "make_client", lambda: (model, "scripted-model"))

    def fake_katago(cls, **kwargs):  # KataGoEngine.from_env, with the repository's fake engine behind it
        return app_mod.KataGoEngine(
            exe=sys.executable,
            exe_args=[str(app_mod.FAKE_ENGINE)],
            model="fake-main.bin.gz",
            human_model="fake-human.bin.gz",
            config="fake-analysis.cfg",
            stderr_log=kwargs.get("stderr_log"),
            query_timeout=120.0,
        )

    monkeypatch.setattr(app_mod.KataGoEngine, "from_env", classmethod(fake_katago))
    overrides = {
        "play": {"mistake_threshold": 0.01},  # the fake engine's values are close together
        "topics": {"loss_bins": [0.01, 0.02, 0.03], "model": str(tmp_path / "no_topics.json"), "names": []},
    }
    app = app_mod.create_app(
        "live",
        cfg=overrides,
        profiles_dir=tmp_path / "profiles",
        sessions_dir=tmp_path / "sessions",
        turns_log=tmp_path / "turns.jsonl",
        seed=7,
        engine_stderr_log=tmp_path / "engine.log",
        backend="fake",
    )
    with TestClient(app) as client:
        info = client.get("/api/info").json()
        assert info["mode"] == "live" and info["model"] == "scripted-model" and info["topics_fitted"] is False
        assert 'data-mode="live"' in client.get("/").text

        learner = "live_t"
        assert client.post("/api/game", json={"learner_id": learner, "rank": "10k"}).status_code == 200
        mistake = None
        for _ in range(12):
            board = client.get("/api/state", params={"learner_id": learner}).json()["board"]
            point = next(p for p in [*CORNER_POINTS, *CENTER_POINTS] if p not in board)
            response = client.post("/api/move", json={"learner_id": learner, "move": point})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["sensei"]["explanation"] == SCRIPTED_TEXT and body["sensei"]["model"] == "scripted-model"
            assert (body["sensei"]["prompt_tokens"], body["sensei"]["completion_tokens"]) == (120, 25)
            if body["report"]["is_mistake"]:
                mistake = body["report"]
                break
        # with no fitted topics every mistake is in the target topic, so it is explained on the spot by the model
        assert mistake is not None and mistake["topic"] == app_mod.UNTAGGED
        assert mistake["explanation"] == SCRIPTED_TEXT and mistake["explanation_model"] == "scripted-model"

        board = client.get("/api/state", params={"learner_id": learner}).json()["board"]
        line = [p for p in [*CENTER_POINTS, *CORNER_POINTS] if p not in board][:3]
        model.plan = [("evaluate_move", {"move": line[0]}), ("show_variation", {"moves": line})]
        asked = client.post("/api/ask", json={"learner_id": learner, "question": f"Why not {line[0]}?"})
        assert asked.status_code == 200, asked.text
        answer = asked.json()
        assert answer["text"] == SCRIPTED_ANSWER and answer["model"] == "scripted-model"
        assert answer["variations"] == [line]
        assert [c["name"] for c in answer["tool_calls"]] == ["evaluate_move", "show_variation"]
        assert all(c["error"] is None for c in answer["tool_calls"])
        assert answer["prompt_tokens"] > 0 and answer["guard"]["ungrounded"] == []
        assert model.calls[-1]["tools"], "Ask Sensei offers the model its tools"

        # get_learner_profile reaches the explainer through the session: the declared rank is in the tool result
        model.plan = [("get_learner_profile", {})]
        client.post("/api/ask", json={"learner_id": learner, "question": "How am I doing?"})
        tool_messages = [m for m in model.calls[-1]["messages"] if m.get("role") == "tool"]
        assert tool_messages and "10k" in tool_messages[-1]["content"]

        summary = client.post("/api/end", json={"learner_id": learner})
        assert summary.status_code == 200 and summary.json()["next_target"] == app_mod.UNTAGGED

    # every model call is in the API log, and every Sensei turn in the turn log with the hardware block
    logged = [json.loads(line) for line in api_log.read_text(encoding="utf-8").splitlines()]
    assert len(logged) == len(model.calls) and all(r["model"] == "scripted-model" for r in logged)
    turns = [json.loads(line) for line in (tmp_path / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
    assert turns and all(t["learner"] == learner and t["prompt_tokens"] == 120 for t in turns)
    assert all(t["hardware"]["katago_backend"] == "fake" for t in turns)
    recording = json.loads(next((tmp_path / "sessions").glob(f"{learner}_*.json")).read_text(encoding="utf-8"))
    assert recording["mode"] == "live" and recording["model"] == "scripted-model" and len(recording["asks"]) == 2


# ---- replay mode ---------------------------------------------------------------------------------


def test_replay_mode_serves_a_recording_and_refuses_to_play(env, finished, tmp_path):
    from fastapi.testclient import TestClient

    name = finished.recording
    pinned = env.sessions / f"{name}.json"
    app = app_mod.create_app("replay", replay_file=pinned, sessions_dir=tmp_path / "none")
    assert app.state.components.engine is None  # no engine and no model in replay mode
    with TestClient(app) as client:
        page = client.get("/").text
        assert re.search(r'id="mode-badge"[^>]*>\s*replay\s*<', page) and 'data-mode="replay"' in page
        info = client.get("/api/info").json()
        assert info["mode"] == "replay" and info["playable"] is False
        listing = client.get("/api/replay").json()
        assert listing["default"] == name and [r["name"] for r in listing["recordings"]] == [name]
        assert client.get(f"/api/replay/{name}").json()["summary"] == finished.summary
        refused = client.post("/api/game", json={"learner_id": "someone", "rank": "10k"})
        assert refused.status_code == 409 and "replay" in refused.json()["detail"]
        assert client.post("/api/move", json={"learner_id": "someone", "move": "D4"}).status_code == 409


def test_replay_mode_rejects_files_that_are_not_recordings(tmp_path):
    with pytest.raises(FileNotFoundError):
        app_mod.create_app("replay", replay_file=tmp_path / "missing.json", sessions_dir=tmp_path)
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"format": "something-else", "moves": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a Sensei session recording"):
        app_mod.create_app("replay", replay_file=other, sessions_dir=tmp_path)


def test_main_reports_startup_errors_instead_of_a_traceback(tmp_path, capsys):
    assert app_mod.main(["--replay", str(tmp_path / "missing.json")]) == 2
    assert "cannot start in replay mode" in capsys.readouterr().err


def test_a_live_start_without_katago_points_to_the_offline_demo(monkeypatch, capsys):
    def no_katago(cls, **kwargs):
        raise RuntimeError("KATAGO_EXE is not set")

    monkeypatch.setattr(app_mod.KataGoEngine, "from_env", classmethod(no_katago))
    assert app_mod.main([]) == 2
    err = capsys.readouterr().err
    assert "cannot start in live mode: KATAGO_EXE is not set" in err and "--fake" in err


def test_fake_and_replay_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        app_mod.build_parser().parse_args(["--fake", "--replay", "x.json"])


def test_replay_without_a_file_lists_the_recordings(env, finished, monkeypatch):
    from fastapi.testclient import TestClient

    assert app_mod.build_parser().parse_args(["--replay"]).replay == ""
    seen = {}

    def fake_create_app(mode, **kwargs):
        seen.update(mode=mode, **kwargs)
        raise ValueError("stop here")

    monkeypatch.setattr(app_mod, "create_app", fake_create_app)
    assert app_mod.main(["--replay"]) == 2 and seen["mode"] == "replay" and seen["replay_file"] is None
    monkeypatch.undo()
    app = app_mod.create_app("replay", sessions_dir=env.sessions)
    with TestClient(app) as client:
        listing = client.get("/api/replay").json()
        assert finished.recording in [r["name"] for r in listing["recordings"]]


# ---- the stand-ins and the T7 hook ----------------------------------------------------------------------------------


def facts(**over):
    base = {
        "move": "Q16",
        "candidates": [
            {"move": "D4", "winrate_pct": 44.1},
            {"move": "Q16", "winrate_pct": 43.0},
        ],
        "learner": {"label": "8k"},
        "position_class": "only_move",
        "n_admissible": 1,
        "instructiveness": 0.2,
        "value_loss_pct": 1.1,
    }
    return {**base, **over}


@pytest.mark.parametrize(
    "case",
    [
        facts(),  # the move gives up winrate against the engine's top candidate
        facts(value_loss_pct=0.0),  # nothing given up, but it is not the top candidate
        facts(move="D4", value_loss_pct=0.0, n_admissible=3, instructiveness=0.0),  # it is the top candidate
        facts(candidates=[], value_loss_pct=0.0),  # no candidate list at all
    ],
)
def test_canned_move_text_only_names_points_from_the_facts(case):
    text = app_mod.canned_move_text(case)
    allowed = {case["move"], *(c["move"] for c in case["candidates"])}
    assert set(find_coords(text, 19)) <= allowed
    assert case["move"] in text and "8k" in text and text.count(".") >= 3


def test_canned_mistake_text_only_names_points_from_the_facts():
    base = {"played": "C16", "best_move": "D4", "loss_pct": 1.5, "topic": "corner_moves"}
    both = {"move": "D4", "winrate_pct": 44.7}, {"move": "C16", "winrate_pct": 43.2}
    for candidates in (list(both), [both[0]], []):
        text = app_mod.canned_mistake_text({**base, "candidates": candidates})
        assert set(find_coords(text, 19)) <= {"C16", "D4"}
        assert "corner moves" in text
    assert "corner" not in app_mod.canned_mistake_text({**base, "topic": None, "candidates": []})


def test_a_game_without_fitted_topics_tags_every_move_untagged(tmp_path):
    """What live mode does until T7 provides a topic model: one 'untagged' arm and no weakness topics."""
    comp = app_mod.build_components(
        "fake", profiles_dir=tmp_path / "p", sessions_dir=tmp_path / "s", seed=3, engine_stderr_log=tmp_path / "e.log"
    )
    comp.topics, comp.topic_names = None, []
    try:
        assert comp.arms == [app_mod.UNTAGGED]
        service = app_mod.GameService(comp)
        state = service.start_game("no_topics", "10k")
        assert state["profile"]["target_topic"] == app_mod.UNTAGGED
        for _ in range(3):
            board = service.get_state("no_topics")["board"]
            move = next(p for p in CENTER_POINTS if p not in board)
            body = service.play("no_topics", move)
            assert body["report"]["topic"] == app_mod.UNTAGGED
        summary = service.end_game("no_topics")
        assert summary["weakest_topics"] == [] and summary["next_target"] == app_mod.UNTAGGED
        assert summary["moves_this_game"] == 3
    finally:
        comp.close()


class FittedStandIn:
    """What `LdaMistakeTopics.load` returns, as far as the app uses it."""

    n_topics = 3

    def assign(self, bag):
        return [1.0, 0.0, 0.0]


def topic_cfg(**topics):
    return {"topics": {"model": "results/topics_v1.json", "names": [], **topics}}


def write_model_file(root, content=b"{}"):
    path = root / "results" / "topics_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def loader(monkeypatch):
    """Replaces LdaMistakeTopics.load (T7's JSON reader) with a stand-in; `loader.paths` lists what was read."""
    calls = SimpleNamespace(paths=[], result=FittedStandIn())

    def load(cls, path):
        calls.paths.append(path)
        if isinstance(calls.result, Exception):
            raise calls.result
        return calls.result

    monkeypatch.setattr(app_mod.LdaMistakeTopics, "load", classmethod(load), raising=False)
    return calls


def test_load_topics_without_a_model_file_gives_no_topics(tmp_path, loader):
    assert app_mod.load_topics(topic_cfg(), root=tmp_path) == (None, [])
    assert app_mod.load_topics({}, root=tmp_path) == (None, [])  # no topics section at all
    assert loader.paths == []


def test_load_topics_warns_when_names_are_set_but_the_model_is_missing(tmp_path, loader, caplog):
    with caplog.at_level("WARNING", logger=app_mod.logger.name):
        assert app_mod.load_topics(topic_cfg(names=["a", "b", "c"]), root=tmp_path) == (None, [])
    assert "no topic model" in caplog.text


def test_load_topics_reads_the_t7_model_and_the_names_from_the_config(tmp_path, loader):
    path = write_model_file(tmp_path)
    model, names = app_mod.load_topics(topic_cfg(names=["a", " b ", "c"]), root=tmp_path)
    assert model is loader.result and names == ["a", "b", "c"]
    assert loader.paths == [path]  # a relative `topics.model` is resolved against the repository root


def test_load_topics_follows_the_configured_model_path(tmp_path, loader):
    elsewhere = tmp_path / "elsewhere" / "my_topics.json"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"{}")
    cfg = topic_cfg(model=str(elsewhere), names=["a", "b", "c"])
    assert app_mod.load_topics(cfg, root=tmp_path / "unused")[1] == ["a", "b", "c"]
    assert loader.paths == [elsewhere]
    # without `topics.model` the T7 default location is used
    default = write_model_file(tmp_path)
    assert app_mod.load_topics({"topics": {"names": ["a", "b", "c"]}}, root=tmp_path)[1] == ["a", "b", "c"]
    assert loader.paths[-1] == default and app_mod.DEFAULT_TOPICS_PATH == "results/topics_v1.json"


@pytest.mark.parametrize(
    ("names", "fragment"),
    [
        ([], "topics.names is empty"),  # the T7 human gate has not named the topics yet
        (["a", "b"], "2 entries but the model has 3"),
        (["a", "b", "c", "d"], "4 entries but the model has 3"),
        (["a", "a", "b"], "unique"),  # the curriculum refuses duplicate arms
        (["a", "", "c"], "non-empty"),
        (["a", "b", app_mod.UNTAGGED], "unique"),
    ],
)
def test_load_topics_needs_names_that_fit_the_model(tmp_path, loader, caplog, names, fragment):
    write_model_file(tmp_path)
    with caplog.at_level("WARNING", logger=app_mod.logger.name):
        assert app_mod.load_topics(topic_cfg(names=names), root=tmp_path) == (None, [])
    assert fragment in caplog.text and "untagged" in caplog.text


@pytest.mark.parametrize("error", [ValueError("bad components"), KeyError("vocabulary"), OSError("disk")])
def test_load_topics_survives_an_unreadable_model_file(tmp_path, loader, caplog, error):
    write_model_file(tmp_path)
    loader.result = error
    with caplog.at_level("WARNING", logger=app_mod.logger.name):
        assert app_mod.load_topics(topic_cfg(names=["a", "b", "c"]), root=tmp_path) == (None, [])
    assert "cannot load the mistake topics" in caplog.text


def test_load_topics_survives_a_corrupt_file_without_any_stand_in(tmp_path):
    write_model_file(tmp_path, b"this is not json")
    assert app_mod.load_topics(topic_cfg(names=["a", "b", "c"]), root=tmp_path) == (None, [])


def test_the_resilient_explainer_falls_back_to_the_template_when_the_model_fails(env):
    class Broken:
        model = "broken"

        def explain(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

        def explain_mistake(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

        def ask(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

    explainer = app_mod.ResilientExplainer(Broken())
    comp = env.components
    pos = comp.game.initial_position()
    analysis = comp.engine.analyze(pos, 30)
    learner_policy = {c.move: c.prior for c in analysis.candidates}
    import numpy as np

    choice = comp.selector.choose(analysis, learner_policy, 0.0, 0.0, np.random.default_rng(0))
    out = explainer.explain(analysis, choice, SimpleNamespace(rank=-9.0, rank_sd=3.0, n_moves=0))
    assert out.model == app_mod.TEMPLATE_MODEL and choice.move in out.text
    assert out.grounded_on["guard"]["template"] is True
    with pytest.raises(RuntimeError):
        explainer.ask("why?", position=pos)
