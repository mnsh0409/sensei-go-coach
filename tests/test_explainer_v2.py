"""Explainer prompt v2 (2026-11-04, after the T5 grading of v1): colors and voice, no engine internals, locations in
plain words, what a move does to the stones next to it, and tool results without prior or visits."""

from __future__ import annotations

import pytest

ex = pytest.importorskip("sensei.explainer")
from sensei.game_go import GoGame  # noqa: E402
from sensei.interfaces import Candidate, Choice, EngineAnalysis, LearnerEstimate, PositionClass  # noqa: E402

JARGON = ("prior", "visits", "instructiveness", "admissible", "value bound", "policy")


def test_describe_point_in_plain_go_words():
    assert ex.describe_point("S7") == "right side, 2nd line, lower half"
    assert ex.describe_point("D17") == "upper-left corner (3-4 point)"
    assert ex.describe_point("Q16") == "upper-right corner (4-4 point)"
    assert ex.describe_point("C9") == "left side, 3rd line, lower half"
    assert ex.describe_point("K2") == "bottom side, 2nd line"
    assert ex.describe_point("K10") == "center"
    assert ex.describe_point("pass") is None and ex.describe_point("Z99") is None and ex.describe_point(None) is None


def play(moves):
    game = GoGame()
    pos = game.initial_position()
    for m in moves:
        pos = game.play(pos, m)
    return game, pos


def test_local_facts_atari_capture_and_liberties():
    # White D4 is surrounded by Black on three sides; Black to play D3 captures it.
    game, pos = play(["C4", "D4", "E4", "Q16", "D5", "Q4"])
    capture = ex.local_facts(game, pos, "D3")
    assert capture["captured"] == 1 and capture["touches_opponent"] and capture["atari"] == []
    assert "captures 1 of your stone." in ex.local_text(capture, "my", "your")
    # Two Black stones next to White D4: the third one (D5) puts it in atari.
    game, pos = play(["C4", "D4", "E4", "Q16"])
    atari = ex.local_facts(game, pos, "D5")
    assert atari["atari"] == ["D4"] and atari["captured"] == 0
    assert "puts your stones at D4 in atari" in ex.local_text(atari, "my", "your")
    quiet = ex.local_facts(game, pos, "K10")
    assert not quiet["touches_opponent"] and quiet["own_group"] == {"stones": 1, "liberties": 4}
    assert ex.local_facts(game, pos, "pass") is None


def sample_facts():
    game, pos = play(["D4", "Q16", "C16", "Q3", "R4"])  # White to move: Sensei plays White, the learner Black
    cands = (
        Candidate("R14", 0.30, 0.62, 120, 4.5, ("R14", "Q5", "R6")),
        Candidate("Q5", 0.40, 0.61, 90, 2.0, ("Q5", "R14")),
        Candidate("D17", 0.10, 0.55, 30, 0.5, ("D17",)),
    )
    analysis = EngineAnalysis(pos, cands, {c.move: c.prior for c in cands}, 0.60)
    choice = Choice("Q5", 0.8, 0.01, PositionClass.STRATEGIC, {"Q5": 0.8, "R14": -0.1}, ("R14", "Q5"))
    learner = LearnerEstimate(rank=-9.0, rank_sd=1.0, n_moves=40)
    return game, analysis, choice, learner


def test_explanation_prompt_v2_voice_locations_and_no_jargon():
    game, analysis, choice, learner = sample_facts()
    facts = ex.explanation_facts(analysis, choice, learner, game=game)
    assert (
        facts["sensei_color"] == "W"
        and facts["learner_color"] == "B"
        and facts["prompt_version"] == ex.PROMPT_VERSION == "v3"
    )
    assert facts["best_move"] == "R14" and facts["points_vs_best"] == 2.5 and facts["continuation"] == ["R14"]
    assert facts["where"]["Q5"] == "lower-right corner (4-5 point)"
    msgs = ex.build_explanation_messages(analysis, choice, learner, max_sentences=4, facts=facts)
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "You play White; the learner plays Black" in system and "never call White's stones" in system
    assert "I (White) now play Q5 (lower-right corner (4-5 point))" in user
    assert "Last move: Black (you) played R4" in user
    assert "R14 (right side, 3rd line, upper half): about 2.5 points better for me" in user
    assert "but R14 gains about 2.5 points more than my move, and your text must say so plainly" in user
    assert "Winning chances before my move: White (me) about 60%" in user  # a close game keeps the percentages
    assert "The engine expects you to answer at R14" in user and "players around 10k rarely find" in user
    assert not any(word in user.lower() for word in JARGON), user
    assert ex.allowed_coords_from_facts(facts) >= {"Q5", "R14", "R4", "D17"}


