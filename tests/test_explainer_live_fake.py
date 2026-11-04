"""Offline tests of MetaExplainer's tool loop and guard with a scripted fake client, a fake engine and a fake game.

The fakes implement the Engine and Game protocols directly, so these tests do not need sensei.engine_katago or
sensei.game_go.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sensei.coords import GO_COLUMNS, gtp_to_rc
from sensei.interfaces import (
    Candidate,
    Choice,
    Engine,
    EngineAnalysis,
    Explainer,
    Game,
    LearnerEstimate,
    Position,
    PositionClass,
)

explainer_mod = pytest.importorskip("sensei.explainer")
MetaExplainer = explainer_mod.MetaExplainer

CFG = {
    "play": {"max_visits": 50},
    "explainer": {"max_tool_rounds": 2, "max_sentences": 3, "reasoning_effort": "low", "language": "en"},
    "api": {"price_input": 1.25, "price_cached_input": 0.15, "price_output": 4.25, "budget_warn_usd": 100},
}

ROOT_CANDS = [("Q16", 0.5, 0.60, 300, 2.0), ("R14", 0.3, 0.59, 100, 1.5), ("C3", 0.2, 0.55, 50, 0.4)]
LATER_CANDS = [("D16", 0.6, 0.45, 200, -1.0), ("Q4", 0.4, 0.44, 100, -1.2)]


# ---- fakes ----------------------------------------------------------------------


class FakeGame:
    """Stones never get captured; occupied points, bad coordinates and off-board moves are illegal."""

    name = "go"

    def initial_position(self, size=19, komi=7.5, rules="chinese"):
        return Position(game="go", moves=(), to_move="B", meta={"size": size, "komi": komi, "rules": rules})

    def board(self, pos):
        return {move: color for color, move in pos.moves if move != "pass"}

    def legal_moves(self, pos):
        size = pos.meta.get("size", 19)
        taken = set(self.board(pos))
        points = [f"{GO_COLUMNS[c]}{r}" for c in range(size) for r in range(1, size + 1)]
        return [p for p in points if p not in taken] + ["pass"]

    def play(self, pos, move):
        gtp_to_rc(move, pos.meta.get("size", 19))  # ValueError when off the board
        if move != "pass" and move in self.board(pos):
            raise ValueError(f"{move} is occupied")
        nxt = "W" if pos.to_move == "B" else "B"
        return Position(game="go", moves=(*pos.moves, (pos.to_move, move)), to_move=nxt, meta=pos.meta)

    def is_terminal(self, pos):
        return len(pos.moves) >= 2 and pos.moves[-1][1] == "pass" and pos.moves[-2][1] == "pass"

    def result(self, pos):
        return None

    def render(self, pos):
        return " ".join(f"{c}{m}" for c, m in pos.moves)


class FakeEngine:
    """Root position: ROOT_CANDS; any later position: LATER_CANDS minus occupied points."""

    def __init__(self):
        self.queries: list[Position] = []

    def analyze(self, pos, max_visits):
        self.queries.append(pos)
        taken = {m for _, m in pos.moves}
        rows = ROOT_CANDS if not pos.moves else LATER_CANDS
        rows = [r for r in rows if r[0] not in taken] or [("K10", 1.0, 0.5, 10, 0.0)]
        cands = tuple(Candidate(move=m, prior=p, value=v, visits=n, score_lead=s) for m, p, v, n, s in rows)
        total = sum(c.prior for c in cands)
        return EngineAnalysis(
            position=pos,
            candidates=cands,
            policy={c.move: c.prior / total for c in cands},
            root_value=max(c.value for c in cands),
        )

    def close(self):
        return None


def text_response(text, prompt_tokens=100, completion_tokens=20):
    message = SimpleNamespace(content=text, tool_calls=None)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def tool_response(*calls, content=None):
    """calls: (name, arguments dict) pairs, all in one assistant message."""
    tool_calls = [
        SimpleNamespace(
            id=f"call_{i}",
            type="function",
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )
        for i, (name, args) in enumerate(calls, start=1)
    ]
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(prompt_tokens=150, completion_tokens=30)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeClient:
    """Scripted chat.completions.create: pops responses in order, falls back to `default` when they run out."""

    def __init__(self, responses=(), default=None):
        self.responses = list(responses)
        self.default = default
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        if self.default is None:
            raise AssertionError("no scripted response left")
        return self.default(kwargs)


def make(client, tmp_path, cfg=CFG):
    engine, game = FakeEngine(), FakeGame()
    ex = MetaExplainer(client, engine, game, cfg, model="fake-model", log_path=tmp_path / "api_calls.jsonl")
    return ex, engine, game


def root_position():
    return FakeGame().initial_position()


def sample_explain_inputs():
    analysis = FakeEngine().analyze(root_position(), 50)
    choice = Choice(
        move="R14",
        instructiveness=1.02,
        value_loss=0.01,
        position_class=PositionClass.STRATEGIC,
        scores={"Q16": -0.29, "R14": 1.02},
        admissible=("Q16", "R14"),
    )
    return analysis, choice, LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40)


def tool_messages(call):
    return [m for m in call["messages"] if m.get("role") == "tool"]


# ---- protocols ----------------------------------------------------------------------


def test_fakes_and_explainer_satisfy_protocols(tmp_path):
    ex, engine, game = make(FakeClient(), tmp_path)
    assert isinstance(engine, Engine)
    assert isinstance(game, Game)
    assert isinstance(ex, Explainer)


# ---- explain and the guard ----------------------------------------------------------------------


def test_explain_grounds_on_exact_numbers_and_logs(tmp_path):
    client = FakeClient([text_response("R14 builds a framework; Q16 is the engine's top choice at 60.0%.")])
    ex, _engine, _game = make(client, tmp_path)
    analysis, choice, learner = sample_explain_inputs()

    out = ex.explain(analysis, choice, learner)

    assert out.text.startswith("R14 builds a framework")
    assert out.model == "fake-model"
    assert (out.prompt_tokens, out.completion_tokens) == (100, 20)
    assert out.grounded_on["move"] == "R14"
    assert out.grounded_on["value_loss"] == 0.01 and out.grounded_on["value_loss_pct"] == 1.0
    assert [c["move"] for c in out.grounded_on["candidates"]] == ["Q16", "R14", "C3"]
    assert out.grounded_on["candidates"][0]["winrate_pct"] == 60.0
    assert out.grounded_on["learner"]["label"] == "10k"
    assert out.grounded_on["guard"] == {"regenerated": False, "dropped": [], "ungrounded": []}
    call = client.calls[0]
    assert call["model"] == "fake-model" and call["reasoning_effort"] == "low" and "tools" not in call
    assert "60.0%" in call["messages"][1]["content"]
    lines = (tmp_path / "api_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["step"] == "explain"
    assert ex.position == analysis.position  # Ask Sensei now refers to this position


def test_guard_regenerates_once_then_accepts(tmp_path):
    client = FakeClient(
        [
            text_response("R14 is good. Later K10 kills the corner."),
            text_response("R14 is good. Q16 was the engine's first choice."),
        ]
    )
    ex, _engine, _game = make(client, tmp_path)
    out = ex.explain(*sample_explain_inputs())

    assert out.text == "R14 is good. Q16 was the engine's first choice."
    assert len(client.calls) == 2
    retry = client.calls[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "R14 is good. Later K10 kills the corner."}
    assert retry[-1]["role"] == "user" and "K10" in retry[-1]["content"]
    assert out.grounded_on["guard"]["regenerated"] is True
    assert out.grounded_on["guard"]["ungrounded"] == ["K10"]
    assert out.grounded_on["guard"]["dropped"] == []
    assert (out.prompt_tokens, out.completion_tokens) == (200, 40)
    steps = [json.loads(line)["step"] for line in (tmp_path / "api_calls.jsonl").read_text().splitlines()]
    assert steps == ["explain", "explain_regen"]


def test_guard_drops_offending_sentences_after_second_failure(tmp_path):
    client = FakeClient(
        [
            text_response("R14 is good. Later K10 kills the corner."),
            text_response("R14 is a fine move. Then k10 and m3 follow! Q16 was the alternative."),
        ]
    )
    ex, _engine, _game = make(client, tmp_path)
    out = ex.explain(*sample_explain_inputs())

    assert out.text == "R14 is a fine move. Q16 was the alternative."
    assert out.grounded_on["guard"]["dropped"] == ["Then k10 and m3 follow!"]
    assert set(out.grounded_on["guard"]["ungrounded"]) == {"K10", "M3"}
    assert len(client.calls) == 2  # regenerate once, never twice


def test_guard_falls_back_to_template_when_nothing_survives(tmp_path):
    client = FakeClient([text_response("K10 wins."), text_response("K10 still wins.")])
    ex, _engine, _game = make(client, tmp_path)
    out = ex.explain(*sample_explain_inputs())
    assert out.grounded_on["guard"].get("template") is True
    assert "R14" in out.text and "Q16" in out.text
    assert explainer_mod.find_ungrounded_coords(out.text, out.grounded_on["allowed_coords"], 19) == []


# ---- Ask Sensei tool loop ----------------------------------------------------------------------


def test_ask_scripted_tool_call_round_trip(tmp_path):
    client = FakeClient(
        [
            tool_response(("analyze_position", {"top_k": 2})),
            text_response("The engine prefers Q16 at 60.0%; R14 is close behind at 59.0%."),
        ]
    )
    ex, engine, _game = make(client, tmp_path)
    ex.set_position(root_position())

    out = ex.ask("What is the best move here?", history=[])

    assert out.text == "The engine prefers Q16 at 60.0%; R14 is close behind at 59.0%."
    assert len(client.calls) == 2
    first, second = client.calls
    assert first["tools"] is explainer_mod.TOOLS and first["tool_choice"] == "auto"
    assert first["messages"][0]["role"] == "system" and "never" in first["messages"][0]["content"].lower()
    assert first["messages"][-1] == {"role": "user", "content": "What is the best move here?"}
    # The assistant tool call and the tool result were appended in OpenAI format.
    assistant = second["messages"][-2]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["function"]["name"] == "analyze_position"
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"
    result = json.loads(tool_msg["content"])
    assert [c["move"] for c in result["candidates"]] == ["Q16", "R14"]
    assert result["candidates"][0]["winrate_pct"] == 60.0 and result["to_move"] == "B"
    assert len(engine.queries) == 1
    # grounded_on records the call, the allowed coordinates and the token totals.
    log = out.grounded_on["tool_calls"]
    assert len(log) == 1 and log[0]["name"] == "analyze_position" and log[0]["result"] == result
    assert {"Q16", "R14"} <= set(out.grounded_on["allowed_coords"])
    assert out.grounded_on["tool_rounds"] == 1 and out.grounded_on["variations"] == []
    assert (out.prompt_tokens, out.completion_tokens) == (250, 50)
    steps = [json.loads(line)["step"] for line in (tmp_path / "api_calls.jsonl").read_text().splitlines()]
    assert steps == ["ask_round_1", "ask_round_2"]


def test_ask_guard_regenerates_once_using_tool_results(tmp_path):
    client = FakeClient(
        [
            tool_response(("evaluate_move", {"move": "c3"})),
            text_response("C3 loses 5.0% against Q16, and K10 would be worse."),
            text_response("C3 loses 5.0% against Q16."),
        ]
    )
    ex, _engine, _game = make(client, tmp_path)
    ex.set_position(root_position())
    out = ex.ask("Why not C3?")
    assert out.text == "C3 loses 5.0% against Q16."
    assert out.grounded_on["guard"]["regenerated"] is True
    assert out.grounded_on["guard"]["ungrounded"] == ["K10"]
    assert client.calls[-1]["tool_choice"] == "none"
    assert "K10" in client.calls[-1]["messages"][-1]["content"]
    result = out.grounded_on["tool_calls"][0]["result"]
    assert result["move"] == "C3" and result["winrate_pct"] == 55.0 and result["loss_pct"] == 5.0
    assert result["best_move"] == "Q16"


def test_ask_stops_at_max_tool_rounds(tmp_path):
    client = FakeClient(default=lambda kw: tool_response(("get_learner_profile", {}), content="still thinking"))
    ex, _engine, _game = make(client, tmp_path)
    ex.set_position(root_position())
    ex.set_learner_profile({"rank_label": "10k", "weak_topics": ["t1"], "target_topic": "t1"})

    out = ex.ask("Tell me everything.")

    assert len(client.calls) == CFG["explainer"]["max_tool_rounds"] + 1
    assert [c["tool_choice"] for c in client.calls] == ["auto", "auto", "none"]
    assert len(out.grounded_on["tool_calls"]) == CFG["explainer"]["max_tool_rounds"]
    assert out.grounded_on["tool_rounds"] == CFG["explainer"]["max_tool_rounds"]
    assert all(c["result"]["rank_label"] == "10k" for c in out.grounded_on["tool_calls"])
    assert out.text == "still thinking"  # the capped final message's text is used; its tool calls are ignored
    # Two tool messages per round were appended before the final call.
    assert len(tool_messages(client.calls[-1])) == CFG["explainer"]["max_tool_rounds"]


def test_show_variation_rejects_illegal_move(tmp_path):
    client = FakeClient(
        [
            tool_response(("show_variation", {"moves": ["Q16", "D4", "Q16"]})),
            text_response("Q16 cannot be played twice."),
        ]
    )
    ex, engine, _game = make(client, tmp_path)
    ex.set_position(root_position())

    out = ex.ask("What if Q16, D4, then Q16 again?")

    result = json.loads(tool_messages(client.calls[1])[0]["content"])
    assert "error" in result and "Q16" in result["error"] and "step 3" in result["error"]
    assert result["played"] == ["Q16", "D4"]
    assert out.grounded_on["variations"] == []  # nothing for the UI to draw
    assert engine.queries == []  # no analysis of a position that was never reached
    assert out.text == "Q16 cannot be played twice."


def test_show_variation_evaluates_end_position_for_the_asker(tmp_path):
    client = FakeClient(
        [
            tool_response(("show_variation", {"moves": ["Q16", "D4", "C3"]})),
            text_response("After Q16, D4 and C3 the engine would answer D16."),
        ]
    )
    ex, engine, _game = make(client, tmp_path)
    ex.set_position(root_position())

    out = ex.ask("Show me Q16, D4, C3.")

    result = out.grounded_on["tool_calls"][0]["result"]
    assert result["moves"] == ["Q16", "D4", "C3"] and result["to_move_after"] == "W"
    # The end position has White to move with root value 0.45; for Black (the asker) that is 55.0%.
    assert result["perspective"] == "B" and result["winrate_pct"] == 55.0
    assert result["engine_reply"] == "D16" and result["score_lead"] == 1.0
    assert out.grounded_on["variations"] == [["Q16", "D4", "C3"]]
    assert len(engine.queries) == 1 and engine.queries[0].moves == (("B", "Q16"), ("W", "D4"), ("B", "C3"))
    assert out.text == "After Q16, D4 and C3 the engine would answer D16."


def test_evaluate_move_handles_illegal_and_non_candidate_moves(tmp_path):
    ex, engine, game = make(FakeClient(), tmp_path)
    pos = root_position()
    assert "error" in ex.tool_evaluate_move(pos, "Z99")
    occupied = game.play(pos, "Q16")
    assert "error" in ex.tool_evaluate_move(occupied, "Q16")
    # D4 is legal but not a root candidate: evaluated from the position after it (1 - root value).
    result = ex.tool_evaluate_move(pos, "d4")
    assert result["move"] == "D4" and result["source"].startswith("second query")
    assert result["winrate_pct"] == 55.0 and result["loss_pct"] == 5.0
    assert len(engine.queries) == 2


def test_bad_tool_arguments_and_unknown_tools_return_errors(tmp_path):
    client = FakeClient(
        [
            tool_response(("analyze_position", {"top_k": "many"}), ("no_such_tool", {})),
            text_response("Sorry, I could not analyze that."),
        ]
    )
    ex, _engine, _game = make(client, tmp_path)
    ex.set_position(root_position())
    out = ex.ask("Hmm?")
    results = [c["result"] for c in out.grounded_on["tool_calls"]]
    assert all("error" in r for r in results)
    assert "unknown tool" in results[1]["error"]
    assert out.text == "Sorry, I could not analyze that."


def test_ask_requires_a_position(tmp_path):
    ex, _engine, _game = make(FakeClient(), tmp_path)
    with pytest.raises(ValueError):
        ex.ask("Where am I?")


# ---- learner mistakes ----------------------------------------------------------------------


def test_explain_mistake_grounds_on_the_played_and_best_moves(tmp_path):
    client = FakeClient(
        [
            text_response("C3 is slow; Q16 takes the big point. K10 would also lose."),
            text_response("C3 is slow while Q16 takes the big point at 60.0%."),
        ]
    )
    ex, _engine, _game = make(client, tmp_path)
    analysis = FakeEngine().analyze(root_position(), 50)
    learner = LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40)

    out = ex.explain_mistake(analysis, "C3", "Q16", 0.05, learner, topic="t_corner")

    assert out.text == "C3 is slow while Q16 takes the big point at 60.0%."
    assert out.grounded_on["kind"] == "mistake"
    assert out.grounded_on["played"] == "C3" and out.grounded_on["best_move"] == "Q16"
    assert out.grounded_on["loss_pct"] == 5.0 and out.grounded_on["topic"] == "t_corner"
    assert out.grounded_on["guard"]["regenerated"] is True and out.grounded_on["guard"]["ungrounded"] == ["K10"]
    user = client.calls[0]["messages"][1]["content"]
    assert "You (Black) played C3" in user and "5.0% of your winning chances" in user  # v2 voice: "you" = learner


def test_explain_mistake_falls_back_to_a_template(tmp_path):
    client = FakeClient([text_response("K10 now."), text_response("T19 instead.")])
    ex, _engine, _game = make(client, tmp_path)
    analysis = FakeEngine().analyze(root_position(), 50)
    out = ex.explain_mistake(analysis, "A1", "Q16", 0.12, LearnerEstimate(rank=-4.0, rank_sd=1.0, n_moves=30))
    assert out.text == "A1 cost 12.0% winrate. The engine preferred Q16 here."
    assert out.grounded_on["guard"]["template"] is True