def test_template_explanation_has_no_jargon():
    game, analysis, choice, learner = sample_facts()
    facts = ex.explanation_facts(analysis, choice, learner, game=game)
    text = ex.template_explanation(facts)
    assert text.startswith("I played Q5") and "R14" in text and "prior" not in text


class Client:
    """Answers once with plain text; records the messages."""

    def __init__(self):
        self.calls = []
        outer = self

        class Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                from types import SimpleNamespace as NS

                message = NS(content="Fine.", tool_calls=None)
                return NS(choices=[NS(message=message)], usage=NS(prompt_tokens=1, completion_tokens=1))

        self.chat = type("Chat", (), {"completions": Completions()})()


class Engine:
    def __init__(self, analysis):
        self.analysis = analysis

    def analyze(self, pos, max_visits):
        return self.analysis


def test_ask_prompt_and_tools_name_the_sides(tmp_path):
    game, analysis, choice, learner = sample_facts()
    client = Client()
    explainer = ex.MetaExplainer(client, Engine(analysis), game, {"explainer": {}}, model="m", log_path=tmp_path / "a")
    explainer.explain(analysis, choice, learner)
    assert explainer.learner_color == "B"
    pos = analysis.position
    explainer.ask("Why Q5?", position=pos)
    system = client.calls[-1]["messages"][0]["content"]
    assert "playing White against the learner" in system and "who plays Black" in system and "my turn" in system
    result = explainer.tool_analyze_position(pos, 2)
    assert result["for"] == "White (me)" and result["candidates"][0]["where"] == "right side, 3rd line, upper half"
    assert all("prior_pct" not in c and "visits" not in c for c in result["candidates"])
    move = explainer.tool_evaluate_move(pos, "R5")
    assert move["for"] == "White (me)" and "visits" not in move and move["where"] == "lower-right corner (3-5 point)"
    assert "touches Black (you)'s stones" in move["on_the_board"]
    line = explainer.tool_show_variation(pos, ["Q5", "R14"])
    assert [s["by"] for s in line["steps"]] == ["White (me)", "Black (you)"]
    explainer.set_learner_color(None)
    explainer.ask("And now?", position=pos)
    assert "Refer to the players as Black and White" in client.calls[-1]["messages"][0]["content"]


def test_v3_decided_games_drop_percentages_and_even_scores_say_even():
    game, analysis, choice, learner = sample_facts()
    won = EngineAnalysis(analysis.position, analysis.candidates, analysis.policy, 0.99)
    facts = ex.explanation_facts(won, choice, learner, game=game)
    user = ex.build_explanation_messages(won, choice, learner, max_sentences=4, facts=facts)[1]["content"]
    assert "Winning chances" not in user and "%" not in user and "talk about points" not in user
    assert "White (me) ahead by about 2.0 points" in user
    even = [Candidate(c.move, c.prior, c.value, c.visits, 0.2 if c.move == "Q5" else c.score_lead, c.pv)
            for c in analysis.candidates]  # fmt: skip
    close = EngineAnalysis(analysis.position, tuple(even), analysis.policy, 0.60)
    facts = ex.explanation_facts(close, choice, learner, game=game)
    user = ex.build_explanation_messages(close, choice, learner, max_sentences=4, facts=facts)[1]["content"]
    assert "Score after my move, by the engine's count: about even." in user


def test_v3_quiet_moves_name_the_nearest_stones_and_my_new_stone():
    game, pos = play(["D4", "Q16"])
    local = ex.local_facts(game, pos, "K10")
    assert local["nearest"] == {"own": None, "opponent": None}
    assert "No stone is within 4 points of it: it is in an open area." in ex.local_text(local, "my", "your")
    local = ex.local_facts(game, pos, "E8")  # Black's own D4 is 4 points away, in no named shape
    text = ex.local_text(local, "my", "your")
    assert local["nearest"]["own"] == {"stone": "D4", "distance": 4} and "my stone at D4, 4 points away" in text
    assert "After it, my new stone has 4 liberties." in text
    jump = ex.local_text(ex.local_facts(game, pos, "D7"), "my", "your")
    assert "a two-space jump from my stone at D4" in jump and "points away" not in jump  # named once, not twice
    assert (
        ex.describe_point("K6") == "center, toward the bottom"
        and ex.describe_point("F14") == "center, toward the top-left"
    )
